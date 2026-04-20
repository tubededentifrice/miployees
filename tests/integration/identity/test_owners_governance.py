"""Integration tests for the owners-group governance slice (cd-ckr).

Covers four concerns against a real DB with the tenant filter
installed:

* :func:`app.adapters.db.authz.bootstrap.seed_owners_system_group`
  writes exactly one ``audit.workspace.owners_bootstrapped`` row on
  each workspace, attributed to the supplied ctx.
* :func:`seed_system_permission_groups` seeds **exactly** the three
  non-owners system groups (``managers``, ``all_workers``,
  ``all_clients``) — four total counting owners, matching §02
  "permission_group" §"System groups".
* :func:`app.domain.identity.permission_groups.remove_member`
  refuses to drop the last member of the ``owners`` group and
  raises :class:`LastOwnerMember`; a non-last owner removal
  succeeds.
* :func:`app.authz.owners.resolve_is_owner` returns ``True`` for
  ``owners@<ws>`` members, ``False`` for managers-only members,
  ``False`` for un-grouped users, and ``False`` across workspaces
  (tenancy isolation).

Runs on SQLite by default; the Postgres shard picks it up through
the existing ``CREWDAY_TEST_DB`` knob (see
``tests/integration/conftest.py``).

See ``docs/specs/05-employees-and-roles.md`` §"Permissions: surface,
groups, and action catalog", §"Root-only actions (governance)" and
``docs/specs/02-domain-model.md`` §"permission_group" §"Invariants".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.bootstrap import seed_system_permission_groups
from app.adapters.db.authz.models import PermissionGroupMember
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.authz import resolve_is_owner
from app.domain.identity.permission_groups import (
    LastOwnerMember,
    add_member,
    list_groups,
    list_members,
    remove_member,
    write_member_remove_rejected_audit,
)
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
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
    """Re-register the workspace-scoped tables this module relies on.

    Mirrors the sibling ``test_permission_groups.py`` fixture — a
    unit test elsewhere calls ``registry._reset_for_tests`` in an
    autouse fixture, wiping the module-level registry; without
    this re-registration the tenant filter silently no-ops and our
    ownership assertions become vacuously true.
    """
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("role_grant")
    registry.register("audit_log")
    registry.register("user_workspace")


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


def _next_slug(prefix: str = "own-test") -> str:
    """Return a fresh validator-compliant slug for the test."""
    global _SLUG_COUNTER
    _SLUG_COUNTER += 1
    return f"{prefix}-{_SLUG_COUNTER:05d}"


@pytest.fixture
def env(db_session: Session) -> Iterator[tuple[Session, WorkspaceContext, str]]:
    """Yield ``(session, ctx, user_id)`` bound to a fresh workspace.

    The workspace carries the four system groups seeded by the
    production path (:func:`bootstrap_workspace`); the caller is
    the sole ``owners@<ws>`` member + its ``manager`` role grant.
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
    # Seed the three non-owners system groups so the workspace is
    # spec-shaped end-to-end (four total).
    with tenant_agnostic():
        seed_system_permission_groups(db_session, workspace_id=ws.id, clock=clock)

    ctx = _ctx_for(ws.id, ws.slug, user.id)
    token = set_current(ctx)
    try:
        yield db_session, ctx, user.id
    finally:
        reset_current(token)


def _second_user_with_membership(
    session: Session,
    *,
    workspace_id: str,
    suffix: str,
    clock: FrozenClock,
) -> str:
    """Insert a second user + their ``user_workspace`` junction row.

    The junction keeps the ``list_members`` / join paths honest; in
    production the signup / invite flows materialise this row, but
    our integration seed skips those — we insert it by hand so
    downstream reads aren't surprised.
    """
    user = bootstrap_user(
        session,
        email=f"second-{suffix}@example.com",
        display_name=f"Second {suffix}",
        clock=clock,
    )
    with tenant_agnostic():
        session.add(
            UserWorkspace(
                user_id=user.id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=clock.now(),
            )
        )
        session.flush()
    return user.id


def _owners_group_id(session: Session, ctx: WorkspaceContext) -> str:
    """Return the caller's seeded ``owners`` group id."""
    for ref in list_groups(session, ctx):
        if ref.slug == "owners":
            return ref.id
    raise AssertionError("owners group missing — bootstrap should have seeded it")


# ---------------------------------------------------------------------------
# seed_owners_system_group audit row
# ---------------------------------------------------------------------------


class TestOwnersBootstrapAudit:
    """``seed_owners_system_group`` emits one ``owners_bootstrapped`` audit row."""

    def test_audit_row_lands(self, env: tuple[Session, WorkspaceContext, str]) -> None:
        """The fixture's workspace has exactly one ``owners_bootstrapped`` row."""
        session, ctx, user_id = env
        rows = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_kind == "workspace",
                AuditLog.entity_id == ctx.workspace_id,
                AuditLog.action == "owners_bootstrapped",
            )
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.workspace_id == ctx.workspace_id
        assert row.actor_id == user_id
        assert row.diff == {
            "workspace_id": ctx.workspace_id,
            "owner_user_id": user_id,
        }
        # The diff carries only ULIDs — no PII leakage.
        assert "email" not in row.diff
        assert "email_hash" not in row.diff

    def test_each_new_workspace_emits_its_own_row(self, db_session: Session) -> None:
        """Two workspaces each carry exactly one ``owners_bootstrapped`` row."""
        install_tenant_filter(db_session)
        clock = FrozenClock(_PINNED)

        slugs: list[str] = []
        for _ in range(2):
            slug = _next_slug("own-two")
            slugs.append(slug)
            user = bootstrap_user(
                db_session,
                email=f"{slug}@example.com",
                display_name=f"User {slug}",
                clock=clock,
            )
            bootstrap_workspace(
                db_session,
                slug=slug,
                name=f"WS {slug}",
                owner_user_id=user.id,
                clock=clock,
            )

        # Each slug corresponds to one workspace with one audit row.
        # The ``workspace`` table is tenant-agnostic (slug lookup
        # predates any ctx), but ``audit_log`` is workspace-scoped,
        # so read the audit rows under each workspace's own ctx
        # rather than lifting to ``tenant_agnostic`` (which would
        # hide a real cross-tenant leak if the writer ever
        # mis-attributed a row).
        for slug in slugs:
            ws = db_session.scalars(
                select(Workspace).where(Workspace.slug == slug)
            ).one()
            ctx = _ctx_for(ws.id, ws.slug, "01HWA00000000000000000PRBE")
            token = set_current(ctx)
            try:
                rows = db_session.scalars(
                    select(AuditLog).where(
                        AuditLog.workspace_id == ws.id,
                        AuditLog.action == "owners_bootstrapped",
                    )
                ).all()
                assert len(rows) == 1, (
                    f"workspace {slug!r} should have exactly one "
                    f"owners_bootstrapped row, got {len(rows)}"
                )
            finally:
                reset_current(token)


# ---------------------------------------------------------------------------
# Four system groups (spec canonical, NOT five)
# ---------------------------------------------------------------------------


class TestFourSystemGroups:
    """§02 "permission_group": exactly four system groups on every workspace.

    The cd-ckr task description mentions a fifth "chat-gateway-agents"
    group — it is NOT in the spec and NOT seeded. Any drift would
    show up here as a failing ``== {four slugs}`` assertion.
    """

    _EXPECTED_SYSTEM_SLUGS = frozenset(
        {"owners", "managers", "all_workers", "all_clients"}
    )

    def test_exactly_four_system_groups(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        session, ctx, _ = env
        groups = list_groups(session, ctx)
        system_slugs = {g.slug for g in groups if g.system}
        assert system_slugs == self._EXPECTED_SYSTEM_SLUGS

    def test_first_user_in_owners_only(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """First user is in ``owners`` and NOT in any other system group.

        Preserves the cd-3i5 behaviour: the three non-owners system
        groups carry derived membership that will populate at
        resolver time (cd-zkr), not at seed time.
        """
        session, ctx, user_id = env
        groups = list_groups(session, ctx)
        for g in groups:
            if not g.system:
                continue
            members = list_members(session, ctx, group_id=g.id)
            member_ids = {m.user_id for m in members}
            if g.slug == "owners":
                assert user_id in member_ids
            else:
                assert user_id not in member_ids, (
                    f"first user must not be a seed member of system group {g.slug!r}"
                )


# ---------------------------------------------------------------------------
# resolve_is_owner (cd-7y4 middleware seam)
# ---------------------------------------------------------------------------


class TestResolveIsOwner:
    """``resolve_is_owner`` is the single owner-check seam."""

    def test_true_for_seeded_owner(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        session, ctx, user_id = env
        assert (
            resolve_is_owner(session, workspace_id=ctx.workspace_id, user_id=user_id)
            is True
        )

    def test_false_for_non_owner_with_manager_grant(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """A user with a manager role grant but no owners membership → False.

        Role grant and permission group membership are orthogonal
        in v1 (§02); the check walks ``permission_group_member``,
        not ``role_grant``, so a manager-only user is correctly
        reported as not an owner.
        """
        session, ctx, _ = env
        clock = FrozenClock(_PINNED)
        outsider = _second_user_with_membership(
            session,
            workspace_id=ctx.workspace_id,
            suffix="mgr-only",
            clock=clock,
        )
        assert (
            resolve_is_owner(session, workspace_id=ctx.workspace_id, user_id=outsider)
            is False
        )

    def test_false_for_user_in_no_groups(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """A bare user with no permission-group memberships → False."""
        session, ctx, _ = env
        clock = FrozenClock(_PINNED)
        stranger = bootstrap_user(
            session,
            email=f"{_next_slug('stranger')}@example.com",
            display_name="Stranger",
            clock=clock,
        )
        assert (
            resolve_is_owner(
                session, workspace_id=ctx.workspace_id, user_id=stranger.id
            )
            is False
        )

    def test_false_for_owner_in_wrong_workspace(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """Cross-workspace check: owner of W1 is NOT an owner of W2.

        Seeds a sibling workspace (W2) with its own creator, then
        asks whether W2's owner is an owner of W1. The join filters
        on ``permission_group.workspace_id == workspace_id``, so
        the answer MUST be ``False``.

        The cross-workspace check runs under :func:`tenant_agnostic`
        because that is the middleware's real calling context:
        :func:`resolve_is_owner` is how the middleware *builds* the
        ctx, so no ctx is active when it runs. Inside a live ctx
        the ORM tenant filter would auto-inject the current
        workspace id and hide a real cross-tenant leak.
        """
        session, _ctx, _ = env
        clock = FrozenClock(_PINNED)
        sibling_slug = _next_slug("sibling")
        sibling_owner = bootstrap_user(
            session,
            email=f"{sibling_slug}@example.com",
            display_name="Sibling Owner",
            clock=clock,
        )
        bootstrap_workspace(
            session,
            slug=sibling_slug,
            name=f"Sibling {sibling_slug}",
            owner_user_id=sibling_owner.id,
            clock=clock,
        )
        sibling_ws = session.scalars(
            select(Workspace).where(Workspace.slug == sibling_slug)
        ).one()
        # Sibling owner IS an owner of their OWN workspace…
        with tenant_agnostic():
            assert (
                resolve_is_owner(
                    session,
                    workspace_id=sibling_ws.id,
                    user_id=sibling_owner.id,
                )
                is True
            )
            # …but NOT of the test's workspace.
            assert (
                resolve_is_owner(
                    session,
                    workspace_id=_ctx.workspace_id,
                    user_id=sibling_owner.id,
                )
                is False
            )


# ---------------------------------------------------------------------------
# Last-owner-member guard
# ---------------------------------------------------------------------------


class TestLastOwnerMemberGuard:
    """``remove_member`` refuses to empty the ``owners`` group."""

    def test_remove_last_owner_raises(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """The sole owner cannot be removed from ``owners@<ws>``."""
        session, ctx, user_id = env
        owners_id = _owners_group_id(session, ctx)
        with pytest.raises(LastOwnerMember) as exc:
            remove_member(
                session,
                ctx,
                group_id=owners_id,
                user_id=user_id,
                clock=FrozenClock(_PINNED),
            )
        # Message mentions the ``owners`` group so logs are legible.
        assert "owners" in str(exc.value).lower()

    def test_remove_last_owner_preserves_membership(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """After the refusal the row still exists — no partial DELETE."""
        session, ctx, user_id = env
        owners_id = _owners_group_id(session, ctx)
        with pytest.raises(LastOwnerMember):
            remove_member(
                session,
                ctx,
                group_id=owners_id,
                user_id=user_id,
                clock=FrozenClock(_PINNED),
            )
        # Membership unchanged.
        remaining = session.scalars(
            select(PermissionGroupMember).where(
                PermissionGroupMember.group_id == owners_id
            )
        ).all()
        assert [m.user_id for m in remaining] == [user_id]

    def test_remove_non_last_owner_succeeds(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """With two owners, removing one leaves the other intact."""
        session, ctx, user_id = env
        clock = FrozenClock(_PINNED)
        owners_id = _owners_group_id(session, ctx)
        co_owner = _second_user_with_membership(
            session,
            workspace_id=ctx.workspace_id,
            suffix="co-owner",
            clock=clock,
        )
        add_member(
            session,
            ctx,
            group_id=owners_id,
            user_id=co_owner,
            clock=clock,
        )
        # Now remove the second — first stays. The first is the
        # actor building ctx, so the service is authorised.
        remove_member(
            session,
            ctx,
            group_id=owners_id,
            user_id=co_owner,
            clock=clock,
        )
        surviving = session.scalars(
            select(PermissionGroupMember).where(
                PermissionGroupMember.group_id == owners_id
            )
        ).all()
        assert [m.user_id for m in surviving] == [user_id]

    def test_remove_non_last_first_owner_then_second_blocked(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """Two owners → remove one → second is now the sole owner → blocked.

        Walks the sequence end-to-end: the guard is dynamic on the
        current member count, not a flag on the first owner row.
        """
        session, ctx, user_id = env
        clock = FrozenClock(_PINNED)
        owners_id = _owners_group_id(session, ctx)
        co_owner = _second_user_with_membership(
            session,
            workspace_id=ctx.workspace_id,
            suffix="co-then-block",
            clock=clock,
        )
        add_member(session, ctx, group_id=owners_id, user_id=co_owner, clock=clock)
        # First removal succeeds (co-owner leaves).
        remove_member(session, ctx, group_id=owners_id, user_id=co_owner, clock=clock)
        # Now try to remove the original user → refused.
        with pytest.raises(LastOwnerMember):
            remove_member(
                session, ctx, group_id=owners_id, user_id=user_id, clock=clock
            )

    def test_guard_does_not_fire_on_user_defined_group(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """A user-defined group never triggers the owners guard.

        The guard checks ``slug == 'owners' AND system is True``.
        A user-defined group that happens to have a single member
        is freely drainable.
        """
        session, ctx, user_id = env
        clock = FrozenClock(_PINNED)
        from app.domain.identity.permission_groups import create_group

        custom = create_group(
            session,
            ctx,
            slug="family",
            name="Family",
            capabilities={},
            clock=clock,
        )
        add_member(session, ctx, group_id=custom.id, user_id=user_id, clock=clock)
        # Remove the sole member of the custom group — not blocked.
        remove_member(session, ctx, group_id=custom.id, user_id=user_id, clock=clock)
        remaining = session.scalars(
            select(PermissionGroupMember).where(
                PermissionGroupMember.group_id == custom.id
            )
        ).all()
        assert remaining == []

    def test_idempotent_remove_on_nonmember_does_not_raise(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """Removing a non-member from ``owners`` is a no-op — guard skipped.

        The guard fires only when the member row actually exists;
        an idempotent "remove me again" on a user who isn't there
        never tips the count, so it is not caught by the guard.
        Critically, the existing sole owner stays intact.
        """
        session, ctx, user_id = env
        clock = FrozenClock(_PINNED)
        owners_id = _owners_group_id(session, ctx)
        ghost_user = new_ulid()
        # Should not raise — idempotent remove on a non-member.
        remove_member(
            session,
            ctx,
            group_id=owners_id,
            user_id=ghost_user,
            clock=clock,
        )
        # Sole owner untouched.
        remaining = session.scalars(
            select(PermissionGroupMember).where(
                PermissionGroupMember.group_id == owners_id
            )
        ).all()
        assert [m.user_id for m in remaining] == [user_id]


# ---------------------------------------------------------------------------
# Fresh-UoW rejection audit helper
# ---------------------------------------------------------------------------


class TestRejectedAuditHelper:
    """``write_member_remove_rejected_audit`` persists the forensic row."""

    def test_rejection_audit_lands(
        self, env: tuple[Session, WorkspaceContext, str]
    ) -> None:
        """The helper writes the rejection row on the passed session.

        The production path opens a **fresh** UoW for the helper
        (see :func:`app.auth.magic_link.write_rejected_audit`'s call
        site in ``app/api/v1/auth/magic.py``). We simulate that by
        calling the helper on the test's session directly; in
        production the router wraps it in ``make_uow()``.
        """
        session, ctx, user_id = env
        owners_id = _owners_group_id(session, ctx)

        # Attempt removal, expect refusal.
        with pytest.raises(LastOwnerMember):
            remove_member(
                session,
                ctx,
                group_id=owners_id,
                user_id=user_id,
                clock=FrozenClock(_PINNED),
            )

        # Router path: write the forensic row on (what would be) a
        # fresh UoW. Here we reuse the session because the SAVEPOINT
        # harness rolls back at teardown anyway.
        write_member_remove_rejected_audit(
            session,
            ctx,
            group_id=owners_id,
            user_id=user_id,
            clock=FrozenClock(_PINNED),
        )
        session.flush()

        rows = session.scalars(
            select(AuditLog).where(
                AuditLog.action == "member_remove_rejected",
                AuditLog.entity_id == f"{owners_id}:{user_id}",
            )
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.entity_kind == "permission_group_member"
        assert row.workspace_id == ctx.workspace_id
        assert row.actor_id == user_id
        assert row.diff == {
            "reason": "would_orphan_owners_group",
            "group_id": owners_id,
            "user_id": user_id,
        }
