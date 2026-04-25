"""Expense-claim CRUD + state-machine service (cd-7rfu).

The :class:`~app.adapters.db.expenses.models.ExpenseClaim` row records
a worker's request for reimbursement against a
:class:`~app.adapters.db.workspace.models.WorkEngagement`. The v1 state
machine is:

    draft -> submitted -> approved -> reimbursed
                       \\-> rejected
    draft -> (soft-deleted via ``deleted_at``)

with the explicit guard that the submit / approve / reimburse / reject
transitions are one-way — once a claim leaves ``draft`` its scalar
fields and attachments are read-only at the worker surface, and any
later state mutation is gated through the manager-approval service
(cd-9guk, out of scope here).

Public surface:

* **DTOs** — :class:`ExpenseClaimCreate` (POST body),
  :class:`ExpenseClaimUpdate` (PATCH body, partial), :class:`ReceiptAttach`
  (attachment metadata), plus the read projection
  :class:`ExpenseClaimView` and its nested :class:`ExpenseAttachmentView`.
  Shape-level validation (currency length / case, amount sign,
  category enum, mime allow-list, size cap) lives on the DTO so the
  same rule fires for HTTP + Python callers.
* **Service functions** — :func:`create_claim`, :func:`update_claim`,
  :func:`attach_receipt`, :func:`detach_receipt`, :func:`submit_claim`,
  :func:`cancel_claim`, :func:`get_claim`, :func:`list_for_user`,
  :func:`list_for_workspace`. Every function takes a
  :class:`~app.tenancy.WorkspaceContext` as its first argument; the
  ``workspace_id`` is resolved from the context, never from the
  caller's payload (v1 invariant §01).
* **Errors** — :class:`ClaimNotFound`, :class:`ClaimNotEditable`,
  :class:`ClaimStateTransitionInvalid`, :class:`CurrencyInvalid`,
  :class:`BlobMissing`, :class:`BlobMimeNotAllowed`,
  :class:`BlobTooLarge`, :class:`TooManyAttachments`,
  :class:`ClaimPermissionDenied`. Each subclasses the stdlib parent
  the router's error map points at (``LookupError`` -> 404,
  ``ValueError`` -> 409 / 422, ``PermissionError`` -> 403).

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation writes
one :mod:`app.audit` row in the same transaction; submit additionally
publishes :class:`~app.events.ExpenseSubmitted` AFTER the audit write
so a failed publish still leaves the audit row in the UoW.

**Authorisation model.**

* Worker self-CRUD on draft claims relies on **ownership** (the
  bound :class:`~app.adapters.db.workspace.models.WorkEngagement`'s
  ``user_id`` equals ``ctx.actor_id``), NOT a capability — every
  user can file their own claims by default. Cross-user creation
  / edit / cancel paths raise :class:`ClaimPermissionDenied`.
* :func:`submit_claim` additionally runs ``require(...,
  action_key='expenses.submit')`` for defence-in-depth, since the
  draft-to-submitted transition triggers manager workflow downstream.
* Listing other users' claims requires the ``expenses.approve``
  capability (the manager queue).

**Cancel semantics** (deviation flagged for review). The §02 schema
fixes the state enum to ``draft | submitted | approved | rejected |
reimbursed`` — there is no first-class ``cancelled`` value. The
spec's ``cancel_claim`` ("owner only, only while draft or submitted,
not after approved") therefore maps to two backend transitions:

* ``draft`` -> soft-delete (``deleted_at`` is set; the row is
  excluded from every read path other than audit).
* ``submitted`` -> ``rejected`` with ``decided_by = ctx.actor_id``
  and ``decision_note_md = 'cancelled by requester'``. This
  preserves the audit linkage (``decided_by`` holds the canceller,
  not a manager) and keeps the schema closed; the manager inbox
  filters ``state='rejected'`` claims out of the active queue, so
  the cancelled-by-requester case is invisible to approvers.

A future ``cancelled`` enum extension (§02 follow-up) would let the
two paths share a state — see the in-line comment on
:func:`cancel_claim` for the rationale.

**Storage seam.** :func:`attach_receipt` requires the caller to have
already streamed the bytes through :class:`~app.adapters.storage.ports.Storage`
(the typical flow: a separate ``POST /uploads`` endpoint owned by
cd-t6y2 invokes ``storage.put`` and returns the ``content_hash``).
The service verifies ``storage.exists(content_hash)`` before
inserting the attachment row; the asserted ``content_type`` /
``size_bytes`` are checked against the mime allow-list and the 10 MB
cap respectively. We do not re-fetch the blob to confirm the mime
back-look — the v1 :class:`Storage` protocol does not expose
metadata retrieval for existing blobs and re-streaming a 10 MB PDF
just to double-check the sniff is wasteful when the upload endpoint
already validated it. The asserted values land in the audit diff so
a malicious caller's lie is preserved for forensics.

See ``docs/specs/02-domain-model.md`` §"expense_claim" /
§"expense_attachment" / §"Enums",
``docs/specs/09-time-payroll-expenses.md`` §"Expense claims" /
§"Submission flow (worker)".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.expenses.models import (
    _ATTACHMENT_KIND_VALUES,
    _CATEGORY_VALUES,
    _STATE_VALUES,
    ExpenseAttachment,
    ExpenseClaim,
)
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import WorkEngagement
from app.adapters.storage.ports import Storage
from app.audit import write_audit
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.events import ExpenseSubmitted, bus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "BlobMimeNotAllowed",
    "BlobMissing",
    "BlobTooLarge",
    "ClaimNotEditable",
    "ClaimNotFound",
    "ClaimPermissionDenied",
    "ClaimStateTransitionInvalid",
    "CurrencyInvalid",
    "CurrencyTotal",
    "ExpenseAttachmentView",
    "ExpenseCategory",
    "ExpenseClaimCreate",
    "ExpenseClaimUpdate",
    "ExpenseClaimView",
    "ExpenseState",
    "PendingReimbursementUserBreakdown",
    "PendingReimbursementView",
    "PurchaseDateInFuture",
    "ReceiptAttach",
    "ReceiptKind",
    "TooManyAttachments",
    "attach_receipt",
    "cancel_claim",
    "create_claim",
    "detach_receipt",
    "get_claim",
    "list_for_user",
    "list_for_workspace",
    "pending_reimbursement",
    "submit_claim",
    "update_claim",
]


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums (string literals — keep parity with the DB CHECK constraints)
# ---------------------------------------------------------------------------


ExpenseState = Literal["draft", "submitted", "approved", "rejected", "reimbursed"]
ExpenseCategory = Literal[
    "supplies", "fuel", "food", "transport", "maintenance", "other"
]
ReceiptKind = Literal["receipt", "invoice", "other"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Caps kept modest to bound audit + DB payload without being
# restrictive in practice. Mirrors the shape of sibling
# :mod:`app.services.leave.service` constants.
_MAX_VENDOR_LEN = 200
_MAX_NOTE_LEN = 20_000
_MAX_ID_LEN = 40
_BLOB_HASH_LEN = 64  # SHA-256 hex.
_MAX_ATTACHMENTS_PER_CLAIM = 10
_MAX_BLOB_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB.

# Mime types we accept for receipts. The §09 §"Submission flow"
# discussion settles on the common phone-camera + mailed-PDF set:
# JPEG / PNG / WebP / HEIC for camera output, plus PDF for vendor
# invoices that arrive as attachments. No SVG (script-execution
# vector), no GIF (no legitimate receipt is animated), no generic
# ``application/octet-stream`` (caller must assert a real type).
_ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "application/pdf",
    }
)

# ISO-4217 allow-list — covers the common reserve currencies plus
# every 3-decimal-minor-unit currency we expect a household-manager
# workspace to encounter. The DB CHECK only enforces ``LENGTH = 3``;
# the domain layer narrows to the known set so a typo (``EURO``,
# ``UDS``, ``GPB``) surfaces at the boundary instead of corrupting
# the cross-currency exchange-rate snapshot at approval time. A
# future migration that adds a real ``currency`` table (cd-* TBD)
# will collapse this constant into a DB lookup.
#
# Coverage rationale: vacation-rental / household-manager workspaces
# routinely span North America, Europe, the Gulf, India, LATAM, and
# Southeast Asia (a workspace running villas in Bali bills owners in
# AUD, pays cleaners in IDR, and reimburses guests in EUR). The list
# is therefore intentionally broad — every major reserve currency,
# every G20 economy, the GCC + India + Israel for the Middle East,
# and the largest Southeast-Asian + LATAM economies. New entries pay
# only a tiny memory cost; missing entries surface as a hard 422 to
# real users.
_ISO_4217_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Reserve / G7 currencies.
        "USD",
        "EUR",
        "GBP",
        "CAD",
        "AUD",
        "JPY",
        "CHF",
        "NZD",
        # Nordic.
        "SEK",
        "NOK",
        "DKK",
        "ISK",
        # Central / Eastern Europe.
        "PLN",
        "CZK",
        "HUF",
        "RON",
        "BGN",
        "HRK",
        "TRY",
        # Asia-Pacific finance hubs.
        "SGD",
        "HKD",
        "TWD",
        "KRW",
        "CNY",
        # South + Southeast Asia.
        "INR",
        "IDR",
        "MYR",
        "THB",
        "PHP",
        "VND",
        # Middle East — GCC + Israel + Egypt.
        "AED",
        "SAR",
        "QAR",
        "ILS",
        "EGP",
        # Africa.
        "ZAR",
        # LATAM.
        "MXN",
        "BRL",
        "ARS",
        "CLP",
        "COP",
        "PEN",
        # 3-decimal minor-unit currencies (§02 §"Money" calls these
        # out so the integer-cents convention divides by 1000, not
        # 100). Including BHD + JOD + KWD + OMR + TND here ensures
        # the allow-list does not accidentally regress that contract.
        "BHD",
        "JOD",
        "KWD",
        "OMR",
        "TND",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ClaimNotFound(LookupError):
    """The requested claim does not exist in the caller's workspace.

    404-equivalent. Soft-deleted (``deleted_at IS NOT NULL``) claims
    collapse to this same error — a draft the worker cancelled is
    indistinguishable from a never-existed claim at the read surface.
    Cross-tenant probes also surface as 404 per §01 "tenant surface
    is not enumerable".
    """


class ClaimNotEditable(ValueError):
    """The claim is past ``draft`` and cannot be mutated by the worker.

    409-equivalent. Fires on update / attach / detach attempts against
    a ``submitted`` / ``approved`` / ``rejected`` / ``reimbursed`` row.
    Soft-deleted rows surface as :class:`ClaimNotFound` instead — the
    worker can't see them at all.
    """


class ClaimStateTransitionInvalid(ValueError):
    """The requested state transition is forbidden from the current state.

    409-equivalent. Submit on non-draft, cancel on
    ``approved`` / ``rejected`` / ``reimbursed`` — the state machine
    refuses. Approve / reject / reimburse transitions live on the
    manager service (cd-9guk) and have their own error class.
    """


class CurrencyInvalid(ValueError):
    """``currency`` is not in the known ISO-4217 allow-list.

    422-equivalent. The DB CHECK enforces ``LENGTH(currency) = 3``;
    the domain narrows to the curated set so a 3-letter typo
    (``EURO`` → would already fail the length CHECK; ``GPB`` /
    ``UDS`` / ``EURO`` after silent truncation → catches here).
    """


class BlobMissing(LookupError):
    """The asserted ``blob_hash`` has no blob in the storage layer.

    422-equivalent. The caller is expected to have streamed the bytes
    through :class:`~app.adapters.storage.ports.Storage` before
    invoking :func:`attach_receipt`. A missing blob means the upload
    failed or the caller fabricated the hash; either way the row would
    be a dangling reference.

    Subclasses :class:`LookupError` so the router error map can route
    the missing-blob path to a 422 ``blob_not_found`` envelope (the
    blob is "not found" but it's a validation failure on the caller's
    payload, not an enumerable claim resource).
    """


class BlobMimeNotAllowed(ValueError):
    """The asserted ``content_type`` is outside the receipt allow-list.

    422-equivalent. The allow-list is image/jpeg / image/png /
    image/webp / image/heic / application/pdf — see the module-level
    rationale.
    """


class BlobTooLarge(ValueError):
    """The asserted ``size_bytes`` exceeds the 10 MB cap.

    422-equivalent. A receipt photo above 10 MB is almost certainly an
    unprocessed RAW/HEIC dump or an accidental video; the cap forces
    the client to recompress before upload.
    """


class TooManyAttachments(ValueError):
    """Adding one more attachment would push the claim past the 10-cap.

    422-equivalent. The §09 spec caps a claim at 10 receipts /
    invoices. The 11th attempt raises here so the UI can show a
    helpful "you've reached the limit" message instead of letting
    the row land and rejecting later at approval.
    """


class PurchaseDateInFuture(ValueError):
    """``purchased_at`` is later than the caller's current clock instant.

    422-equivalent. A receipt cannot exist before the purchase has
    happened. We tolerate small clock skew (up to a minute) so a SPA
    that resolved ``Date.now()`` on a slightly fast laptop doesn't
    surface a confusing 422 to the worker; anything beyond that is a
    genuine bug or a malicious attempt to back-date a future expense.
    """


class ClaimPermissionDenied(PermissionError):
    """The caller is not the claim's author and lacks the manager capability.

    403-equivalent. Fires when:

    * a worker tries to create a claim against another user's
      engagement;
    * a worker tries to read / list / cancel another user's claim;
    * the ``expenses.submit`` / ``expenses.approve`` capability check
      surfaces a :class:`~app.authz.PermissionDenied`.

    The router maps this to the 403 envelope; the underlying
    permission detail stays in the audit log via
    :func:`write_audit`'s redaction seam.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class ExpenseClaimCreate(BaseModel):
    """Request body for ``POST /expense_claims``.

    Every field is required at the boundary except ``property_id`` and
    ``note_md``. ``property_id`` is the optional pin to a property
    context (§09 §"Property attribution"); ``note_md`` defaults to
    empty string to match the column's NOT NULL contract without
    forcing every caller to thread an empty string.

    Currency is uppercased on the boundary so a SPA that sends
    ``"usd"`` round-trips without surprise; the DTO enforces the case
    invariant via a validator rather than a free :class:`str` so the
    audit diff records the canonical form.
    """

    model_config = ConfigDict(extra="forbid")

    work_engagement_id: str = Field(min_length=1, max_length=_MAX_ID_LEN)
    vendor: str = Field(min_length=1, max_length=_MAX_VENDOR_LEN)
    purchased_at: datetime
    currency: str = Field(min_length=3, max_length=3)
    # Strictly positive — a zero-cents claim is nonsensical (the
    # worker is asking for nothing) and a negative one is impossible.
    # The DB CHECK is ``>= 0`` (it has to admit zero rows produced by
    # a future split-payment refund flow), so the domain narrows
    # further on the worker-create boundary.
    total_amount_cents: int = Field(gt=0)
    category: ExpenseCategory
    property_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    note_md: str = Field(default="", max_length=_MAX_NOTE_LEN)

    @field_validator("purchased_at")
    @classmethod
    def _purchased_at_is_aware(cls, value: datetime) -> datetime:
        """Reject naive timestamps at the boundary.

        ``time is UTC at rest`` — accepting a naive timestamp would
        let a SPA in the wrong locale silently shift the receipt
        date. The future-bound guard lives on the service (it needs a
        clock); the ``aware`` guard lives here because it has no
        clock dependency.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "purchased_at must be a timezone-aware datetime; got naive."
            )
        return value


class ExpenseClaimUpdate(BaseModel):
    """Request body for ``PATCH /expense_claims/{claim_id}``.

    Partial update — every field is optional. A field omitted from
    the body is left untouched; a field present with its declared
    type rewrites the column. ``None`` is intentionally NOT a valid
    value for any field (omit the key instead) — Pydantic's
    ``model_dump(exclude_unset=True)`` is what the service consumes.

    Editable only while the claim is in ``state='draft'``; the
    service raises :class:`ClaimNotEditable` on a submitted+ row.
    """

    model_config = ConfigDict(extra="forbid")

    work_engagement_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    vendor: str | None = Field(default=None, max_length=_MAX_VENDOR_LEN)
    purchased_at: datetime | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    # Strictly positive on edits too — same rationale as
    # :class:`ExpenseClaimCreate`. A worker who tries to PATCH the
    # amount to zero is presumably trying to cancel; the cancel path
    # is the right surface.
    total_amount_cents: int | None = Field(default=None, gt=0)
    category: ExpenseCategory | None = None
    property_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    note_md: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)

    @field_validator("purchased_at")
    @classmethod
    def _purchased_at_is_aware(cls, value: datetime | None) -> datetime | None:
        """Reject naive timestamps at the boundary (same rule as
        :class:`ExpenseClaimCreate`)."""
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "purchased_at must be a timezone-aware datetime; got naive."
            )
        return value


class ReceiptAttach(BaseModel):
    """Request body for ``POST /expense_claims/{claim_id}/attachments``.

    The HTTP layer (cd-t6y2) wires a separate ``POST /uploads``
    endpoint that streams bytes through Storage and returns the
    ``content_hash`` + asserted ``content_type`` / ``size_bytes``;
    the SPA chains the two calls. ``pages`` is reserved for multi-
    page PDFs (§09 §"Model"); single-image receipts leave it ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    blob_hash: str = Field(min_length=_BLOB_HASH_LEN, max_length=_BLOB_HASH_LEN)
    kind: ReceiptKind = "receipt"
    pages: int | None = Field(default=None, ge=1)


@dataclass(frozen=True, slots=True)
class ExpenseAttachmentView:
    """Immutable read projection of one ``expense_attachment`` row."""

    id: str
    claim_id: str
    blob_hash: str
    kind: ReceiptKind
    pages: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExpenseClaimView:
    """Immutable read projection of an ``expense_claim`` row.

    Returned by every service read + write. A frozen / slotted
    dataclass (not a Pydantic model) because reads carry audit-
    sensitive fields (``decided_by``, ``decided_at``,
    ``submitted_at``) that are managed by the service, not the
    caller's payload — matches the convention on
    :class:`~app.services.leave.service.LeaveView`.

    ``attachments`` is a tuple (not a list) so the view is
    transitively immutable; callers that need a mutable list build
    one explicitly.
    """

    id: str
    workspace_id: str
    work_engagement_id: str
    vendor: str
    purchased_at: datetime
    currency: str
    total_amount_cents: int
    category: ExpenseCategory
    property_id: str | None
    note_md: str
    state: ExpenseState
    submitted_at: datetime | None
    decided_by: str | None
    decided_at: datetime | None
    decision_note_md: str | None
    created_at: datetime
    deleted_at: datetime | None
    attachments: tuple[ExpenseAttachmentView, ...]


# ---------------------------------------------------------------------------
# Row <-> view projection
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime.

    SQLite's ``DateTime(timezone=True)`` strips tzinfo on read; the
    cross-backend invariant ("time is UTC at rest") lets us tag a
    naive value as UTC without guessing. Mirrors
    :func:`app.services.leave.service._ensure_utc`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _narrow_state(value: str) -> ExpenseState:
    """Narrow a loaded DB string to the :data:`ExpenseState` literal.

    The DB CHECK already enforces the set; this guard exists purely to
    satisfy mypy's strict-Literal reading without a ``cast``. An
    unexpected value indicates schema drift — raise rather than
    silently downgrade.
    """
    if value == "draft":
        return "draft"
    if value == "submitted":
        return "submitted"
    if value == "approved":
        return "approved"
    if value == "rejected":
        return "rejected"
    if value == "reimbursed":
        return "reimbursed"
    raise ValueError(f"unknown expense_claim.state {value!r} on loaded row")


def _narrow_category(value: str) -> ExpenseCategory:
    """Narrow a loaded DB string to :data:`ExpenseCategory`."""
    if value == "supplies":
        return "supplies"
    if value == "fuel":
        return "fuel"
    if value == "food":
        return "food"
    if value == "transport":
        return "transport"
    if value == "maintenance":
        return "maintenance"
    if value == "other":
        return "other"
    raise ValueError(f"unknown expense_claim.category {value!r} on loaded row")


def _narrow_kind(value: str) -> ReceiptKind:
    """Narrow a loaded DB string to :data:`ReceiptKind`."""
    if value == "receipt":
        return "receipt"
    if value == "invoice":
        return "invoice"
    if value == "other":
        return "other"
    raise ValueError(f"unknown expense_attachment.kind {value!r} on loaded row")


def _attachment_to_view(row: ExpenseAttachment) -> ExpenseAttachmentView:
    return ExpenseAttachmentView(
        id=row.id,
        claim_id=row.claim_id,
        blob_hash=row.blob_hash,
        kind=_narrow_kind(row.kind),
        pages=row.pages,
        created_at=_ensure_utc(row.created_at),
    )


def _load_attachments(
    session: Session, *, workspace_id: str, claim_id: str
) -> tuple[ExpenseAttachmentView, ...]:
    """Return every attachment for ``claim_id`` in upload order."""
    stmt = (
        select(ExpenseAttachment)
        .where(
            ExpenseAttachment.workspace_id == workspace_id,
            ExpenseAttachment.claim_id == claim_id,
        )
        .order_by(ExpenseAttachment.created_at.asc(), ExpenseAttachment.id.asc())
    )
    rows = session.scalars(stmt).all()
    return tuple(_attachment_to_view(r) for r in rows)


def _row_to_view(session: Session, row: ExpenseClaim) -> ExpenseClaimView:
    """Project a loaded :class:`ExpenseClaim` row into a read view.

    Loads the attachment tuple in the same SELECT pass — read views
    are always whole-claim, never half-populated. Callers that need
    a no-attachment projection build one inline.
    """
    return ExpenseClaimView(
        id=row.id,
        workspace_id=row.workspace_id,
        work_engagement_id=row.work_engagement_id,
        vendor=row.vendor,
        purchased_at=_ensure_utc(row.purchased_at),
        currency=row.currency,
        total_amount_cents=row.total_amount_cents,
        category=_narrow_category(row.category),
        property_id=row.property_id,
        note_md=row.note_md,
        state=_narrow_state(row.state),
        submitted_at=(
            _ensure_utc(row.submitted_at) if row.submitted_at is not None else None
        ),
        decided_by=row.decided_by,
        decided_at=(
            _ensure_utc(row.decided_at) if row.decided_at is not None else None
        ),
        decision_note_md=row.decision_note_md,
        created_at=_ensure_utc(row.created_at),
        deleted_at=(
            _ensure_utc(row.deleted_at) if row.deleted_at is not None else None
        ),
        attachments=_load_attachments(
            session, workspace_id=row.workspace_id, claim_id=row.id
        ),
    )


def _view_to_diff_dict(view: ExpenseClaimView) -> dict[str, Any]:
    """Flatten a :class:`ExpenseClaimView` into a JSON-safe dict for audit.

    Stringifies datetime columns and renders the attachment tuple as
    a list of dicts so the audit row's ``diff`` JSON payload stays
    portable across SQLite + Postgres. Mirrors
    :func:`app.services.leave.service._view_to_diff_dict`.
    """
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "work_engagement_id": view.work_engagement_id,
        "vendor": view.vendor,
        "purchased_at": view.purchased_at.isoformat(),
        "currency": view.currency,
        "total_amount_cents": view.total_amount_cents,
        "category": view.category,
        "property_id": view.property_id,
        "note_md": view.note_md,
        "state": view.state,
        "submitted_at": (
            view.submitted_at.isoformat() if view.submitted_at is not None else None
        ),
        "decided_by": view.decided_by,
        "decided_at": (
            view.decided_at.isoformat() if view.decided_at is not None else None
        ),
        "decision_note_md": view.decision_note_md,
        "created_at": view.created_at.isoformat(),
        "deleted_at": (
            view.deleted_at.isoformat() if view.deleted_at is not None else None
        ),
        "attachments": [
            {
                "id": att.id,
                "blob_hash": att.blob_hash,
                "kind": att.kind,
                "pages": att.pages,
                "created_at": att.created_at.isoformat(),
            }
            for att in view.attachments
        ],
    }


# ---------------------------------------------------------------------------
# Authz helpers
# ---------------------------------------------------------------------------


def _require_capability(
    session: Session,
    ctx: WorkspaceContext,
    *,
    action_key: str,
) -> None:
    """Enforce ``action_key`` on the caller's workspace or raise.

    Wraps :func:`app.authz.require` + translates a caller-bug
    (unknown key / invalid scope) into :class:`RuntimeError` so the
    router layer surfaces it as 500. Mirrors
    :func:`app.services.leave.service._require_capability`.
    """
    try:
        require(
            session,
            ctx,
            action_key=action_key,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for {action_key!r}: {exc!s}"
        ) from exc


def _load_engagement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    engagement_id: str,
) -> WorkEngagement | None:
    """Return the engagement scoped to the caller's workspace, or ``None``.

    The defence-in-depth ``workspace_id`` predicate ensures a caller
    can never bind a claim to an engagement in another tenant — the
    ORM tenant filter already enforces this on every SELECT, but
    pinning the predicate explicitly here keeps the authorisation
    rule visible at the call site.
    """
    stmt = select(WorkEngagement).where(
        WorkEngagement.id == engagement_id,
        WorkEngagement.workspace_id == ctx.workspace_id,
    )
    return session.scalars(stmt).one_or_none()


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_currency(value: str) -> str:
    """Return the canonical (uppercased) currency or raise.

    Tolerates lowercase / mixed-case input (the SPA may forward a
    locale-dependent formatter); any code outside the allow-list
    raises :class:`CurrencyInvalid`.
    """
    upper = value.upper()
    if upper not in _ISO_4217_ALLOWLIST:
        raise CurrencyInvalid(
            f"currency {value!r} is not in the known ISO-4217 allow-list "
            f"({sorted(_ISO_4217_ALLOWLIST)!r})"
        )
    return upper


def _validate_category(value: str) -> ExpenseCategory:
    """Return the narrowed :data:`ExpenseCategory` or raise.

    The DTO's ``Literal`` already enforces the set on the HTTP
    boundary; this guard fires for Python callers that bypass the
    DTO (``model_construct`` / a subclass with loosened validators).
    """
    if value not in _CATEGORY_VALUES:
        raise ValueError(
            f"category {value!r} is not one of {sorted(_CATEGORY_VALUES)!r}"
        )
    return _narrow_category(value)


# Permitted clock skew between the SPA's wall clock and the server's
# ``Clock.now()`` when a worker hits "submit" the moment they pay.
# The window has to be wider than the typical NTP drift (~tens of ms)
# but narrower than what a back-dating exploit would need.
_PURCHASED_AT_SKEW_SECONDS = 60


def _validate_purchased_at_not_future(value: datetime, *, now: datetime) -> datetime:
    """Reject a ``purchased_at`` that is more than the skew window in the future.

    A receipt cannot exist before the purchase is made; a future
    date is either a clock-skew artefact (small) or an exploit
    attempt at back-dating a not-yet-incurred expense. The
    short tolerance window keeps honest "I just paid" submissions
    flowing while denying everything beyond it.
    """
    aware = _ensure_utc(value)
    if (aware - _ensure_utc(now)).total_seconds() > _PURCHASED_AT_SKEW_SECONDS:
        raise PurchaseDateInFuture(
            f"purchased_at={aware.isoformat()} is in the future relative to "
            f"now={now.isoformat()} (tolerated skew "
            f"{_PURCHASED_AT_SKEW_SECONDS}s)."
        )
    return value


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    include_deleted: bool = False,
    for_update: bool = False,
) -> ExpenseClaim:
    """Load ``claim_id`` scoped to the caller's workspace.

    Soft-deleted rows (``deleted_at IS NOT NULL``) are hidden by
    default — the worker can't see them, the manager queue filters
    them out. Pass ``include_deleted=True`` for the audit-replay path
    (not currently used by the public surface but reserved for the
    history API).

    ``for_update`` toggles a row-level ``SELECT ... FOR UPDATE`` so
    every state mutation (update / attach / detach / submit / cancel)
    serialises against any concurrent mutation of the same claim. On
    PostgreSQL this is the standard advisory predicate; on SQLite the
    clause is silently dropped (the engine's whole-database write
    lock already serialises). Without this guard, two concurrent
    ``submit_claim`` calls on the same draft could both observe
    ``state='draft'``, both flip to ``submitted``, and both publish
    an :class:`~app.events.ExpenseSubmitted` event — the manager
    queue would surface the same claim twice and notification fanout
    would double-fire. Read paths leave it ``False``.
    """
    stmt = select(ExpenseClaim).where(
        ExpenseClaim.id == claim_id,
        ExpenseClaim.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(ExpenseClaim.deleted_at.is_(None))
    if for_update:
        stmt = stmt.with_for_update()
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise ClaimNotFound(claim_id)
    return row


def _claim_user_id(
    session: Session, ctx: WorkspaceContext, *, claim: ExpenseClaim
) -> str:
    """Return the user_id behind the claim's bound engagement.

    Cached locally per claim — the engagement row is small and the
    extra round-trip is negligible against the audit + attachment
    inserts already required by every write path. Raising
    :class:`RuntimeError` on a dangling FK reflects the schema's
    ``RESTRICT`` ondelete contract: the engagement cannot vanish
    while a claim references it, so a missing row signals a data
    bug.
    """
    eng = _load_engagement(session, ctx, engagement_id=claim.work_engagement_id)
    if eng is None:
        raise RuntimeError(
            f"claim {claim.id!r} references missing engagement "
            f"{claim.work_engagement_id!r}"
        )
    return eng.user_id


def get_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
) -> ExpenseClaimView:
    """Return the claim identified by ``claim_id`` or raise.

    Authorisation: the caller must be the claim's author (the bound
    engagement's ``user_id``) or hold ``expenses.approve``. A peer
    worker probing another worker's claim collapses to
    :class:`ClaimPermissionDenied` (403); a cross-tenant probe
    collapses to :class:`ClaimNotFound` (404) per §01.
    """
    row = _load_row(session, ctx, claim_id=claim_id)
    author_user_id = _claim_user_id(session, ctx, claim=row)
    if author_user_id != ctx.actor_id:
        try:
            _require_capability(session, ctx, action_key="expenses.approve")
        except PermissionDenied as exc:
            raise ClaimPermissionDenied(str(exc)) from exc
    return _row_to_view(session, row)


def list_for_user(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
    state: ExpenseState | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> tuple[list[ExpenseClaimView], str | None]:
    """Return claims for ``user_id`` (default: the caller).

    Listing OTHER users' claims requires ``expenses.approve``; listing
    your own is always allowed. Soft-deleted claims are excluded.

    Pagination uses an opaque cursor (the last-returned claim's id);
    rows are ordered by ``created_at`` descending, with ``id`` DESC as
    a tiebreaker for the same-millisecond case. ``limit`` is clamped
    to ``[1, 500]`` to prevent a runaway scan; ``cursor=None`` starts
    from the most-recent claim.

    The return tuple is ``(claims, next_cursor)`` — ``next_cursor`` is
    ``None`` when the caller has reached the end of the queryset.
    """
    target_user_id = user_id if user_id is not None else ctx.actor_id
    if target_user_id != ctx.actor_id:
        try:
            _require_capability(session, ctx, action_key="expenses.approve")
        except PermissionDenied as exc:
            raise ClaimPermissionDenied(str(exc)) from exc

    bounded_limit = max(1, min(limit, 500))

    # Filter through the engagement table — claims are author-keyed by
    # their engagement's ``user_id``, not directly. The join is cheap
    # because the engagement is single-row per claim.
    stmt = (
        select(ExpenseClaim)
        .join(
            WorkEngagement,
            (WorkEngagement.id == ExpenseClaim.work_engagement_id)
            & (WorkEngagement.workspace_id == ExpenseClaim.workspace_id),
        )
        .where(
            ExpenseClaim.workspace_id == ctx.workspace_id,
            ExpenseClaim.deleted_at.is_(None),
            WorkEngagement.user_id == target_user_id,
        )
    )
    if state is not None:
        stmt = stmt.where(ExpenseClaim.state == state)
    if cursor is not None:
        stmt = stmt.where(ExpenseClaim.id < cursor)
    stmt = stmt.order_by(ExpenseClaim.id.desc()).limit(bounded_limit + 1)

    rows = list(session.scalars(stmt).all())
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    next_cursor = rows[-1].id if has_more and rows else None
    return [_row_to_view(session, r) for r in rows], next_cursor


def list_for_workspace(
    session: Session,
    ctx: WorkspaceContext,
    *,
    state: ExpenseState | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> tuple[list[ExpenseClaimView], str | None]:
    """Return every claim in the workspace (manager queue).

    Always requires ``expenses.approve``. Soft-deleted claims are
    excluded. Pagination semantics match :func:`list_for_user`.
    """
    try:
        _require_capability(session, ctx, action_key="expenses.approve")
    except PermissionDenied as exc:
        raise ClaimPermissionDenied(str(exc)) from exc

    bounded_limit = max(1, min(limit, 500))

    stmt = select(ExpenseClaim).where(
        ExpenseClaim.workspace_id == ctx.workspace_id,
        ExpenseClaim.deleted_at.is_(None),
    )
    if state is not None:
        stmt = stmt.where(ExpenseClaim.state == state)
    if cursor is not None:
        stmt = stmt.where(ExpenseClaim.id < cursor)
    stmt = stmt.order_by(ExpenseClaim.id.desc()).limit(bounded_limit + 1)

    rows = list(session.scalars(stmt).all())
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    next_cursor = rows[-1].id if has_more and rows else None
    return [_row_to_view(session, r) for r in rows], next_cursor


# ---------------------------------------------------------------------------
# Pending-reimbursement aggregate (cd-mh4p)
# ---------------------------------------------------------------------------
#
# Approved-but-not-yet-reimbursed claims are the "owed to the employee"
# pool (§09 §"Amount owed to the employee"). The aggregate underlies
# the worker's "Owed to you" panel (per-user totals) and the manager
# "Pay" surface (workspace-wide totals + per-user breakdown).
#
# Authority lives here rather than on the router because the totals
# aggregation is shared between the worker / manager / cross-user
# paths and a future CSV export of "what's outstanding" surface will
# reuse the same shape.


@dataclass(frozen=True, slots=True)
class CurrencyTotal:
    """One ``(currency, amount_cents)`` total grouped by claim currency.

    Currencies are keyed off ``expense_claim.currency`` (the original
    purchase currency), NOT ``owed_currency``. The per-claim
    ``owed_currency`` snapshot lands at approval time per §09 but is
    not yet wired into the v1 service surface (the
    ``payout_destination`` table is still deferred — see
    :mod:`app.adapters.db.expenses.models` deviation note). Once that
    lands, this aggregate switches to ``owed_currency``; the wire
    shape stays the same.
    """

    currency: str
    amount_cents: int


@dataclass(frozen=True, slots=True)
class PendingReimbursementUserBreakdown:
    """Per-user slice of the workspace-wide pending-reimbursement total.

    Surfaced only when the caller queried the workspace-wide aggregate
    (no ``user_id`` filter); the per-user surface omits this slice
    because it would just echo the top-level total back.

    ``user_name`` is the :class:`User.display_name` at read time —
    pinned so the manager UI can surface "Maya's outstanding" without
    a second round-trip. Renames flow through naturally on the next
    read.
    """

    user_id: str
    user_name: str
    totals_by_currency: tuple[CurrencyTotal, ...]


@dataclass(frozen=True, slots=True)
class PendingReimbursementView:
    """The full pending-reimbursement read view.

    ``user_id`` echoes the resolved filter target (``ctx.actor_id`` for
    the ``user_id=me`` form, the explicit id for the manager-driven
    form, ``None`` for the workspace-wide aggregate).

    ``claims`` is the list of approved-but-not-reimbursed claims in
    scope — the worker UI itemises each as a "due to you" row; the
    manager UI sums via ``totals_by_currency`` but keeps the underlying
    list available for drill-down.

    ``totals_by_currency`` is the per-currency sum across ``claims``.
    Empty when the scope yields no rows; the wire envelope still
    includes the (empty) list so SPA consumers don't have to guard
    on a missing key.

    ``by_user`` is populated only on the workspace-wide aggregate
    response — ``None`` for the per-user form, since the breakdown
    would just echo ``totals_by_currency`` back.
    """

    user_id: str | None
    claims: tuple[ExpenseClaimView, ...]
    totals_by_currency: tuple[CurrencyTotal, ...]
    by_user: tuple[PendingReimbursementUserBreakdown, ...] | None


def _sum_by_currency(
    claims: tuple[ExpenseClaimView, ...],
) -> tuple[CurrencyTotal, ...]:
    """Aggregate ``claims`` into one ``CurrencyTotal`` per currency.

    Order is deterministic — sorted alphabetically by currency code so
    a manager UI's table doesn't reshuffle on refresh and a snapshot-
    test stays stable. Currencies with a zero sum cannot appear (the
    only path here is "claim row exists in this currency"), so the
    list length matches the distinct-currency count.
    """
    totals: dict[str, int] = {}
    for claim in claims:
        totals[claim.currency] = (
            totals.get(claim.currency, 0) + claim.total_amount_cents
        )
    return tuple(
        CurrencyTotal(currency=ccy, amount_cents=amt)
        for ccy, amt in sorted(totals.items())
    )


def _load_pending_claims(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None,
) -> tuple[ExpenseClaimView, ...]:
    """Load every approved-but-not-yet-reimbursed claim in scope.

    Soft-deleted rows are excluded by definition (the ``deleted_at IS
    NULL`` predicate). ``user_id`` narrows via the engagement join;
    ``None`` returns every workspace claim. Order is ``id ASC`` so
    same-currency totals roll up deterministically (the SUM is
    associative either way; this just keeps fixture-driven tests
    stable on the inline ``claims`` list).
    """
    stmt = select(ExpenseClaim).where(
        ExpenseClaim.workspace_id == ctx.workspace_id,
        ExpenseClaim.state == "approved",
        ExpenseClaim.deleted_at.is_(None),
    )
    if user_id is not None:
        stmt = stmt.join(
            WorkEngagement,
            (WorkEngagement.id == ExpenseClaim.work_engagement_id)
            & (WorkEngagement.workspace_id == ExpenseClaim.workspace_id),
        ).where(WorkEngagement.user_id == user_id)
    stmt = stmt.order_by(ExpenseClaim.id.asc())
    rows = list(session.scalars(stmt).all())
    return tuple(_row_to_view(session, r) for r in rows)


def _user_display_names(
    session: Session,
    *,
    user_ids: set[str],
) -> dict[str, str]:
    """Bulk-resolve ``user_id -> display_name`` for ``user_ids``.

    Returns an empty dict when ``user_ids`` is empty so the caller can
    pass an empty set without conditional plumbing. A user row that
    has been hard-deleted (rare; the platform soft-deletes via
    ``user.deleted_at`` once that lands) collapses to a synthetic
    ``"unknown"`` label rather than missing-key — the manager UI
    must always render a name on every breakdown row.
    """
    if not user_ids:
        return {}
    stmt = select(User.id, User.display_name).where(User.id.in_(user_ids))
    return {uid: name for uid, name in session.execute(stmt).all()}


def pending_reimbursement(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None,
) -> PendingReimbursementView:
    """Return the pending-reimbursement aggregate for ``user_id``.

    ``user_id`` semantics:

    * ``ctx.actor_id`` (or any value matching it) — narrow to the
      caller's own approved-but-not-reimbursed claims. No capability
      check; every worker can read their own pool.
    * Any other id — narrow to that user's pool. Requires
      ``expenses.approve`` (the manager queue gate); raises
      :class:`ClaimPermissionDenied` otherwise.
    * ``None`` — workspace-wide aggregate; ``by_user`` carries the
      per-user breakdown. Requires ``expenses.approve`` for the same
      reason as the cross-user form.

    The wire envelope mirrors the SPA's ``PendingReimbursement``
    type 1:1 (see ``app/web/src/types/expense.ts``). The router
    layer projects to JSON; this function returns immutable
    domain views.

    Per §09 §"Amount owed to the employee", the pool is "approved-
    but-not-yet-reimbursed" — claims in any other state are excluded
    (a draft / submitted hasn't crossed the approval gate; a rejected
    is terminal; a reimbursed has already settled). Soft-deleted rows
    can't be in ``approved`` by the state-machine invariant, but the
    ``deleted_at IS NULL`` predicate is kept for defence-in-depth.
    """
    # Self-only ("user_id=me" → ``user_id == ctx.actor_id``) is the
    # single path that bypasses the manager-cap gate; every other
    # path (cross-user, workspace-wide aggregate) runs the capability
    # check so a worker probing another user's pool or the workspace-
    # wide aggregate gets a 403 envelope.
    is_self_only = user_id is not None and user_id == ctx.actor_id
    if not is_self_only:
        try:
            _require_capability(session, ctx, action_key="expenses.approve")
        except PermissionDenied as exc:
            raise ClaimPermissionDenied(str(exc)) from exc

    if user_id is None:
        # Workspace-wide aggregate. Load once, slice per-user from the
        # same projection so the totals + by_user shares come from the
        # same SELECT pass.
        claims = _load_pending_claims(session, ctx, user_id=None)
        totals = _sum_by_currency(claims)

        # Group claims by author user_id for the breakdown. We need a
        # claim → user_id map; load every engagement referenced in the
        # claim batch in one round-trip rather than per-claim.
        engagement_ids = {c.work_engagement_id for c in claims}
        eng_to_user: dict[str, str] = {}
        if engagement_ids:
            eng_stmt = select(WorkEngagement.id, WorkEngagement.user_id).where(
                WorkEngagement.workspace_id == ctx.workspace_id,
                WorkEngagement.id.in_(engagement_ids),
            )
            eng_to_user = {eid: uid for eid, uid in session.execute(eng_stmt).all()}

        per_user_claims: dict[str, list[ExpenseClaimView]] = {}
        for claim in claims:
            uid = eng_to_user.get(claim.work_engagement_id)
            if uid is None:
                # Dangling engagement reference — the schema's RESTRICT
                # ondelete makes this impossible without a manual
                # tampering. Surface loudly rather than silently dropping
                # the row from the aggregate.
                raise RuntimeError(
                    f"claim {claim.id!r} references missing engagement "
                    f"{claim.work_engagement_id!r}"
                )
            per_user_claims.setdefault(uid, []).append(claim)

        names = _user_display_names(session, user_ids=set(per_user_claims))
        by_user = tuple(
            PendingReimbursementUserBreakdown(
                user_id=uid,
                user_name=names.get(uid, "unknown"),
                totals_by_currency=_sum_by_currency(tuple(per_user_claims[uid])),
            )
            for uid in sorted(per_user_claims)
        )
        return PendingReimbursementView(
            user_id=None,
            claims=claims,
            totals_by_currency=totals,
            by_user=by_user,
        )

    # Single-user (caller or other-with-capability) path.
    claims = _load_pending_claims(session, ctx, user_id=user_id)
    totals = _sum_by_currency(claims)
    return PendingReimbursementView(
        user_id=user_id,
        claims=claims,
        totals_by_currency=totals,
        by_user=None,
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: ExpenseClaimCreate,
    clock: Clock | None = None,
) -> ExpenseClaimView:
    """Create a fresh claim in ``state='draft'`` and return its view.

    Authorisation: the bound engagement MUST belong to the caller —
    ``WorkEngagement.user_id == ctx.actor_id`` AND ``WorkEngagement.workspace_id
    == ctx.workspace_id``. A worker can only file claims against their
    own engagement; manager-on-behalf-of submission is a future
    capability (``expenses.create_for_other``, not in v1). A
    cross-tenant or cross-user attempt raises
    :class:`ClaimPermissionDenied` (NOT 404 — the engagement may
    exist; we refuse to leak that fact via differentiated errors).

    The DTO enforces shape (currency length, amount sign, category
    enum); the service additionally narrows currency to the ISO-4217
    allow-list and uppercases it on the way in.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    canonical_currency = _validate_currency(body.currency)
    category = _validate_category(body.category)
    _validate_purchased_at_not_future(body.purchased_at, now=now)

    eng = _load_engagement(session, ctx, engagement_id=body.work_engagement_id)
    if eng is None or eng.user_id != ctx.actor_id:
        # Collapse "engagement does not exist in my workspace" and
        # "engagement exists but belongs to someone else" to the same
        # 403 — the differentiated error would leak engagement
        # presence across users.
        raise ClaimPermissionDenied(
            f"engagement {body.work_engagement_id!r} is not owned by "
            f"actor {ctx.actor_id!r}"
        )

    row = ExpenseClaim(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        work_engagement_id=body.work_engagement_id,
        vendor=body.vendor,
        purchased_at=body.purchased_at,
        currency=canonical_currency,
        total_amount_cents=body.total_amount_cents,
        category=category,
        property_id=body.property_id,
        note_md=body.note_md,
        state="draft",
        submitted_at=None,
        decided_by=None,
        decided_at=None,
        decision_note_md=None,
        created_at=now,
        deleted_at=None,
    )
    session.add(row)
    session.flush()

    view = _row_to_view(session, row)
    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=row.id,
        action="expense.claim.created",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    return view


def update_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    body: ExpenseClaimUpdate,
    clock: Clock | None = None,
) -> ExpenseClaimView:
    """Partial update on a draft claim.

    Authorisation: author-only (the bound engagement's ``user_id``).
    Manager edits on submitted claims live on the approval service
    (cd-9guk) — this path refuses every non-author caller, and
    refuses every non-draft state.

    A field omitted from the body is left untouched; a field present
    rewrites the column. Reassigning ``work_engagement_id`` is allowed
    while draft (the worker realised they were on the wrong gig) but
    the new engagement must still belong to the caller — a
    cross-engagement reassignment to someone else's gig surfaces as
    :class:`ClaimPermissionDenied`. ``currency`` is re-validated +
    uppercased on update; ``category`` is re-narrowed.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, claim_id=claim_id, for_update=True)

    author_user_id = _claim_user_id(session, ctx, claim=row)
    if author_user_id != ctx.actor_id:
        raise ClaimPermissionDenied(
            f"claim {claim_id!r} is not owned by actor {ctx.actor_id!r}"
        )

    if row.state != "draft":
        raise ClaimNotEditable(
            f"claim {claim_id!r} is in state {row.state!r}; only draft "
            "claims may be edited"
        )

    diff = body.model_dump(exclude_unset=True)
    if not diff:
        # No-op PATCH — return the current view without writing audit.
        return _row_to_view(session, row)

    before = _row_to_view(session, row)

    if "work_engagement_id" in diff:
        new_eng_id = diff["work_engagement_id"]
        new_eng = _load_engagement(session, ctx, engagement_id=new_eng_id)
        if new_eng is None or new_eng.user_id != ctx.actor_id:
            raise ClaimPermissionDenied(
                f"engagement {new_eng_id!r} is not owned by actor {ctx.actor_id!r}"
            )
        row.work_engagement_id = new_eng_id

    if "vendor" in diff:
        row.vendor = diff["vendor"]
    if "purchased_at" in diff:
        _validate_purchased_at_not_future(
            diff["purchased_at"], now=resolved_clock.now()
        )
        row.purchased_at = diff["purchased_at"]
    if "currency" in diff:
        row.currency = _validate_currency(diff["currency"])
    if "total_amount_cents" in diff:
        row.total_amount_cents = diff["total_amount_cents"]
    if "category" in diff:
        row.category = _validate_category(diff["category"])
    if "property_id" in diff:
        row.property_id = diff["property_id"]
    if "note_md" in diff:
        row.note_md = diff["note_md"]

    session.flush()
    after = _row_to_view(session, row)
    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=row.id,
        action="expense.claim.updated",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def attach_receipt(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    blob_hash: str,
    content_type: str,
    size_bytes: int,
    storage: Storage,
    kind: ReceiptKind = "receipt",
    pages: int | None = None,
    clock: Clock | None = None,
    extraction_runner: Callable[..., Any] | None = None,
) -> ExpenseAttachmentView:
    """Attach a receipt blob to a draft claim.

    The caller is responsible for streaming the bytes through
    :class:`~app.adapters.storage.ports.Storage` first (a separate
    upload endpoint owned by cd-t6y2) and asserting the resulting
    ``content_hash`` / ``content_type`` / ``size_bytes`` triplet.
    The service:

    1. confirms the bound claim is editable (draft, author-owned);
    2. validates the asserted mime against the receipt allow-list;
    3. validates the asserted size against the 10 MB cap;
    4. checks ``storage.exists(blob_hash)`` to prove the upload landed;
    5. enforces the 10-attachments-per-claim cap;
    6. inserts the row + writes an audit row.

    The asserted ``content_type`` and ``size_bytes`` land in the
    audit diff so a malicious caller's claim is preserved for
    forensics. We do NOT re-stream the blob to confirm the mime
    sniff — see the module docstring for the rationale.

    ``extraction_runner`` is the cd-95zb DI seam: when non-``None``
    AND the claim is still ``draft`` after the attach lands, the
    callable is invoked synchronously with
    ``(session, ctx, claim_id=..., attachment_id=...)`` so the LLM
    OCR / autofill pipeline can write back to the same UoW. Pass
    ``None`` (the default) when no autofill is desired — tests, the
    legacy call sites that predate cd-95zb, and any deployment
    where ``settings.llm_ocr_model`` is unset. The runner contract
    matches :func:`app.worker.tasks.receipt_ocr.run_receipt_ocr` so
    a future async-queue scaffolding can swap the call site without
    a domain-layer change.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, claim_id=claim_id, for_update=True)

    author_user_id = _claim_user_id(session, ctx, claim=row)
    if author_user_id != ctx.actor_id:
        raise ClaimPermissionDenied(
            f"claim {claim_id!r} is not owned by actor {ctx.actor_id!r}"
        )

    if row.state != "draft":
        raise ClaimNotEditable(
            f"claim {claim_id!r} is in state {row.state!r}; cannot attach "
            "receipts outside the draft state"
        )

    # Boundary validations FIRST — cheap, deterministic, no DB hit.
    if content_type not in _ALLOWED_MIME_TYPES:
        raise BlobMimeNotAllowed(
            f"content_type {content_type!r} is not in the receipt allow-list "
            f"({sorted(_ALLOWED_MIME_TYPES)!r})"
        )
    if size_bytes > _MAX_BLOB_SIZE_BYTES:
        raise BlobTooLarge(
            f"size_bytes={size_bytes} exceeds the {_MAX_BLOB_SIZE_BYTES} byte "
            f"({_MAX_BLOB_SIZE_BYTES // (1024 * 1024)} MB) cap"
        )
    if size_bytes < 0:
        # Defence-in-depth — the HTTP layer rejects negative sizes,
        # but a Python caller bypassing the DTO would otherwise land
        # a nonsensical row.
        raise BlobTooLarge(f"size_bytes={size_bytes} must be non-negative")

    # Storage existence check AFTER cheap validation: a malformed
    # mime / oversized blob should never trigger a storage round-trip.
    if not storage.exists(blob_hash):
        raise BlobMissing(
            f"blob {blob_hash!r} is not in storage; upload it before attaching"
        )

    # Cap check — count live attachments only. Soft-deleted claims
    # don't get here (``_load_row`` filters them) so every loaded
    # attachment is live.
    existing = _load_attachments(
        session, workspace_id=ctx.workspace_id, claim_id=claim_id
    )
    if len(existing) >= _MAX_ATTACHMENTS_PER_CLAIM:
        raise TooManyAttachments(
            f"claim {claim_id!r} already has {_MAX_ATTACHMENTS_PER_CLAIM} "
            "attachments; remove one before attaching another"
        )

    # Defence-in-depth — the DTO's ``Literal`` enforces the receipt
    # kind set on HTTP, but a Python caller bypassing the DTO would
    # otherwise land an out-of-set value at the DB CHECK constraint.
    if kind not in _ATTACHMENT_KIND_VALUES:
        raise ValueError(
            f"kind {kind!r} is not one of {sorted(_ATTACHMENT_KIND_VALUES)!r}"
        )

    now = resolved_clock.now()
    attachment = ExpenseAttachment(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        claim_id=claim_id,
        blob_hash=blob_hash,
        kind=kind,
        pages=pages,
        created_at=now,
    )
    session.add(attachment)
    session.flush()
    view = _attachment_to_view(attachment)

    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=claim_id,
        action="expense.claim.receipt_attached",
        diff={
            "after": {
                "attachment_id": view.id,
                "blob_hash": view.blob_hash,
                "kind": view.kind,
                "pages": view.pages,
                # Asserted — preserved for forensics. NOT validated
                # against a back-look on the blob.
                "content_type": content_type,
                "size_bytes": size_bytes,
            }
        },
        clock=resolved_clock,
    )

    # cd-95zb: run the OCR / autofill pipeline synchronously when a
    # runner is wired AND the claim is still draft. Passing ``None``
    # (the default) keeps every existing test + the manual-entry
    # path untouched.
    #
    # The runner writes its own ``receipt.ocr_failed`` audit row +
    # :class:`LlmUsageRow` BEFORE re-raising on every failure mode
    # (timeout, rate-limit, parse error, provider error). If we let
    # those exceptions propagate here, the surrounding UoW would
    # rollback — wiping out the attach row, the attach audit row,
    # AND the failure-mode rows the runner just wrote — leaving the
    # caller with a 5xx and a stranded blob in storage.
    #
    # The cd-95zb contract explicitly says "failure modes leave the
    # claim untouched and audit the failure". "Untouched" means the
    # attach DID happen; the worker can retry the OCR offline. We
    # therefore swallow the runner's exception, keep the attach +
    # audit + usage rows, and surface the failure through the
    # already-persisted audit trail. Operators see the failure on
    # /admin/usage and the SPA polls ``llm_autofill_json`` (which
    # stays NULL) to know to fall back to manual entry.
    if extraction_runner is not None and row.state == "draft":
        try:
            extraction_runner(session, ctx, claim_id=claim_id, attachment_id=view.id)
        except Exception as exc:
            # The runner has already written its own audit row +
            # ``LlmUsage`` row before raising; we want those rows
            # to survive the commit. A bare ``Exception`` catch is
            # the right shape here precisely because we do not want
            # an unexpected runner crash to corrupt the attach UoW.
            _log.warning(
                "expense.claim.receipt_attached: extraction runner failed",
                extra={
                    "event": "expense.autofill.runner_failed",
                    "claim_id": claim_id,
                    "attachment_id": view.id,
                    "error_kind": type(exc).__name__,
                    # ``str(exc)`` could leak provider-side detail —
                    # rely on the runner's own audit row for the
                    # full message; this log line stays terse.
                },
            )

    return view


def detach_receipt(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    attachment_id: str,
    clock: Clock | None = None,
) -> None:
    """Remove an attachment from a draft claim.

    Authorisation: author-only. Only valid while the claim is in
    ``state='draft'`` — once submitted, attachments are immutable
    (the manager approval audit trail relies on the row's history).

    The blob itself is NOT deleted from storage — the same hash may
    still be referenced by another claim or a sibling tasks/Evidence
    row. Storage GC is a separate sweep (cd-* TBD).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, claim_id=claim_id, for_update=True)

    author_user_id = _claim_user_id(session, ctx, claim=row)
    if author_user_id != ctx.actor_id:
        raise ClaimPermissionDenied(
            f"claim {claim_id!r} is not owned by actor {ctx.actor_id!r}"
        )

    if row.state != "draft":
        raise ClaimNotEditable(
            f"claim {claim_id!r} is in state {row.state!r}; cannot detach "
            "receipts outside the draft state"
        )

    stmt = select(ExpenseAttachment).where(
        ExpenseAttachment.id == attachment_id,
        ExpenseAttachment.claim_id == claim_id,
        ExpenseAttachment.workspace_id == ctx.workspace_id,
    )
    attachment = session.scalars(stmt).one_or_none()
    if attachment is None:
        raise ClaimNotFound(attachment_id)

    before = _attachment_to_view(attachment)
    session.delete(attachment)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=claim_id,
        action="expense.claim.receipt_detached",
        diff={
            "before": {
                "attachment_id": before.id,
                "blob_hash": before.blob_hash,
                "kind": before.kind,
                "pages": before.pages,
                "created_at": before.created_at.isoformat(),
            }
        },
        clock=resolved_clock,
    )


def submit_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    clock: Clock | None = None,
) -> ExpenseClaimView:
    """Transition a draft claim to ``submitted``.

    Authorisation:

    * author-only (the bound engagement's ``user_id``);
    * additionally requires the ``expenses.submit`` capability —
      defence-in-depth so a workspace can revoke submission rights
      from a specific user via a deny rule even though every worker
      holds the capability by default (auto-allowed for ``all_workers``
      via :data:`~app.domain.identity._action_catalog.ACTION_CATALOG`).

    Effects: ``state`` -> ``submitted``, ``submitted_at`` -> ``now``;
    audit row written; :class:`~app.events.ExpenseSubmitted` published
    AFTER the audit write so a failed publish still leaves the audit
    trail in the UoW.

    Re-submitting a claim that is already submitted (or beyond)
    raises :class:`ClaimStateTransitionInvalid` — the worker should
    refresh their view if they think the submission was lost.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    # ``for_update=True`` serialises this submit against any
    # concurrent submit / cancel / update on the same draft so two
    # concurrent ``submit_claim`` calls cannot both flip the state
    # and double-publish :class:`ExpenseSubmitted`.
    row = _load_row(session, ctx, claim_id=claim_id, for_update=True)

    author_user_id = _claim_user_id(session, ctx, claim=row)
    if author_user_id != ctx.actor_id:
        raise ClaimPermissionDenied(
            f"claim {claim_id!r} is not owned by actor {ctx.actor_id!r}"
        )

    # Capability re-check is intentionally AFTER ownership — a
    # non-author caller never reaches the capability check, so the
    # 403 envelope is consistent regardless of which guard fires.
    try:
        _require_capability(session, ctx, action_key="expenses.submit")
    except PermissionDenied as exc:
        raise ClaimPermissionDenied(str(exc)) from exc

    if row.state != "draft":
        raise ClaimStateTransitionInvalid(
            f"claim {claim_id!r} is in state {row.state!r}; only draft "
            "claims may be submitted"
        )

    before = _row_to_view(session, row)
    row.state = "submitted"
    row.submitted_at = now
    session.flush()
    after = _row_to_view(session, row)

    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=row.id,
        action="expense.claim.submitted",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    bus.publish(
        ExpenseSubmitted(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            claim_id=row.id,
            work_engagement_id=row.work_engagement_id,
            submitter_user_id=ctx.actor_id,
            currency=row.currency,
            total_amount_cents=row.total_amount_cents,
        )
    )
    return after


def cancel_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    reason_md: str | None = None,
    clock: Clock | None = None,
) -> ExpenseClaimView:
    """Cancel a claim the caller authored.

    Authorisation: author-only. Manager-driven rejection lives on the
    approval service (cd-9guk).

    State-machine guards:

    * ``draft`` -> soft-delete (``deleted_at`` set); the row is
      excluded from every read path other than audit.
    * ``submitted`` -> ``rejected`` with ``decided_by = ctx.actor_id``
      and ``decision_note_md = "cancelled by requester"`` (or the
      caller's ``reason_md``, prefixed with "cancelled by requester:
      " for the audit narrative). See the module docstring for the
      schema-deviation rationale.
    * ``approved`` / ``rejected`` / ``reimbursed`` -> raise
      :class:`ClaimStateTransitionInvalid`. A reimbursed claim
      cannot be undone here; the manager must issue a refund claim.

    The post-cancel view's ``state`` reflects the resulting row
    state (``draft`` for soft-deletes — the row still says draft,
    ``deleted_at`` is the cancellation marker; ``rejected`` for
    submitted-cancellations).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    # ``for_update=True`` keeps cancel serialised against any
    # concurrent submit on the same draft, so we cannot soft-delete a
    # row that another transaction simultaneously transitioned to
    # ``submitted`` (or cancel + submit racing in the other direction).
    row = _load_row(session, ctx, claim_id=claim_id, for_update=True)

    author_user_id = _claim_user_id(session, ctx, claim=row)
    if author_user_id != ctx.actor_id:
        raise ClaimPermissionDenied(
            f"claim {claim_id!r} is not owned by actor {ctx.actor_id!r}"
        )

    current_state = _narrow_state(row.state)
    before = _row_to_view(session, row)

    if current_state == "draft":
        row.deleted_at = now
        action = "expense.claim.cancelled"
    elif current_state == "submitted":
        # Schema deviation flagged in the module docstring: there is
        # no first-class ``cancelled`` enum, so worker cancellation of
        # a submitted claim maps to the ``rejected`` terminal state
        # with an explicit decision_note. ``decided_by`` carries the
        # canceller (the worker themselves), not a manager — the
        # manager queue filters ``rejected`` rows out of the active
        # queue and the cancellation audit row carries the narrative.
        row.state = "rejected"
        row.decided_by = ctx.actor_id
        row.decided_at = now
        prefix = "cancelled by requester"
        row.decision_note_md = prefix if reason_md is None else f"{prefix}: {reason_md}"
        action = "expense.claim.cancelled"
    else:
        # approved / rejected / reimbursed — terminal from the
        # worker's perspective.
        raise ClaimStateTransitionInvalid(
            f"claim {claim_id!r} is in state {row.state!r}; cannot cancel "
            "from this state"
        )

    session.flush()
    after = _row_to_view(session, row)

    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=row.id,
        action=action,
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


# ---------------------------------------------------------------------------
# Guardrails against drift
# ---------------------------------------------------------------------------


# Pin the assumptions this module makes about the DB enum sets. If a
# future migration widens the state / category / kind vocabulary, the
# narrow helpers above break (unknown value on a loaded row) or these
# asserts catch the drift at import time — whichever fires first
# makes the drift explicit. Mirrors the guardrail at the bottom of
# :mod:`app.services.leave.service`.
assert set(_STATE_VALUES) == {
    "draft",
    "submitted",
    "approved",
    "rejected",
    "reimbursed",
}, "ExpenseState literal diverged from DB CHECK set"
assert set(_CATEGORY_VALUES) == {
    "supplies",
    "fuel",
    "food",
    "transport",
    "maintenance",
    "other",
}, "ExpenseCategory literal diverged from DB CHECK set"
assert set(_ATTACHMENT_KIND_VALUES) == {
    "receipt",
    "invoice",
    "other",
}, "ReceiptKind literal diverged from DB CHECK set"
