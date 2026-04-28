"""Inventory item CRUD service."""

from __future__ import annotations

from builtins import list as _list
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.inventory.models import Item
from app.adapters.db.places.models import PropertyWorkspace
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "InventoryItemConflict",
    "InventoryItemCreate",
    "InventoryItemNotFound",
    "InventoryItemUpdate",
    "InventoryItemValidationError",
    "InventoryItemView",
    "InventoryPropertyNotFound",
    "archive",
    "create",
    "get_by_barcode",
    "get_by_sku",
    "list",
    "restore",
    "update",
]


_ITEM_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "sku",
        "unit",
        "reorder_point",
        "reorder_target",
        "vendor",
        "vendor_url",
        "unit_cost_cents",
        "barcode_ean13",
        "tags",
        "notes_md",
    }
)
_QTY_QUANTUM = Decimal("0.0001")


class InventoryItemNotFound(LookupError):
    """No inventory item matched the workspace/property/id filter."""


class InventoryPropertyNotFound(LookupError):
    """The property is not active in the caller's workspace."""


class InventoryItemConflict(ValueError):
    """An active item already owns the submitted SKU or barcode."""

    __slots__ = ("field",)

    def __init__(self, field: Literal["sku", "barcode_ean13"]) -> None:
        super().__init__(f"active inventory item already uses {field}")
        self.field = field


class InventoryItemValidationError(ValueError):
    """Submitted item data failed service-level validation."""

    __slots__ = ("error", "field")

    def __init__(self, field: str, error: str) -> None:
        super().__init__(f"{field}: {error}")
        self.field = field
        self.error = error


@dataclass(frozen=True, slots=True)
class InventoryItemCreate:
    name: str
    unit: str
    sku: str | None = None
    reorder_point: Decimal | None = None
    reorder_target: Decimal | None = None
    vendor: str | None = None
    vendor_url: str | None = None
    unit_cost_cents: int | None = None
    barcode_ean13: str | None = None
    tags: tuple[str, ...] = ()
    notes_md: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryItemUpdate:
    fields_set: frozenset[str] = field(default_factory=frozenset)
    name: str | None = None
    sku: str | None = None
    unit: str | None = None
    reorder_point: Decimal | None = None
    reorder_target: Decimal | None = None
    vendor: str | None = None
    vendor_url: str | None = None
    unit_cost_cents: int | None = None
    barcode_ean13: str | None = None
    tags: tuple[str, ...] = ()
    notes_md: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryItemView:
    id: str
    workspace_id: str
    property_id: str
    name: str
    sku: str | None
    unit: str
    on_hand: Decimal
    reorder_point: Decimal | None
    reorder_target: Decimal | None
    vendor: str | None
    vendor_url: str | None
    unit_cost_cents: int | None
    barcode_ean13: str | None
    tags: tuple[str, ...]
    notes_md: str | None
    created_at: datetime
    updated_at: datetime | None
    deleted_at: datetime | None


def create(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    body: InventoryItemCreate,
    clock: Clock | None = None,
) -> InventoryItemView:
    """Create an active item scoped to ``(workspace, property)``."""
    _ensure_property(session, ctx, property_id)
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    sku = _clean_optional(body.sku)
    barcode = _clean_optional(body.barcode_ean13)
    _ensure_unique(session, ctx, property_id=property_id, sku=sku, barcode=barcode)

    row = Item(
        id=new_ulid(clock=clock),
        workspace_id=ctx.workspace_id,
        property_id=property_id,
        sku=sku,
        name=_clean_required(body.name, field_name="name"),
        unit=_clean_required(body.unit, field_name="unit"),
        current_qty=Decimal("0"),
        min_qty=_clean_quantity(body.reorder_point, field_name="reorder_point"),
        reorder_target=_clean_quantity(
            body.reorder_target, field_name="reorder_target"
        ),
        vendor=_clean_optional(body.vendor),
        vendor_url=_clean_optional(body.vendor_url),
        unit_cost_cents=body.unit_cost_cents,
        barcode=barcode,
        barcode_ean13=barcode,
        tags_json=_clean_tags(body.tags),
        notes_md=_clean_optional(body.notes_md),
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict_from_integrity_error(exc) from exc

    write_audit(
        session,
        ctx,
        entity_kind="inventory_item",
        entity_id=row.id,
        action="inventory_item.created",
        diff={"after": _audit_dict(row)},
        clock=resolved_clock,
    )
    return _project(row)


def update(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    item_id: str,
    body: InventoryItemUpdate,
    clock: Clock | None = None,
) -> InventoryItemView:
    """Patch an active item."""
    unknown = body.fields_set - _ITEM_FIELDS
    if unknown:
        raise ValueError(f"unknown inventory item fields: {sorted(unknown)!r}")

    row = _get_active_row(session, ctx, property_id=property_id, item_id=item_id)
    before = _audit_dict(row)

    next_sku = row.sku
    next_barcode = row.barcode_ean13
    if "sku" in body.fields_set:
        next_sku = _clean_optional(body.sku)
    if "barcode_ean13" in body.fields_set:
        next_barcode = _clean_optional(body.barcode_ean13)
    _ensure_unique(
        session,
        ctx,
        property_id=property_id,
        sku=next_sku,
        barcode=next_barcode,
        excluding_item_id=row.id,
    )

    if "name" in body.fields_set:
        row.name = _clean_required(body.name, field_name="name")
    if "sku" in body.fields_set:
        row.sku = next_sku
    if "unit" in body.fields_set:
        row.unit = _clean_required(body.unit, field_name="unit")
    if "reorder_point" in body.fields_set:
        row.min_qty = _clean_quantity(body.reorder_point, field_name="reorder_point")
    if "reorder_target" in body.fields_set:
        row.reorder_target = _clean_quantity(
            body.reorder_target, field_name="reorder_target"
        )
    if "vendor" in body.fields_set:
        row.vendor = _clean_optional(body.vendor)
    if "vendor_url" in body.fields_set:
        row.vendor_url = _clean_optional(body.vendor_url)
    if "unit_cost_cents" in body.fields_set:
        row.unit_cost_cents = body.unit_cost_cents
    if "barcode_ean13" in body.fields_set:
        row.barcode = next_barcode
        row.barcode_ean13 = next_barcode
    if "tags" in body.fields_set:
        row.tags_json = _clean_tags(body.tags)
    if "notes_md" in body.fields_set:
        row.notes_md = _clean_optional(body.notes_md)

    after = _audit_dict(row)
    if before == after:
        return _project(row)

    resolved_clock = clock if clock is not None else SystemClock()
    row.updated_at = resolved_clock.now()
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict_from_integrity_error(exc) from exc

    write_audit(
        session,
        ctx,
        entity_kind="inventory_item",
        entity_id=row.id,
        action="inventory_item.updated",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    return _project(row)


def archive(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    item_id: str,
    clock: Clock | None = None,
) -> InventoryItemView:
    """Soft-delete an item; repeated calls are no-ops."""
    row = _get_row(session, ctx, property_id=property_id, item_id=item_id)
    if row.deleted_at is not None:
        return _project(row)

    resolved_clock = clock if clock is not None else SystemClock()
    before = _audit_dict(row)
    now = resolved_clock.now()
    row.deleted_at = now
    row.updated_at = now
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="inventory_item",
        entity_id=row.id,
        action="inventory_item.archived",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    return _project(row)


def restore(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    item_id: str,
    clock: Clock | None = None,
) -> InventoryItemView:
    """Restore a soft-deleted item; repeated calls are no-ops."""
    row = _get_row(session, ctx, property_id=property_id, item_id=item_id)
    if row.deleted_at is None:
        return _project(row)

    _ensure_unique(
        session,
        ctx,
        property_id=property_id,
        sku=row.sku,
        barcode=row.barcode_ean13,
        excluding_item_id=row.id,
    )
    resolved_clock = clock if clock is not None else SystemClock()
    before = _audit_dict(row)
    now = resolved_clock.now()
    row.deleted_at = None
    row.updated_at = now
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _conflict_from_integrity_error(exc) from exc
    write_audit(
        session,
        ctx,
        entity_kind="inventory_item",
        entity_id=row.id,
        action="inventory_item.restored",
        diff={"before": before, "after": _audit_dict(row)},
        clock=resolved_clock,
    )
    return _project(row)


def list(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    include_archived: bool = False,
) -> tuple[InventoryItemView, ...]:
    """List items for a property in name order."""
    _ensure_property(session, ctx, property_id)
    stmt = select(Item).where(
        Item.workspace_id == ctx.workspace_id,
        Item.property_id == property_id,
    )
    if not include_archived:
        stmt = stmt.where(Item.deleted_at.is_(None))
    stmt = stmt.order_by(Item.name, Item.id)
    return tuple(_project(row) for row in session.scalars(stmt).all())


def get_by_barcode(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    barcode_ean13: str,
) -> InventoryItemView:
    """Return one active item by barcode or raise not found."""
    _ensure_property(session, ctx, property_id)
    barcode = _clean_required(barcode_ean13, field_name="barcode_ean13")
    row = session.scalar(
        select(Item).where(
            Item.workspace_id == ctx.workspace_id,
            Item.property_id == property_id,
            Item.barcode_ean13 == barcode,
            Item.deleted_at.is_(None),
        )
    )
    if row is None:
        raise InventoryItemNotFound("inventory item barcode not found")
    return _project(row)


def get_by_sku(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    sku: str,
) -> InventoryItemView:
    """Return one active item by SKU or raise not found."""
    _ensure_property(session, ctx, property_id)
    clean_sku = _clean_required(sku, field_name="sku")
    row = session.scalar(
        select(Item).where(
            Item.workspace_id == ctx.workspace_id,
            Item.property_id == property_id,
            Item.sku == clean_sku,
            Item.deleted_at.is_(None),
        )
    )
    if row is None:
        raise InventoryItemNotFound("inventory item sku not found")
    return _project(row)


def _ensure_property(session: Session, ctx: WorkspaceContext, property_id: str) -> None:
    row = session.scalar(
        select(PropertyWorkspace).where(
            PropertyWorkspace.workspace_id == ctx.workspace_id,
            PropertyWorkspace.property_id == property_id,
            PropertyWorkspace.status == "active",
        )
    )
    if row is None:
        raise InventoryPropertyNotFound("property not found in workspace")


def _get_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    item_id: str,
) -> Item:
    row = session.scalar(
        select(Item).where(
            Item.workspace_id == ctx.workspace_id,
            Item.property_id == property_id,
            Item.id == item_id,
        )
    )
    if row is None:
        raise InventoryItemNotFound("inventory item not found")
    return row


def _get_active_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    item_id: str,
) -> Item:
    row = session.scalar(
        select(Item).where(
            Item.workspace_id == ctx.workspace_id,
            Item.property_id == property_id,
            Item.id == item_id,
            Item.deleted_at.is_(None),
        )
    )
    if row is None:
        raise InventoryItemNotFound("active inventory item not found")
    return row


def _ensure_unique(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    sku: str | None,
    barcode: str | None,
    excluding_item_id: str | None = None,
) -> None:
    if sku is not None:
        stmt = select(Item.id).where(
            Item.workspace_id == ctx.workspace_id,
            Item.property_id == property_id,
            Item.sku == sku,
            Item.deleted_at.is_(None),
        )
        if excluding_item_id is not None:
            stmt = stmt.where(Item.id != excluding_item_id)
        if session.scalar(stmt) is not None:
            raise InventoryItemConflict("sku")

    if barcode is not None:
        stmt = select(Item.id).where(
            Item.workspace_id == ctx.workspace_id,
            Item.property_id == property_id,
            Item.barcode_ean13 == barcode,
            Item.deleted_at.is_(None),
        )
        if excluding_item_id is not None:
            stmt = stmt.where(Item.id != excluding_item_id)
        if session.scalar(stmt) is not None:
            raise InventoryItemConflict("barcode_ean13")


def _conflict_from_integrity_error(exc: IntegrityError) -> InventoryItemConflict:
    message = str(exc.orig).lower()
    if "barcode" in message:
        return InventoryItemConflict("barcode_ean13")
    return InventoryItemConflict("sku")


def _clean_required(value: str | None, *, field_name: str) -> str:
    if value is None:
        raise InventoryItemValidationError(field_name, "required")
    cleaned = value.strip()
    if not cleaned:
        raise InventoryItemValidationError(field_name, "blank")
    return cleaned


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_tags(tags: tuple[str, ...]) -> list[str]:
    return [tag.strip() for tag in tags if tag.strip()]


def _clean_quantity(value: Decimal | None, *, field_name: str) -> Decimal | None:
    if value is None:
        return None
    quantized = value.quantize(_QTY_QUANTUM)
    if value != quantized:
        raise InventoryItemValidationError(field_name, "quantity_precision")
    return quantized


def _project(row: Item) -> InventoryItemView:
    if row.property_id is None:
        raise InventoryItemNotFound("inventory item has no property scope")
    return InventoryItemView(
        id=row.id,
        workspace_id=row.workspace_id,
        property_id=row.property_id,
        name=row.name,
        sku=row.sku,
        unit=row.unit,
        on_hand=row.current_qty,
        reorder_point=row.min_qty,
        reorder_target=row.reorder_target,
        vendor=row.vendor,
        vendor_url=row.vendor_url,
        unit_cost_cents=row.unit_cost_cents,
        barcode_ean13=row.barcode_ean13,
        tags=tuple(row.tags_json or []),
        notes_md=row.notes_md,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _audit_dict(row: Item) -> dict[str, object]:
    return {
        "id": row.id,
        "property_id": row.property_id,
        "name": row.name,
        "sku": row.sku,
        "unit": row.unit,
        "on_hand": str(row.current_qty),
        "reorder_point": str(row.min_qty) if row.min_qty is not None else None,
        "reorder_target": (
            str(row.reorder_target) if row.reorder_target is not None else None
        ),
        "vendor": row.vendor,
        "vendor_url": row.vendor_url,
        "unit_cost_cents": row.unit_cost_cents,
        "barcode_ean13": row.barcode_ean13,
        "tags": _list(row.tags_json or []),
        "notes_md": row.notes_md,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
    }
