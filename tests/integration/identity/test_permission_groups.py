"""Integration tests for :mod:`app.domain.identity.permission_groups`.

Exercises the CRUD + membership surface against a real DB with the
tenant filter installed so every function walks the same code paths
it will when called from a FastAPI route handler (cd-rpxd).

Each test:

* Bootstraps a user + workspace (so the ``owners`` system group is
  seeded via :func:`seed_owners_system_group`).
* Sets a :class:`WorkspaceContext` for that workspace so the ORM
  filter and the audit writer both see a live context.
* Calls the domain service and asserts the resulting rows +
  corresponding ``audit_log`` entry.

See ``docs/specs/02-domain-model.md`` §"permission_group",
§"permission_group_member" and ``docs/specs/05-employees-and-roles.md``
§"Permissions".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import PermissionGroupMember
from app.domain.identity.permission_groups import (
    PermissionGroupNotFound,
    PermissionGroupRef,
    PermissionGroupSlugTaken,
    SystemGroupProtected,
    UnknownCapability,
    add_member,
    create_group,
    delete_group,
    get_group,
    list_groups,
    list_members,
    remove_member,
    update_group,
)
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Re-register the workspace-scoped tables this test module depends on.

    A sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) resets the
    process-wide registry in its autouse fixture. Without re-
    registering here the filter silently no-ops on subsequent tests
    — a soft failure mode we want the test to prove it doesn't rely
    on.
    """
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("audit_log")


def _ctx_for(workspace_id: str, workspace_slug: str, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to the given workspace."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLA",
    )


_SLUG_COUNTER = 0


def _next_slug() -> str:
    """Return a fresh, validator-compliant workspace slug for the test.

    Tests run serially (pytest default) and share the same engine,
    so a monotonically incremented counter is enough for per-test
    isolation. Deriving the slug from ``request.node.name`` is
    tempting but test names carry underscores / parametrize brackets
    that fall outside the kebab-case pattern; a synthetic slug
    sidesteps that fragility.
    """
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"pg-test-{_SLUG_COUNTER:05d}"


@pytest.fixture
def env(
    db_session: Session,
) -> Iterator[tuple[Session, WorkspaceContext]]:
    """Yield a ``(session, ctx)`` pair bound to a fresh workspace.

    Builds on the parent conftest's ``db_session`` fixture, which
    wraps every test in a SAVEPOINT transaction that rolls back on
    teardown — no manual scrub needed. Installs the tenant filter on
    the session directly so the ORM filter is active for every
    query the domain service runs, matching the production path.
    """
    install_tenant_filter(db_session)

    slug = _next_slug()
    clock = FrozenClock(_PINNED)

    user = bootstrap_user(
        db_session,
        email=f"{slug}@example.com",
        display_name=f"User {slug}",
        clock=clock,
    )
    ws = bootstrap_workspace(
        db_session,
        slug=slug,
        name=f"WS {slug}",
        owner_user_id=user.id,
        clock=clock,
    )
    ctx = _ctx_for(ws.id, ws.slug, user.id)

    token = set_current(ctx)
    try:
        yield db_session, ctx
    finally:
        reset_current(token)


# ---------------------------------------------------------------------------
# Group CRUD
# ---------------------------------------------------------------------------


class TestCreateAndRead:
    """``create_group`` + ``get_group`` + ``list_groups`` round-trip."""

    def test_create_then_get(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={"tasks.create": True},
            clock=FrozenClock(_PINNED),
        )
        assert ref.slug == "family"
        assert ref.name == "Family"
        assert ref.system is False
        assert ref.capabilities == {"tasks.create": True}

        fetched = get_group(session, ctx, group_id=ref.id)
        assert fetched.id == ref.id
        assert fetched.slug == ref.slug
        assert fetched.name == ref.name
        assert fetched.system == ref.system
        assert fetched.capabilities == ref.capabilities

    def test_list_includes_owners_plus_new(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        family = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        rows = list_groups(session, ctx)
        slugs = {r.slug for r in rows}
        assert "owners" in slugs
        assert "family" in slugs
        assert family.id in {r.id for r in rows}

    def test_get_missing_raises(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        with pytest.raises(PermissionGroupNotFound):
            get_group(session, ctx, group_id="01HWA00000000000000000NONE")


class TestDuplicateSlug:
    """Unique-slug enforcement surfaces as :class:`PermissionGroupSlugTaken`."""

    def test_duplicate_slug_raises(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        first = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        with pytest.raises(PermissionGroupSlugTaken) as exc:
            create_group(
                session,
                ctx,
                slug="family",
                name="Family II",
                capabilities={},
                clock=FrozenClock(_PINNED),
            )
        assert "family" in str(exc.value)

        # The first creation survives the failed duplicate — the
        # ``create_group`` implementation must wrap its flush in a
        # SAVEPOINT so an IntegrityError can't poison the outer
        # transaction.
        surviving = get_group(session, ctx, group_id=first.id)
        assert surviving.slug == "family"
        assert surviving.name == "Family"

    def test_owners_slug_rejected(self, env: tuple[Session, WorkspaceContext]) -> None:
        """``owners`` is already seeded — re-creating it collides.

        Even though ``create_group`` always writes ``system=False``,
        the ``(workspace_id, slug)`` uniqueness catches the attempt.
        """
        session, ctx = env
        with pytest.raises(PermissionGroupSlugTaken):
            create_group(
                session,
                ctx,
                slug="owners",
                name="Fake Owners",
                capabilities={},
                clock=FrozenClock(_PINNED),
            )


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdateGroup:
    """``update_group`` honours the system-group protection rules."""

    def test_update_non_system_group_succeeds(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={"tasks.create": True},
            clock=FrozenClock(_PINNED),
        )
        updated = update_group(
            session,
            ctx,
            group_id=ref.id,
            name="Family (renamed)",
            capabilities={"tasks.create": True, "tasks.skip_other": True},
        )
        assert updated.name == "Family (renamed)"
        assert updated.capabilities == {
            "tasks.create": True,
            "tasks.skip_other": True,
        }

    def test_update_system_group_name_only_succeeds(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """Renaming a system group is allowed; capabilities stay frozen."""
        session, ctx = env
        owners = _owners_group(session, ctx)
        updated = update_group(
            session,
            ctx,
            group_id=owners.id,
            name="Custodians",
        )
        assert updated.name == "Custodians"
        # Capabilities untouched.
        assert updated.capabilities == owners.capabilities

    def test_update_system_group_capabilities_rejected(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        owners = _owners_group(session, ctx)
        with pytest.raises(SystemGroupProtected):
            update_group(
                session,
                ctx,
                group_id=owners.id,
                capabilities={"tasks.create": True},
            )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDeleteGroup:
    """``delete_group`` removes non-system groups and protects system ones."""

    def test_delete_non_system_group(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        delete_group(session, ctx, group_id=ref.id)

        with pytest.raises(PermissionGroupNotFound):
            get_group(session, ctx, group_id=ref.id)

    def test_delete_cascades_members(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """Members are swept by the FK cascade when the group goes."""
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        add_member(
            session,
            ctx,
            group_id=ref.id,
            user_id=ctx.actor_id,
            clock=FrozenClock(_PINNED),
        )

        delete_group(session, ctx, group_id=ref.id)

        # No member rows linger for that group.
        rows = session.scalars(
            select(PermissionGroupMember).where(
                PermissionGroupMember.group_id == ref.id
            )
        ).all()
        assert rows == []

    def test_delete_system_group_rejected(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        owners = _owners_group(session, ctx)
        with pytest.raises(SystemGroupProtected):
            delete_group(session, ctx, group_id=owners.id)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


class TestMembers:
    """``add_member`` + ``list_members`` + ``remove_member`` round-trip."""

    def test_add_list_remove(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        member = add_member(
            session,
            ctx,
            group_id=ref.id,
            user_id=ctx.actor_id,
            clock=FrozenClock(_PINNED),
        )
        assert member.group_id == ref.id
        assert member.user_id == ctx.actor_id
        assert member.added_by_user_id == ctx.actor_id

        listed = list_members(session, ctx, group_id=ref.id)
        assert [m.user_id for m in listed] == [ctx.actor_id]

        remove_member(session, ctx, group_id=ref.id, user_id=ctx.actor_id)

        listed = list_members(session, ctx, group_id=ref.id)
        assert listed == []

    def test_list_members_missing_group_raises(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        with pytest.raises(PermissionGroupNotFound):
            list_members(session, ctx, group_id="01HWA00000000000000000NONE")

    def test_add_member_is_idempotent(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """A duplicate ``add_member`` is a no-op that still emits an audit row.

        Guards against the bare PK-collision path: without the
        pre-check the second ``session.add`` would trip the composite
        ``(group_id, user_id)`` primary key at flush time, raise
        :class:`~sqlalchemy.exc.IntegrityError`, and poison the outer
        transaction — killing every prior write the caller made in
        the same request.
        """
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        first = add_member(
            session,
            ctx,
            group_id=ref.id,
            user_id=ctx.actor_id,
            clock=FrozenClock(_PINNED),
        )
        second = add_member(
            session,
            ctx,
            group_id=ref.id,
            user_id=ctx.actor_id,
            clock=FrozenClock(_PINNED),
        )
        # The membership row is unchanged — the second call returns
        # the existing row verbatim rather than inserting a fresh one.
        # ``added_at`` is not compared directly here: SQLite strips
        # ``tzinfo`` on read so comparing the two refs' timestamps
        # would need backend-specific normalisation; the composite PK
        # equality below is already sufficient to prove idempotency.
        assert second.group_id == first.group_id
        assert second.user_id == first.user_id
        assert second.added_by_user_id == first.added_by_user_id

        # Exactly one membership row persisted.
        listed = list_members(session, ctx, group_id=ref.id)
        assert len(listed) == 1

        # Two ``member_added`` audit rows — admin intent is recorded
        # even when the write was a no-op.
        member_entity = f"{ref.id}:{ctx.actor_id}"
        rows = _all_audit_for(session, entity_id=member_entity)
        added = [r for r in rows if r.action == "member_added"]
        assert len(added) == 2

    def test_remove_member_is_idempotent(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        """Calling ``remove_member`` twice emits two audit rows, no error.

        Matches the service docstring's §02 "Audit" contract:
        absence + re-emit is the spec's idempotent admin semantic.
        """
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        add_member(
            session,
            ctx,
            group_id=ref.id,
            user_id=ctx.actor_id,
            clock=FrozenClock(_PINNED),
        )
        # First remove deletes the row.
        remove_member(session, ctx, group_id=ref.id, user_id=ctx.actor_id)
        # Second remove is a pure no-op write — no row, still an audit.
        remove_member(session, ctx, group_id=ref.id, user_id=ctx.actor_id)

        member_entity = f"{ref.id}:{ctx.actor_id}"
        rows = _all_audit_for(session, entity_id=member_entity)
        removed = [r for r in rows if r.action == "member_removed"]
        assert len(removed) == 2


# ---------------------------------------------------------------------------
# Unknown capabilities
# ---------------------------------------------------------------------------


class TestUnknownCapability:
    """Unknown capability keys blow up before any DB write."""

    def test_create_with_unknown_key(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        with pytest.raises(UnknownCapability) as exc:
            create_group(
                session,
                ctx,
                slug="broken",
                name="Broken",
                capabilities={"does.not_exist": True},
                clock=FrozenClock(_PINNED),
            )
        assert str(exc.value) == "does.not_exist"

        # Nothing persisted — listing by slug finds no row.
        rows = list_groups(session, ctx)
        assert "broken" not in {r.slug for r in rows}

    def test_update_with_unknown_key(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={"tasks.create": True},
            clock=FrozenClock(_PINNED),
        )
        with pytest.raises(UnknownCapability):
            update_group(
                session,
                ctx,
                group_id=ref.id,
                capabilities={"bogus.key": True},
            )

        # Row still carries the original capabilities payload.
        again = get_group(session, ctx, group_id=ref.id)
        assert again.capabilities == {"tasks.create": True}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class TestAuditEmission:
    """Every mutation emits an ``audit_log`` row with the spec-shaped payload."""

    def test_create_emits_audit(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={"tasks.create": True},
            clock=FrozenClock(_PINNED),
        )
        row = _only_audit_for(session, entity_id=ref.id)
        assert row.entity_kind == "permission_group"
        assert row.action == "created"
        assert row.diff == {
            "slug": "family",
            "name": "Family",
            "capabilities": {"tasks.create": True},
        }
        assert row.actor_id == ctx.actor_id

    def test_update_emits_audit_with_before_after(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={"tasks.create": True},
            clock=FrozenClock(_PINNED),
        )
        update_group(
            session,
            ctx,
            group_id=ref.id,
            name="Family v2",
        )
        rows = _all_audit_for(session, entity_id=ref.id)
        actions = [r.action for r in rows]
        assert "created" in actions
        assert "updated" in actions

        updated = next(r for r in rows if r.action == "updated")
        assert updated.entity_kind == "permission_group"
        assert updated.diff["before"]["name"] == "Family"
        assert updated.diff["after"]["name"] == "Family v2"
        # Capabilities untouched in this update — before/after match.
        assert updated.diff["before"]["capabilities"] == {"tasks.create": True}
        assert updated.diff["after"]["capabilities"] == {"tasks.create": True}

    def test_delete_emits_audit(self, env: tuple[Session, WorkspaceContext]) -> None:
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        delete_group(session, ctx, group_id=ref.id)

        rows = _all_audit_for(session, entity_id=ref.id)
        actions = [r.action for r in rows]
        assert "deleted" in actions
        deleted = next(r for r in rows if r.action == "deleted")
        assert deleted.entity_kind == "permission_group"
        assert deleted.diff == {"slug": "family", "name": "Family"}

    def test_member_add_remove_emit_audit(
        self, env: tuple[Session, WorkspaceContext]
    ) -> None:
        session, ctx = env
        ref = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=FrozenClock(_PINNED),
        )
        add_member(
            session,
            ctx,
            group_id=ref.id,
            user_id=ctx.actor_id,
            clock=FrozenClock(_PINNED),
        )
        remove_member(session, ctx, group_id=ref.id, user_id=ctx.actor_id)

        member_entity = f"{ref.id}:{ctx.actor_id}"
        rows = _all_audit_for(session, entity_id=member_entity)
        actions = {r.action for r in rows}
        assert {"member_added", "member_removed"} == actions
        for r in rows:
            assert r.entity_kind == "permission_group_member"
            assert r.diff == {"group_id": ref.id, "user_id": ctx.actor_id}


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _owners_group(session: Session, ctx: WorkspaceContext) -> PermissionGroupRef:
    """Return the caller's seeded ``owners`` group as a dataclass ref."""
    for ref in list_groups(session, ctx):
        if ref.slug == "owners":
            return ref
    raise AssertionError("owners group missing — bootstrap should have seeded it")


def _only_audit_for(session: Session, *, entity_id: str) -> AuditLog:
    """Return the single audit row for ``entity_id`` or raise."""
    rows = _all_audit_for(session, entity_id=entity_id)
    assert len(rows) == 1, f"expected one audit row, got {len(rows)}"
    return rows[0]


def _all_audit_for(session: Session, *, entity_id: str) -> list[AuditLog]:
    """Return every audit row for ``entity_id`` ordered by creation."""
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    )
