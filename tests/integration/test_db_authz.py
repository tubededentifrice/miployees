"""Integration tests for :mod:`app.adapters.db.authz` against a real DB.

Covers the post-migration schema shape (tables, composite + unique
keys, FKs, CHECK constraints), the referential-integrity contract
on all three authz tables (CASCADE on workspace / user / group,
SET NULL on ``added_by_user_id`` / ``created_by_user_id``), the
``seed_owners_system_group`` happy path + double-seed conflict, and
the tenant-filter behaviour (all three tables scoped; SELECT without
a :class:`WorkspaceContext` raises :class:`TenantFilterMissing`).

The sibling ``tests/unit/test_db_authz.py`` covers pure-Python model
construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"permission_group",
§"permission_group_member", §"role_grants" and
``docs/specs/05-employees-and-roles.md`` §"Roles & groups".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.bootstrap import seed_owners_system_group
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across tests — mirrors the pattern used by
    ``tests/integration/test_db_workspace.py``. The top-level
    ``db_session`` fixture binds directly to a raw connection for
    SAVEPOINT isolation, which bypasses the default sessionmaker and
    therefore the filter. Tests that need to observe
    :class:`TenantFilterMissing` use this factory explicitly.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_authz_registered() -> None:
    """Re-register authz tables as workspace-scoped before each test.

    ``app.adapters.db.authz.__init__`` registers these tables at
    import time, but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite. Matches the workaround in
    ``tests/integration/test_db_workspace.py``.
    """
    registry.register("permission_group")
    registry.register("permission_group_member")
    registry.register("role_grant")


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace``."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLA",
    )


class TestMigrationShape:
    """The migration lands all three authz tables with correct keys."""

    def test_permission_group_table_exists(self, engine: Engine) -> None:
        assert "permission_group" in inspect(engine).get_table_names()

    def test_permission_group_member_table_exists(self, engine: Engine) -> None:
        assert "permission_group_member" in inspect(engine).get_table_names()

    def test_role_grant_table_exists(self, engine: Engine) -> None:
        assert "role_grant" in inspect(engine).get_table_names()

    def test_permission_group_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("permission_group")}
        expected = {
            "id",
            "workspace_id",
            "slug",
            "name",
            "system",
            "capabilities_json",
            "created_at",
        }
        assert set(cols) == expected
        # Every column is NOT NULL in the v1 slice.
        for name in expected:
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"
        pk = inspect(engine).get_pk_constraint("permission_group")
        assert pk["constrained_columns"] == ["id"]

    def test_permission_group_workspace_slug_unique(self, engine: Engine) -> None:
        """``(workspace_id, slug)`` carries a unique constraint."""
        unique_cols: list[list[str]] = [
            uc["column_names"]
            for uc in inspect(engine).get_unique_constraints("permission_group")
        ]
        unique_idx_cols: list[list[str]] = [
            ix["column_names"]
            for ix in inspect(engine).get_indexes("permission_group")
            if ix.get("unique")
        ]
        assert ["workspace_id", "slug"] in unique_cols + unique_idx_cols

    def test_permission_group_cascades_on_workspace(self, engine: Engine) -> None:
        fks = [
            fk
            for fk in inspect(engine).get_foreign_keys("permission_group")
            if fk["referred_table"] == "workspace"
        ]
        assert len(fks) == 1
        assert fks[0]["options"].get("ondelete") == "CASCADE"

    def test_permission_group_member_composite_pk(self, engine: Engine) -> None:
        pk = inspect(engine).get_pk_constraint("permission_group_member")
        assert pk["constrained_columns"] == ["group_id", "user_id"]

    def test_permission_group_member_columns(self, engine: Engine) -> None:
        cols = {
            c["name"]: c for c in inspect(engine).get_columns("permission_group_member")
        }
        expected = {
            "group_id",
            "user_id",
            "workspace_id",
            "added_at",
            "added_by_user_id",
        }
        assert set(cols) == expected
        # Everything except ``added_by_user_id`` is NOT NULL.
        assert cols["added_by_user_id"]["nullable"] is True
        for name in expected - {"added_by_user_id"}:
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"

    def test_permission_group_member_fks(self, engine: Engine) -> None:
        fks = {
            fk["referred_table"]: fk
            for fk in inspect(engine).get_foreign_keys("permission_group_member")
        }
        # Hard deletes on group / user / workspace sweep the junction.
        for table in ("permission_group", "user", "workspace"):
            assert table in fks, f"missing FK on {table}"
            assert fks[table]["options"].get("ondelete") == "CASCADE"
        # ``added_by_user_id`` sets NULL so history survives the actor.
        added_by_fk = next(
            fk
            for fk in inspect(engine).get_foreign_keys("permission_group_member")
            if fk["constrained_columns"] == ["added_by_user_id"]
        )
        assert added_by_fk["referred_table"] == "user"
        assert added_by_fk["options"].get("ondelete") == "SET NULL"

    def test_permission_group_member_workspace_index(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix
            for ix in inspect(engine).get_indexes("permission_group_member")
        }
        assert "ix_permission_group_member_workspace" in indexes
        assert indexes["ix_permission_group_member_workspace"]["column_names"] == [
            "workspace_id"
        ]

    def test_role_grant_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("role_grant")}
        expected = {
            "id",
            "workspace_id",
            "user_id",
            "grant_role",
            "scope_property_id",
            "created_at",
            "created_by_user_id",
        }
        assert set(cols) == expected
        # ``scope_property_id`` (property-scope narrowing) and
        # ``created_by_user_id`` (self-bootstrap rows) are nullable.
        nullable = {"scope_property_id", "created_by_user_id"}
        for name in nullable:
            assert cols[name]["nullable"] is True, f"{name} must be nullable"
        for name in expected - nullable:
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"

    def test_role_grant_fks(self, engine: Engine) -> None:
        fks = inspect(engine).get_foreign_keys("role_grant")
        by_col = {tuple(fk["constrained_columns"]): fk for fk in fks}
        # Hard deletes on workspace / user sweep the grant.
        assert by_col[("workspace_id",)]["referred_table"] == "workspace"
        assert by_col[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert by_col[("user_id",)]["referred_table"] == "user"
        assert by_col[("user_id",)]["options"].get("ondelete") == "CASCADE"
        # ``created_by_user_id`` sets NULL so history survives the actor.
        assert by_col[("created_by_user_id",)]["referred_table"] == "user"
        assert by_col[("created_by_user_id",)]["options"].get("ondelete") == "SET NULL"
        # ``scope_property_id`` is a soft reference in v1 (no FK until cd-i6u).
        assert ("scope_property_id",) not in by_col

    def test_role_grant_indexes(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("role_grant")}
        assert "ix_role_grant_workspace_user" in indexes
        assert indexes["ix_role_grant_workspace_user"]["column_names"] == [
            "workspace_id",
            "user_id",
        ]
        assert "ix_role_grant_scope_property" in indexes
        assert indexes["ix_role_grant_scope_property"]["column_names"] == [
            "scope_property_id"
        ]


class TestAuthzRoundtrip:
    """Insert group + member + grant, commit, read back."""

    def test_round_trip(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="round-trip@example.com",
            display_name="RoundTrip",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="round-trip",
            name="RoundTrip",
            owner_user_id=user.id,
            clock=clock,
        )

        token = set_current(_ctx_for(ws, user.id))
        try:
            groups = db_session.scalars(
                select(PermissionGroup).where(PermissionGroup.workspace_id == ws.id)
            ).all()
            members = db_session.scalars(
                select(PermissionGroupMember).where(
                    PermissionGroupMember.workspace_id == ws.id
                )
            ).all()
            grants = db_session.scalars(
                select(RoleGrant).where(RoleGrant.workspace_id == ws.id)
            ).all()
        finally:
            reset_current(token)

        assert len(groups) == 1
        assert groups[0].slug == "owners"
        assert groups[0].system is True
        assert groups[0].capabilities_json == {"all": True}

        assert len(members) == 1
        assert members[0].user_id == user.id
        assert members[0].added_by_user_id is None

        assert len(grants) == 1
        assert grants[0].user_id == user.id
        assert grants[0].grant_role == "manager"
        assert grants[0].scope_property_id is None
        assert grants[0].created_by_user_id is None


class TestUniquePermissionGroupSlug:
    """Two groups in the same workspace cannot share a slug."""

    def test_duplicate_slug_in_same_workspace_rejected(
        self, db_session: Session
    ) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="dup-slug@example.com",
            display_name="DupSlug",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="dup-slug-ws",
            name="DupSlugWS",
            owner_user_id=user.id,
            clock=clock,
        )

        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                PermissionGroup(
                    id="01HWA00000000000000000PDP1",
                    workspace_id=ws.id,
                    slug="family",
                    name="Family",
                    system=False,
                    capabilities_json={},
                    created_at=_PINNED,
                )
            )
            db_session.flush()
            db_session.add(
                PermissionGroup(
                    id="01HWA00000000000000000PDP2",
                    workspace_id=ws.id,
                    slug="family",
                    name="Family Duplicate",
                    system=False,
                    capabilities_json={},
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_same_slug_different_workspace_allowed(self, db_session: Session) -> None:
        """``family`` in workspace A and workspace B must both persist."""
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="ws-split@example.com",
            display_name="WsSplit",
            clock=clock,
        )
        ws_a = bootstrap_workspace(
            db_session,
            slug="ws-split-a",
            name="A",
            owner_user_id=user.id,
            clock=clock,
        )
        ws_b = bootstrap_workspace(
            db_session,
            slug="ws-split-b",
            name="B",
            owner_user_id=user.id,
            clock=clock,
        )

        # Two groups with the same slug in different workspaces.
        token = set_current(_ctx_for(ws_a, user.id))
        try:
            db_session.add(
                PermissionGroup(
                    id="01HWA00000000000000000PSPA",
                    workspace_id=ws_a.id,
                    slug="family",
                    name="Family",
                    system=False,
                    capabilities_json={},
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        token = set_current(_ctx_for(ws_b, user.id))
        try:
            db_session.add(
                PermissionGroup(
                    id="01HWA00000000000000000PSPB",
                    workspace_id=ws_b.id,
                    slug="family",
                    name="Family",
                    system=False,
                    capabilities_json={},
                    created_at=_PINNED,
                )
            )
            # No IntegrityError — uniqueness is per-workspace.
            db_session.flush()
        finally:
            reset_current(token)


class TestRoleGrantCheckConstraint:
    """``grant_role`` CHECK rejects values outside the v1 enum."""

    def test_bogus_role_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-role@example.com",
            display_name="BogusRole",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-role-ws",
            name="BogusRoleWS",
            owner_user_id=user.id,
            clock=clock,
        )

        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                RoleGrant(
                    id="01HWA00000000000000000RBOG",
                    workspace_id=ws.id,
                    user_id=user.id,
                    grant_role="bogus",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_every_allowed_role_roundtrips(self, db_session: Session) -> None:
        """Each allowed ``grant_role`` persists."""
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="every-role@example.com",
            display_name="EveryRole",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="every-role-ws",
            name="EveryRoleWS",
            owner_user_id=user.id,
            clock=clock,
        )
        token = set_current(_ctx_for(ws, user.id))
        try:
            for idx, role in enumerate(("manager", "worker", "client", "guest")):
                db_session.add(
                    RoleGrant(
                        id=f"01HWA00000000000000000RE{idx:02d}",
                        workspace_id=ws.id,
                        user_id=user.id,
                        grant_role=role,
                        created_at=_PINNED,
                    )
                )
            db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_v0_owner_role_rejected(self, db_session: Session) -> None:
        """v1 drops the v0 ``owner`` value — it must fail the CHECK."""
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="v0-owner@example.com",
            display_name="V0Owner",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="v0-owner-ws",
            name="V0OwnerWS",
            owner_user_id=user.id,
            clock=clock,
        )
        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                RoleGrant(
                    id="01HWA00000000000000000RV0O",
                    workspace_id=ws.id,
                    user_id=user.id,
                    grant_role="owner",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps authz rows under it."""

    def test_workspace_delete_cascades(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="wscascade-authz@example.com",
            display_name="WsCascadeAuthz",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="ws-authz-cascade",
            name="WsAuthzCascade",
            owner_user_id=user.id,
            clock=clock,
        )

        loaded_ws = db_session.get(Workspace, ws.id)
        assert loaded_ws is not None
        db_session.delete(loaded_ws)
        db_session.flush()

        # Every authz row that pointed at the workspace is gone. The
        # follow-up queries run under the just-deleted ws ctx because
        # the tables are workspace-scoped; the filter treats a missing
        # row set as an expected zero match.
        token = set_current(_ctx_for(ws, user.id))
        try:
            assert (
                db_session.scalars(
                    select(PermissionGroup).where(PermissionGroup.workspace_id == ws.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(PermissionGroupMember).where(
                        PermissionGroupMember.workspace_id == ws.id
                    )
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(RoleGrant).where(RoleGrant.workspace_id == ws.id)
                ).all()
                == []
            )
        finally:
            reset_current(token)


class TestCascadeOnUserDelete:
    """Deleting a user sweeps their membership + grant rows."""

    def test_user_delete_cascades_member_and_grant(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        owner = bootstrap_user(
            db_session,
            email="userowner-authz@example.com",
            display_name="UserOwnerAuthz",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="user-del-authz",
            name="UserDelAuthz",
            owner_user_id=owner.id,
            clock=clock,
        )

        # Delete the owner user (the bootstrap seeded member + grant rows).
        loaded_user = db_session.get(User, owner.id)
        assert loaded_user is not None
        db_session.delete(loaded_user)
        db_session.flush()

        token = set_current(_ctx_for(ws, owner.id))
        try:
            assert (
                db_session.scalars(
                    select(PermissionGroupMember).where(
                        PermissionGroupMember.user_id == owner.id
                    )
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(RoleGrant).where(RoleGrant.user_id == owner.id)
                ).all()
                == []
            )
        finally:
            reset_current(token)

    def test_user_delete_sets_null_on_added_by_and_created_by(
        self, db_session: Session
    ) -> None:
        """Deleting the actor clears ``added_by_user_id`` / ``created_by_user_id``.

        The audit columns don't cascade — they must keep the
        surrounding row for history and NULL the actor pointer (§02
        audit trail durability).
        """
        clock = FrozenClock(_PINNED)
        owner = bootstrap_user(
            db_session,
            email="audit-owner@example.com",
            display_name="AuditOwner",
            clock=clock,
        )
        actor = bootstrap_user(
            db_session,
            email="audit-actor@example.com",
            display_name="AuditActor",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="audit-setnull",
            name="AuditSetNull",
            owner_user_id=owner.id,
            clock=clock,
        )

        # Seed a second, non-bootstrap group membership + role grant
        # whose audit pointers reference ``actor``.
        token = set_current(_ctx_for(ws, owner.id))
        try:
            extra_group = PermissionGroup(
                id="01HWA00000000000000000PGXT",
                workspace_id=ws.id,
                slug="extras",
                name="Extras",
                system=False,
                capabilities_json={},
                created_at=_PINNED,
            )
            db_session.add(extra_group)
            db_session.flush()
            db_session.add(
                PermissionGroupMember(
                    group_id=extra_group.id,
                    user_id=owner.id,
                    workspace_id=ws.id,
                    added_at=_PINNED,
                    added_by_user_id=actor.id,
                )
            )
            db_session.add(
                RoleGrant(
                    id="01HWA00000000000000000RGXT",
                    workspace_id=ws.id,
                    user_id=owner.id,
                    grant_role="worker",
                    created_at=_PINNED,
                    created_by_user_id=actor.id,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        # Drop the actor — the audit pointer must NULL-out, not cascade.
        loaded_actor = db_session.get(User, actor.id)
        assert loaded_actor is not None
        db_session.delete(loaded_actor)
        db_session.flush()

        token = set_current(_ctx_for(ws, owner.id))
        try:
            member = db_session.scalars(
                select(PermissionGroupMember).where(
                    PermissionGroupMember.group_id == extra_group.id,
                    PermissionGroupMember.user_id == owner.id,
                )
            ).one()
            assert member.added_by_user_id is None

            grant = db_session.scalars(
                select(RoleGrant).where(RoleGrant.id == "01HWA00000000000000000RGXT")
            ).one()
            assert grant.created_by_user_id is None
        finally:
            reset_current(token)


class TestTenantFilter:
    """Every authz table is workspace-scoped — bare SELECTs fail closed."""

    def test_permission_group_read_without_ctx_raises(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(PermissionGroup))
        assert exc.value.table == "permission_group"

    def test_permission_group_member_read_without_ctx_raises(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(PermissionGroupMember))
        assert exc.value.table == "permission_group_member"

    def test_role_grant_read_without_ctx_raises(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(RoleGrant))
        assert exc.value.table == "role_grant"


class TestSeedOwnersSystemGroup:
    """``seed_owners_system_group`` creates exactly the expected 3 rows."""

    def test_returns_group_member_grant(self, db_session: Session) -> None:
        """The helper returns the three inserted rows, shape-checked."""
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="seed-explicit@example.com",
            display_name="SeedExplicit",
            clock=clock,
        )
        # Build the tenancy anchor manually so we can exercise the seed
        # helper in isolation from ``bootstrap_workspace``.
        ws = Workspace(
            id="01HWA00000000000000000SEEW",
            slug="seed-explicit",
            name="SeedExplicit",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
        db_session.add(ws)
        db_session.flush()

        ctx = _ctx_for(ws, user.id)
        token = set_current(ctx)
        try:
            group, member, grant = seed_owners_system_group(
                db_session,
                ctx,
                workspace_id=ws.id,
                owner_user_id=user.id,
                clock=clock,
            )
        finally:
            reset_current(token)

        assert group.slug == "owners"
        assert group.name == "Owners"
        assert group.system is True
        assert group.capabilities_json == {"all": True}
        assert group.workspace_id == ws.id

        assert member.group_id == group.id
        assert member.user_id == user.id
        assert member.workspace_id == ws.id
        assert member.added_by_user_id is None

        assert grant.user_id == user.id
        assert grant.workspace_id == ws.id
        assert grant.grant_role == "manager"
        assert grant.scope_property_id is None
        assert grant.created_by_user_id is None

    def test_double_seed_raises_integrity_error(self, db_session: Session) -> None:
        """Re-running ``seed_owners_system_group`` on the same workspace fails.

        The unique ``(workspace_id, slug)`` constraint prevents a
        second ``owners`` group, matching §02's "exactly the four
        system groups at any time" invariant.
        """
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="double-seed@example.com",
            display_name="DoubleSeed",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="double-seed",
            name="DoubleSeed",
            owner_user_id=user.id,
            clock=clock,
        )
        ctx = _ctx_for(ws, user.id)
        token = set_current(ctx)
        try:
            with pytest.raises(IntegrityError):
                seed_owners_system_group(
                    db_session,
                    ctx,
                    workspace_id=ws.id,
                    owner_user_id=user.id,
                    clock=clock,
                )
            db_session.rollback()
        finally:
            reset_current(token)


class TestBootstrapWorkspaceSeedsOwners:
    """``bootstrap_workspace`` end-to-end seeds the ``owners`` anchor.

    Mirrors the spec's §02 invariant: every workspace has the
    ``owners`` system group with at least one member + a matching
    ``role_grants`` row from the moment it is created.
    """

    def test_owners_group_and_members_present(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bootstrap-owners@example.com",
            display_name="BootstrapOwners",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bootstrap-owners",
            name="BootstrapOwners",
            owner_user_id=user.id,
            clock=clock,
        )

        token = set_current(_ctx_for(ws, user.id))
        try:
            group = db_session.scalars(
                select(PermissionGroup).where(
                    PermissionGroup.workspace_id == ws.id,
                    PermissionGroup.slug == "owners",
                )
            ).one()
            assert group.system is True
            assert group.capabilities_json == {"all": True}
            # SQLite's ``DateTime(timezone=True)`` drops tzinfo on
            # reload; compare wall-clock components so the assertion
            # holds on SQLite and Postgres alike.
            assert group.created_at.replace(tzinfo=None) == _PINNED.replace(tzinfo=None)

            members = db_session.scalars(
                select(PermissionGroupMember).where(
                    PermissionGroupMember.group_id == group.id
                )
            ).all()
            assert len(members) == 1
            assert members[0].user_id == user.id
            assert members[0].workspace_id == ws.id

            grants = db_session.scalars(
                select(RoleGrant).where(
                    RoleGrant.workspace_id == ws.id, RoleGrant.user_id == user.id
                )
            ).all()
            assert len(grants) == 1
            assert grants[0].grant_role == "manager"
            assert grants[0].scope_property_id is None
            assert grants[0].created_by_user_id is None
        finally:
            reset_current(token)
