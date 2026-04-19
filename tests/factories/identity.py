"""Identity-context factories.

Builders for the identity primitives (workspace, user membership,
``User``) shared across every test tier. Production signup (cd-3i5)
ships its own flow; the helpers here exist purely to seed a DB for
the integration + API suites before that flow lands.

See ``docs/specs/17-testing-quality.md`` §"Unit" and
``docs/specs/03-auth-and-tokens.md`` §"WorkspaceContext".
"""

from __future__ import annotations

import factory
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "UserFactory",
    "WorkspaceContextFactory",
    "bootstrap_user",
    "bootstrap_workspace",
    "build_workspace_context",
]


class WorkspaceContextFactory(factory.Factory):
    """Build a :class:`~app.tenancy.WorkspaceContext` with deterministic
    defaults.

    factory-boy is not annotated, so the class-level attributes look
    untyped; the built instance is still a ``WorkspaceContext`` at
    runtime. Prefer :func:`build_workspace_context` at call sites for
    a typed wrapper.
    """

    class Meta:
        model = WorkspaceContext

    workspace_id = factory.LazyFunction(new_ulid)
    workspace_slug = factory.Sequence(lambda n: f"ws-{n}")
    actor_id = factory.LazyFunction(new_ulid)
    actor_kind = "user"
    actor_grant_role = "manager"
    actor_was_owner_member = True
    audit_correlation_id = factory.LazyFunction(new_ulid)


def build_workspace_context(**overrides: object) -> WorkspaceContext:
    """Return a :class:`WorkspaceContext` built from the factory.

    Typed wrapper that hides factory-boy's untyped call surface from
    callers. ``overrides`` is declared ``object`` to keep the factory
    untyped kwargs permissive while avoiding a public ``Any``.
    """
    built = WorkspaceContextFactory(**overrides)
    assert isinstance(built, WorkspaceContext)
    return built


class UserFactory(factory.Factory):
    """Build a :class:`~app.adapters.db.identity.models.User` with
    deterministic defaults.

    factory-boy is not annotated, so the class-level attributes look
    untyped; the built instance is still a ``User`` at runtime.
    ``email_lower`` is deliberately omitted — the SQLAlchemy
    ``before_insert`` / ``before_update`` listeners keep it in sync
    with ``email``. Tests that need the canonical value before a
    flush can call :func:`~app.adapters.db.identity.models.canonicalise_email`
    directly.
    """

    class Meta:
        model = User

    id = factory.LazyFunction(new_ulid)
    email = factory.Sequence(lambda n: f"user-{n}@example.com")
    # Set eagerly so unit-level construction (no SQLAlchemy flush) still
    # sees a value that satisfies the NOT NULL column. The event listener
    # overwrites it on insert / update if ``email`` drifts.
    email_lower = factory.LazyAttribute(lambda o: canonicalise_email(o.email))
    display_name = factory.Sequence(lambda n: f"User {n}")
    locale = None
    timezone = None
    avatar_blob_hash = None
    last_login_at = None
    # Pinned to a fixed UTC moment so ULID ordering inside a single test
    # stays deterministic even without a ``Clock`` fixture; tests that
    # care about the exact wall-clock value override explicitly.
    created_at = factory.LazyFunction(lambda: SystemClock().now())


def bootstrap_user(
    session: Session,
    *,
    email: str,
    display_name: str,
    clock: Clock | None = None,
) -> User:
    """Insert a :class:`User` row with the canonical email lookup.

    Test-only. Production signup (cd-3i5) ships its own flow that
    also seeds the first ``role_grant`` + audit trail; this helper
    exists purely to unblock integration tests that need a live
    identity row before that flow lands.

    The helper doesn't wrap itself in :func:`tenant_agnostic` because
    ``user`` is **not** registered as workspace-scoped (see
    :mod:`app.adapters.db.identity`) — the ORM tenant filter
    ignores the table entirely.
    """
    now = (clock if clock is not None else SystemClock()).now()
    user = User(
        id=new_ulid(),
        email=email,
        # ``email_lower`` is set eagerly so a pre-flush read round-trips
        # the canonical form; the event listener will reassert it on
        # flush. Duplicating the rule here keeps the helper honest
        # against future changes that might skip the listener (e.g. a
        # bulk INSERT via Core).
        email_lower=canonicalise_email(email),
        display_name=display_name,
        created_at=now,
    )
    session.add(user)
    session.flush()
    return user


def bootstrap_workspace(
    session: Session,
    *,
    slug: str,
    name: str,
    owner_user_id: str,
    clock: Clock | None = None,
) -> Workspace:
    """Seed a :class:`Workspace` + owner :class:`UserWorkspace` row.

    Test-only. Production signup (cd-3i5) will ship its own flow that
    also seeds ``role_grants``, emits ``workspace.created`` audit, and
    honours quota. This helper does none of that; its sole purpose is
    to unblock integration tests that need a live tenant row before the
    signup domain service lands.

    The helper runs under :func:`~app.tenancy.tenant_agnostic` because
    it creates the tenancy anchor before any
    :class:`~app.tenancy.WorkspaceContext` exists — there is literally
    nothing to filter against yet.
    """
    now = (clock if clock is not None else SystemClock()).now()
    workspace_id = new_ulid()
    # justification: seeding the tenancy anchor before a WorkspaceContext
    # exists; the ORM tenant filter has no ctx to apply here.
    with tenant_agnostic():
        workspace = Workspace(
            id=workspace_id,
            slug=slug,
            name=name,
            plan="free",
            quota_json={},
            created_at=now,
        )
        session.add(workspace)
        session.flush()
        session.add(
            UserWorkspace(
                user_id=owner_user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=now,
            )
        )
        session.flush()
    return workspace
