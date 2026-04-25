"""Item / Movement / ReorderRule SQLAlchemy models.

v1 slice per cd-bxt — sufficient for the ``inventory_item`` CRUD
follow-up (cd-jkwr) and the consume-on-task-completion worker (§08
§"Consumption on task completion") to layer business rules on top.
The richer §02 / §08 surface (``on_hand`` recomputation from the
movement ledger, ``deleted_at`` soft-delete, ``vendor`` /
``vendor_url`` / ``unit_cost_cents`` / ``tags`` / ``notes_md`` on
:class:`Item`, ``inventory_snapshot`` rollups, transfer correlation,
the wider reason enum, the wider unit vocabulary) lands with those
follow-ups without breaking this migration's public write contract.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. FK hygiene:

* ``workspace_id`` cascades on delete — sweeping a workspace sweeps
  its stock library, ledger, and reorder rules (the §15 tombstone /
  export worker snapshots first).
* ``item_id`` on :class:`Movement` and :class:`ReorderRule` cascades —
  deleting an :class:`Item` drops every movement and reorder rule
  referencing it in one go. The two child rows have no meaning
  independent of their parent: a ledger entry without an item is an
  orphan, and a reorder rule for a deleted item would silently
  resurrect the item's worker task on the next hourly
  ``check_reorder_points`` pass. The normal archive path (cd-jkwr)
  is a ``deleted_at`` soft-delete on the item row, not a hard DELETE.
* ``occurrence_id`` on :class:`Movement` is a plain :class:`str`
  soft-ref (no SQL foreign key). Spec §08 §"Consumption on task
  completion" names the consuming occurrence, but §06's occurrence
  identifier is still landing; keeping it as a soft-ref here lets
  the domain layer resolve it without pinning this migration to a
  cross-package shape that is still in motion. Same pattern as
  ``shift.property_id`` in :mod:`app.adapters.db.time`.
* ``created_by`` on :class:`Movement` is a plain :class:`str`
  soft-ref — the actor on a consume row may be a system process
  (the consume-on-task worker) rather than a user. Audit linkage
  lives in :mod:`app.adapters.db.audit`, not here.

Allowed enum values — the v1 slice matches cd-bxt's explicit
taxonomy:

* ``unit`` values: ``ea | l | kg | m | pkg | box | other``. Spec §08
  names ``each | pack | kg | liter | roll`` — the slightly different
  taxonomy here matches the task body and the mock surface. Widening
  or aligning on the spec's vocabulary is a one-line CHECK-list
  change in a later migration.
* ``reason`` values: ``receive | issue | adjust | consume``. Spec §02
  §"Enums" names a richer ``restock | consume | adjust | waste |
  transfer_in | transfer_out | audit_correction``; the narrower set
  here is what the v1 slice needs to record a single stock movement,
  and the widening lands with the transfer + waste + audit
  follow-ups.

Money, SKU, barcode columns stay plain :class:`str` / :class:`int` —
no catalog lookup in the v1 schema; the domain layer validates.

See ``docs/specs/02-domain-model.md`` §"inventory_item",
§"inventory_movement", and ``docs/specs/08-inventory.md``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``workspace.id`` FKs below
# resolve against ``Base.metadata`` only if ``workspace.models`` has
# been imported, so we register it here as a side effect.
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = ["Item", "Movement", "ReorderRule"]


# Allowed ``inventory_item.unit`` values — the v1 taxonomy matching
# cd-bxt's explicit scope. The richer §08 vocabulary (``each | pack
# | kg | liter | roll``) is a superset-adjacent variant; aligning
# the two is a later CHECK-list update without a data rewrite.
_UNIT_VALUES: tuple[str, ...] = (
    "ea",
    "l",
    "kg",
    "m",
    "pkg",
    "box",
    "other",
)

# Allowed ``inventory_movement.reason`` values — the v1 slice. The
# richer §02 / §08 enum (``restock | consume | adjust | waste |
# transfer_in | transfer_out | audit_correction``) lands with the
# transfer + waste + audit follow-ups; the narrower set here covers
# the minimum the consume-on-task worker (§08) + manual restock /
# issue / adjust UI need.
_REASON_VALUES: tuple[str, ...] = (
    "receive",
    "issue",
    "adjust",
    "consume",
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

    An item binds a ``(workspace, sku)`` pair to a human-friendly
    name, a unit-of-measure (enum), optional category and barcode,
    and two cached quantities: ``current_qty`` (the running total
    recomputed from the movement ledger, defaulted to 0) and
    ``min_qty`` (the reorder threshold, nullable — NULL means "no
    low-stock alert for this item"). ``created_at`` anchors the
    audit timeline.

    UNIQUE ``(workspace_id, sku)`` enforces cd-bxt's acceptance
    criterion: a workspace cannot have two items sharing a SKU. The
    richer §08 surface (``vendor`` / ``vendor_url`` /
    ``unit_cost_cents`` / ``tags`` / ``notes_md`` / ``deleted_at``
    soft-delete) lands with cd-jkwr and later follow-ups; the v1
    slice is the minimum the consume-on-task worker (§08) needs to
    resolve a movement against a live item row.
    """

    __tablename__ = "inventory_item"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Free-form stock-keeping unit. The composite UNIQUE
    # ``(workspace_id, sku)`` enforces the one-sku-per-workspace
    # invariant at the DB.
    sku: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # The CHECK enum clamps the v1 unit taxonomy at the DB layer;
    # the domain layer validates against the same list before the
    # write reaches here. Widening to §08's vocabulary lands in a
    # later migration.
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
    # Cached running total — recomputed from the :class:`Movement`
    # ledger in the domain layer, stored here so the "low stock"
    # report can scan items without scanning the ledger. Default 0
    # because a freshly-minted item has no movements yet.
    # ``Numeric(18, 4)`` is spec-portable: SQLite stores it as TEXT,
    # Postgres as NUMERIC(18, 4); both preserve the 4-dp precision
    # needed for fractional units (0.25 kg, 1.500 l).
    current_qty: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    # Reorder threshold — the periodic ``check_reorder_points``
    # worker (§08 §"Reorder logic") ensures an open restock task
    # exists when ``current_qty <= min_qty``. NULL means "no
    # low-stock alert for this item" — an optional flag rather than
    # the zero-default used for ``current_qty``.
    min_qty: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"unit IN ({_in_clause(_UNIT_VALUES)})",
            name="unit",
        ),
        # Per-acceptance: a workspace cannot mint two items sharing
        # a SKU. The composite UNIQUE also powers the "fetch by sku"
        # lookup the §08 barcode UI runs on every scan.
        UniqueConstraint(
            "workspace_id",
            "sku",
            name="uq_inventory_item_workspace_sku",
        ),
    )


class Movement(Base):
    """An append-only ledger row recording one stock change.

    A movement carries a signed :class:`Decimal` ``delta``
    (``receive`` rows are positive, ``issue`` / ``consume`` negative,
    ``adjust`` either sign), a reason enum pinning the cause, an
    optional occurrence pointer (when the movement flows from a
    completed §06 task), an optional markdown note, and the usual
    audit pair (``created_by`` / ``created_at``).

    The ``(workspace_id, item_id, created_at)`` index powers the
    "ledger for this item, newest first" lookup the item-detail
    screen runs on every open (§08 §"Reports"). Leading
    ``workspace_id`` lets the tenant filter ride the same B-tree;
    ``item_id`` carries the equality filter; ``created_at`` carries
    the ORDER BY DESC.

    The narrower v1 reason enum (``receive | issue | adjust |
    consume``) widens to §02 / §08's full vocabulary with the
    transfer + waste + audit follow-ups — see the module docstring.
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
    # Signed decimal — positive for stock gain (``receive``),
    # negative for loss (``issue`` / ``consume``), either sign for
    # ``adjust``. No CHECK on the sign; the domain layer enforces
    # the per-reason sign rules.
    delta: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    # Soft-ref :class:`str` — see the module docstring. NULL when
    # the movement is not tied to a task occurrence (manual restock,
    # a standalone adjust row).
    occurrence_id: Mapped[str | None] = mapped_column(String, nullable=True)
    note_md: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Soft-ref :class:`str` — see the module docstring. NULL when
    # the movement is written by a system process (the
    # consume-on-task worker has no user id to pin).
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"reason IN ({_in_clause(_REASON_VALUES)})",
            name="reason",
        ),
        # Per-acceptance: "ledger for this item, newest first" rides
        # the composite B-tree. Leading ``workspace_id`` lets the
        # tenant filter's equality predicate ride the same index.
        Index(
            "ix_inventory_movement_workspace_item_created",
            "workspace_id",
            "item_id",
            "created_at",
        ),
    )


class ReorderRule(Base):
    """The reorder-threshold rule for a single item.

    A rule binds a ``(workspace, item)`` pair to a ``reorder_at``
    threshold (the ``current_qty`` level at or below which the
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
    # Threshold: open a restock task when ``item.current_qty <=
    # reorder_at``. Non-negative (a negative threshold is a data
    # bug — the domain would never fire).
    reorder_at: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    # Target quantity to bring the item back up to — not a delta,
    # but a level. Strictly positive (ordering zero units is
    # meaningless).
    reorder_qty: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
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
