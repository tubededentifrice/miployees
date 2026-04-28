"""Integration tests for :mod:`app.adapters.db.inventory` against a real DB.

Covers the post-migration schema shape (tables, unique composites,
FKs, CHECK constraints, indexes), the referential-integrity contract
on all three tables (``workspace_id`` CASCADE on all;
``item_id`` CASCADE on both child tables), happy-path round-trip of
every model (insert + select + update + delete), CHECK + UNIQUE
violations, signed-delta round-trip, ``Numeric(18, 4)`` precision
round-trip, cross-workspace isolation (SKU may repeat across
workspaces), CASCADE on workspace delete (sweeps the library) and on
item delete (sweeps movements + reorder rules), and tenant-filter
behaviour (all three tables scoped; SELECT without a
:class:`WorkspaceContext` raises :class:`TenantFilterMissing`).

The sibling ``tests/unit/test_db_inventory.py`` covers pure-Python
model construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"inventory_item",
§"inventory_movement", and ``docs/specs/08-inventory.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User
from app.adapters.db.inventory.models import Item, Movement, ReorderRule
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
_LATER = _PINNED + timedelta(hours=1)


_INVENTORY_TABLES: tuple[str, ...] = (
    "inventory_item",
    "inventory_movement",
    "inventory_reorder_rule",
)


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
def _ensure_inventory_registered() -> None:
    """Re-register the three inventory tables as workspace-scoped.

    ``app.adapters.db.inventory.__init__`` registers them at import
    time, but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite.
    """
    for table in _INVENTORY_TABLES:
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


def _seed_property_workspace(
    session: Session,
    *,
    workspace: Workspace,
    property_id: str,
    label: str = "Inventory property",
) -> str:
    with tenant_agnostic():
        session.add(
            Property(
                id=property_id,
                address=f"{label} address",
                timezone="UTC",
                tags_json=[],
                created_at=_PINNED,
            )
        )
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=workspace.id,
                label=label,
                membership_role="owner_workspace",
                status="active",
                created_at=_PINNED,
            )
        )
        session.flush()
    return property_id


class TestMigrationShape:
    """The migration lands all three tables with correct keys + indexes."""

    def test_all_tables_exist(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        for table in _INVENTORY_TABLES:
            assert table in tables, f"{table} missing from schema"

    def test_inventory_item_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("inventory_item")}
        expected = {
            "id",
            "workspace_id",
            "property_id",
            "sku",
            "name",
            "unit",
            "category",
            "barcode",
            "barcode_ean13",
            "current_qty",
            "min_qty",
            "reorder_target",
            "vendor",
            "vendor_url",
            "unit_cost_cents",
            "tags_json",
            "notes_md",
            "created_at",
            "updated_at",
            "deleted_at",
        }
        assert set(cols) == expected
        for nullable in (
            "property_id",
            "sku",
            "category",
            "barcode",
            "barcode_ean13",
            "min_qty",
            "reorder_target",
            "vendor",
            "vendor_url",
            "unit_cost_cents",
            "notes_md",
            "updated_at",
            "deleted_at",
        ):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"
        for notnull in expected - {
            "property_id",
            "sku",
            "category",
            "barcode",
            "barcode_ean13",
            "min_qty",
            "reorder_target",
            "vendor",
            "vendor_url",
            "unit_cost_cents",
            "notes_md",
            "updated_at",
            "deleted_at",
        }:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_inventory_item_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("inventory_item")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("property_id",)]["referred_table"] == "property"
        assert fks[("property_id",)]["options"].get("ondelete") == "CASCADE"

    def test_inventory_item_active_unique_indexes(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("inventory_item")
        }
        assert indexes["ix_inventory_item_workspace_property_deleted"][
            "column_names"
        ] == ["workspace_id", "property_id", "deleted_at"]
        assert indexes["uq_inventory_item_workspace_property_sku_active"][
            "column_names"
        ] == [
            "workspace_id",
            "property_id",
            "sku",
        ]
        assert indexes["uq_inventory_item_workspace_property_sku_active"]["unique"] == 1
        assert indexes["uq_inventory_item_workspace_property_barcode_active"][
            "column_names"
        ] == [
            "workspace_id",
            "property_id",
            "barcode_ean13",
        ]
        assert (
            indexes["uq_inventory_item_workspace_property_barcode_active"]["unique"]
            == 1
        )

    def test_inventory_movement_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("inventory_movement")}
        expected = {
            "id",
            "workspace_id",
            "item_id",
            "delta",
            "reason",
            "occurrence_id",
            "note_md",
            "created_at",
            "created_by",
        }
        assert set(cols) == expected
        for nullable in ("occurrence_id", "note_md", "created_by"):
            assert cols[nullable]["nullable"] is True, f"{nullable} must be nullable"
        for notnull in expected - {"occurrence_id", "note_md", "created_by"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_inventory_movement_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("inventory_movement")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("item_id",)]["referred_table"] == "inventory_item"
        # CASCADE — deleting an item drops its ledger.
        assert fks[("item_id",)]["options"].get("ondelete") == "CASCADE"
        # ``occurrence_id`` and ``created_by`` are soft-refs; no FK.
        assert ("occurrence_id",) not in fks
        assert ("created_by",) not in fks

    def test_inventory_movement_index(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("inventory_movement")
        }
        assert "ix_inventory_movement_workspace_item_created" in indexes
        assert indexes["ix_inventory_movement_workspace_item_created"][
            "column_names"
        ] == ["workspace_id", "item_id", "created_at"]

    def test_inventory_reorder_rule_columns(self, engine: Engine) -> None:
        cols = {
            c["name"]: c for c in inspect(engine).get_columns("inventory_reorder_rule")
        }
        expected = {
            "id",
            "workspace_id",
            "item_id",
            "reorder_at",
            "reorder_qty",
            "enabled",
        }
        assert set(cols) == expected
        for notnull in expected:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_inventory_reorder_rule_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("inventory_reorder_rule")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("item_id",)]["referred_table"] == "inventory_item"
        assert fks[("item_id",)]["options"].get("ondelete") == "CASCADE"

    def test_inventory_reorder_rule_unique_workspace_item(self, engine: Engine) -> None:
        uniques = {
            u["name"]: u
            for u in inspect(engine).get_unique_constraints("inventory_reorder_rule")
        }
        assert "uq_inventory_reorder_rule_workspace_item" in uniques
        assert uniques["uq_inventory_reorder_rule_workspace_item"]["column_names"] == [
            "workspace_id",
            "item_id",
        ]


class TestItemCrud:
    """Insert + select + update + delete round-trip on :class:`Item`."""

    def test_round_trip_with_numeric_precision(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="item-crud@example.com",
            display="ItemCrud",
            slug="item-crud-ws",
            name="ItemCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            # Fractional quantities exercise the ``Numeric(18, 4)``
            # precision — 4dp is essential for items sold by
            # weight / volume (0.25 kg, 1.500 l).
            item = Item(
                id="01HWA00000000000000000ITMA",
                workspace_id=workspace.id,
                sku="TP-2PLY-12",
                name="Toilet paper (2-ply, 12-pack)",
                unit="pkg",
                category="guest-amenity",
                barcode="3017620422003",
                current_qty=Decimal("14.2500"),
                min_qty=Decimal("2.0000"),
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()

            reloaded = db_session.get(Item, item.id)
            assert reloaded is not None
            assert isinstance(reloaded.current_qty, Decimal)
            # Numeric(18, 4) preserves the 4dp precision across a
            # flush + reload round-trip on both SQLite and Postgres.
            assert reloaded.current_qty == Decimal("14.2500")
            assert reloaded.min_qty == Decimal("2.0000")
            assert reloaded.category == "guest-amenity"
            assert reloaded.barcode == "3017620422003"
            assert reloaded.unit == "pkg"

            # Update: rename + change threshold.
            reloaded.name = "Toilet paper (2-ply, 12-pack, premium)"
            reloaded.min_qty = Decimal("3.0000")
            db_session.flush()
            db_session.expire_all()
            re_reloaded = db_session.get(Item, item.id)
            assert re_reloaded is not None
            assert re_reloaded.name == "Toilet paper (2-ply, 12-pack, premium)"
            assert re_reloaded.min_qty == Decimal("3.0000")

            db_session.delete(re_reloaded)
            db_session.flush()
            assert db_session.get(Item, item.id) is None
        finally:
            reset_current(token)

    def test_current_qty_default_zero(self, db_session: Session) -> None:
        """Newly-minted items default ``current_qty`` to 0."""
        workspace, user = _bootstrap(
            db_session,
            email="item-zero@example.com",
            display="ItemZero",
            slug="item-zero-ws",
            name="ItemZeroWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITMZ",
                workspace_id=workspace.id,
                sku="ZERO-1",
                name="Zero",
                unit="ea",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()
            db_session.expire_all()
            reloaded = db_session.get(Item, item.id)
            assert reloaded is not None
            assert reloaded.current_qty == Decimal("0")
        finally:
            reset_current(token)


class TestMovementCrud:
    """Insert + select + update + delete round-trip on :class:`Movement`."""

    def test_signed_delta_round_trip_and_ledger_lookup(
        self, db_session: Session
    ) -> None:
        """Key acceptance: delta may be negative; ledger orders newest first."""
        workspace, user = _bootstrap(
            db_session,
            email="mvmt-crud@example.com",
            display="MvmtCrud",
            slug="mvmt-crud-ws",
            name="MvmtCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITMM",
                workspace_id=workspace.id,
                sku="SOAP-1L",
                name="Soap 1L",
                unit="l",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()

            # Receive: positive delta.
            receive = Movement(
                id="01HWA00000000000000000MV01",
                workspace_id=workspace.id,
                item_id=item.id,
                delta=Decimal("12.0000"),
                reason="receive",
                note_md="Monthly restock",
                created_by=user.id,
                created_at=_PINNED,
            )
            # Consume: negative delta flowing from a task occurrence.
            consume = Movement(
                id="01HWA00000000000000000MV02",
                workspace_id=workspace.id,
                item_id=item.id,
                delta=Decimal("-0.7500"),
                reason="consume",
                occurrence_id="01HWA00000000000000000OCCA",
                created_by=user.id,
                created_at=_LATER,
            )
            db_session.add_all([receive, consume])
            db_session.flush()

            # Ledger lookup: newest first rides the composite index.
            rows = db_session.scalars(
                select(Movement)
                .where(Movement.workspace_id == workspace.id)
                .where(Movement.item_id == item.id)
                .order_by(Movement.created_at.desc())
            ).all()
            assert [r.id for r in rows] == [
                "01HWA00000000000000000MV02",
                "01HWA00000000000000000MV01",
            ]

            # Negative delta round-trips through the DB.
            reloaded = db_session.get(Movement, consume.id)
            assert reloaded is not None
            assert isinstance(reloaded.delta, Decimal)
            assert reloaded.delta == Decimal("-0.7500")
            assert reloaded.reason == "consume"
            assert reloaded.occurrence_id == "01HWA00000000000000000OCCA"

            # Update: the note is mutable (a manager can annotate).
            reloaded.note_md = "Cleaning pass for Apt 3B"
            db_session.flush()
            db_session.expire_all()
            re_reloaded = db_session.get(Movement, consume.id)
            assert re_reloaded is not None
            assert re_reloaded.note_md == "Cleaning pass for Apt 3B"
        finally:
            reset_current(token)


class TestReorderRuleCrud:
    """Insert + select + update + delete round-trip on :class:`ReorderRule`."""

    def test_round_trip_with_disable(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="reorder-crud@example.com",
            display="ReorderCrud",
            slug="reorder-crud-ws",
            name="ReorderCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITMR",
                workspace_id=workspace.id,
                sku="TP-1",
                name="Toilet paper",
                unit="pkg",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()

            rule = ReorderRule(
                id="01HWA00000000000000000RORA",
                workspace_id=workspace.id,
                item_id=item.id,
                reorder_at=Decimal("2.0000"),
                reorder_qty=Decimal("12.0000"),
                enabled=True,
            )
            db_session.add(rule)
            db_session.flush()

            reloaded = db_session.get(ReorderRule, rule.id)
            assert reloaded is not None
            assert isinstance(reloaded.reorder_at, Decimal)
            assert reloaded.reorder_at == Decimal("2.0000")
            assert reloaded.reorder_qty == Decimal("12.0000")
            assert reloaded.enabled is True

            # Disable the rule without deleting it — the kill switch.
            reloaded.enabled = False
            db_session.flush()
            db_session.expire_all()
            paused = db_session.get(ReorderRule, rule.id)
            assert paused is not None
            assert paused.enabled is False

            db_session.delete(paused)
            db_session.flush()
            assert db_session.get(ReorderRule, rule.id) is None
        finally:
            reset_current(token)


class TestCheckConstraints:
    """CHECK constraints reject values outside the v1 enums / bounds."""

    def test_unit_is_free_text(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="free-unit@example.com",
            display="FreeUnit",
            slug="free-unit-ws",
            name="FreeUnitWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITBX",
                workspace_id=workspace.id,
                sku="FREE-UNIT",
                name="Free unit",
                unit="operator carton",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()
            assert db_session.get(Item, item.id).unit == "operator carton"
        finally:
            reset_current(token)

    def test_bogus_reason_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-reason@example.com",
            display="BogusReason",
            slug="bogus-reason-ws",
            name="BogusReasonWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITBR",
                workspace_id=workspace.id,
                sku="REASON-1",
                name="Reason",
                unit="ea",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()
            db_session.add(
                Movement(
                    id="01HWA00000000000000000MVBR",
                    workspace_id=workspace.id,
                    item_id=item.id,
                    delta=Decimal("1"),
                    reason="restock",  # richer §02 enum, not in v1 slice
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_negative_reorder_at_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="neg-at@example.com",
            display="NegAt",
            slug="neg-at-ws",
            name="NegAtWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITNA",
                workspace_id=workspace.id,
                sku="NEG-AT",
                name="Neg at",
                unit="ea",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()
            db_session.add(
                ReorderRule(
                    id="01HWA00000000000000000RONA",
                    workspace_id=workspace.id,
                    item_id=item.id,
                    reorder_at=Decimal("-0.5"),
                    reorder_qty=Decimal("1"),
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_zero_reorder_qty_rejected(self, db_session: Session) -> None:
        """Strict > 0: a zero target is meaningless."""
        workspace, user = _bootstrap(
            db_session,
            email="zero-qty@example.com",
            display="ZeroQty",
            slug="zero-qty-ws",
            name="ZeroQtyWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITZQ",
                workspace_id=workspace.id,
                sku="ZERO-QTY",
                name="Zero qty",
                unit="ea",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()
            db_session.add(
                ReorderRule(
                    id="01HWA00000000000000000ROZQ",
                    workspace_id=workspace.id,
                    item_id=item.id,
                    reorder_at=Decimal("0"),
                    reorder_qty=Decimal("0"),
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_negative_reorder_qty_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="neg-qty@example.com",
            display="NegQty",
            slug="neg-qty-ws",
            name="NegQtyWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITNQ",
                workspace_id=workspace.id,
                sku="NEG-QTY",
                name="Neg qty",
                unit="ea",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()
            db_session.add(
                ReorderRule(
                    id="01HWA00000000000000000RONQ",
                    workspace_id=workspace.id,
                    item_id=item.id,
                    reorder_at=Decimal("0"),
                    reorder_qty=Decimal("-1"),
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestUniqueConstraints:
    """UNIQUE composites enforce the v1 invariants."""

    def test_duplicate_active_property_sku_rejected(
        self, db_session: Session
    ) -> None:
        """Key acceptance: a property cannot have two active items sharing a SKU."""
        workspace, user = _bootstrap(
            db_session,
            email="sku-dup@example.com",
            display="SkuDup",
            slug="sku-dup-ws",
            name="SkuDupWS",
        )
        property_id = _seed_property_workspace(
            db_session,
            workspace=workspace,
            property_id="01HWA00000000000000000PRDA",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Item(
                    id="01HWA00000000000000000ITDA",
                    workspace_id=workspace.id,
                    property_id=property_id,
                    sku="DUP-1",
                    name="Dup 1",
                    unit="ea",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            db_session.add(
                Item(
                    id="01HWA00000000000000000ITDB",
                    workspace_id=workspace.id,
                    property_id=property_id,
                    sku="DUP-1",  # same workspace, property, SKU
                    name="Dup 2",
                    unit="pkg",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_same_sku_different_properties_allowed(
        self, db_session: Session
    ) -> None:
        """Uniqueness is scoped to active rows per workspace and property."""
        workspace, user = _bootstrap(
            db_session,
            email="sku-prop@example.com",
            display="SkuProp",
            slug="sku-prop-ws",
            name="SkuPropWS",
        )
        property_a = _seed_property_workspace(
            db_session,
            workspace=workspace,
            property_id="01HWA00000000000000000PRXA",
            label="A",
        )
        property_b = _seed_property_workspace(
            db_session,
            workspace=workspace,
            property_id="01HWA00000000000000000PRXB",
            label="B",
        )

        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add_all(
                [
                    Item(
                        id="01HWA00000000000000000ITXA",
                        workspace_id=workspace.id,
                        property_id=property_a,
                        sku="TP-2PLY-12",
                        name="TP (A)",
                        unit="pkg",
                        created_at=_PINNED,
                    ),
                    Item(
                        id="01HWA00000000000000000ITXB",
                        workspace_id=workspace.id,
                        property_id=property_b,
                        sku="TP-2PLY-12",
                        name="TP (B)",
                        unit="pkg",
                        created_at=_PINNED,
                    ),
                ]
            )
            db_session.flush()

            rows = db_session.scalars(
                select(Item).where(Item.sku == "TP-2PLY-12")
            ).all()
            assert {r.property_id for r in rows} == {property_a, property_b}
        finally:
            reset_current(token)

    def test_same_sku_different_workspaces_allowed(self, db_session: Session) -> None:
        """Two workspaces may still share a SKU."""
        ws_a, user_a = _bootstrap(
            db_session,
            email="sku-iso-a@example.com",
            display="SkuIsoA",
            slug="sku-iso-a-ws",
            name="SkuIsoAWS",
        )
        ws_b, user_b = _bootstrap(
            db_session,
            email="sku-iso-b@example.com",
            display="SkuIsoB",
            slug="sku-iso-b-ws",
            name="SkuIsoBWS",
        )

        token = set_current(_ctx_for(ws_a, user_a.id))
        try:
            db_session.add(
                Item(
                    id="01HWA00000000000000000ITXA",
                    workspace_id=ws_a.id,
                    sku="TP-2PLY-12",
                    name="TP (A)",
                    unit="pkg",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        token = set_current(_ctx_for(ws_b, user_b.id))
        try:
            db_session.add(
                Item(
                    id="01HWA00000000000000000ITXB",
                    workspace_id=ws_b.id,
                    sku="TP-2PLY-12",  # same SKU, sibling workspace
                    name="TP (B)",
                    unit="pkg",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            rows = db_session.scalars(
                select(Item).where(Item.sku == "TP-2PLY-12")
            ).all()
            assert {r.workspace_id for r in rows} == {ws_a.id, ws_b.id}
        finally:
            reset_current(token)

    def test_duplicate_reorder_rule_per_item_rejected(
        self, db_session: Session
    ) -> None:
        """One reorder rule per (workspace, item) — the key acceptance."""
        workspace, user = _bootstrap(
            db_session,
            email="rule-dup@example.com",
            display="RuleDup",
            slug="rule-dup-ws",
            name="RuleDupWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITRD",
                workspace_id=workspace.id,
                sku="DUP-RULE",
                name="Dup rule",
                unit="ea",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()

            db_session.add(
                ReorderRule(
                    id="01HWA00000000000000000RORD1",
                    workspace_id=workspace.id,
                    item_id=item.id,
                    reorder_at=Decimal("2"),
                    reorder_qty=Decimal("10"),
                    enabled=True,
                )
            )
            db_session.flush()

            db_session.add(
                ReorderRule(
                    id="01HWA00000000000000000RORD2",
                    workspace_id=workspace.id,
                    item_id=item.id,  # duplicate (workspace, item)
                    reorder_at=Decimal("3"),
                    reorder_qty=Decimal("15"),
                    enabled=True,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestCrossWorkspaceIsolation:
    """A workspace's inventory does not leak to a sibling workspace."""

    def test_sibling_workspace_sees_own_rows_only(self, db_session: Session) -> None:
        ws_a, user_a = _bootstrap(
            db_session,
            email="xws-inv-a@example.com",
            display="XwsInvA",
            slug="xws-inv-a-ws",
            name="XwsInvAWS",
        )
        ws_b, user_b = _bootstrap(
            db_session,
            email="xws-inv-b@example.com",
            display="XwsInvB",
            slug="xws-inv-b-ws",
            name="XwsInvBWS",
        )

        token = set_current(_ctx_for(ws_a, user_a.id))
        try:
            db_session.add(
                Item(
                    id="01HWA00000000000000000ITXA",
                    workspace_id=ws_a.id,
                    sku="ONLY-A",
                    name="Only A",
                    unit="ea",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        token = set_current(_ctx_for(ws_b, user_b.id))
        try:
            db_session.add(
                Item(
                    id="01HWA00000000000000000ITXB",
                    workspace_id=ws_b.id,
                    sku="ONLY-B",
                    name="Only B",
                    unit="ea",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            b_only = db_session.scalars(
                select(Item).where(Item.workspace_id == ws_b.id)
            ).all()
            assert {r.sku for r in b_only} == {"ONLY-B"}

            a_only = db_session.scalars(
                select(Item).where(Item.workspace_id == ws_a.id)
            ).all()
            assert {r.sku for r in a_only} == {"ONLY-A"}
        finally:
            reset_current(token)


class TestCascadeOnItemDelete:
    """Deleting an :class:`Item` sweeps its movements + reorder rule."""

    def test_delete_item_cascades(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="cascade-item@example.com",
            display="CascadeItem",
            slug="cascade-item-ws",
            name="CascadeItemWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITCD",
                workspace_id=workspace.id,
                sku="CASCADE-ME",
                name="Cascade me",
                unit="ea",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()

            mv1 = Movement(
                id="01HWA00000000000000000MVC1",
                workspace_id=workspace.id,
                item_id=item.id,
                delta=Decimal("10"),
                reason="receive",
                created_at=_PINNED,
            )
            mv2 = Movement(
                id="01HWA00000000000000000MVC2",
                workspace_id=workspace.id,
                item_id=item.id,
                delta=Decimal("-1"),
                reason="consume",
                created_at=_LATER,
            )
            rule = ReorderRule(
                id="01HWA00000000000000000ROCD",
                workspace_id=workspace.id,
                item_id=item.id,
                reorder_at=Decimal("2"),
                reorder_qty=Decimal("12"),
                enabled=True,
            )
            db_session.add_all([mv1, mv2, rule])
            db_session.flush()

            mv_ids = [mv1.id, mv2.id]
            rule_id = rule.id
            db_session.delete(item)
            db_session.flush()
            # The cascade swept every child row at the DB level. The
            # ORM identity map still references the stale instances;
            # drop them before re-querying so ``get`` doesn't
            # refresh-raise, and observe absence via a fresh SELECT.
            db_session.expunge(mv1)
            db_session.expunge(mv2)
            db_session.expunge(rule)
            surviving_mvs = db_session.scalars(
                select(Movement).where(Movement.id.in_(mv_ids))
            ).all()
            assert surviving_mvs == []
            surviving_rule = db_session.scalars(
                select(ReorderRule).where(ReorderRule.id == rule_id)
            ).all()
            assert surviving_rule == []
            assert db_session.get(Item, item.id) is None
        finally:
            reset_current(token)


class TestCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps every inventory row belonging to it."""

    def test_delete_workspace_cascades(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="cascade-inv-ws@example.com",
            display="CascadeInvWs",
            slug="cascade-inv-ws-ws",
            name="CascadeInvWsWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            item = Item(
                id="01HWA00000000000000000ITWS",
                workspace_id=workspace.id,
                sku="WS-CASCADE",
                name="WS cascade",
                unit="ea",
                created_at=_PINNED,
            )
            db_session.add(item)
            db_session.flush()
            db_session.add(
                Movement(
                    id="01HWA00000000000000000MVWS",
                    workspace_id=workspace.id,
                    item_id=item.id,
                    delta=Decimal("1"),
                    reason="receive",
                    created_at=_PINNED,
                )
            )
            db_session.add(
                ReorderRule(
                    id="01HWA00000000000000000ROWS",
                    workspace_id=workspace.id,
                    item_id=item.id,
                    reorder_at=Decimal("0"),
                    reorder_qty=Decimal("1"),
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

        token = set_current(_ctx_for(workspace, user.id))
        try:
            assert (
                db_session.scalars(
                    select(Item).where(Item.workspace_id == workspace.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(Movement).where(Movement.workspace_id == workspace.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(ReorderRule).where(ReorderRule.workspace_id == workspace.id)
                ).all()
                == []
            )
        finally:
            reset_current(token)


class TestTenantFilter:
    """All three inventory tables are workspace-scoped under the filter."""

    @pytest.mark.parametrize("model", [Item, Movement, ReorderRule])
    def test_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[Item] | type[Movement] | type[ReorderRule],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__
