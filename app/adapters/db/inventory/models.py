"""Inventory item, movement, stocktake, and reorder-rule models.

The inventory context stores a property-scoped SKU catalog and an
append-only movement ledger per §08. Quantities use
``Numeric(14, 4, asdecimal=True)`` for the practical decimal
precision contract, item units are operator-authored free text, and
movement reasons use the final stock-change taxonomy:
``restock | consume | produce | waste | theft | loss | found |
returned_to_vendor | transfer_in | transfer_out | audit_correction |
adjust``.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. FK hygiene:

* ``workspace_id`` cascades on delete — sweeping a workspace sweeps
  its stock library, ledger, and reorder rules (the §15 tombstone /
  export worker snapshots first).
* ``item_id`` on :class:`Movement` and :class:`ReorderRule` cascades -
  deleting an :class:`Item` drops every movement and reorder rule
  referencing it in one go. The two child rows have no meaning
  independent of their parent: a ledger entry without an item is an
  orphan, and a reorder rule for a deleted item would silently
  resurrect the item's worker task on the next hourly
  ``check_reorder_points`` pass. The normal archive path (cd-jkwr)
  is a ``deleted_at`` soft-delete on the item row, not a hard DELETE.
* ``source_task_id`` and ``source_stocktake_id`` on :class:`Movement`
  are nullable FKs to the task row and property-wide reconciliation
  session that caused the movement. Routine manual rows leave both
  NULL.
* ``actor_id`` on :class:`Movement` and :class:`Stocktake` points to
  ``user.id`` with ``SET NULL`` so history survives user deletion;
  ``actor_kind`` distinguishes human, agent, and system authors.

Money, SKU, barcode columns stay plain :class:`str` / :class:`int` —
no catalog lookup in the v1 schema; the domain layer validates.

See ``docs/specs/02-domain-model.md`` §"inventory_item",
§"inventory_movement", and ``docs/specs/08-inventory.md``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``workspace.id`` FKs below
# resolve against ``Base.metadata`` only if ``workspace.models`` has
# been imported, so we register it here as a side effect.
from app.adapters.db.identity import models as _identity_models  # noqa: F401
from app.adapters.db.places import models as _places_models  # noqa: F401
from app.adapters.db.tasks import models as _tasks_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = ["Item", "Movement", "ReorderRule", "Stocktake"]


_REASON_VALUES: tuple[str, ...] = (
    "restock",
    "consume",
    "produce",
    "waste",
    "theft",
    "loss",
    "found",
    "returned_to_vendor",
    "transfer_in",
    "transfer_out",
    "audit_correction",
    "adjust",
)
_MOVEMENT_ACTOR_KIND_VALUES: tuple[str, ...] = ("user", "agent", "system")
_STOCKTAKE_ACTOR_KIND_VALUES: tuple[str, ...] = ("user", "agent")
_MOVEMENT_REASON_ENUM = Enum(
    *_REASON_VALUES,
    name="inventory_movement_reason",
    native_enum=True,
    create_constraint=True,
)


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Mirrors the helper in sibling ``time`` / ``payroll`` / ``tasks`` /
    ``stays`` / ``places`` / ``instructions`` modules so the enum
    CHECK constraints below stay readable.
    """
    return "'" + "', '".join(values) + "'"


class Item(Base):
    """A stock-keeping unit tracked by the workspace.

    An item binds an optional property-scoped SKU to a human-friendly
    name, a free-text unit-of-measure, optional category / tags /
    barcode / vendor metadata, cached quantities, and soft-delete
    timestamps.

    Partial unique indexes enforce that active rows cannot share SKU
    or barcode inside one ``(workspace_id, property_id)`` scope.
    """

    __tablename__ = "inventory_item"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Free-form stock-keeping unit. cd-jkwr scopes uniqueness to active
    # rows per (workspace, property); SKU itself remains optional.
    sku: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # §08 makes unit operator-authored free text. There is no CHECK
    # constraint; UI pickers may suggest common values but must allow
    # custom entries.
    unit: Mapped[str] = mapped_column(String, nullable=False)
    # Optional grouping label (``cleaning``, ``guest-amenity``, …).
    # A richer ``tags`` array lands with the §08 follow-up; until
    # then the single ``category`` column covers the "group items by
    # one label" UI case.
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    # Optional EAN-13 (or similar) barcode for the scanner UI (§08
    # §"Barcode scanning"). Free-form string — validated length /
    # checksum at the domain layer.
    barcode: Mapped[str | None] = mapped_column(String, nullable=True)
    barcode_ean13: Mapped[str | None] = mapped_column(String, nullable=True)
    # Cached running total — recomputed from the :class:`Movement`
    # ledger in the domain layer, stored here so the "low stock"
    # report can scan items without scanning the ledger. Default 0
    # because a freshly-minted item has no movements yet.
    on_hand: Mapped[Decimal] = mapped_column(
        Numeric(14, 4, asdecimal=True), nullable=False, default=Decimal("0")
    )
    # Reorder threshold — the periodic ``check_reorder_points``
    # worker (§08 §"Reorder logic") ensures an open restock task exists
    # when ``on_hand <= reorder_point``. NULL means "no
    # low-stock alert for this item" — an optional flag rather than
    # the zero-default used for ``on_hand``.
    reorder_point: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4, asdecimal=True), nullable=True
    )
    reorder_target: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4, asdecimal=True), nullable=True
    )
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    vendor_url: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_cost_cents: Mapped[int | None] = mapped_column(nullable=True)
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    notes_md: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "ix_inventory_item_workspace_property_deleted",
            "workspace_id",
            "property_id",
            "deleted_at",
        ),
        Index(
            "uq_inventory_item_workspace_property_sku_active",
            "workspace_id",
            "property_id",
            "sku",
            unique=True,
            sqlite_where=text("deleted_at IS NULL AND sku IS NOT NULL"),
            postgresql_where=text("deleted_at IS NULL AND sku IS NOT NULL"),
        ),
        Index(
            "uq_inventory_item_workspace_property_barcode_active",
            "workspace_id",
            "property_id",
            "barcode_ean13",
            unique=True,
            sqlite_where=text("deleted_at IS NULL AND barcode_ean13 IS NOT NULL"),
            postgresql_where=text("deleted_at IS NULL AND barcode_ean13 IS NOT NULL"),
        ),
    )

    @property
    def current_qty(self) -> Decimal:
        """Backward-compatible Python alias for service code still being renamed."""
        return self.on_hand

    @current_qty.setter
    def current_qty(self, value: Decimal) -> None:
        self.on_hand = value

    @property
    def min_qty(self) -> Decimal | None:
        """Backward-compatible Python alias for service code still being renamed."""
        return self.reorder_point

    @min_qty.setter
    def min_qty(self, value: Decimal | None) -> None:
        self.reorder_point = value


class Stocktake(Base):
    """A property-wide inventory reconciliation session."""

    __tablename__ = "inventory_stocktake"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actor_kind: Mapped[str] = mapped_column(String, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    note_md: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"actor_kind IN ({_in_clause(_STOCKTAKE_ACTOR_KIND_VALUES)})",
            name="actor_kind",
        ),
        Index(
            "ix_inventory_stocktake_workspace_property_started",
            "workspace_id",
            "property_id",
            text("started_at DESC"),
        ),
    )


class Movement(Base):
    """An append-only ledger row recording one stock change.

    A movement carries a signed :class:`Decimal` ``delta`` and a reason
    enum pinning the cause. Task-driven consumption/production rows set
    ``source_task_id``; stocktake reconciliation rows set
    ``source_stocktake_id``.

    The ``(workspace_id, item_id, at)`` index powers the
    "ledger for this item, newest first" lookup the item-detail
    screen runs on every open (§08 §"Reports"). Leading
    ``workspace_id`` lets the tenant filter ride the same B-tree;
    ``item_id`` carries the equality filter; ``at`` carries
    the ORDER BY DESC.
    """

    __tablename__ = "inventory_movement"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CASCADE — deleting an item drops every movement row. The
    # normal archive path is a ``deleted_at`` soft-delete (cd-jkwr);
    # hard DELETE is a conscious platform-level op.
    item_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("inventory_item.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Signed decimal — positive for stock gain, negative for stock
    # loss, either sign for audit/manual correction. No CHECK on the
    # sign; the domain layer enforces per-reason sign rules.
    delta: Mapped[Decimal] = mapped_column(
        Numeric(14, 4, asdecimal=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(_MOVEMENT_REASON_ENUM, nullable=False)
    source_task_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("occurrence.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_stocktake_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("inventory_stocktake.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_kind: Mapped[str] = mapped_column(String, nullable=False, default="system")
    actor_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"actor_kind IN ({_in_clause(_MOVEMENT_ACTOR_KIND_VALUES)})",
            name="actor_kind",
        ),
        # Per-acceptance: "ledger for this item, newest first" rides
        # the composite B-tree. Leading ``workspace_id`` lets the
        # tenant filter's equality predicate ride the same index.
        Index(
            "ix_inventory_movement_workspace_item_at",
            "workspace_id",
            "item_id",
            "at",
        ),
    )

    @property
    def occurrence_id(self) -> str | None:
        """Backward-compatible Python alias for task-source rows."""
        return self.source_task_id

    @occurrence_id.setter
    def occurrence_id(self, value: str | None) -> None:
        self.source_task_id = value

    @property
    def created_by(self) -> str | None:
        """Backward-compatible Python alias for actor id."""
        return self.actor_id

    @created_by.setter
    def created_by(self, value: str | None) -> None:
        self.actor_id = value

    @property
    def created_at(self) -> datetime:
        """Backward-compatible Python alias for movement timestamp."""
        return self.at

    @created_at.setter
    def created_at(self, value: datetime) -> None:
        self.at = value

    @property
    def note_md(self) -> str | None:
        """Backward-compatible Python alias for the movement note."""
        return self.note

    @note_md.setter
    def note_md(self, value: str | None) -> None:
        self.note = value


class ReorderRule(Base):
    """The reorder-threshold rule for a single item.

    A rule binds a ``(workspace, item)`` pair to a ``reorder_at``
    threshold (the ``on_hand`` level at or below which the
    periodic ``check_reorder_points`` worker opens a restock task)
    and a ``reorder_qty`` target (the quantity the restock task
    should bring the item back up to). An ``enabled`` kill switch
    lets a manager pause the rule without deleting it.

    UNIQUE ``(workspace_id, item_id)`` enforces cd-bxt's acceptance
    criterion: a workspace cannot have two reorder rules for the
    same item. CHECK ``reorder_at >= 0`` guards against the
    off-by-one bug of writing a negative threshold; CHECK
    ``reorder_qty > 0`` blocks the nonsense rule that would order
    zero units.
    """

    __tablename__ = "inventory_reorder_rule"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CASCADE — deleting an item drops its reorder rule. Keeping a
    # rule for a deleted item would silently resurrect the item's
    # worker task on the next hourly pass.
    item_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("inventory_item.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Threshold: open a restock task when ``item.on_hand <=
    # reorder_at``. Non-negative (a negative threshold is a data
    # bug — the domain would never fire).
    reorder_at: Mapped[Decimal] = mapped_column(
        Numeric(14, 4, asdecimal=True), nullable=False
    )
    # Target quantity to bring the item back up to — not a delta,
    # but a level. Strictly positive (ordering zero units is
    # meaningless).
    reorder_qty: Mapped[Decimal] = mapped_column(
        Numeric(14, 4, asdecimal=True), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("reorder_at >= 0", name="reorder_at_nonneg"),
        CheckConstraint("reorder_qty > 0", name="reorder_qty_positive"),
        # Per-acceptance: one rule per (workspace, item). The
        # composite UNIQUE also powers the "fetch rule for this
        # item" lookup the worker runs on every hourly pass.
        UniqueConstraint(
            "workspace_id",
            "item_id",
            name="uq_inventory_reorder_rule_workspace_item",
        ),
    )
