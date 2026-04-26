"""Manager-side approval + reimbursement service for expense claims (cd-9guk).

Layers on top of the worker-side CRUD shipped in
:mod:`app.domain.expenses.claims`. The v1 manager-flow state machine
extends the claim lifecycle to:

    submitted -> approved   -> reimbursed
              \\-> rejected

with the explicit guards that:

* approve / reject only move from ``submitted`` (rejecting an already-
  rejected claim or approving an already-approved one is a 409 from
  :class:`ClaimNotApprovable`);
* mark-reimbursed only moves from ``approved`` (settling a draft, a
  submitted, or a rejected claim is a 409 from
  :class:`ClaimNotReimbursable`);
* every transition requires the right capability — ``expenses.approve``
  for the approve / reject / queue-read path, ``expenses.reimburse``
  for the settle path. Workspace-scoped capabilities, both already in
  the catalog (cd-dzp).

Public surface:

* **DTOs** — :class:`ApprovalEdits` (partial inline edits the
  approver may apply at the same time as the approve transition),
  :class:`RejectBody` (the rejection narrative), :class:`ReimburseBody`
  (channel + optional paid-at). All three are Pydantic v2 models with
  ``extra='forbid'`` and the same shape-validation rules as the
  worker-side DTOs (currency length / case, amount sign, category
  enum, no-future ``paid_at`` skew window).
* **Service functions** — :func:`approve_claim`, :func:`reject_claim`,
  :func:`mark_reimbursed`, :func:`list_pending`. Same argument
  convention as :mod:`app.domain.expenses.claims`: ``session`` first,
  :class:`~app.tenancy.WorkspaceContext` second, the rest keyword-only.
* **Errors** — :class:`ClaimNotApprovable` (409 — wrong state for
  approve / reject), :class:`ClaimNotReimbursable` (409 — wrong state
  for mark-reimbursed), :class:`ApprovalPermissionDenied` (403 —
  missing ``expenses.approve``), :class:`ReimbursePermissionDenied`
  (403 — missing ``expenses.reimburse``). :class:`ClaimNotFound` is
  re-exported from :mod:`app.domain.expenses.claims` so the router's
  error map points at one class.

**Transaction boundary.** Same as the worker-side service: never
``session.commit()``; the caller's Unit-of-Work owns the transaction.
Every mutation writes one :mod:`app.audit` row, and the corresponding
event is published AFTER the audit write so a failed publish leaves
the audit trail intact in the UoW.

**Authorisation model.**

* Approve / reject runs ``require(..., 'expenses.approve')`` —
  workspace-scoped, default-allowed for owners + managers (catalog).
  A worker without manager grant cannot reach the queue or transition
  any claim.
* Reimburse runs ``require(..., 'expenses.reimburse')`` — workspace-
  scoped, default-allowed for owners + managers, ``root_protected_deny``
  on (per cd-dzp the deployment-root account is ringfenced from
  workspace data without an explicit grant).
* Listing the pending queue requires ``expenses.approve`` (same gate
  as :func:`app.domain.expenses.claims.list_for_workspace`).

**Inline-edit shape.** :class:`ApprovalEdits` mirrors
:class:`~app.domain.expenses.claims.ExpenseClaimUpdate` minus
``work_engagement_id`` (an approver cannot reassign a claim to a
different engagement — the binding is a worker-side concern). Each
field present in the body rewrites the column; absent fields are
left untouched. The before/after diff lands in the audit row's
``diff`` payload alongside the transition narrative so a manager's
adjustment ("vendor was misspelled, total off by 1¢") is visible
without walking the audit log.

See ``docs/specs/02-domain-model.md`` §"expense_claim",
``docs/specs/09-time-payroll-expenses.md`` §"Approval (owner or
manager)" / §"Reimbursement".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.expenses.models import (
    _REIMBURSED_VIA_VALUES,
    ExpenseAttachment,
    ExpenseClaim,
)
from app.adapters.db.workspace.models import WorkEngagement
from app.audit import write_audit
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.domain.expenses.claims import (
    ClaimNotFound,
    ExpenseAttachmentView,
    ExpenseCategory,
    ExpenseClaimView,
    _ensure_utc,
    _narrow_category,
    _narrow_kind,
    _narrow_state,
    _validate_category,
    _validate_currency,
    _validate_purchased_at_not_future,
    _view_to_diff_dict,
)
from app.events import (
    ExpenseApproved,
    ExpenseReimbursed,
    ExpenseRejected,
    bus,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "ApprovalEdits",
    "ApprovalPermissionDenied",
    "ClaimNotApprovable",
    "ClaimNotFound",
    "ClaimNotReimbursable",
    "ReimburseBody",
    "ReimbursePermissionDenied",
    "ReimburseVia",
    "RejectBody",
    "approve_claim",
    "list_pending",
    "mark_reimbursed",
    "reject_claim",
]


# ---------------------------------------------------------------------------
# Enums (string literals — keep parity with the DB CHECK constraints)
# ---------------------------------------------------------------------------


# Same shape as :data:`app.domain.expenses.claims.ExpenseState` —
# kept as a narrow literal at the boundary so the DTO + event stay
# in lock-step with the DB CHECK clamp on ``reimbursed_via``.
ReimburseVia = Literal["cash", "bank", "card", "other"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_MAX_VENDOR_LEN = 200
_MAX_NOTE_LEN = 20_000
_MAX_REASON_LEN = 20_000
_MAX_ID_LEN = 40

# Same skew window as
# :data:`app.domain.expenses.claims._PURCHASED_AT_SKEW_SECONDS` — a
# manager hitting "mark reimbursed" the moment they push the
# transfer must not be tripped by a few-second clock skew between
# the SPA and the server, but the window is narrow enough that a
# back-dated future ``paid_at`` is rejected.
_PAID_AT_SKEW_SECONDS = 60


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ClaimNotApprovable(ValueError):
    """The claim is not in ``submitted`` and cannot be approved or rejected.

    409-equivalent. Fires on :func:`approve_claim` / :func:`reject_claim`
    when the row is in any state other than ``submitted``: a ``draft``
    has not been submitted, an ``approved`` / ``rejected`` /
    ``reimbursed`` row already has a recorded decision and cannot be
    transitioned again. The state machine is one-way per §09 — a
    rejected claim stays rejected; the worker must file a fresh claim.
    """


class ClaimNotReimbursable(ValueError):
    """The claim is not in ``approved`` and cannot be marked reimbursed.

    409-equivalent. Fires on :func:`mark_reimbursed` when the row is
    in any state other than ``approved``. ``draft`` / ``submitted``
    cannot skip approval; ``rejected`` is terminal; an already
    ``reimbursed`` row has already been settled.
    """


class ApprovalPermissionDenied(PermissionError):
    """The caller lacks ``expenses.approve`` for the workspace.

    403-equivalent. Fires on :func:`approve_claim`, :func:`reject_claim`,
    and :func:`list_pending` when the resolver returns
    :class:`~app.authz.PermissionDenied`. Default-allowed for
    owners + managers (catalog cd-dzp); workers without an explicit
    grant cannot reach the queue.
    """


class ReimbursePermissionDenied(PermissionError):
    """The caller lacks ``expenses.reimburse`` for the workspace.

    403-equivalent. Distinct error class from
    :class:`ApprovalPermissionDenied` because the two capabilities
    serve different audiences: ``expenses.approve`` is the
    "decide-the-claim" gate, ``expenses.reimburse`` is the
    "actually-pushed-the-money" gate. A workspace may delegate
    approval to a line manager but reserve settlement for the
    finance owner — the two roles are independent.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class ApprovalEdits(BaseModel):
    """Inline edits the approver may apply at the same time as approval.

    Mirrors :class:`~app.domain.expenses.claims.ExpenseClaimUpdate`
    minus ``work_engagement_id`` (the worker-engagement binding is
    a worker-side concern; an approver who finds the wrong binding
    must reject and ask the worker to refile, not silently rewrite
    it). Every field is optional; ``model_dump(exclude_unset=True)``
    is what :func:`approve_claim` consumes. ``None`` is intentionally
    NOT a valid value for any field — omit the key instead.

    Same validation rules as :class:`ExpenseClaimUpdate`:

    * ``currency`` is exactly 3 chars and is uppercased + narrowed
      to the ISO-4217 allow-list at the service layer;
    * ``total_amount_cents`` is strictly positive (a zero-cents
      approval is nonsensical);
    * ``purchased_at`` must be a UTC-aware datetime AND must not
      sit beyond the no-future skew window.
    """

    model_config = ConfigDict(extra="forbid")

    vendor: str | None = Field(default=None, min_length=1, max_length=_MAX_VENDOR_LEN)
    purchased_at: datetime | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    total_amount_cents: int | None = Field(default=None, gt=0)
    category: ExpenseCategory | None = None
    property_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    note_md: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)

    @field_validator("purchased_at")
    @classmethod
    def _purchased_at_is_aware(cls, value: datetime | None) -> datetime | None:
        """Reject naive timestamps at the boundary (same rule as
        :class:`~app.domain.expenses.claims.ExpenseClaimUpdate`)."""
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "purchased_at must be a timezone-aware datetime; got naive."
            )
        return value


class RejectBody(BaseModel):
    """Request body for :func:`reject_claim`.

    The single ``reason_md`` field carries the manager's rejection
    narrative. Required and non-empty: an empty rejection note hides
    the *why* from the worker, who then has nothing actionable to
    fix. Pydantic's ``min_length=1`` enforces the non-empty guard at
    the DTO layer (422-equivalent on the HTTP boundary), matching
    the spec's "Reject without reason rejected with 422" acceptance.

    The rendered reason is PII — a manager may name a vendor,
    reference a personal-spend categorisation, or quote a receipt
    detail — so it stays out of the published event payload (see
    :class:`~app.events.ExpenseRejected`) and travels through
    :func:`app.audit.write_audit`'s redaction seam at audit time.
    """

    model_config = ConfigDict(extra="forbid")

    reason_md: str = Field(min_length=1, max_length=_MAX_REASON_LEN)


class ReimburseBody(BaseModel):
    """Request body for :func:`mark_reimbursed`.

    ``via`` is the channel actually used to settle the claim
    (``cash | bank | card | other``); the column is CHECK-clamped at
    the DB to the same set. ``paid_at`` is optional — when omitted,
    the service stamps the server clock at transition time (the
    common case: a manager taps "mark reimbursed" right after pushing
    the transfer). When provided, the value must be UTC-aware and
    must not sit beyond the no-future skew window: a manager
    back-stamping a Friday transfer on Monday is fine; a manager
    forward-stamping a future transfer is a bug or back-dating
    attempt.
    """

    model_config = ConfigDict(extra="forbid")

    via: ReimburseVia
    paid_at: datetime | None = None

    @field_validator("paid_at")
    @classmethod
    def _paid_at_is_aware(cls, value: datetime | None) -> datetime | None:
        """Reject naive timestamps at the boundary.

        ``time is UTC at rest`` — accepting a naive timestamp would
        let a SPA in the wrong locale silently shift the recorded
        settlement instant. The future-bound guard lives on the
        service (it needs a clock); the ``aware`` guard lives here
        because it has no clock dependency.
        """
        if value is None:
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("paid_at must be a timezone-aware datetime; got naive.")
        return value


# ---------------------------------------------------------------------------
# ORM-bound helpers (cd-zoj4 follow-up will migrate these onto the seam)
# ---------------------------------------------------------------------------
#
# The cd-0e8i refactor pulled :mod:`app.domain.expenses.claims` off
# :mod:`app.adapters.db.expenses.models`, so the
# ``_load_row(session, ctx, ...)`` / ``_row_to_view(session, row)`` /
# ``_load_attachments(session, ...)`` helpers approval previously
# imported from claims now live here as local equivalents until the
# cd-zoj4 follow-up rewires this module onto
# :class:`~app.domain.expenses.ports.ExpensesRepository` end-to-end.
#
# The import edges (``app.domain.expenses.approval ->
# app.adapters.db.expenses.models`` / ``-> app.adapters.db.workspace.models``)
# are already covered by the cd-9guk stopgap entries in
# ``pyproject.toml``; nothing new gets added.


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    include_deleted: bool = False,
    for_update: bool = False,
) -> ExpenseClaim:
    """Load ``claim_id`` scoped to the caller's workspace or raise.

    Mirrors the cd-0e8i-removed
    :func:`app.domain.expenses.claims._load_row` shape so the rest of
    the approval flow keeps reading. ``include_deleted`` /
    ``for_update`` follow the same semantics as the original.
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


def _load_attachments(
    session: Session, *, workspace_id: str, claim_id: str
) -> tuple[ExpenseAttachmentView, ...]:
    """Return every attachment for ``claim_id`` in upload order.

    Local copy of the cd-0e8i-removed
    :func:`app.domain.expenses.claims._load_attachments` so
    :func:`_row_to_view` can attach the receipts list to the projected
    view.
    """
    stmt = (
        select(ExpenseAttachment)
        .where(
            ExpenseAttachment.workspace_id == workspace_id,
            ExpenseAttachment.claim_id == claim_id,
        )
        .order_by(ExpenseAttachment.created_at.asc(), ExpenseAttachment.id.asc())
    )
    rows = session.scalars(stmt).all()
    return tuple(
        ExpenseAttachmentView(
            id=r.id,
            claim_id=r.claim_id,
            blob_hash=r.blob_hash,
            kind=_narrow_kind(r.kind),
            pages=r.pages,
            created_at=_ensure_utc(r.created_at),
        )
        for r in rows
    )


def _row_to_view(session: Session, row: ExpenseClaim) -> ExpenseClaimView:
    """Project a loaded :class:`ExpenseClaim` row into the domain read view.

    Local copy of the cd-0e8i-removed
    :func:`app.domain.expenses.claims._row_to_view`. Loads the
    attachment tuple in the same SELECT pass — read views are always
    whole-claim, never half-populated.
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


# ---------------------------------------------------------------------------
# Authz helpers
# ---------------------------------------------------------------------------


def _require_approval(session: Session, ctx: WorkspaceContext) -> None:
    """Enforce ``expenses.approve`` or raise.

    Wraps :func:`app.authz.require` + translates a caller-bug
    (unknown key / invalid scope) into :class:`RuntimeError` so the
    router layer surfaces it as 500. Mirrors
    :func:`app.domain.expenses.claims._require_capability` but with
    the dedicated 403 type for the approval surface.
    """
    try:
        require(
            session,
            ctx,
            action_key="expenses.approve",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'expenses.approve': {exc!s}"
        ) from exc
    except PermissionDenied as exc:
        raise ApprovalPermissionDenied(str(exc)) from exc


def _require_reimburse(session: Session, ctx: WorkspaceContext) -> None:
    """Enforce ``expenses.reimburse`` or raise.

    Distinct from :func:`_require_approval` so the router emits the
    right 403 envelope ("missing approve" vs "missing reimburse").
    """
    try:
        require(
            session,
            ctx,
            action_key="expenses.reimburse",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            f"authz catalog misconfigured for 'expenses.reimburse': {exc!s}"
        ) from exc
    except PermissionDenied as exc:
        raise ReimbursePermissionDenied(str(exc)) from exc


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_paid_at_not_future(value: datetime, *, now: datetime) -> datetime:
    """Reject a ``paid_at`` more than the skew window in the future.

    Same shape as
    :func:`app.domain.expenses.claims._validate_purchased_at_not_future`
    but raises a vanilla :class:`ValueError` (the router maps this
    to 422); a dedicated error class would over-specify the surface
    when the only caller path is the ``mark_reimbursed`` DTO.
    """
    aware = _ensure_utc(value)
    if (aware - _ensure_utc(now)).total_seconds() > _PAID_AT_SKEW_SECONDS:
        raise ValueError(
            f"paid_at={aware.isoformat()} is in the future relative to "
            f"now={now.isoformat()} (tolerated skew "
            f"{_PAID_AT_SKEW_SECONDS}s)."
        )
    return value


# ---------------------------------------------------------------------------
# Engagement lookup (for the submitter user_id on every event)
# ---------------------------------------------------------------------------


def _submitter_user_id(
    session: Session, ctx: WorkspaceContext, *, claim: ExpenseClaim
) -> str:
    """Return the user_id behind the claim's bound engagement.

    Cached locally per claim. A missing FK target signals a data bug
    (the schema enforces ``RESTRICT`` on
    ``expense_claim.work_engagement_id``); raise rather than silently
    pass ``None`` into an event payload that requires a non-null
    ``submitter_user_id``.
    """
    stmt = select(WorkEngagement).where(
        WorkEngagement.id == claim.work_engagement_id,
        WorkEngagement.workspace_id == ctx.workspace_id,
    )
    eng = session.scalars(stmt).one_or_none()
    if eng is None:
        raise RuntimeError(
            f"claim {claim.id!r} references missing engagement "
            f"{claim.work_engagement_id!r}"
        )
    return eng.user_id


# ---------------------------------------------------------------------------
# approve_claim
# ---------------------------------------------------------------------------


def _apply_edits(claim: ExpenseClaim, edits: ApprovalEdits, *, now: datetime) -> bool:
    """Apply ``edits`` to ``claim`` in place.

    Returns ``True`` when at least one field was rewritten (so the
    audit row + event ``had_edits`` flag carry the truth), ``False``
    when ``edits`` was a no-op (every supplied field omitted, or the
    DTO was constructed but every key absent). Currency is
    re-validated + uppercased; ``purchased_at`` re-runs the no-future
    guard against the *current* clock (an approver cannot push the
    receipt date into the future even if the worker had submitted
    earlier).
    """
    diff = edits.model_dump(exclude_unset=True)
    if not diff:
        return False

    if "vendor" in diff:
        claim.vendor = diff["vendor"]
    if "purchased_at" in diff:
        _validate_purchased_at_not_future(diff["purchased_at"], now=now)
        claim.purchased_at = diff["purchased_at"]
    if "currency" in diff:
        claim.currency = _validate_currency(diff["currency"])
    if "total_amount_cents" in diff:
        claim.total_amount_cents = diff["total_amount_cents"]
    if "category" in diff:
        # The DTO's ``Literal`` narrows the category on HTTP, but a
        # Python caller bypassing the DTO would otherwise land an
        # out-of-set value at the DB CHECK constraint. Mirrors the
        # safety net in :func:`app.domain.expenses.claims.update_claim`.
        claim.category = _validate_category(diff["category"])
    if "property_id" in diff:
        claim.property_id = diff["property_id"]
    if "note_md" in diff:
        claim.note_md = diff["note_md"]
    return True


def approve_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    edits: ApprovalEdits | None = None,
    clock: Clock | None = None,
) -> ExpenseClaimView:
    """Transition a submitted claim to ``approved``.

    Authorisation: requires ``expenses.approve`` (workspace-scoped).

    Optional ``edits`` apply inline before the state flip — the
    approver fixes a typo, adjusts an off-by-one cents amount, or
    re-categorises a row in the same call. Every applied field
    lands in the audit row's ``diff`` payload as a before/after
    pair; the published :class:`~app.events.ExpenseApproved` event
    carries a single ``had_edits`` boolean so client surfaces can
    surface a "manager adjusted the figures" chip without re-fetching
    the audit log.

    Effects:
    * ``state='approved'``;
    * ``decided_by = ctx.actor_id``, ``decided_at = now``;
    * audit row ``expense.claim.approved`` with the before/after
      diff (or just the transition when no edits applied);
    * :class:`~app.events.ExpenseApproved` published AFTER the audit
      write so a failed publish leaves the audit trail intact.

    Re-approving an already-approved claim (or any non-submitted
    claim) raises :class:`ClaimNotApprovable`.

    Uses :func:`~app.domain.expenses.claims._load_row`'s
    ``for_update=True`` so two concurrent approvers cannot both flip
    the state and double-publish the event.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _require_approval(session, ctx)

    row = _load_row(session, ctx, claim_id=claim_id, for_update=True)

    if row.state != "submitted":
        raise ClaimNotApprovable(
            f"claim {claim_id!r} is in state {row.state!r}; only submitted "
            "claims may be approved"
        )

    submitter_user_id = _submitter_user_id(session, ctx, claim=row)
    before = _row_to_view(session, row)

    had_edits = False
    if edits is not None:
        had_edits = _apply_edits(row, edits, now=now)

    row.state = "approved"
    row.decided_by = ctx.actor_id
    row.decided_at = now
    session.flush()
    after = _row_to_view(session, row)

    audit_diff: dict[str, Any] = {
        "before": _view_to_diff_dict(before),
        "after": _view_to_diff_dict(after),
    }
    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=row.id,
        action="expense.claim.approved",
        diff=audit_diff,
        clock=resolved_clock,
    )
    bus.publish(
        ExpenseApproved(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            claim_id=row.id,
            work_engagement_id=row.work_engagement_id,
            submitter_user_id=submitter_user_id,
            decided_by_user_id=ctx.actor_id,
            had_edits=had_edits,
        )
    )
    return after


# ---------------------------------------------------------------------------
# reject_claim
# ---------------------------------------------------------------------------


def reject_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    reason_md: str,
    clock: Clock | None = None,
) -> ExpenseClaimView:
    """Transition a submitted claim to ``rejected``.

    Authorisation: requires ``expenses.approve`` (workspace-scoped).

    The caller MUST pass a non-empty ``reason_md`` — the DTO layer
    (:class:`RejectBody`) enforces ``min_length=1`` so the 422
    surfaces at the boundary, but Python callers bypassing the DTO
    must still observe the rule. An empty / whitespace-only reason
    raises :class:`ValueError` (mapped to 422 by the router).

    Effects:
    * ``state='rejected'``;
    * ``decided_by = ctx.actor_id``, ``decided_at = now``;
    * ``decision_note_md = reason_md`` (the rendered reason lives on
      the row, behind the per-row authz path; subscribers fetch it
      via REST);
    * audit row ``expense.claim.rejected`` with the before/after
      diff. ``reason_md`` lands in the diff payload too — it
      flows through :func:`app.audit.write_audit`'s redaction seam
      so a personally-identifying snippet is scrubbed before the
      row hits disk.
    * :class:`~app.events.ExpenseRejected` published AFTER the
      audit write. The event payload deliberately omits ``reason_md``
      — see the event class docstring.

    Rejecting a non-submitted claim raises
    :class:`ClaimNotApprovable`. Row-locked for the same race-
    avoidance reason as :func:`approve_claim`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _require_approval(session, ctx)

    if not reason_md or not reason_md.strip():
        # Defence-in-depth — the DTO enforces ``min_length=1`` on
        # the HTTP path, but a Python caller bypassing the DTO
        # would otherwise persist an empty rejection note. The
        # state-machine guard is checked AFTER ownership-equivalent
        # authz so the error envelope is consistent.
        raise ValueError(
            "reason_md must be a non-empty string; "
            "an empty rejection hides the why from the worker."
        )

    row = _load_row(session, ctx, claim_id=claim_id, for_update=True)

    if row.state != "submitted":
        raise ClaimNotApprovable(
            f"claim {claim_id!r} is in state {row.state!r}; only submitted "
            "claims may be rejected"
        )

    submitter_user_id = _submitter_user_id(session, ctx, claim=row)
    before = _row_to_view(session, row)

    row.state = "rejected"
    row.decided_by = ctx.actor_id
    row.decided_at = now
    row.decision_note_md = reason_md
    session.flush()
    after = _row_to_view(session, row)

    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=row.id,
        action="expense.claim.rejected",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    bus.publish(
        ExpenseRejected(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            claim_id=row.id,
            work_engagement_id=row.work_engagement_id,
            submitter_user_id=submitter_user_id,
            decided_by_user_id=ctx.actor_id,
        )
    )
    return after


# ---------------------------------------------------------------------------
# mark_reimbursed
# ---------------------------------------------------------------------------


def _narrow_reimbursed_via(value: str) -> ReimburseVia:
    """Narrow a string to :data:`ReimburseVia` or raise.

    The DTO's ``Literal`` already enforces the set on the HTTP
    boundary; this guard fires for Python callers that bypass the
    DTO. An unexpected value indicates schema drift — raise rather
    than silently downgrade.
    """
    if value == "cash":
        return "cash"
    if value == "bank":
        return "bank"
    if value == "card":
        return "card"
    if value == "other":
        return "other"
    raise ValueError(
        f"reimbursed_via {value!r} is not one of {sorted(_REIMBURSED_VIA_VALUES)!r}"
    )


def mark_reimbursed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    body: ReimburseBody,
    clock: Clock | None = None,
) -> ExpenseClaimView:
    """Transition an approved claim to ``reimbursed``.

    Authorisation: requires ``expenses.reimburse`` (workspace-scoped,
    ``root_protected_deny`` per cd-dzp).

    ``body.via`` records the channel actually used (``cash | bank |
    card | other``). ``body.paid_at`` is optional — when omitted,
    the service stamps ``now`` (server clock); when provided, the
    value must be UTC-aware (DTO-layer guard) AND must not sit
    beyond the small future-skew window (service-layer guard,
    fires at 422 from the router).

    Effects:
    * ``state='reimbursed'``;
    * ``reimbursed_at = body.paid_at or now``;
    * ``reimbursed_via = body.via``;
    * ``reimbursed_by = ctx.actor_id`` (may differ from
      ``decided_by`` — the original approver);
    * audit row ``expense.claim.reimbursed`` with before/after diff;
    * :class:`~app.events.ExpenseReimbursed` published AFTER the
      audit write.

    Settling a non-approved claim raises
    :class:`ClaimNotReimbursable`. Row-locked.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    _require_reimburse(session, ctx)

    if body.paid_at is not None:
        _validate_paid_at_not_future(body.paid_at, now=now)
    paid_at = body.paid_at if body.paid_at is not None else now

    row = _load_row(session, ctx, claim_id=claim_id, for_update=True)

    if row.state != "approved":
        raise ClaimNotReimbursable(
            f"claim {claim_id!r} is in state {row.state!r}; only approved "
            "claims may be marked reimbursed"
        )

    submitter_user_id = _submitter_user_id(session, ctx, claim=row)
    before = _row_to_view(session, row)

    via = _narrow_reimbursed_via(body.via)
    row.state = "reimbursed"
    row.reimbursed_at = paid_at
    row.reimbursed_via = via
    row.reimbursed_by = ctx.actor_id
    session.flush()
    after = _row_to_view(session, row)

    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=row.id,
        action="expense.claim.reimbursed",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    bus.publish(
        ExpenseReimbursed(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            claim_id=row.id,
            work_engagement_id=row.work_engagement_id,
            submitter_user_id=submitter_user_id,
            reimbursed_via=via,
            reimbursed_by_user_id=ctx.actor_id,
        )
    )
    return after


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------


def _encode_pending_cursor(*, submitted_at: datetime, claim_id: str) -> str:
    """Encode a (submitted_at, claim_id) pair as a domain-side cursor.

    The pair is rendered as ``<isoformat>|<id>`` — the pipe is not
    a legal character in either an ISO-8601 timestamp or a ULID, so
    the round-trip is lossless. The router layer wraps this in the
    HTTP-side base64 envelope (see :func:`app.api.pagination.encode_cursor`);
    keeping the domain cursor a plain string lets non-HTTP callers
    (CLI, test fixtures) inspect / construct cursors directly.
    """
    return f"{_ensure_utc(submitted_at).isoformat()}|{claim_id}"


def _decode_pending_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode a domain-side cursor produced by :func:`_encode_pending_cursor`.

    Raises :class:`ValueError` on a malformed payload — callers
    (router, test) translate to a 422.
    """
    if "|" not in cursor:
        raise ValueError(f"cursor {cursor!r} is not a valid pending-queue cursor")
    iso, claim_id = cursor.split("|", 1)
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            f"cursor {cursor!r} carries malformed timestamp {iso!r}"
        ) from exc
    if ts.tzinfo is None:
        raise ValueError(
            f"cursor {cursor!r} carries naive timestamp; must be UTC-aware"
        )
    if not claim_id:
        raise ValueError(f"cursor {cursor!r} carries empty claim id")
    return ts, claim_id


def list_pending(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claimant_user_id: str | None = None,
    property_id: str | None = None,
    category: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> tuple[list[ExpenseClaimView], str | None]:
    """Return submitted claims awaiting approval, newest first.

    Authorisation: requires ``expenses.approve`` (workspace-scoped).

    Filters (all optional, ANDed together):
    * ``claimant_user_id`` — only claims whose bound engagement has
      ``user_id == claimant_user_id``. The join rides the same
      ``WorkEngagement`` predicate
      :func:`app.domain.expenses.claims.list_for_user` uses.
    * ``property_id`` — only claims pinned to ``property_id``. Soft-
      ref equality; no join.
    * ``category`` — exact match on the claim's category enum.

    Order: ``submitted_at DESC`` — the manager's queue surfaces the
    newest claim first. ``id DESC`` is the tiebreaker (ULID-monotonic
    means same-millisecond submissions still order deterministically).

    Cursor: opaque ``"<submitted_at-iso>|<id>"`` pair encoded by
    :func:`_encode_pending_cursor`. The HTTP layer wraps this in the
    base64 envelope from :mod:`app.api.pagination`.

    Soft-deleted claims are excluded; ``submitted_at IS NULL`` rows
    (only ``draft`` claims, by invariant) are excluded by the
    ``state='submitted'`` filter. ``limit`` is clamped to ``[1, 500]``
    matching the worker-side service.
    """
    _require_approval(session, ctx)

    bounded_limit = max(1, min(limit, 500))

    stmt = select(ExpenseClaim).where(
        ExpenseClaim.workspace_id == ctx.workspace_id,
        ExpenseClaim.state == "submitted",
        ExpenseClaim.deleted_at.is_(None),
    )
    if claimant_user_id is not None:
        stmt = stmt.join(
            WorkEngagement,
            (WorkEngagement.id == ExpenseClaim.work_engagement_id)
            & (WorkEngagement.workspace_id == ExpenseClaim.workspace_id),
        ).where(WorkEngagement.user_id == claimant_user_id)
    if property_id is not None:
        stmt = stmt.where(ExpenseClaim.property_id == property_id)
    if category is not None:
        stmt = stmt.where(ExpenseClaim.category == category)

    if cursor is not None:
        cursor_ts, cursor_id = _decode_pending_cursor(cursor)
        # Forward-cursor: rows strictly *after* the supplied position
        # under the (submitted_at DESC, id DESC) ordering. "After"
        # under a DESC sort means "less than" — older / smaller-ULID.
        # The composite predicate splits on equality so two same-
        # millisecond submissions break the tie via id alone.
        stmt = stmt.where(
            (ExpenseClaim.submitted_at < cursor_ts)
            | ((ExpenseClaim.submitted_at == cursor_ts) & (ExpenseClaim.id < cursor_id))
        )

    stmt = stmt.order_by(
        ExpenseClaim.submitted_at.desc(),
        ExpenseClaim.id.desc(),
    ).limit(bounded_limit + 1)

    rows = list(session.scalars(stmt).all())
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    next_cursor: str | None
    if has_more and rows:
        last = rows[-1]
        if last.submitted_at is None:  # pragma: no cover - state filter excludes
            raise RuntimeError(
                f"submitted claim {last.id!r} has no submitted_at — "
                "schema invariant broken"
            )
        next_cursor = _encode_pending_cursor(
            submitted_at=last.submitted_at, claim_id=last.id
        )
    else:
        next_cursor = None
    return [_row_to_view(session, r) for r in rows], next_cursor
