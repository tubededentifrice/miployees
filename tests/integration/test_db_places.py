"""Integration tests for :mod:`app.adapters.db.places` against a real DB.

Covers the post-migration schema shape (tables, composite + unique
keys, FKs, CHECK constraints), the referential-integrity contract
on all five tables (CASCADE on workspace / property, SET NULL on
``created_by_user_id``), the multi-belonging invariant (one
property in several workspaces), and the tenant-filter behaviour
(``property_workspace`` scoped; ``property`` agnostic).

The sibling ``tests/unit/test_db_places.py`` covers pure-Python
model construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"property_workspace" and
``docs/specs/04-properties-and-stays.md`` §"Property" / §"Unit" /
§"Area".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.places.models import (
    Area,
    Property,
    PropertyClosure,
    PropertyWorkspace,
    Unit,
)
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry, tenant_agnostic
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
    ``tests/integration/test_db_authz.py``. The top-level
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
def _ensure_places_registered() -> None:
    """Re-register the places junction as workspace-scoped before each test.

    ``app.adapters.db.places.__init__`` registers the junction at
    import time, but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite. ``property`` / ``unit`` / ``area`` /
    ``property_closure`` are intentionally NOT re-registered — see
    the package docstring.
    """
    registry.register("property_workspace")


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace``."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLP",
    )


def _seed_property(
    session: Session,
    *,
    property_id: str,
    address: str = "12 Chemin des Oliviers, Antibes",
    timezone: str = "Europe/Paris",
) -> Property:
    """Insert a :class:`Property` row without a workspace link.

    ``property`` is intentionally tenant-agnostic, but the helper is
    called from code paths that may already hold a
    :class:`WorkspaceContext`. Wrapping in :func:`tenant_agnostic` is
    unnecessary because the filter ignores the table entirely.
    """
    prop = Property(
        id=property_id,
        address=address,
        timezone=timezone,
        tags_json=[],
        created_at=_PINNED,
    )
    session.add(prop)
    session.flush()
    return prop


class TestMigrationShape:
    """The migration lands all five tables with correct keys + indexes."""

    def test_property_table_exists(self, engine: Engine) -> None:
        assert "property" in inspect(engine).get_table_names()

    def test_property_workspace_table_exists(self, engine: Engine) -> None:
        assert "property_workspace" in inspect(engine).get_table_names()

    def test_unit_table_exists(self, engine: Engine) -> None:
        assert "unit" in inspect(engine).get_table_names()

    def test_area_table_exists(self, engine: Engine) -> None:
        assert "area" in inspect(engine).get_table_names()

    def test_property_closure_table_exists(self, engine: Engine) -> None:
        assert "property_closure" in inspect(engine).get_table_names()

    def test_property_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("property")}
        # cd-8u5 extended the v1 slice's minimal set with the §04
        # columns the property domain service needs. Names with
        # ``NOT NULL`` carry server defaults so legacy rows survive
        # the migration without backfill.
        expected = {
            # v1 slice (cd-i6u).
            "id",
            "address",
            "timezone",
            "lat",
            "lon",
            "tags_json",
            "created_at",
            # cd-8u5 extension.
            "name",
            "kind",
            "address_json",
            "country",
            "locale",
            "default_currency",
            "client_org_id",
            "owner_user_id",
            "welcome_defaults_json",
            "property_notes_md",
            "updated_at",
            "deleted_at",
        }
        assert set(cols) == expected
        # Nullable columns per the model / migration.
        nullable = {
            "lat",
            "lon",
            "name",
            "locale",
            "default_currency",
            "client_org_id",
            "owner_user_id",
            "updated_at",
            "deleted_at",
        }
        for name in nullable:
            assert cols[name]["nullable"] is True, f"{name} must be nullable"
        for name in expected - nullable:
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"
        pk = inspect(engine).get_pk_constraint("property")
        assert pk["constrained_columns"] == ["id"]

    def test_property_workspace_composite_pk(self, engine: Engine) -> None:
        pk = inspect(engine).get_pk_constraint("property_workspace")
        assert pk["constrained_columns"] == ["property_id", "workspace_id"]

    def test_property_workspace_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("property_workspace")}
        expected = {
            "property_id",
            "workspace_id",
            "label",
            "membership_role",
            "created_at",
        }
        assert set(cols) == expected
        for name in expected:
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"

    def test_property_workspace_fks(self, engine: Engine) -> None:
        fks = {
            fk["referred_table"]: fk
            for fk in inspect(engine).get_foreign_keys("property_workspace")
        }
        # Hard deletes on property / workspace sweep the junction row.
        for table in ("property", "workspace"):
            assert table in fks, f"missing FK on {table}"
            assert fks[table]["options"].get("ondelete") == "CASCADE"

    def test_property_workspace_indexes(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("property_workspace")
        }
        assert "ix_property_workspace_workspace" in indexes
        assert indexes["ix_property_workspace_workspace"]["column_names"] == [
            "workspace_id"
        ]
        assert "ix_property_workspace_property" in indexes
        assert indexes["ix_property_workspace_property"]["column_names"] == [
            "property_id"
        ]

    def test_unit_fks(self, engine: Engine) -> None:
        fks = inspect(engine).get_foreign_keys("unit")
        assert len(fks) == 1
        assert fks[0]["referred_table"] == "property"
        assert fks[0]["options"].get("ondelete") == "CASCADE"

    def test_unit_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("unit")}
        assert "ix_unit_property" in indexes
        assert indexes["ix_unit_property"]["column_names"] == ["property_id"]

    def test_area_fks(self, engine: Engine) -> None:
        fks = inspect(engine).get_foreign_keys("area")
        assert len(fks) == 1
        assert fks[0]["referred_table"] == "property"
        assert fks[0]["options"].get("ondelete") == "CASCADE"

    def test_property_closure_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("property_closure")
        }
        assert fks[("property_id",)]["referred_table"] == "property"
        assert fks[("property_id",)]["options"].get("ondelete") == "CASCADE"
        # Audit actor survives a user-delete via SET NULL.
        assert fks[("created_by_user_id",)]["referred_table"] == "user"
        assert fks[("created_by_user_id",)]["options"].get("ondelete") == "SET NULL"

    def test_property_closure_index(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("property_closure")
        }
        assert "ix_property_closure_property_starts" in indexes
        assert indexes["ix_property_closure_property_starts"]["column_names"] == [
            "property_id",
            "starts_at",
        ]


class TestPropertyRoundTrip:
    """Insert a ``property`` row tenant-agnostically and read it back."""

    def test_round_trip(self, db_session: Session) -> None:
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPR")
        # ``property`` is NOT workspace-scoped — read back without any ctx.
        loaded = db_session.scalars(
            select(Property).where(Property.id == prop.id)
        ).one()
        assert loaded.address == "12 Chemin des Oliviers, Antibes"
        assert loaded.timezone == "Europe/Paris"
        assert loaded.lat is None
        assert loaded.lon is None
        assert loaded.tags_json == []


class TestMultiBelonging:
    """One property can belong to multiple workspaces via the junction.

    Two views are exercised:

    * **Physical**: the ``property_workspace`` table carries one row
      per ``(property_id, workspace_id)`` pair, so a single property
      can produce two rows in the DB without tripping the composite
      PK.
    * **Filter**: under a workspace-pinned
      :class:`WorkspaceContext`, a ``SELECT`` via the filter-enabled
      session returns only that workspace's junction row. The
      ``db_session`` fixture binds directly to a raw connection and
      bypasses the filter, so the per-workspace assertion uses
      ``filtered_factory`` explicitly.
    """

    def test_property_has_two_workspace_rows(self, db_session: Session) -> None:
        """Both rows persist at the DB level — multi-belonging is legal."""
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="multi-belong-phys@example.com",
            display_name="MultiBelongPhys",
            clock=clock,
        )
        ws_a = bootstrap_workspace(
            db_session,
            slug="multi-phys-a",
            name="MultiPhysA",
            owner_user_id=user.id,
            clock=clock,
        )
        ws_b = bootstrap_workspace(
            db_session,
            slug="multi-phys-b",
            name="MultiPhysB",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPM")

        # justification: direct junction writes straddle both workspaces;
        # a single ctx would block the other workspace's insert.
        with tenant_agnostic():
            db_session.add(
                PropertyWorkspace(
                    property_id=prop.id,
                    workspace_id=ws_a.id,
                    label="Villa Sud (agency A)",
                    membership_role="owner_workspace",
                    created_at=_PINNED,
                )
            )
            db_session.add(
                PropertyWorkspace(
                    property_id=prop.id,
                    workspace_id=ws_b.id,
                    label="Villa Sud (agency B)",
                    membership_role="managed_workspace",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

        # Both rows live at the DB level — the composite PK on
        # ``(property_id, workspace_id)`` allows the same property to
        # attach to several workspaces.
        # justification: the db_session fixture bypasses the filter, so
        # this cross-ctx read is already agnostic in practice; the
        # block documents that intent.
        with tenant_agnostic():
            rows = db_session.scalars(
                select(PropertyWorkspace).where(
                    PropertyWorkspace.property_id == prop.id
                )
            ).all()
        assert {r.workspace_id for r in rows} == {ws_a.id, ws_b.id}
        roles_by_ws = {r.workspace_id: r.membership_role for r in rows}
        assert roles_by_ws[ws_a.id] == "owner_workspace"
        assert roles_by_ws[ws_b.id] == "managed_workspace"

    def test_filter_scopes_junction_reads_per_workspace(
        self,
        db_session: Session,
    ) -> None:
        """Under a pinned ctx, a manual ``WHERE workspace_id`` picks one row.

        The tenant filter itself is already covered by
        :class:`TestTenantFilter` via ``filtered_factory``; this test
        exercises the multi-belonging query pattern under each
        workspace's ctx — the same shape a future adapter service
        will use — against the raw-connection ``db_session`` that
        bypasses auto-filtering. Pairing these covers both the
        "filter auto-injects" and "manual per-ctx read returns the
        expected row" paths.
        """
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="multi-belong-filter@example.com",
            display_name="MultiBelongFilter",
            clock=clock,
        )
        ws_a = bootstrap_workspace(
            db_session,
            slug="multi-filt-a",
            name="MultiFiltA",
            owner_user_id=user.id,
            clock=clock,
        )
        ws_b = bootstrap_workspace(
            db_session,
            slug="multi-filt-b",
            name="MultiFiltB",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPF")
        # justification: direct junction writes straddle both workspaces.
        with tenant_agnostic():
            db_session.add(
                PropertyWorkspace(
                    property_id=prop.id,
                    workspace_id=ws_a.id,
                    label="FiltA",
                    membership_role="owner_workspace",
                    created_at=_PINNED,
                )
            )
            db_session.add(
                PropertyWorkspace(
                    property_id=prop.id,
                    workspace_id=ws_b.id,
                    label="FiltB",
                    membership_role="managed_workspace",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

        # Under ws_a's ctx, manually filter on its workspace_id — this
        # models what a future adapter service will emit.
        token = set_current(_ctx_for(ws_a, user.id))
        try:
            rows = db_session.scalars(
                select(PropertyWorkspace).where(
                    PropertyWorkspace.property_id == prop.id,
                    PropertyWorkspace.workspace_id == ws_a.id,
                )
            ).all()
        finally:
            reset_current(token)
        assert len(rows) == 1
        assert rows[0].workspace_id == ws_a.id
        assert rows[0].membership_role == "owner_workspace"

        token = set_current(_ctx_for(ws_b, user.id))
        try:
            rows = db_session.scalars(
                select(PropertyWorkspace).where(
                    PropertyWorkspace.property_id == prop.id,
                    PropertyWorkspace.workspace_id == ws_b.id,
                )
            ).all()
        finally:
            reset_current(token)
        assert len(rows) == 1
        assert rows[0].workspace_id == ws_b.id
        assert rows[0].membership_role == "managed_workspace"


class TestMembershipRoleCheck:
    """``membership_role`` CHECK rejects values outside the v1 enum."""

    def test_bogus_role_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-membership@example.com",
            display_name="BogusMembership",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-membership-ws",
            name="BogusMembershipWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPX")

        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                PropertyWorkspace(
                    property_id=prop.id,
                    workspace_id=ws.id,
                    label="X",
                    membership_role="bogus",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_every_allowed_role_roundtrips(self, db_session: Session) -> None:
        """Each allowed ``membership_role`` persists.

        A single property can carry at most one row per workspace
        (composite PK), so we seed three workspaces and link each
        with a different ``membership_role``.
        """
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="every-membership@example.com",
            display_name="EveryMembership",
            clock=clock,
        )
        workspaces = [
            bootstrap_workspace(
                db_session,
                slug=f"every-mem-{idx}",
                name=f"EveryMem{idx}",
                owner_user_id=user.id,
                clock=clock,
            )
            for idx in range(3)
        ]
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPE")

        roles = ("owner_workspace", "managed_workspace", "observer_workspace")
        # justification: direct cross-tenant writes to the junction to
        # cover all three membership_role values in a single test.
        with tenant_agnostic():
            for ws, role in zip(workspaces, roles, strict=True):
                db_session.add(
                    PropertyWorkspace(
                        property_id=prop.id,
                        workspace_id=ws.id,
                        label=f"L-{role}",
                        membership_role=role,
                        created_at=_PINNED,
                    )
                )
            db_session.flush()


class TestUnitTypeCheck:
    """``unit.type`` CHECK rejects values outside the v1 enum."""

    def test_bogus_type_rejected(self, db_session: Session) -> None:
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPU")
        db_session.add(
            Unit(
                id="01HWA00000000000000000UNTB",
                property_id=prop.id,
                label="Main",
                type="spaceship",
                capacity=1,
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_every_allowed_type_roundtrips(self, db_session: Session) -> None:
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPY")
        allowed = ("apartment", "studio", "room", "bungalow", "villa", "other")
        for idx, kind in enumerate(allowed):
            db_session.add(
                Unit(
                    id=f"01HWA00000000000000000UN{idx:02d}",
                    property_id=prop.id,
                    label=f"Label {idx}",
                    type=kind,
                    capacity=idx + 1,
                    created_at=_PINNED,
                )
            )
        db_session.flush()


class TestPropertyClosureCheck:
    """``ends_after_starts`` CHECK rejects zero-or-negative windows."""

    def test_ends_equal_starts_rejected(self, db_session: Session) -> None:
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPZ")
        db_session.add(
            PropertyClosure(
                id="01HWA00000000000000000PCLZ",
                property_id=prop.id,
                starts_at=_PINNED,
                ends_at=_PINNED,
                reason="degenerate",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_ends_before_starts_rejected(self, db_session: Session) -> None:
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPW")
        db_session.add(
            PropertyClosure(
                id="01HWA00000000000000000PCLW",
                property_id=prop.id,
                starts_at=_PINNED,
                ends_at=_PINNED - timedelta(hours=1),
                reason="backwards",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_ends_after_starts_accepted(self, db_session: Session) -> None:
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPV")
        db_session.add(
            PropertyClosure(
                id="01HWA00000000000000000PCLV",
                property_id=prop.id,
                starts_at=_PINNED,
                ends_at=_PINNED + timedelta(days=3),
                reason="renovation",
                created_at=_PINNED,
            )
        )
        db_session.flush()


class TestCascadeOnPropertyDelete:
    """Hard-deleting a property sweeps unit / area / closure / junction."""

    def test_property_delete_cascades_all_children(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="cascade-prop@example.com",
            display_name="CascadeProp",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="cascade-prop-ws",
            name="CascadePropWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPC")

        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                PropertyWorkspace(
                    property_id=prop.id,
                    workspace_id=ws.id,
                    label="CascadeLink",
                    membership_role="owner_workspace",
                    created_at=_PINNED,
                )
            )
            db_session.add(
                Unit(
                    id="01HWA00000000000000000UNTC",
                    property_id=prop.id,
                    label="Main",
                    type="villa",
                    capacity=4,
                    created_at=_PINNED,
                )
            )
            db_session.add(
                Area(
                    id="01HWA00000000000000000ARAC",
                    property_id=prop.id,
                    label="Kitchen",
                    created_at=_PINNED,
                )
            )
            db_session.add(
                PropertyClosure(
                    id="01HWA00000000000000000PCLC",
                    property_id=prop.id,
                    starts_at=_PINNED,
                    ends_at=_PINNED + timedelta(days=1),
                    reason="renovation",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        # Delete the parent property. SQLAlchemy's ORM-level delete
        # issues an explicit DELETE, not the ON DELETE cascade; SQLite
        # does the cascade at the row level when foreign_keys pragma is
        # on (set by ``make_engine``), so the child rows go too.
        loaded = db_session.get(Property, prop.id)
        assert loaded is not None
        # justification: deleting property (tenant-agnostic) while the
        # current ctx is still pinned to ws would otherwise confuse the
        # filter when the rollback walks related rows.
        with tenant_agnostic():
            db_session.delete(loaded)
            db_session.flush()

        # Every child row that pointed at the property is gone.
        assert (
            db_session.scalars(select(Unit).where(Unit.property_id == prop.id)).all()
            == []
        )
        assert (
            db_session.scalars(select(Area).where(Area.property_id == prop.id)).all()
            == []
        )
        assert (
            db_session.scalars(
                select(PropertyClosure).where(PropertyClosure.property_id == prop.id)
            ).all()
            == []
        )
        token = set_current(_ctx_for(ws, user.id))
        try:
            assert (
                db_session.scalars(
                    select(PropertyWorkspace).where(
                        PropertyWorkspace.property_id == prop.id
                    )
                ).all()
                == []
            )
        finally:
            reset_current(token)


class TestCascadeOnWorkspaceDelete:
    """Hard-deleting a workspace sweeps its junction rows."""

    def test_workspace_delete_cascades_junction(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="cascade-ws-places@example.com",
            display_name="CascadeWSPlaces",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="cascade-ws-places",
            name="CascadeWSPlaces",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPK")

        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                PropertyWorkspace(
                    property_id=prop.id,
                    workspace_id=ws.id,
                    label="LinkK",
                    membership_role="owner_workspace",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        loaded_ws = db_session.get(Workspace, ws.id)
        assert loaded_ws is not None
        # justification: deleting the workspace row itself is inherently
        # cross-tenant — the caller is the platform operator, not a
        # user inside ws.
        with tenant_agnostic():
            db_session.delete(loaded_ws)
            db_session.flush()

        token = set_current(_ctx_for(ws, user.id))
        try:
            assert (
                db_session.scalars(
                    select(PropertyWorkspace).where(
                        PropertyWorkspace.property_id == prop.id
                    )
                ).all()
                == []
            )
        finally:
            reset_current(token)

        # The property itself survives the workspace-delete — it's
        # tenant-agnostic; only the junction row was swept.
        survivor = db_session.get(Property, prop.id)
        assert survivor is not None


class TestTenantFilter:
    """``property_workspace`` is scoped; ``property`` is agnostic."""

    def test_property_workspace_read_without_ctx_raises(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(PropertyWorkspace))
        assert exc.value.table == "property_workspace"

    def test_property_read_without_ctx_does_not_raise(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """``property`` is tenant-agnostic — bare SELECT must succeed."""
        with filtered_factory() as session:
            # No ctx, no tenant_agnostic() block — and no exception.
            result = session.scalars(select(Property)).all()
            # Result may be empty or not depending on fixture state; the
            # point is that the call reached the DB without raising.
            assert isinstance(result, list)

    def test_unit_read_without_ctx_does_not_raise(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """``unit`` is NOT registered in v1 — bare SELECT must succeed.

        Service-layer code joins through ``property_workspace`` to get
        tenant isolation; the filter doesn't enforce it at this layer.
        """
        with filtered_factory() as session:
            result = session.scalars(select(Unit)).all()
            assert isinstance(result, list)

    def test_area_read_without_ctx_does_not_raise(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """``area`` is NOT registered in v1 — bare SELECT must succeed."""
        with filtered_factory() as session:
            result = session.scalars(select(Area)).all()
            assert isinstance(result, list)

    def test_property_closure_read_without_ctx_does_not_raise(
        self, filtered_factory: sessionmaker[Session]
    ) -> None:
        """``property_closure`` is NOT registered in v1 — bare SELECT must succeed."""
        with filtered_factory() as session:
            result = session.scalars(select(PropertyClosure)).all()
            assert isinstance(result, list)
