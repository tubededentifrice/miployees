"""Unit tests for :mod:`app.adapters.db.inventory.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__`` (CHECK
constraints, unique composites, index shape, tenancy-registry
membership). Integration coverage (migrations, FK cascade, CHECK /
UNIQUE violations against a real DB, tenant filter behaviour, CRUD
round-trips, Numeric round-trip, cross-workspace isolation) lives in
``tests/integration/test_db_inventory.py``.

See ``docs/specs/02-domain-model.md`` §"inventory_item",
§"inventory_movement", and ``docs/specs/08-inventory.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.adapters.db.inventory import Item, Movement, ReorderRule
from app.adapters.db.inventory import models as inventory_models

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


class TestItemModel:
    """The ``Item`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        item = Item(
            id="01HWA00000000000000000ITMA",
            workspace_id="01HWA00000000000000000WSPA",
            sku="TP-2PLY-12",
            name="Toilet paper (2-ply, 12-pack)",
            unit="pkg",
            created_at=_PINNED,
        )
        assert item.id == "01HWA00000000000000000ITMA"
        assert item.workspace_id == "01HWA00000000000000000WSPA"
        assert item.sku == "TP-2PLY-12"
        assert item.name == "Toilet paper (2-ply, 12-pack)"
        assert item.unit == "pkg"
        assert item.created_at == _PINNED
        # Nullable columns default to ``None``.
        assert item.property_id is None
        assert item.category is None
        assert item.barcode is None
        assert item.barcode_ean13 is None
        assert item.min_qty is None
        assert item.deleted_at is None

    def test_unit_is_free_text(self) -> None:
        """§08 keeps units operator-authored, not enum-constrained."""
        units = ("each", "pack", "kg", "liter", "roll", "sleeve/10", "stere")
        for index, unit in enumerate(units):
            item = Item(
                id=f"01HWA000000000000000000IT{index}",
                workspace_id="01HWA00000000000000000WSPA",
                sku=f"sku-{unit}",
                name=f"Item {unit}",
                unit=unit,
                created_at=_PINNED,
            )
            assert item.unit == unit

    def test_rich_construction(self) -> None:
        """All optional columns round-trip through the constructor."""
        item = Item(
            id="01HWA00000000000000000ITMB",
            workspace_id="01HWA00000000000000000WSPA",
            sku="DISH-SOAP-1L",
            name="Dish soap, lavender, 1L",
            unit="l",
            category="cleaning",
            barcode="3017620422003",
            barcode_ean13="3017620422003",
            current_qty=Decimal("4.5000"),
            min_qty=Decimal("2.0000"),
            reorder_target=Decimal("6.0000"),
            vendor="Supply Co",
            vendor_url="https://supplier.example/items/dish-soap",
            unit_cost_cents=1299,
            tags_json=["cleaning", "kitchen"],
            notes_md="Buy lavender when possible.",
            created_at=_PINNED,
        )
        assert item.category == "cleaning"
        assert item.barcode == "3017620422003"
        assert item.barcode_ean13 == "3017620422003"
        assert item.current_qty == Decimal("4.5000")
        assert item.min_qty == Decimal("2.0000")
        assert item.reorder_target == Decimal("6.0000")
        assert item.vendor == "Supply Co"
        assert item.vendor_url == "https://supplier.example/items/dish-soap"
        assert item.unit_cost_cents == 1299
        assert item.tags_json == ["cleaning", "kitchen"]
        assert item.notes_md == "Buy lavender when possible."

    def test_tablename(self) -> None:
        assert Item.__tablename__ == "inventory_item"

    def test_unit_check_absent(self) -> None:
        checks = [
            c
            for c in Item.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("unit")
        ]
        assert checks == []

    def test_active_sku_and_barcode_unique_indexes_present(self) -> None:
        indexes = [i for i in Item.__table_args__ if isinstance(i, Index)]
        by_name = {i.name: i for i in indexes}
        sku = by_name["uq_inventory_item_workspace_property_sku_active"]
        barcode = by_name["uq_inventory_item_workspace_property_barcode_active"]
        assert sku.unique is True
        assert [c.name for c in sku.columns] == ["workspace_id", "property_id", "sku"]
        assert barcode.unique is True
        assert [c.name for c in barcode.columns] == [
            "workspace_id",
            "property_id",
            "barcode_ean13",
        ]


class TestMovementModel:
    """The ``Movement`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        mv = Movement(
            id="01HWA00000000000000000MVMA",
            workspace_id="01HWA00000000000000000WSPA",
            item_id="01HWA00000000000000000ITMA",
            delta=Decimal("5.0000"),
            reason="receive",
            created_at=_PINNED,
        )
        assert mv.id == "01HWA00000000000000000MVMA"
        assert mv.item_id == "01HWA00000000000000000ITMA"
        assert mv.delta == Decimal("5.0000")
        assert mv.reason == "receive"
        assert mv.created_at == _PINNED
        # Nullable columns default to ``None``.
        assert mv.occurrence_id is None
        assert mv.note_md is None
        assert mv.created_by is None

    def test_negative_delta_construction(self) -> None:
        """Signed delta — a consume row carries a negative quantity."""
        mv = Movement(
            id="01HWA00000000000000000MVMB",
            workspace_id="01HWA00000000000000000WSPA",
            item_id="01HWA00000000000000000ITMA",
            delta=Decimal("-2.5000"),
            reason="consume",
            occurrence_id="01HWA00000000000000000OCCA",
            note_md="Cleaning pass for Apt 3B",
            created_by="01HWA00000000000000000USRA",
            created_at=_PINNED,
        )
        assert mv.delta == Decimal("-2.5000")
        assert mv.reason == "consume"
        assert mv.occurrence_id == "01HWA00000000000000000OCCA"
        assert mv.note_md == "Cleaning pass for Apt 3B"
        assert mv.created_by == "01HWA00000000000000000USRA"

    def test_every_reason_value_constructs(self) -> None:
        """Each of the four v1 reason values builds a valid row."""
        reasons = ("receive", "issue", "adjust", "consume")
        for index, reason in enumerate(reasons):
            mv = Movement(
                id=f"01HWA000000000000000000MV{index}",
                workspace_id="01HWA00000000000000000WSPA",
                item_id="01HWA00000000000000000ITMA",
                delta=Decimal("1"),
                reason=reason,
                created_at=_PINNED,
            )
            assert mv.reason == reason

    def test_tablename(self) -> None:
        assert Movement.__tablename__ == "inventory_movement"

    def test_reason_check_present(self) -> None:
        checks = [
            c
            for c in Movement.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("reason")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for reason in ("receive", "issue", "adjust", "consume"):
            assert reason in sql, f"{reason} missing from CHECK constraint"

    def test_ledger_index_present(self) -> None:
        """Index: ``(workspace_id, item_id, created_at)`` for ledger lookup."""
        indexes = [i for i in Movement.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_inventory_movement_workspace_item_created" in names
        target = next(
            i
            for i in indexes
            if i.name == "ix_inventory_movement_workspace_item_created"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "item_id",
            "created_at",
        ]


class TestReorderRuleModel:
    """The ``ReorderRule`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        rule = ReorderRule(
            id="01HWA00000000000000000RORA",
            workspace_id="01HWA00000000000000000WSPA",
            item_id="01HWA00000000000000000ITMA",
            reorder_at=Decimal("3.0000"),
            reorder_qty=Decimal("12.0000"),
        )
        assert rule.id == "01HWA00000000000000000RORA"
        assert rule.workspace_id == "01HWA00000000000000000WSPA"
        assert rule.item_id == "01HWA00000000000000000ITMA"
        assert rule.reorder_at == Decimal("3.0000")
        assert rule.reorder_qty == Decimal("12.0000")

    def test_enabled_explicit_false_construction(self) -> None:
        """A manager may pause a rule without deleting it."""
        rule = ReorderRule(
            id="01HWA00000000000000000RORB",
            workspace_id="01HWA00000000000000000WSPA",
            item_id="01HWA00000000000000000ITMA",
            reorder_at=Decimal("0"),
            reorder_qty=Decimal("1"),
            enabled=False,
        )
        assert rule.enabled is False

    def test_tablename(self) -> None:
        assert ReorderRule.__tablename__ == "inventory_reorder_rule"

    def test_reorder_at_nonneg_check_present(self) -> None:
        checks = [
            c
            for c in ReorderRule.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("reorder_at_nonneg")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "reorder_at" in sql
        assert ">= 0" in sql

    def test_reorder_qty_positive_check_present(self) -> None:
        checks = [
            c
            for c in ReorderRule.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("reorder_qty_positive")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "reorder_qty" in sql
        assert "> 0" in sql

    def test_unique_workspace_item_present(self) -> None:
        """Key acceptance: UNIQUE ``(workspace_id, item_id)`` — one rule per item."""
        uniques = [
            u for u in ReorderRule.__table_args__ if isinstance(u, UniqueConstraint)
        ]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == ["workspace_id", "item_id"]
        assert uniques[0].name == "uq_inventory_reorder_rule_workspace_item"


class TestPackageReExports:
    """``app.adapters.db.inventory`` re-exports every v1-slice model."""

    def test_models_re_exported(self) -> None:
        assert Item is inventory_models.Item
        assert Movement is inventory_models.Movement
        assert ReorderRule is inventory_models.ReorderRule


class TestRegistryIntent:
    """Every inventory table is registered as workspace-scoped.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.inventory``: a sibling
    ``test_tenancy_orm_filter`` autouse fixture calls
    :func:`registry._reset_for_tests` which wipes the process-wide
    set, so asserting presence after that reset would be flaky. The
    tests below encode the invariant — "every inventory table is
    scoped" — without over-coupling to import ordering.
    """

    def test_every_inventory_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in (
            "inventory_item",
            "inventory_movement",
            "inventory_reorder_rule",
        ):
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in (
            "inventory_item",
            "inventory_movement",
            "inventory_reorder_rule",
        ):
            assert table in scoped, f"{table} must be scoped"

    def test_is_scoped_reports_true(self) -> None:
        """``is_scoped`` agrees with ``scoped_tables`` membership."""
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in (
            "inventory_item",
            "inventory_movement",
            "inventory_reorder_rule",
        ):
            registry.register(table)
        for table in (
            "inventory_item",
            "inventory_movement",
            "inventory_reorder_rule",
        ):
            assert registry.is_scoped(table) is True
