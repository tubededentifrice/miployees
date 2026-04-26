"""Integration tests for :mod:`app.adapters.db.workspace` against a real DB.

Covers the post-migration schema shape (tables, unique slug, FK,
composite index), the referential-integrity contract on
``user_workspace`` (cascade delete, FK rejection, CHECK constraints),
and the tenant-filter behaviour on reads (``user_workspace`` scoped
vs. ``workspace`` agnostic).

The sibling ``tests/unit/test_db_workspace.py`` covers pure-Python
model construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"workspaces",
§"user_workspace", and ``docs/specs/01-architecture.md`` §"Workspace
addressing" / §"Tenant filter enforcement".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_CTX = WorkspaceContext(
    workspace_id="01HWA00000000000000000WSPA",
    workspace_slug="workspace-a",
    actor_id="01HWA00000000000000000USRA",
    actor_kind="user",
    actor_grant_role="manager",
    actor_was_owner_member=True,
    audit_correlation_id="01HWA00000000000000000CRLA",
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across tests (same rationale as the audit module's
    ``filtered_factory``). Building a fresh ``sessionmaker`` each test
    and re-installing the hook has hit a known SQLAlchemy quirk where
    ``event.contains`` reports the listener but the new sessions'
    dispatch list comes back empty.

    The top-level ``db_session`` fixture binds directly to a raw
    connection for SAVEPOINT isolation, bypassing the default
    sessionmaker and therefore the filter. Tests that need to observe
    ``TenantFilterMissing`` use this factory explicitly.
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
def _ensure_user_workspace_registered() -> None:
    """Re-register ``user_workspace`` as workspace-scoped before each test.

    ``app.adapters.db.workspace`` registers the table at import time,
    but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the import-
    time registration loses the race and our tenant-filter assertions
    pass in isolation yet silently drop the filter under the full
    suite. ``workspace`` is intentionally NOT re-registered — it is
    tenant-agnostic by design.
    """
    registry.register("user_workspace")


class TestMigrationShape:
    """The migration lands ``workspace`` + ``user_workspace`` with FKs."""

    def test_workspace_table_exists(self, engine: Engine) -> None:
        assert "workspace" in inspect(engine).get_table_names()

    def test_user_workspace_table_exists(self, engine: Engine) -> None:
        assert "user_workspace" in inspect(engine).get_table_names()

    def test_workspace_columns_match_v1_slice(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("workspace")}
        expected = {
            "id",
            "slug",
            "name",
            "plan",
            "quota_json",
            # ``settings_json`` landed with cd-jdhm to host the recovery
            # kill-switch flag; §02 "Settings cascade" documents the
            # single flat-map shape this column carries for every
            # registered non-base workspace setting (the four owner-
            # mutable base columns below land via cd-n6p as first-class
            # columns rather than dotted keys on this map).
            "settings_json",
            # cd-n6p — owner-mutable identity-level base columns +
            # ``updated_at`` SSE invalidation seam (§02 "workspaces"
            # base columns).
            "default_timezone",
            "default_locale",
            "default_currency",
            "updated_at",
            "created_at",
            "owner_onboarded_at",
        }
        assert set(cols) == expected
        # Only ``owner_onboarded_at`` is nullable in the v1 slice; all
        # other columns carry NOT NULL.
        assert cols["owner_onboarded_at"]["nullable"] is True
        for name in expected - {"owner_onboarded_at"}:
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"
        pk = inspect(engine).get_pk_constraint("workspace")
        assert pk["constrained_columns"] == ["id"]

    def test_workspace_slug_unique_index(self, engine: Engine) -> None:
        """``slug`` must carry a unique constraint / index."""
        unique_cols: list[list[str]] = [
            uc["column_names"]
            for uc in inspect(engine).get_unique_constraints("workspace")
        ]
        # Unique indexes reported via ``get_indexes`` on some dialects
        # (Postgres). Union both sources so either shape satisfies.
        unique_idx_cols: list[list[str]] = [
            ix["column_names"]
            for ix in inspect(engine).get_indexes("workspace")
            if ix.get("unique")
        ]
        assert ["slug"] in unique_cols + unique_idx_cols

    def test_user_workspace_composite_pk(self, engine: Engine) -> None:
        pk = inspect(engine).get_pk_constraint("user_workspace")
        assert pk["constrained_columns"] == ["user_id", "workspace_id"]

    def test_user_workspace_fk_to_workspace(self, engine: Engine) -> None:
        fks = inspect(engine).get_foreign_keys("user_workspace")
        assert len(fks) == 1
        fk = fks[0]
        assert fk["constrained_columns"] == ["workspace_id"]
        assert fk["referred_table"] == "workspace"
        assert fk["referred_columns"] == ["id"]
        # Cascade delete: removing a workspace sweeps the junction.
        assert fk["options"].get("ondelete") == "CASCADE"

    def test_user_workspace_index_is_present(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("user_workspace")
        }
        assert "ix_user_workspace_workspace" in indexes
        assert indexes["ix_user_workspace_workspace"]["column_names"] == [
            "workspace_id"
        ]


class TestWorkspaceInsertAndRead:
    """A workspace round-trips through the DB verbatim."""

    def test_insert_then_read_back(self, db_session: Session) -> None:
        """Insert a workspace, commit, reload, compare."""
        ws = Workspace(
            id="01HWA00000000000000000WRBA",
            slug="read-back-slug",
            name="Read Back",
            plan="free",
            quota_json={"users_max": 5},
            settings_json={"auth.self_service_recovery_enabled": False},
            created_at=_PINNED,
        )
        db_session.add(ws)
        db_session.commit()
        db_session.expire_all()

        loaded = db_session.scalars(
            select(Workspace).where(Workspace.slug == "read-back-slug")
        ).one()
        assert loaded.id == "01HWA00000000000000000WRBA"
        assert loaded.name == "Read Back"
        assert loaded.plan == "free"
        assert loaded.quota_json == {"users_max": 5}
        # ``settings_json`` round-trips verbatim — the resolver (cd-n6p)
        # reads whole payloads here without further coalescing.
        assert loaded.settings_json == {"auth.self_service_recovery_enabled": False}

    def test_insert_defaults_settings_json_to_empty(self, db_session: Session) -> None:
        """Omitting ``settings_json`` yields the empty-map default.

        The ORM default + server-side default together ensure a
        caller that doesn't know about the column still writes
        ``{}`` — both the unit-level path (ORM ``default=dict``) and
        the raw-SQL path (``server_default='{}'``) land the same
        sentinel, so the recovery kill-switch can read the column
        without worrying about NULL on a pre-cd-jdhm row.
        """
        ws = Workspace(
            id="01HWA00000000000000000DFLT",
            slug="default-settings-slug",
            name="Default Settings",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
        db_session.add(ws)
        db_session.commit()
        db_session.expire_all()

        loaded = db_session.scalars(
            select(Workspace).where(Workspace.slug == "default-settings-slug")
        ).one()
        assert loaded.settings_json == {}


class TestUniqueSlug:
    """Two workspaces cannot share a slug."""

    def test_duplicate_slug_raises(self, db_session: Session) -> None:
        db_session.add(
            Workspace(
                id="01HWA00000000000000000DUP1",
                slug="duplicate-slug",
                name="First",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        db_session.flush()

        db_session.add(
            Workspace(
                id="01HWA00000000000000000DUP2",
                slug="duplicate-slug",
                name="Second",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


class TestPlanCheckConstraint:
    """``plan`` is constrained to the enum set."""

    def test_bogus_plan_rejected(self, db_session: Session) -> None:
        db_session.add(
            Workspace(
                id="01HWA00000000000000000BPLA",
                slug="bogus-plan",
                name="Bogus",
                plan="bogus",
                quota_json={},
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_every_allowed_plan_roundtrips(self, db_session: Session) -> None:
        """Each of the four allowed plan values persists."""
        for idx, plan in enumerate(("free", "pro", "enterprise", "unlimited")):
            db_session.add(
                Workspace(
                    id=f"01HWA00000000000000000PL{idx:02d}",
                    slug=f"plan-test-{plan}",
                    name=f"Plan {plan}",
                    plan=plan,
                    quota_json={},
                    created_at=_PINNED,
                )
            )
        db_session.flush()
        db_session.rollback()


class TestUserWorkspaceForeignKey:
    """The FK on ``workspace_id`` rejects orphan inserts."""

    def test_orphan_workspace_id_rejected(self, db_session: Session) -> None:
        """Inserting a ``user_workspace`` with no matching workspace fails.

        Guards SQLite FK enforcement — the engine factory installs
        ``PRAGMA foreign_keys=ON`` on every connection. On Postgres the
        FK is always enforced.
        """
        db_session.add(
            UserWorkspace(
                user_id="01HWA00000000000000000USRZ",
                workspace_id="01HWA00000000000000000WSPZ",
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


class TestUserWorkspaceSourceCheck:
    """``source`` is constrained to the allowed grant kinds."""

    def test_invalid_source_rejected(self, db_session: Session) -> None:
        db_session.add(
            Workspace(
                id="01HWA00000000000000000SRCA",
                slug="source-check",
                name="SrcCheck",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        db_session.flush()
        db_session.add(
            UserWorkspace(
                user_id="01HWA00000000000000000USRC",
                workspace_id="01HWA00000000000000000SRCA",
                source="bogus_grant",
                added_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


class TestCascadeDelete:
    """Deleting a workspace sweeps its ``user_workspace`` rows."""

    def test_delete_workspace_cascades(self, db_session: Session) -> None:
        workspace_id = "01HWA00000000000000000CSCA"
        db_session.add(
            Workspace(
                id=workspace_id,
                slug="cascade-slug",
                name="Cascade",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        db_session.flush()
        db_session.add(
            UserWorkspace(
                user_id="01HWA00000000000000000USRD",
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        db_session.flush()

        ws = db_session.get(Workspace, workspace_id)
        assert ws is not None
        db_session.delete(ws)
        db_session.flush()

        # The junction row is gone; emit the follow-up query under a
        # ctx because ``user_workspace`` is workspace-scoped.
        token = set_current(_CTX)
        try:
            remaining = db_session.scalars(
                select(UserWorkspace).where(UserWorkspace.workspace_id == workspace_id)
            ).all()
        finally:
            reset_current(token)
        assert remaining == []


class TestBootstrapHelper:
    """The ``bootstrap_workspace`` seed helper lands both rows."""

    def test_seeds_workspace_and_membership(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        # ``bootstrap_workspace`` seeds the ``owners`` permission-group
        # row (cd-ctb) whose ``permission_group_member.user_id`` FKs to
        # ``user`` — so the owner must exist as a real row, not a
        # fabricated ULID literal. The ``user_workspace.user_id``
        # column is still a soft reference (see
        # ``app/adapters/db/workspace/models.py`` §docstring), but
        # seeding through the same helper ensures both paths see a
        # consistent identity.
        owner = bootstrap_user(
            db_session,
            email="bootstrap-owner@example.com",
            display_name="BootstrapOwner",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bootstrap-slug",
            name="Bootstrap",
            owner_user_id=owner.id,
            clock=clock,
        )
        assert ws.slug == "bootstrap-slug"
        assert ws.name == "Bootstrap"
        assert ws.plan == "free"
        assert ws.quota_json == {}
        assert ws.created_at == _PINNED

        # The membership row exists and is workspace-scoped; the follow-
        # up query runs under a matching ctx so the tenant filter is
        # happy. ``db_session`` doesn't have the filter installed, but
        # the ctx guard is cheap insurance against future changes.
        token = set_current(
            WorkspaceContext(
                workspace_id=ws.id,
                workspace_slug=ws.slug,
                actor_id=owner.id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id="01HWA00000000000000000CRLX",
            )
        )
        try:
            memberships = db_session.scalars(
                select(UserWorkspace).where(UserWorkspace.workspace_id == ws.id)
            ).all()
        finally:
            reset_current(token)
        assert len(memberships) == 1
        assert memberships[0].user_id == owner.id
        assert memberships[0].source == "workspace_grant"
        # SQLite's ``DateTime(timezone=True)`` loses tzinfo on reload
        # (the driver returns a naive ``datetime``); Postgres keeps it.
        # Compare wall-clock components so the assertion holds on both.
        assert memberships[0].added_at.replace(tzinfo=None) == _PINNED.replace(
            tzinfo=None
        )


class TestTenantFilter:
    """``user_workspace`` is scoped; ``workspace`` is agnostic."""

    def test_user_workspace_read_without_ctx_raises(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(UserWorkspace))
        assert exc.value.table == "user_workspace"

    def test_workspace_read_without_ctx_succeeds(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """``workspace`` is the tenancy anchor; reads without a ctx are fine."""
        with filtered_factory() as session:
            # Returns whatever rows live in the shared module-scoped DB;
            # the point is that the statement executes without raising.
            session.execute(select(Workspace)).all()
