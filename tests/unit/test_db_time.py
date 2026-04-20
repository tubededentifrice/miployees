"""Unit tests for :mod:`app.adapters.db.time.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__`` (CHECK
constraints, unique composites, index shape, tenancy-registry
membership). Integration coverage (migrations, FK cascade, CHECK /
UNIQUE violations against a real DB, tenant filter behaviour, CRUD
round-trips) lives in ``tests/integration/test_db_time.py``.

See ``docs/specs/02-domain-model.md`` §"shift", §"leave",
§"geofence_setting", and ``docs/specs/09-time-payroll-expenses.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.adapters.db.time import GeofenceSetting, Leave, Shift
from app.adapters.db.time import models as time_models

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


class TestShiftModel:
    """The ``Shift`` mapped class constructs from the v1 slice."""

    def test_minimal_open_shift_construction(self) -> None:
        shift = Shift(
            id="01HWA00000000000000000SHFA",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            starts_at=_PINNED,
            source="manual",
        )
        assert shift.id == "01HWA00000000000000000SHFA"
        assert shift.workspace_id == "01HWA00000000000000000WSPA"
        assert shift.user_id == "01HWA00000000000000000USRA"
        assert shift.starts_at == _PINNED
        # Nullable columns default to ``None``.
        assert shift.ends_at is None
        assert shift.property_id is None
        assert shift.notes_md is None
        assert shift.approved_by is None
        assert shift.approved_at is None
        assert shift.source == "manual"

    def test_closed_shift_with_approval(self) -> None:
        shift = Shift(
            id="01HWA00000000000000000SHFB",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            starts_at=_PINNED,
            ends_at=_LATER,
            property_id="01HWA00000000000000000PRPA",
            source="geofence",
            notes_md="Closed the pool at sundown.",
            approved_by="01HWA00000000000000000USRM",
            approved_at=_LATER,
        )
        assert shift.ends_at == _LATER
        assert shift.property_id == "01HWA00000000000000000PRPA"
        assert shift.source == "geofence"
        assert shift.notes_md == "Closed the pool at sundown."
        assert shift.approved_by == "01HWA00000000000000000USRM"
        assert shift.approved_at == _LATER

    def test_tablename(self) -> None:
        assert Shift.__tablename__ == "shift"

    def test_source_check_present(self) -> None:
        # Constraint name ``source`` on the model; the shared naming
        # convention rewrites it to ``ck_shift_source`` on the bound
        # column, so match by suffix (mirrors the sibling ``tasks`` /
        # ``stays`` test pattern).
        checks = [
            c
            for c in Shift.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("source")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for source in ("manual", "geofence", "occurrence"):
            assert source in sql, f"{source} missing from CHECK constraint"

    def test_user_ends_at_index_present(self) -> None:
        """The open-shift scan index — ``(user_id, ends_at)``."""
        indexes = [i for i in Shift.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_shift_user_ends_at" in names
        target = next(i for i in indexes if i.name == "ix_shift_user_ends_at")
        assert [c.name for c in target.columns] == ["user_id", "ends_at"]

    def test_workspace_starts_index_present(self) -> None:
        indexes = [i for i in Shift.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_shift_workspace_starts" in names
        target = next(i for i in indexes if i.name == "ix_shift_workspace_starts")
        assert [c.name for c in target.columns] == ["workspace_id", "starts_at"]


class TestLeaveModel:
    """The ``Leave`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        leave = Leave(
            id="01HWA00000000000000000LVAA",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            kind="vacation",
            starts_at=_PINNED,
            ends_at=_LATER,
            status="pending",
            created_at=_PINNED,
        )
        assert leave.id == "01HWA00000000000000000LVAA"
        assert leave.kind == "vacation"
        assert leave.status == "pending"
        # Nullable fields default to ``None``.
        assert leave.reason_md is None
        assert leave.decided_by is None
        assert leave.decided_at is None
        assert leave.created_at == _PINNED

    def test_decided_construction(self) -> None:
        leave = Leave(
            id="01HWA00000000000000000LVAB",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            kind="sick",
            starts_at=_PINNED,
            ends_at=_LATER,
            status="approved",
            reason_md="Doctor's note attached.",
            decided_by="01HWA00000000000000000USRM",
            decided_at=_LATER,
            created_at=_PINNED,
        )
        assert leave.reason_md == "Doctor's note attached."
        assert leave.decided_by == "01HWA00000000000000000USRM"
        assert leave.decided_at == _LATER

    def test_tablename(self) -> None:
        assert Leave.__tablename__ == "leave"

    def test_kind_check_present(self) -> None:
        checks = [
            c
            for c in Leave.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("kind")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for kind in ("vacation", "sick", "comp", "other"):
            assert kind in sql, f"{kind} missing from CHECK constraint"

    def test_status_check_present(self) -> None:
        checks = [
            c
            for c in Leave.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("status")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for status in ("pending", "approved", "rejected", "cancelled"):
            assert status in sql, f"{status} missing from CHECK constraint"

    def test_ends_after_starts_check_present(self) -> None:
        checks = [
            c
            for c in Leave.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("ends_after_starts")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "ends_at" in sql
        assert "starts_at" in sql

    def test_workspace_status_index_present(self) -> None:
        indexes = [i for i in Leave.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_leave_workspace_status" in names
        target = next(i for i in indexes if i.name == "ix_leave_workspace_status")
        assert [c.name for c in target.columns] == ["workspace_id", "status"]


class TestGeofenceSettingModel:
    """The ``GeofenceSetting`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        gs = GeofenceSetting(
            id="01HWA00000000000000000GFSA",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPA",
            lat=43.5804,
            lon=7.1251,
            radius_m=75,
            enabled=True,
        )
        assert gs.id == "01HWA00000000000000000GFSA"
        assert gs.workspace_id == "01HWA00000000000000000WSPA"
        assert gs.property_id == "01HWA00000000000000000PRPA"
        assert gs.lat == 43.5804
        assert gs.lon == 7.1251
        assert gs.radius_m == 75
        assert gs.enabled is True

    def test_disabled_construction(self) -> None:
        gs = GeofenceSetting(
            id="01HWA00000000000000000GFSB",
            workspace_id="01HWA00000000000000000WSPA",
            property_id="01HWA00000000000000000PRPB",
            lat=-33.8688,
            lon=151.2093,
            radius_m=200,
            enabled=False,
        )
        assert gs.enabled is False

    def test_tablename(self) -> None:
        assert GeofenceSetting.__tablename__ == "geofence_setting"

    def test_radius_check_present(self) -> None:
        checks = [
            c
            for c in GeofenceSetting.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("radius_m_positive")
        ]
        assert len(checks) == 1
        assert "radius_m" in str(checks[0].sqltext)

    def test_lat_bounds_check_present(self) -> None:
        checks = [
            c
            for c in GeofenceSetting.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("lat_bounds")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "-90" in sql
        assert "90" in sql

    def test_lon_bounds_check_present(self) -> None:
        checks = [
            c
            for c in GeofenceSetting.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("lon_bounds")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "-180" in sql
        assert "180" in sql

    def test_unique_workspace_property_present(self) -> None:
        uniques = [
            u for u in GeofenceSetting.__table_args__ if isinstance(u, UniqueConstraint)
        ]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == ["workspace_id", "property_id"]
        assert uniques[0].name == "uq_geofence_setting_workspace_property"


class TestPackageReExports:
    """``app.adapters.db.time`` re-exports every v1-slice model."""

    def test_models_re_exported(self) -> None:
        assert Shift is time_models.Shift
        assert Leave is time_models.Leave
        assert GeofenceSetting is time_models.GeofenceSetting


class TestRegistryIntent:
    """Every time table is registered as workspace-scoped.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.time``: a sibling ``test_tenancy_orm_filter``
    autouse fixture calls ``registry._reset_for_tests()`` which wipes
    the process-wide set, so asserting presence after that reset
    would be flaky. The tests below encode the invariant — "every
    time table is scoped" — without over-coupling to import ordering.
    """

    def test_every_time_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in ("shift", "leave", "geofence_setting"):
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in ("shift", "leave", "geofence_setting"):
            assert table in scoped, f"{table} must be scoped"

    def test_is_scoped_reports_true(self) -> None:
        """``is_scoped`` agrees with ``scoped_tables`` membership."""
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in ("shift", "leave", "geofence_setting"):
            registry.register(table)
        for table in ("shift", "leave", "geofence_setting"):
            assert registry.is_scoped(table) is True
