"""Integration tests for :mod:`app.adapters.db.time` against a real DB.

Covers the post-migration schema shape (tables, unique composites,
FKs, CHECK constraints), the referential-integrity contract on all
three tables (``workspace_id`` CASCADE; ``user_id`` RESTRICT),
happy-path round-trip of every model (insert + select + update +
delete), CHECK + UNIQUE violations, the open-shift ``ends_at IS
NULL`` query (acceptance criterion), and tenant-filter behaviour
(all three tables scoped; SELECT without a
:class:`WorkspaceContext` raises :class:`TenantFilterMissing`).

The sibling ``tests/unit/test_db_time.py`` covers pure-Python model
construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"shift", §"leave",
§"geofence_setting", and ``docs/specs/09-time-payroll-expenses.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User
from app.adapters.db.time.models import GeofenceSetting, Leave, Shift
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_SHIFT_START = _PINNED
_SHIFT_END = _PINNED + timedelta(hours=4)
_LEAVE_START = _PINNED + timedelta(days=7)
_LEAVE_END = _LEAVE_START + timedelta(days=3)


_TIME_TABLES: tuple[str, ...] = ("shift", "leave", "geofence_setting")


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across tests. The top-level ``db_session`` fixture
    binds directly to a raw connection for SAVEPOINT isolation and
    therefore bypasses the filter; tests that need to observe
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
def _ensure_time_registered() -> None:
    """Re-register the three time tables as workspace-scoped before each test.

    ``app.adapters.db.time.__init__`` registers them at import time,
    but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite.
    """
    for table in _TIME_TABLES:
        registry.register(table)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace``."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLI",
    )


def _bootstrap(
    session: Session, *, email: str, display: str, slug: str, name: str
) -> tuple[Workspace, User]:
    """Seed a user + workspace pair for a test."""
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(session, email=email, display_name=display, clock=clock)
    workspace = bootstrap_workspace(
        session, slug=slug, name=name, owner_user_id=user.id, clock=clock
    )
    return workspace, user


class TestMigrationShape:
    """The migration lands all three tables with correct keys + indexes."""

    def test_all_tables_exist(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        for table in _TIME_TABLES:
            assert table in tables, f"{table} missing from schema"

    def test_shift_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("shift")}
        expected = {
            "id",
            "workspace_id",
            "user_id",
            "starts_at",
            "ends_at",
            "property_id",
            "source",
            "notes_md",
            "approved_by",
            "approved_at",
        }
        assert set(cols) == expected
        for nullable in (
            "ends_at",
            "property_id",
            "notes_md",
            "approved_by",
            "approved_at",
        ):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"
        for notnull in expected - {
            "ends_at",
            "property_id",
            "notes_md",
            "approved_by",
            "approved_at",
        }:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_shift_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("shift")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("user_id",)]["referred_table"] == "user"
        # ``RESTRICT`` preserves labour-law records against a raw
        # ``DELETE FROM user`` (§09 / §15). Normal erasure routes
        # through ``crewday admin purge --person``, which keeps the
        # user row and anonymises it.
        assert fks[("user_id",)]["options"].get("ondelete") == "RESTRICT"
        # ``property_id`` / ``approved_by`` are soft-ref strings, no FK.
        assert ("property_id",) not in fks
        assert ("approved_by",) not in fks

    def test_shift_indexes(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("shift")}
        assert "ix_shift_user_ends_at" in indexes
        assert indexes["ix_shift_user_ends_at"]["column_names"] == [
            "user_id",
            "ends_at",
        ]
        assert "ix_shift_workspace_starts" in indexes
        assert indexes["ix_shift_workspace_starts"]["column_names"] == [
            "workspace_id",
            "starts_at",
        ]

    def test_leave_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("leave")}
        expected = {
            "id",
            "workspace_id",
            "user_id",
            "kind",
            "starts_at",
            "ends_at",
            "status",
            "reason_md",
            "decided_by",
            "decided_at",
            "created_at",
        }
        assert set(cols) == expected
        for nullable in ("reason_md", "decided_by", "decided_at"):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"

    def test_leave_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("leave")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("user_id",)]["referred_table"] == "user"
        # ``RESTRICT`` — same rationale as ``shift.user_id``.
        assert fks[("user_id",)]["options"].get("ondelete") == "RESTRICT"
        assert ("decided_by",) not in fks

    def test_leave_workspace_status_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("leave")}
        assert "ix_leave_workspace_status" in indexes
        assert indexes["ix_leave_workspace_status"]["column_names"] == [
            "workspace_id",
            "status",
        ]

    def test_geofence_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("geofence_setting")}
        expected = {
            "id",
            "workspace_id",
            "property_id",
            "lat",
            "lon",
            "radius_m",
            "enabled",
        }
        assert set(cols) == expected
        for name in expected:
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"

    def test_geofence_unique_workspace_property(self, engine: Engine) -> None:
        uniques = {
            u["name"]: u
            for u in inspect(engine).get_unique_constraints("geofence_setting")
        }
        assert "uq_geofence_setting_workspace_property" in uniques
        assert uniques["uq_geofence_setting_workspace_property"]["column_names"] == [
            "workspace_id",
            "property_id",
        ]

    def test_geofence_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("geofence_setting")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        # ``property_id`` is intentionally a soft-ref — see module docs.
        assert ("property_id",) not in fks


class TestShiftCrud:
    """Insert + select + update + delete round-trip on :class:`Shift`."""

    def test_round_trip_and_open_shift_query(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="shift-crud@example.com",
            display="ShiftCrud",
            slug="shift-crud-ws",
            name="ShiftCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            open_shift = Shift(
                id="01HWA00000000000000000SHOP",
                workspace_id=workspace.id,
                user_id=user.id,
                starts_at=_SHIFT_START,
                source="manual",
            )
            closed_shift = Shift(
                id="01HWA00000000000000000SHCL",
                workspace_id=workspace.id,
                user_id=user.id,
                starts_at=_SHIFT_START - timedelta(days=1),
                ends_at=_SHIFT_END - timedelta(days=1),
                source="geofence",
                property_id="01HWA00000000000000000PRPX",
            )
            db_session.add_all([open_shift, closed_shift])
            db_session.flush()

            # Open-shift index query (acceptance criterion): ``ends_at
            # IS NULL`` filtered by user.
            open_rows = db_session.scalars(
                select(Shift)
                .where(Shift.user_id == user.id)
                .where(Shift.ends_at.is_(None))
            ).all()
            assert [r.id for r in open_rows] == ["01HWA00000000000000000SHOP"]

            # Update: close the open shift + approve it.
            loaded = db_session.get(Shift, open_shift.id)
            assert loaded is not None
            loaded.ends_at = _SHIFT_END
            loaded.approved_by = user.id
            loaded.approved_at = _SHIFT_END
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(Shift, open_shift.id)
            assert reloaded is not None
            # SQLite's ``DateTime(timezone=True)`` storage is a naive
            # ISO-8601 string (§01 "Portable DateTime"), so the reloaded
            # timestamp comes back without tz info even though we wrote
            # a UTC-aware value. Normalise both sides for the equality.
            assert reloaded.ends_at is not None
            assert reloaded.ends_at.replace(tzinfo=None) == _SHIFT_END.replace(
                tzinfo=None
            )
            assert reloaded.approved_by == user.id

            # After the update nothing is open for this user.
            still_open = db_session.scalars(
                select(Shift)
                .where(Shift.user_id == user.id)
                .where(Shift.ends_at.is_(None))
            ).all()
            assert still_open == []

            # Delete: sweep the closed shift directly.
            db_session.delete(closed_shift)
            db_session.flush()
            assert db_session.get(Shift, closed_shift.id) is None
        finally:
            reset_current(token)


class TestLeaveCrud:
    """Insert + select + update + delete round-trip on :class:`Leave`."""

    def test_round_trip_and_status_index_query(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="leave-crud@example.com",
            display="LeaveCrud",
            slug="leave-crud-ws",
            name="LeaveCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            pending = Leave(
                id="01HWA00000000000000000LVPD",
                workspace_id=workspace.id,
                user_id=user.id,
                kind="vacation",
                starts_at=_LEAVE_START,
                ends_at=_LEAVE_END,
                status="pending",
                reason_md="Anniversary trip.",
                created_at=_PINNED,
            )
            approved = Leave(
                id="01HWA00000000000000000LVAP",
                workspace_id=workspace.id,
                user_id=user.id,
                kind="sick",
                starts_at=_LEAVE_START + timedelta(days=14),
                ends_at=_LEAVE_START + timedelta(days=15),
                status="approved",
                decided_by=user.id,
                decided_at=_PINNED,
                created_at=_PINNED,
            )
            db_session.add_all([pending, approved])
            db_session.flush()

            # Inbox query: "pending leave requests for this workspace"
            # — the (workspace_id, status) index's target.
            inbox = db_session.scalars(
                select(Leave)
                .where(Leave.workspace_id == workspace.id)
                .where(Leave.status == "pending")
            ).all()
            assert [r.id for r in inbox] == ["01HWA00000000000000000LVPD"]

            # Update: approve the pending request.
            loaded = db_session.get(Leave, pending.id)
            assert loaded is not None
            loaded.status = "approved"
            loaded.decided_by = user.id
            loaded.decided_at = _PINNED
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(Leave, pending.id)
            assert reloaded is not None
            assert reloaded.status == "approved"
            assert reloaded.decided_by == user.id

            # Delete: worker cancels the other request.
            db_session.delete(approved)
            db_session.flush()
            assert db_session.get(Leave, approved.id) is None
        finally:
            reset_current(token)


class TestGeofenceCrud:
    """Insert + select + update + delete round-trip on :class:`GeofenceSetting`."""

    def test_round_trip_and_toggle(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="gf-crud@example.com",
            display="GfCrud",
            slug="gf-crud-ws",
            name="GfCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            gs = GeofenceSetting(
                id="01HWA00000000000000000GFSC",
                workspace_id=workspace.id,
                property_id="01HWA00000000000000000PRPG",
                lat=43.5804,
                lon=7.1251,
                radius_m=75,
                enabled=True,
            )
            db_session.add(gs)
            db_session.flush()

            loaded = db_session.get(GeofenceSetting, gs.id)
            assert loaded is not None
            assert loaded.enabled is True
            assert loaded.radius_m == 75

            # Update: owner widens the perimeter and flips the kill switch.
            loaded.radius_m = 120
            loaded.enabled = False
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(GeofenceSetting, gs.id)
            assert reloaded is not None
            assert reloaded.radius_m == 120
            assert reloaded.enabled is False

            db_session.delete(reloaded)
            db_session.flush()
            assert db_session.get(GeofenceSetting, gs.id) is None
        finally:
            reset_current(token)


class TestCheckConstraints:
    """CHECK constraints reject values outside the v1 enums / bounds."""

    def test_bogus_shift_source_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-shift-source@example.com",
            display="BogusShiftSource",
            slug="bogus-shift-source-ws",
            name="BogusShiftSourceWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Shift(
                    id="01HWA00000000000000000SHFX",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    starts_at=_SHIFT_START,
                    source="smoke_signal",
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_leave_kind_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-leave-kind@example.com",
            display="BogusLeaveKind",
            slug="bogus-leave-kind-ws",
            name="BogusLeaveKindWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Leave(
                    id="01HWA00000000000000000LVKX",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    kind="jury_duty",
                    starts_at=_LEAVE_START,
                    ends_at=_LEAVE_END,
                    status="pending",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_leave_status_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-leave-status@example.com",
            display="BogusLeaveStatus",
            slug="bogus-leave-status-ws",
            name="BogusLeaveStatusWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Leave(
                    id="01HWA00000000000000000LVSX",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    kind="vacation",
                    starts_at=_LEAVE_START,
                    ends_at=_LEAVE_END,
                    status="maybe_later",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_leave_ends_before_starts_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="leave-reverse@example.com",
            display="LeaveReverse",
            slug="leave-reverse-ws",
            name="LeaveReverseWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Leave(
                    id="01HWA00000000000000000LVRX",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    kind="vacation",
                    # Flip the window — should trip the CHECK.
                    starts_at=_LEAVE_END,
                    ends_at=_LEAVE_START,
                    status="pending",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_leave_zero_length_rejected(self, db_session: Session) -> None:
        """``ends_at > starts_at`` — zero-length leave is a data bug."""
        workspace, user = _bootstrap(
            db_session,
            email="leave-zero@example.com",
            display="LeaveZero",
            slug="leave-zero-ws",
            name="LeaveZeroWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Leave(
                    id="01HWA00000000000000000LVZX",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    kind="vacation",
                    starts_at=_LEAVE_START,
                    ends_at=_LEAVE_START,
                    status="pending",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_geofence_zero_radius_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="gf-zero@example.com",
            display="GfZero",
            slug="gf-zero-ws",
            name="GfZeroWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                GeofenceSetting(
                    id="01HWA00000000000000000GFZX",
                    workspace_id=workspace.id,
                    property_id="01HWA00000000000000000PRPZ",
                    lat=0.0,
                    lon=0.0,
                    radius_m=0,
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_geofence_negative_radius_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="gf-neg@example.com",
            display="GfNeg",
            slug="gf-neg-ws",
            name="GfNegWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                GeofenceSetting(
                    id="01HWA00000000000000000GFNX",
                    workspace_id=workspace.id,
                    property_id="01HWA00000000000000000PRPN",
                    lat=0.0,
                    lon=0.0,
                    radius_m=-1,
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_geofence_lat_out_of_range_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="gf-lat@example.com",
            display="GfLat",
            slug="gf-lat-ws",
            name="GfLatWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                GeofenceSetting(
                    id="01HWA00000000000000000GFLX",
                    workspace_id=workspace.id,
                    property_id="01HWA00000000000000000PRPL",
                    lat=91.0,  # north of the pole
                    lon=0.0,
                    radius_m=50,
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_geofence_lon_out_of_range_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="gf-lon@example.com",
            display="GfLon",
            slug="gf-lon-ws",
            name="GfLonWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                GeofenceSetting(
                    id="01HWA00000000000000000GFOX",
                    workspace_id=workspace.id,
                    property_id="01HWA00000000000000000PRPM",
                    lat=0.0,
                    lon=180.0001,  # east of the antemeridian
                    radius_m=50,
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestUniqueConstraint:
    """``(workspace_id, property_id)`` is a single geofence per property."""

    def test_duplicate_workspace_property_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="gf-dup@example.com",
            display="GfDup",
            slug="gf-dup-ws",
            name="GfDupWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                GeofenceSetting(
                    id="01HWA00000000000000000GFD1",
                    workspace_id=workspace.id,
                    property_id="01HWA00000000000000000PRPD",
                    lat=43.0,
                    lon=7.0,
                    radius_m=50,
                    enabled=True,
                )
            )
            db_session.flush()

            db_session.add(
                GeofenceSetting(
                    id="01HWA00000000000000000GFD2",
                    workspace_id=workspace.id,
                    property_id="01HWA00000000000000000PRPD",
                    lat=43.1,
                    lon=7.1,
                    radius_m=75,
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_same_property_different_workspaces_allowed(
        self, db_session: Session
    ) -> None:
        """Two workspaces may each configure a geofence for the same property.

        ``property`` is tenant-agnostic (see ``places/__init__``) and
        can appear in multiple workspaces' operations via
        ``property_workspace``; uniqueness is on the
        ``(workspace_id, property_id)`` pair, not on ``property_id``
        alone. Both inserts must succeed.
        """
        ws_a, user_a = _bootstrap(
            db_session,
            email="gf-dist-a@example.com",
            display="GfDistA",
            slug="gf-dist-a-ws",
            name="GfDistAWS",
        )
        ws_b, user_b = _bootstrap(
            db_session,
            email="gf-dist-b@example.com",
            display="GfDistB",
            slug="gf-dist-b-ws",
            name="GfDistBWS",
        )
        # Write under ws_a's ctx.
        token = set_current(_ctx_for(ws_a, user_a.id))
        try:
            db_session.add(
                GeofenceSetting(
                    id="01HWA00000000000000000GFAX",
                    workspace_id=ws_a.id,
                    property_id="01HWA00000000000000000PRPS",
                    lat=43.0,
                    lon=7.0,
                    radius_m=50,
                    enabled=True,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        token = set_current(_ctx_for(ws_b, user_b.id))
        try:
            db_session.add(
                GeofenceSetting(
                    id="01HWA00000000000000000GFBX",
                    workspace_id=ws_b.id,
                    property_id="01HWA00000000000000000PRPS",
                    lat=43.1,
                    lon=7.1,
                    radius_m=75,
                    enabled=True,
                )
            )
            db_session.flush()

            rows = db_session.scalars(
                select(GeofenceSetting).where(
                    GeofenceSetting.property_id == "01HWA00000000000000000PRPS"
                )
            ).all()
            assert {r.workspace_id for r in rows} == {ws_a.id, ws_b.id}
        finally:
            reset_current(token)


class TestCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps every time-context row belonging to it."""

    def test_delete_workspace_cascades(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="cascade-time@example.com",
            display="CascadeTime",
            slug="cascade-time-ws",
            name="CascadeTimeWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Shift(
                    id="01HWA00000000000000000SHCD",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    starts_at=_SHIFT_START,
                    source="manual",
                )
            )
            db_session.add(
                Leave(
                    id="01HWA00000000000000000LVCD",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    kind="vacation",
                    starts_at=_LEAVE_START,
                    ends_at=_LEAVE_END,
                    status="pending",
                    created_at=_PINNED,
                )
            )
            db_session.add(
                GeofenceSetting(
                    id="01HWA00000000000000000GFCD",
                    workspace_id=workspace.id,
                    property_id="01HWA00000000000000000PRPC",
                    lat=43.0,
                    lon=7.0,
                    radius_m=50,
                    enabled=True,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        # justification: workspace delete is a platform-level op; no
        # :class:`WorkspaceContext` applies once the tenant itself is
        # the target.
        loaded_ws = db_session.get(Workspace, workspace.id)
        assert loaded_ws is not None
        with tenant_agnostic():
            db_session.delete(loaded_ws)
            db_session.flush()

        # Re-read under a fresh ctx for the vanished workspace; the
        # tenant filter still needs a ctx, but rows are gone.
        token = set_current(_ctx_for(workspace, user.id))
        try:
            assert (
                db_session.scalars(
                    select(Shift).where(Shift.workspace_id == workspace.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(Leave).where(Leave.workspace_id == workspace.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(GeofenceSetting).where(
                        GeofenceSetting.workspace_id == workspace.id
                    )
                ).all()
                == []
            )
        finally:
            reset_current(token)


class TestRestrictOnUserDelete:
    """Hard-deleting a user is blocked while shift / leave rows exist.

    The FK cascade is ``RESTRICT``, not ``CASCADE`` — labour-law
    records (§09) and approved leave (§02 / §09) outlive the user's
    credentials. The normal erasure path is
    ``crewday admin purge --person`` (§15) which anonymises the user
    row in place and keeps the FK reference valid; a raw
    ``DELETE FROM user`` is the unusual path and must be stopped
    here rather than silently taking the evidence with it.
    """

    def test_delete_user_with_shift_raises(self, db_session: Session) -> None:
        """A raw ``DELETE FROM user`` with a dependent shift row trips RESTRICT.

        The ``IntegrityError`` is what tells the admin flow to route
        through ``crewday admin purge --person`` instead. We do not
        re-assert row survival after ``rollback`` because SQLAlchemy's
        rollback unwinds the whole session-level transaction (the
        shift insert included); the constraint contract is the
        IntegrityError itself.
        """
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="restrict-shift@example.com",
            display="RestrictShift",
            slug="restrict-shift-ws",
            name="RestrictShiftWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Shift(
                    id="01HWA00000000000000000SHUR",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    starts_at=_SHIFT_START,
                    source="manual",
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        # justification: user delete is identity-layer, not workspace-
        # scoped; the filter would block the DELETE without this.
        with tenant_agnostic():
            loaded_user = db_session.get(User, user.id)
            assert loaded_user is not None
            db_session.delete(loaded_user)
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()

    def test_delete_user_with_leave_raises(self, db_session: Session) -> None:
        """A raw ``DELETE FROM user`` with a dependent leave row trips RESTRICT."""
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="restrict-leave@example.com",
            display="RestrictLeave",
            slug="restrict-leave-ws",
            name="RestrictLeaveWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Leave(
                    id="01HWA00000000000000000LVUR",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    kind="vacation",
                    starts_at=_LEAVE_START,
                    ends_at=_LEAVE_END,
                    status="pending",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        with tenant_agnostic():
            loaded_user = db_session.get(User, user.id)
            assert loaded_user is not None
            db_session.delete(loaded_user)
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()

    def test_delete_user_allowed_after_rows_cleared(self, db_session: Session) -> None:
        """With no shift / leave rows, ``DELETE FROM user`` succeeds.

        Sanity-check the flip side of the RESTRICT: the FK doesn't
        permanently nail the user in place; it just forces the
        caller to purge / reassign first.
        """
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="restrict-clear@example.com",
            display="RestrictClear",
            slug="restrict-clear-ws",
            name="RestrictClearWS",
        )
        # Need someone else to own the workspace after the delete.
        other = bootstrap_user(
            db_session,
            email="restrict-clear-other@example.com",
            display_name="RestrictClearOther",
            clock=FrozenClock(_PINNED),
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Shift(
                    id="01HWA00000000000000000SHUC",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    starts_at=_SHIFT_START,
                    source="manual",
                )
            )
            db_session.add(
                Shift(
                    id="01HWA00000000000000000SHSW",
                    workspace_id=workspace.id,
                    user_id=other.id,
                    starts_at=_SHIFT_START,
                    source="manual",
                )
            )
            db_session.flush()

            # Purge the user's shift rows the admin way — explicitly.
            target_shift = db_session.get(Shift, "01HWA00000000000000000SHUC")
            assert target_shift is not None
            db_session.delete(target_shift)
            db_session.flush()
        finally:
            reset_current(token)

        with tenant_agnostic():
            loaded_user = db_session.get(User, user.id)
            assert loaded_user is not None
            db_session.delete(loaded_user)
            db_session.flush()
            assert db_session.get(User, user.id) is None

        # The sibling user's shift row survives.
        token = set_current(_ctx_for(workspace, other.id))
        try:
            survivors = db_session.scalars(
                select(Shift).where(Shift.user_id == other.id)
            ).all()
            assert [s.id for s in survivors] == ["01HWA00000000000000000SHSW"]
        finally:
            reset_current(token)


class TestTenantFilter:
    """All three time tables are workspace-scoped under the filter."""

    @pytest.mark.parametrize("model", [Shift, Leave, GeofenceSetting])
    def test_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[Shift] | type[Leave] | type[GeofenceSetting],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__
