"""ExpenseClaim / ExpenseLine / ExpenseAttachment SQLAlchemy models.

v1 slice per cd-lbn — sufficient for the claim CRUD (cd-7rfu) and the
``expenses.autofill`` LLM capability (§11) follow-ups to layer
business rules on top. The richer §09 surface (payout-destination
snapshots computed at approval, ``exchange_rate`` cross-rate cache,
``expense_line.asset_id`` link into §21 asset TCO, ``pages`` per
attachment, ``edited_by_user`` provenance bit, ``autofill_confidence_overall``
derivation) lands with those follow-ups without breaking this
migration's public write contract.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. FK hygiene mirrors
the rest of the app:

* ``workspace_id`` cascades on delete — sweeping a workspace sweeps
  its expense history (the §15 tombstone / export worker snapshots
  first).
* ``work_engagement_id`` on :class:`ExpenseClaim` uses ``RESTRICT`` —
  a claim is the payroll-law evidence for a reimbursement (§09
  §"Expense claims", §15 §"Right to erasure"); archiving the
  engagement must not silently drop claim history. The normal
  archive path is ``work_engagement.archived_on``, not a hard
  DELETE.
* ``claim_id`` on :class:`ExpenseLine` and :class:`ExpenseAttachment`
  cascades — deleting a claim drops every line and attachment in
  lock-step. Lines and attachments have no meaning independent of
  their parent: an orphan line is a free-floating number with no
  currency context, an orphan attachment is a dangling blob ref.
  Domain-layer deletion of a ``draft`` claim is the normal path;
  ``submitted``-or-later rows should never be hard-deleted (the
  audit trail in :mod:`app.adapters.db.audit` carries the write
  history).
* ``property_id`` stays a plain :class:`str` (soft-ref) — matches
  the sibling ``shift.property_id`` / ``movement.occurrence_id``
  rationale in :mod:`app.adapters.db.time` and
  :mod:`app.adapters.db.inventory`. The §05 /
  ``property_workspace`` intersection owns when that becomes a
  hard FK.
* ``decided_by`` is a plain :class:`str` soft-ref — matches the
  sibling ``leave.decided_by`` / ``shift.approved_by`` pattern. The
  approver may be a system process (an agent capability) rather
  than a user; audit linkage lives in :mod:`app.adapters.db.audit`.
* ``owed_destination_id`` / ``reimbursement_destination_id`` are
  plain :class:`str` soft-refs — the ``payout_destination`` table
  does not exist yet (see §09 §"Payout destinations"). Follow-up
  will promote to FKs once the parent table lands, matching the
  ``work_engagement.pay_destination_id`` / ``reimbursement_destination_id``
  convention.
* ``blob_hash`` on :class:`ExpenseAttachment` is a plain
  :class:`str` soft-ref into blob storage (content-addressed hash).
  §09 §"Model" calls this column ``file_id`` — a hard FK to the
  shared ``file`` table — but that table in §02 §"Shared tables"
  has not landed yet, so the v1 slice lands the same
  ``blob_hash`` convention as the sibling
  :class:`app.adapters.db.tasks.models.Evidence` and
  ``payslip.pdf_blob_hash`` columns (column rename + FK promotion
  land together in cd-48c1). The same hash can be referenced by
  multiple rows (same receipt re-used across claims), so keeping
  it blob-ref only preserves the content-addressed storage layer's
  dedup.

Allowed enum values — the v1 slice matches the §02 §"Enums" and §09
§"Model" taxonomy:

* ``expense_claim.state`` — ``draft | submitted | approved | rejected
  | reimbursed`` (§02 ``expense_state``). The domain-layer state
  machine gates transitions (draft → submitted → approved →
  reimbursed, or draft → submitted → rejected); this enum only
  clamps legal values at the DB.
* ``expense_claim.category`` — ``supplies | fuel | food | transport
  | maintenance | other`` (§09 §"Model"). The UI surfaces the
  taxonomy verbatim; widening is a one-line CHECK-list change in a
  later migration.
* ``expense_line.source`` — ``ocr | manual`` (§02
  ``expense_line_source``). A line starts as ``ocr`` when the
  ``expenses.autofill`` capability produced it and stays that way
  even after the worker edits fields — see §09 §"LLM accuracy".
  The ``edited_by_user`` provenance bit lands with cd-7rfu.
* ``expense_attachment.kind`` — ``receipt | invoice | other`` (§09
  §"Model"). The ``receipt`` kind is the OCR-target; the richer
  §02 ``asset_document_kind`` taxonomy (which adds ``manual``,
  ``warranty``, etc.) is asset-scoped and does not apply here.

**Deviation from cd-lbn's prose.** The task body describes a two-
table shape (``Expense`` + ``Receipt``). §02 §"Core entities" and
§09 §"Model" record a three-table shape (``expense_claim``,
``expense_line``, ``expense_attachment``) with per-line itemisation
and richer approval / reimbursement snapshots. The spec is
authoritative (per :file:`.claude/agents/coder.md`), so this module
lands the three-table shape and drops two cd-lbn-prose fields that
disagree with it:

* ``reimbursed_via`` (``cash | bank | card | other``) — §09
  replaces this with ``reimbursement_destination_id`` (soft-ref to
  ``payout_destination``) plus the ``owed_destination_id`` snapshot
  pair captured at approval. The payment-channel taxonomy lives on
  the destination row, not the claim.
* ``Receipt.ocr_json`` / ``ocr_confidence`` — §09 records
  ``llm_autofill_json`` on :class:`ExpenseClaim` (claim-scoped, one
  payload per autofill run covering every page), not per-attachment.
  The ``autofill_confidence_overall`` number lives next to it.

See ``docs/specs/02-domain-model.md`` §"Core entities (by
document)" (§09 row), §"Money", §"Enums"; and
``docs/specs/09-time-payroll-expenses.md`` §"Expense claims".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``workspace.id`` /
# ``work_engagement.id`` FKs below resolve against ``Base.metadata``
# only if ``workspace.models`` has been imported, so we register it
# here as a side effect.
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = ["ExpenseAttachment", "ExpenseClaim", "ExpenseLine"]


# Allowed ``expense_claim.state`` values — the §02 ``expense_state``
# enum. Transitions (draft → submitted → approved → reimbursed, or
# draft → submitted → rejected) are enforced in the domain layer;
# this enum only clamps legal values at the DB.
_STATE_VALUES: tuple[str, ...] = (
    "draft",
    "submitted",
    "approved",
    "rejected",
    "reimbursed",
)

# Allowed ``expense_claim.category`` values — §09 §"Model".
_CATEGORY_VALUES: tuple[str, ...] = (
    "supplies",
    "fuel",
    "food",
    "transport",
    "maintenance",
    "other",
)

# Allowed ``expense_line.source`` values — the §02 ``expense_line_source``
# enum. A line stays ``ocr`` after a user edit (§09 §"LLM accuracy");
# the separate ``edited_by_user`` bit carries the provenance change
# and lands with cd-7rfu.
_SOURCE_VALUES: tuple[str, ...] = ("ocr", "manual")

# Allowed ``expense_attachment.kind`` values — §09 §"Model". The
# richer §02 ``asset_document_kind`` taxonomy is asset-scoped and
# does not apply to expense attachments.
_ATTACHMENT_KIND_VALUES: tuple[str, ...] = ("receipt", "invoice", "other")

# Allowed ``expense_claim.reimbursed_via`` values — added in cd-9guk
# to capture the channel the manager actually used when settling a
# claim (cash hand-off, bank transfer out of band, company-card top-
# up, or a misc "other" bucket for edge cases). The §09 §"Reimbursement"
# spec routes the canonical reimbursement through the payout-period
# rollup, but the manager flow needs an explicit "I paid this now,
# how" signal so a one-off cash hand-off, an early bank transfer
# before period close, or a company-card pre-load is captured for
# the audit + payslip narrative without standing up the full
# ``payout_destination`` table (still deferred — see the module
# docstring's "deviation from cd-lbn's prose" note). Mirrors the
# ``_STATE_VALUES`` shape so the CHECK clause stays uniform.
_REIMBURSED_VIA_VALUES: tuple[str, ...] = ("cash", "bank", "card", "other")


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Mirrors the helper in sibling ``time`` / ``payroll`` / ``tasks`` /
    ``stays`` / ``places`` / ``inventory`` / ``instructions`` modules
    so the enum CHECK constraints below stay readable.
    """
    return "'" + "', '".join(values) + "'"


class ExpenseClaim(Base):
    """One reimbursement request filed by a worker against an engagement.

    A claim binds a ``(workspace, work_engagement)`` pair to a
    purchase event: the total amount paid in the claim's currency,
    the vendor, the purchase wall-clock, an optional property
    pointer, the OCR-autofill payload from the
    ``expenses.autofill`` capability, a state enum driving the
    submit → approve → reimburse lifecycle, and the approval /
    reimbursement snapshots captured at each transition.

    The v1 slice carries the minimum the CRUD follow-up (cd-7rfu)
    needs to let a worker submit a claim and a manager approve or
    reject it. The richer §09 §"Amount owed to the employee"
    snapshot (``exchange_rate_to_default`` to the workspace default
    currency, computed at approval) lands with the exchange-rate
    service; we land the columns here so the snapshot path has
    somewhere to write, but the domain layer is the sole writer.

    The ``(workspace_id, state)`` index powers the manager's "claims
    awaiting approval" inbox; the ``(workspace_id,
    work_engagement_id, submitted_at)`` index powers the worker's
    "my claims, newest first" view. Leading ``workspace_id`` lets
    the tenant filter ride the same B-tree.
    """

    __tablename__ = "expense_claim"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``RESTRICT`` — see the module docstring. A claim is the
    # payroll-law evidence for a reimbursement (§09); archiving the
    # engagement must not silently drop claim history. The normal
    # archive path is ``work_engagement.archived_on``.
    work_engagement_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("work_engagement.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # ``NULL`` while the claim is still ``draft`` — the worker hasn't
    # submitted yet. Set to the server-side submit wall-clock on the
    # draft → submitted transition; immutable from then on.
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    vendor: Mapped[str] = mapped_column(String, nullable=False)
    # ``DateTime`` rather than ``Date``: §09 §"Submission flow" notes
    # "date + approximate time if legible"; a datetime column holds
    # both shapes at the cost of one minor units of storage. The
    # domain layer is free to strip the time portion for display.
    purchased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # ISO-4217 currency code — any code valid per §02 §"Multi-currency
    # expenses". Stored as a 3-char text with a CHECK on length; the
    # domain layer validates against a known-codes set before write.
    currency: Mapped[str] = mapped_column(String, nullable=False)
    # Integer cents (§02 §"Money"). ``BigInteger`` because BHD / JOD
    # are 3-dp minor units and a single high-value claim (workspace
    # buyout, bulk equipment order) can exceed INT32 in a 3-dp
    # currency.
    total_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # §09 §"Amount owed to the employee" snapshot — snapped at the
    # approval transition. ``Numeric(18, 8)`` because cross-currency
    # rates computed via EUR pivot carry rounding precision past the
    # two-decimal minor-unit precision (e.g. USD→BHD via EUR yields
    # rates with 6-8 significant decimal digits). Nullable because
    # the claim is still draft / submitted / rejected.
    exchange_rate_to_default: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    # Snapshot of the ``payout_destination`` id active at the
    # approval moment — soft-ref to the future ``payout_destination``
    # table. Immutable once set (§09 §"Currency alignment rule").
    owed_destination_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Copy of ``payout_destination.currency`` at the approval moment
    # — the authoritative *payment* currency the employee will
    # actually see land in their account.
    owed_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    # ``total_amount_cents`` converted from ``currency`` to
    # ``owed_currency`` using the snapped rate; minor units of
    # ``owed_currency``. ``BigInteger`` per §02 §"Money".
    owed_amount_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Cross-rate ``claim.currency → owed_currency``, derived via EUR
    # pivot from the workspace-default rate pair.
    owed_exchange_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    # ``ecb | manual | stale_carryover`` — copied from the underlying
    # ``exchange_rate`` row at the approval moment. Enum values are
    # domain-layer validated (the ``exchange_rate`` table is still
    # landing in a later migration); no CHECK here yet.
    owed_rate_source: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    # Soft-ref :class:`str` — see the module docstring.
    property_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Empty-string default keeps the column NOT NULL without forcing
    # every seeder / API caller to thread ``note_md=""`` through —
    # mirrors ``work_engagement.notes_md`` (cd-4saj).
    note_md: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    # Full JSON payload returned by the ``expenses.autofill`` LLM
    # capability (§11) — shape defined in §09 §"LLM accuracy &
    # guardrails". ``Mapped[Any]`` is the documented exception for
    # SQLAlchemy JSON columns (see sibling :mod:`app.adapters.db.audit`
    # / :mod:`workspace`). Nullable because manual-entry claims (no
    # autofill run) have no payload. Default ``None`` rather than
    # ``{}`` so the "was autofill ever tried?" check is a single
    # ``IS NULL`` predicate.
    llm_autofill_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    # Derived ``min()`` of per-field confidences in
    # ``llm_autofill_json``. ``Numeric(3, 2)`` matches the 0..1
    # range with two-decimal precision (the per-field scores the LLM
    # emits are already quantised to that precision).
    autofill_confidence_overall: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 2), nullable=True
    )
    state: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    # Soft-ref :class:`str` — see the module docstring.
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Markdown reason attached by the decider. NULL until a decision
    # is recorded; empty-string default would hide the "decided but
    # reason omitted" case.
    decision_note_md: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft-ref :class:`str` — per-claim reimbursement destination
    # override. NULL falls back to the engagement's
    # ``reimbursement_destination_id`` at payout time (§09 §"Per-claim
    # override").
    reimbursement_destination_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    # cd-9guk reimbursement snapshot — populated when the manager
    # marks the claim ``reimbursed``. NULL while the claim is still
    # ``draft`` / ``submitted`` / ``approved`` / ``rejected``. The
    # three columns move together: ``reimbursed_at`` is the wall-clock
    # of the transition, ``reimbursed_via`` is the payment channel
    # (CHECK-clamped to the v1 enum), and ``reimbursed_by`` is a
    # soft-ref to the user who actioned the settlement. ``decided_by``
    # already records the *approver* — ``reimbursed_by`` may differ
    # (a different manager, the operator running treasury, an admin
    # cleaning up after period close), so the two columns must not be
    # collapsed.
    reimbursed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reimbursed_via: Mapped[str | None] = mapped_column(String, nullable=True)
    reimbursed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # NULL = live; set to the wall-clock of a soft-delete. The
    # domain layer enforces the "only ``draft`` claims can be
    # soft-deleted" rule; later states are immutable audit records.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"state IN ({_in_clause(_STATE_VALUES)})",
            name="state",
        ),
        CheckConstraint(
            f"category IN ({_in_clause(_CATEGORY_VALUES)})",
            name="category",
        ),
        # ``reimbursed_via`` is nullable until the claim transitions
        # to ``reimbursed``; once populated it must match the v1 enum.
        # Mirrors the ``state`` / ``category`` CHECK pattern.
        CheckConstraint(
            "reimbursed_via IS NULL "
            f"OR reimbursed_via IN ({_in_clause(_REIMBURSED_VIA_VALUES)})",
            name="reimbursed_via",
        ),
        # ISO-4217 codes are exactly 3 characters — cheapest portable
        # guard; the domain layer validates against a known-codes set.
        CheckConstraint("LENGTH(currency) = 3", name="currency_length"),
        # ``owed_currency`` is nullable but a populated value must be
        # a 3-char ISO code too. Guard both halves of the invariant.
        CheckConstraint(
            "owed_currency IS NULL OR LENGTH(owed_currency) = 3",
            name="owed_currency_length",
        ),
        CheckConstraint(
            "total_amount_cents >= 0",
            name="total_amount_cents_nonneg",
        ),
        # A populated ``owed_amount_cents`` snapshot must be
        # non-negative. NULL is the "not yet approved" state.
        CheckConstraint(
            "owed_amount_cents IS NULL OR owed_amount_cents >= 0",
            name="owed_amount_cents_nonneg",
        ),
        CheckConstraint(
            "autofill_confidence_overall IS NULL "
            "OR (autofill_confidence_overall >= 0 "
            "AND autofill_confidence_overall <= 1)",
            name="autofill_confidence_overall_bounds",
        ),
        # Manager inbox: "claims awaiting approval in this workspace".
        # Leading ``workspace_id`` lets the tenant filter's equality
        # predicate ride the same B-tree; ``state`` carries the
        # filter.
        Index(
            "ix_expense_claim_workspace_state",
            "workspace_id",
            "state",
        ),
        # Worker view: "my claims, newest first". Leading
        # ``workspace_id`` again, then the engagement equality, then
        # the ORDER BY DESC on ``submitted_at``.
        Index(
            "ix_expense_claim_workspace_engagement_submitted",
            "workspace_id",
            "work_engagement_id",
            "submitted_at",
        ),
    )


class ExpenseLine(Base):
    """One line item inside a claim — a single row from the receipt.

    A line carries a description, a quantity (fractional units
    allowed — ``0.5 kg cheese``), a unit price in the claim's
    currency, a derived line total cached at write time (so the
    "sum of lines" invariant can ride an index), and a ``source``
    enum pinning whether the row was produced by the
    ``expenses.autofill`` capability (``ocr``) or typed by a user
    (``manual``).

    **App-layer invariant.** ``line_total_cents == unit_price_cents
    * quantity`` (rounded half-to-even at the claim-currency minor-unit
    precision). SQLite's CHECK dialect cannot evaluate a Decimal
    multiply portably, and the rounding step is currency-aware
    (minor units vary), so the rule is enforced in the domain layer
    — same pattern as ``payslip.net_cents`` in
    :mod:`app.adapters.db.payroll`.

    **App-layer invariant.** The sum of every live line's
    ``line_total_cents`` equals the parent claim's
    ``total_amount_cents`` (minus any rounding drift captured in a
    ``round_adjust`` house-keeping line). §09 §"Model" names it on
    :class:`ExpenseClaim` as a derived field; the domain layer
    recomputes on every line add / remove and refuses a write that
    would drift the claim total.

    The ``(workspace_id, claim_id)`` index powers the "fetch all
    lines for this claim" read path.
    """

    __tablename__ = "expense_line"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Denormalised so the ORM tenant filter rides a local column —
    # same pattern as ``instruction_version.workspace_id``.
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CASCADE — deleting a claim drops its lines. The normal
    # archive path is a soft-delete of the claim (``deleted_at``),
    # not a hard DELETE; cascade is the safety net for a true
    # platform-level sweep.
    claim_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("expense_claim.id", ondelete="CASCADE"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(String, nullable=False)
    # Fractional quantities allowed — a line might be ``0.5 kg
    # cheese`` or ``3.25 h consulting``. ``Numeric(18, 4)`` matches
    # the sibling ``inventory_item.current_qty`` precision.
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    # Integer cents in the *claim*'s currency (not the destination
    # currency). ``BigInteger`` per §02 §"Money".
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Cached line total — recomputed in the domain layer on every
    # line write so the "sum of lines" invariant can ride an index.
    # See the class docstring for the invariant rule.
    line_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Soft-ref :class:`str` — links to an ``asset`` row for §21 TCO
    # tracking. NULL when the line is not a capital-equipment
    # purchase. FK promotion lands with cd-7rfu's service layer once
    # the asset table is in place.
    asset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    # Provenance bit — set ``true`` when a user mutates a row that
    # was originally ``source = 'ocr'``. The ``source`` column
    # itself stays ``ocr`` (§09 §"LLM accuracy"); this flag carries
    # the "edited since autofill" signal for reporting.
    edited_by_user: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    __table_args__ = (
        CheckConstraint(
            f"source IN ({_in_clause(_SOURCE_VALUES)})",
            name="source",
        ),
        # Quantities are non-negative — a negative line (`refund`)
        # is modelled as a line on a separate refund claim, not a
        # signed row on the original.
        CheckConstraint("quantity >= 0", name="quantity_nonneg"),
        CheckConstraint(
            "unit_price_cents >= 0",
            name="unit_price_cents_nonneg",
        ),
        CheckConstraint(
            "line_total_cents >= 0",
            name="line_total_cents_nonneg",
        ),
        # "All lines for this claim" read path — rides the composite
        # B-tree. Leading ``workspace_id`` carries the tenant filter.
        Index(
            "ix_expense_line_workspace_claim",
            "workspace_id",
            "claim_id",
        ),
    )


class ExpenseAttachment(Base):
    """A file attached to a claim — receipt photo, invoice PDF, or other.

    An attachment is a soft-ref into blob storage (content-addressed
    ``blob_hash``) plus a ``kind`` enum pinning the type. Multiple
    attachments per claim are legal (multi-page receipt, receipt +
    invoice). Cardinality is 0..N: a claim may be submitted without
    any attachments (the worker entered fields manually) or with
    many (a taxi receipt, the meal receipt, the tip receipt).

    The ``pages`` int is reserved for multi-page PDF attachments —
    §09 §"Model" names it; the v1 slice lands the column as
    nullable since the autofill capability still operates page-by-
    page and the aggregation lands with cd-7rfu. ``blob_hash`` is
    the content-addressed storage pointer (same convention as
    ``evidence.blob_hash`` and ``payslip.pdf_blob_hash``); the
    shared ``file`` table in §02 §"Shared tables" lands in a later
    migration, at which point the soft-ref becomes a real FK.

    The ``(workspace_id, claim_id)`` index powers the "every
    attachment for this claim" read path — the claim detail view
    lists them in upload order.
    """

    __tablename__ = "expense_attachment"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Denormalised so the ORM tenant filter rides a local column.
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CASCADE — deleting a claim drops its attachments. Soft-delete
    # on the claim keeps attachment rows alive; only hard DELETE
    # (platform-level sweep) cascades.
    claim_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("expense_claim.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Content-addressed blob reference (SHA-256 hash). Soft-ref —
    # see the module docstring. The same hash may be referenced by
    # multiple rows (same receipt re-used across claims), so keeping
    # it blob-ref only preserves the content-addressed storage
    # layer's dedup.
    blob_hash: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="receipt")
    # ``pages`` is populated for multi-page PDFs (§09 §"Model").
    # NULL for single-image receipts — an unset page count is
    # distinct from a one-page document.
    pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_ATTACHMENT_KIND_VALUES)})",
            name="kind",
        ),
        # A populated ``pages`` count must be strictly positive — a
        # zero-page document is nonsensical.
        CheckConstraint(
            "pages IS NULL OR pages >= 1",
            name="pages_positive",
        ),
        # "All attachments for this claim" read path.
        Index(
            "ix_expense_attachment_workspace_claim",
            "workspace_id",
            "claim_id",
        ),
    )
