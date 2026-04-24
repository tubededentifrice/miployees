"""Expenses context router — claim CRUD, attachments, manager approval flow.

Mounted by the app factory under ``/w/<slug>/api/v1/expenses``. The
HTTP layer here is a thin shell over the already-shipped domain
services:

* :mod:`app.domain.expenses.claims` — worker-side CRUD + the
  ``draft -> submitted`` / cancel state machine + attachment writes.
* :mod:`app.domain.expenses.approval` — manager-side
  ``submitted -> approved -> reimbursed`` / ``submitted -> rejected``
  transitions plus the pending-queue read.

Surface (spec §12 "Time, payroll, expenses"):

**Worker self-service**

* ``POST   /``           — create a draft claim bound to the caller's
  engagement.
* ``GET    /``           — caller's own claims (or another user's when
  the caller holds ``expenses.approve``); cursor-paginated with the
  spec §12 ``{data, next_cursor, has_more}`` envelope.
* ``GET    /{id}``       — read; 404 cross-tenant.
* ``PATCH  /{id}``       — partial update, draft-only (409
  ``claim_not_editable`` past draft).
* ``DELETE /{id}``       — cancel; ``draft`` soft-deletes,
  ``submitted`` flips to ``rejected`` with a "cancelled by
  requester" note (see :func:`~app.domain.expenses.claims.cancel_claim`).
* ``POST   /{id}/submit`` — draft → submitted.
* ``POST   /{id}/attachments`` — attach a previously-uploaded blob
  (the bytes flow through ``POST /uploads`` first; this endpoint
  registers the row).
* ``DELETE /{id}/attachments/{attachment_id}`` — detach while draft.
* ``GET    /{id}/attachments`` — list every attachment on a claim
  (mirror of the inline ``ExpenseClaimPayload.attachments`` field
  for clients that prefer the dedicated GET).

**Manager**

* ``POST /{id}/approve`` — body ``ApprovalEdits | null`` for inline
  edits.
* ``POST /{id}/reject``  — body ``RejectBody`` (non-empty
  ``reason_md``).
* ``POST /{id}/reimburse`` — body ``ReimburseBody`` (channel +
  optional ``paid_at``).
* ``GET  /pending``      — manager queue, cursor-paginated.

**OCR autofill (placeholder)**

* ``POST /autofill`` — returns 501 ``autofill_not_implemented``
  until cd-95zb wires the OCR pipeline.

The flat shape mirrors spec §12's "Time, payroll, expenses" REST
table verbatim — ``POST /expenses``, ``GET /expenses``, ``POST
/expenses/{id}/submit``, etc. — so the SPA's ``fetchJson("/api/v1/
expenses")`` and ``fetchJson("/api/v1/expenses/" + id + "/" +
decision)`` calls match the router 1:1. ``/pending`` and
``/autofill`` are sibling literal segments under the same prefix;
``/pending`` is registered before ``/{claim_id}`` so FastAPI's
ordered route table matches the literal first.

**Idempotency.** ``POST`` routes ride the process-wide
:mod:`app.api.middleware.idempotency` middleware — a replayed POST
returns the original response with ``Idempotency-Replay: true``,
a mismatched body under the same key returns 409
``idempotency_conflict``. No per-route plumbing.

**Storage.** :func:`~app.domain.expenses.claims.attach_receipt`
verifies the asserted ``blob_hash`` exists in the configured
:class:`~app.adapters.storage.ports.Storage` backend. The router
injects it via :func:`app.api.deps.get_storage` so test overrides
work the same way as the avatar endpoints.

See ``docs/specs/12-rest-api.md`` §"Time, payroll, expenses",
``docs/specs/09-time-payroll-expenses.md`` §"Expense claims",
``docs/specs/02-domain-model.md`` §"expense_claim" /
§"expense_attachment".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.storage.ports import Storage
from app.api.deps import current_workspace_context, db_session, get_storage
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    encode_cursor,
)
from app.domain.expenses import (
    ApprovalEdits,
    ApprovalPermissionDenied,
    BlobMimeNotAllowed,
    BlobMissing,
    BlobTooLarge,
    ClaimNotApprovable,
    ClaimNotEditable,
    ClaimNotFound,
    ClaimNotReimbursable,
    ClaimPermissionDenied,
    ClaimStateTransitionInvalid,
    CurrencyInvalid,
    ExpenseAttachmentView,
    ExpenseClaimCreate,
    ExpenseClaimUpdate,
    ExpenseClaimView,
    ExpenseState,
    PurchaseDateInFuture,
    ReceiptKind,
    ReimburseBody,
    ReimbursePermissionDenied,
    RejectBody,
    TooManyAttachments,
    approve_claim,
    attach_receipt,
    cancel_claim,
    create_claim,
    detach_receipt,
    get_claim,
    list_for_user,
    list_pending,
    mark_reimbursed,
    reject_claim,
    submit_claim,
    update_claim,
)
from app.tenancy import WorkspaceContext

__all__ = ["router"]


router = APIRouter(tags=["expenses"])


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Storage = Annotated[Storage, Depends(get_storage)]


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------
#
# Every router response is a named Pydantic model so the OpenAPI
# generator emits stable component schemas the SPA can pattern-match
# on. Each shape mirrors the corresponding domain view exactly — no
# derived fields, no filtering — so a round-tripping client (POST →
# echo back on PATCH) does not need a reshape step.


class ExpenseAttachmentPayload(BaseModel):
    """HTTP projection of :class:`ExpenseAttachmentView`."""

    id: str
    claim_id: str
    blob_hash: str
    kind: str
    pages: int | None
    created_at: datetime

    @classmethod
    def from_view(cls, view: ExpenseAttachmentView) -> ExpenseAttachmentPayload:
        """Copy a :class:`ExpenseAttachmentView` into its HTTP payload."""
        return cls(
            id=view.id,
            claim_id=view.claim_id,
            blob_hash=view.blob_hash,
            kind=view.kind,
            pages=view.pages,
            created_at=view.created_at,
        )


class ExpenseClaimPayload(BaseModel):
    """HTTP projection of :class:`ExpenseClaimView`.

    ``attachments`` is rendered inline so a single GET surfaces every
    receipt the SPA needs to draw the claim card. The dedicated
    ``GET /{id}/attachments`` endpoint returns the same list for
    clients that prefer the smaller payload.
    """

    id: str
    workspace_id: str
    work_engagement_id: str
    vendor: str
    purchased_at: datetime
    currency: str
    total_amount_cents: int
    category: str
    property_id: str | None
    note_md: str
    state: str
    submitted_at: datetime | None
    decided_by: str | None
    decided_at: datetime | None
    decision_note_md: str | None
    created_at: datetime
    deleted_at: datetime | None
    attachments: list[ExpenseAttachmentPayload]

    @classmethod
    def from_view(cls, view: ExpenseClaimView) -> ExpenseClaimPayload:
        """Copy a :class:`ExpenseClaimView` into its HTTP payload."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            work_engagement_id=view.work_engagement_id,
            vendor=view.vendor,
            purchased_at=view.purchased_at,
            currency=view.currency,
            total_amount_cents=view.total_amount_cents,
            category=view.category,
            property_id=view.property_id,
            note_md=view.note_md,
            state=view.state,
            submitted_at=view.submitted_at,
            decided_by=view.decided_by,
            decided_at=view.decided_at,
            decision_note_md=view.decision_note_md,
            created_at=view.created_at,
            deleted_at=view.deleted_at,
            attachments=[
                ExpenseAttachmentPayload.from_view(a) for a in view.attachments
            ],
        )


class ExpenseClaimListResponse(BaseModel):
    """Collection envelope for ``GET /``."""

    data: list[ExpenseClaimPayload]
    next_cursor: str | None = None
    has_more: bool = False


class ExpenseClaimPendingListResponse(BaseModel):
    """Collection envelope for ``GET /pending``."""

    data: list[ExpenseClaimPayload]
    next_cursor: str | None = None
    has_more: bool = False


class ExpenseAttachmentListResponse(BaseModel):
    """Collection envelope for ``GET /{id}/attachments``."""

    data: list[ExpenseAttachmentPayload]


# ---------------------------------------------------------------------------
# Request shapes (router-local — domain DTOs handle the rest)
# ---------------------------------------------------------------------------


class AttachReceiptRequest(BaseModel):
    """Body for ``POST /{id}/attachments``.

    Wraps the asserted blob metadata the caller already pushed
    through ``POST /uploads``: the content hash, mime, size, and
    optional ``kind`` / ``pages`` markers. The domain service
    revalidates everything against the receipt allow-list + 10 MB
    cap; this DTO is only the boundary contract.
    """

    model_config = ConfigDict(extra="forbid")

    # SHA-256 hex — 64 ASCII chars. The domain service re-checks this
    # length but pinning it here surfaces typos at the 422 boundary
    # rather than a more confusing service-layer 422.
    blob_hash: str = Field(min_length=64, max_length=64)
    content_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=0)
    kind: ReceiptKind = "receipt"
    pages: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------
#
# Domain exceptions → HTTP envelopes. Centralising the table here
# keeps every route returning the same ``{"error": "<code>", ...}``
# shape for the same domain type — the SPA / CLI switches on
# ``body.detail.error`` without parsing the status code. A new
# error class added without a row here falls through to the 500
# branch, which is loud enough to catch in CI.


def _http(status_code: int, error: str, **extra: object) -> HTTPException:
    """Construct the ``{"error": "<code>", ...}`` detail envelope."""
    detail: dict[str, object] = {"error": error}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def _http_for_claim_error(exc: Exception) -> HTTPException:
    """Map a domain exception (CRUD + attachments + approval) to HTTP.

    Single mapping covers every route in this router so a new error
    class can land in one place. The 4xx codes follow §12 "Errors":

    * 404 ``claim_not_found`` — :class:`ClaimNotFound` (cross-tenant
      probes already collapse to this in the service via ``_load_row``).
    * 409 ``claim_not_editable`` / ``claim_not_approvable`` /
      ``claim_not_reimbursable`` / ``claim_state_transition_invalid``
      — wrong state for the requested verb.
    * 403 ``claim_permission_denied`` / ``approval_permission_denied``
      / ``reimburse_permission_denied`` — distinct codes so the SPA
      can surface the right "you can't do this" message without
      remembering which capability gate fired.
    * 422 ``currency_invalid`` / ``blob_missing`` /
      ``blob_mime_not_allowed`` / ``blob_too_large`` /
      ``too_many_attachments`` / ``purchase_date_in_future`` —
      payload-level validation that lives below the Pydantic boundary
      (e.g. currency allow-list, storage existence).
    """
    if isinstance(exc, ClaimNotFound):
        return _http(status.HTTP_404_NOT_FOUND, "claim_not_found")
    if isinstance(exc, ClaimNotEditable):
        return _http(status.HTTP_409_CONFLICT, "claim_not_editable", message=str(exc))
    if isinstance(exc, ClaimNotApprovable):
        return _http(status.HTTP_409_CONFLICT, "claim_not_approvable", message=str(exc))
    if isinstance(exc, ClaimNotReimbursable):
        return _http(
            status.HTTP_409_CONFLICT, "claim_not_reimbursable", message=str(exc)
        )
    if isinstance(exc, ClaimStateTransitionInvalid):
        return _http(
            status.HTTP_409_CONFLICT,
            "claim_state_transition_invalid",
            message=str(exc),
        )
    if isinstance(exc, ApprovalPermissionDenied):
        return _http(status.HTTP_403_FORBIDDEN, "approval_permission_denied")
    if isinstance(exc, ReimbursePermissionDenied):
        return _http(status.HTTP_403_FORBIDDEN, "reimburse_permission_denied")
    if isinstance(exc, ClaimPermissionDenied):
        return _http(status.HTTP_403_FORBIDDEN, "claim_permission_denied")
    if isinstance(exc, CurrencyInvalid):
        return _http(422, "currency_invalid", message=str(exc))
    if isinstance(exc, BlobMissing):
        return _http(422, "blob_missing", message=str(exc))
    if isinstance(exc, BlobMimeNotAllowed):
        return _http(422, "blob_mime_not_allowed", message=str(exc))
    if isinstance(exc, BlobTooLarge):
        return _http(422, "blob_too_large", message=str(exc))
    if isinstance(exc, TooManyAttachments):
        return _http(422, "too_many_attachments", message=str(exc))
    if isinstance(exc, PurchaseDateInFuture):
        return _http(422, "purchase_date_in_future", message=str(exc))
    return _http(500, "internal")


# ---------------------------------------------------------------------------
# State filter helper
# ---------------------------------------------------------------------------


def _validate_state_filter(value: str | None) -> ExpenseState | None:
    """Narrow a query-string ``state`` to the :data:`ExpenseState` literal.

    ``None`` (omitted query param) passes through. An out-of-set value
    surfaces a 422 ``invalid_state`` with the allow-list so a tampered
    query doesn't fail open via an empty result.
    """
    if value is None:
        return None
    allowed: tuple[ExpenseState, ...] = (
        "draft",
        "submitted",
        "approved",
        "rejected",
        "reimbursed",
    )
    if value not in allowed:
        raise _http(422, "invalid_state", allowed=list(allowed))
    # The membership check above narrows ``value`` to the literal set,
    # but mypy can't see that through the runtime tuple — re-narrow
    # via the explicit ladder so we stay strict-clean.
    if value == "draft":
        return "draft"
    if value == "submitted":
        return "submitted"
    if value == "approved":
        return "approved"
    if value == "rejected":
        return "rejected"
    return "reimbursed"


# ---------------------------------------------------------------------------
# Worker self-service routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ExpenseClaimPayload,
    operation_id="create_expense_claim",
    summary="Create a draft expense claim",
)
def create_expense_claim_route(
    body: ExpenseClaimCreate,
    ctx: _Ctx,
    session: _Db,
) -> ExpenseClaimPayload:
    """Insert a draft claim bound to the caller's engagement."""
    try:
        view = create_claim(session, ctx, body=body)
    except (
        ClaimPermissionDenied,
        CurrencyInvalid,
        PurchaseDateInFuture,
    ) as exc:
        raise _http_for_claim_error(exc) from exc
    return ExpenseClaimPayload.from_view(view)


@router.get(
    "",
    response_model=ExpenseClaimListResponse,
    operation_id="list_expense_claims",
    summary="List claims for the caller (or another user with the cap)",
)
def list_expense_claims_route(
    ctx: _Ctx,
    session: _Db,
    user_id: Annotated[str | None, Query(max_length=64)] = None,
    state: Annotated[str | None, Query(max_length=32)] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> ExpenseClaimListResponse:
    """Return a cursor-paginated page of claims.

    ``user_id`` defaults to the caller; targeting another user
    requires ``expenses.approve`` (the service raises
    :class:`ClaimPermissionDenied`, which the router translates to
    403). ``state`` filters by the lifecycle state. The cursor is
    the last-returned claim's id encoded via
    :func:`app.api.pagination.encode_cursor`; the underlying service
    sorts by ``id DESC`` so "next page" means "older claim".
    """
    state_literal = _validate_state_filter(state)
    after_id = decode_cursor(cursor)
    try:
        rows, next_raw = list_for_user(
            session,
            ctx,
            user_id=user_id,
            state=state_literal,
            limit=limit,
            cursor=after_id,
        )
    except ClaimPermissionDenied as exc:
        raise _http_for_claim_error(exc) from exc
    return ExpenseClaimListResponse(
        data=[ExpenseClaimPayload.from_view(v) for v in rows],
        next_cursor=encode_cursor(next_raw) if next_raw is not None else None,
        has_more=next_raw is not None,
    )


@router.get(
    "/pending",
    response_model=ExpenseClaimPendingListResponse,
    operation_id="list_pending_expense_claims",
    summary="Manager queue — submitted claims awaiting decision",
)
def list_pending_expense_claims_route(
    ctx: _Ctx,
    session: _Db,
    claimant_user_id: Annotated[str | None, Query(max_length=64)] = None,
    property_id: Annotated[str | None, Query(max_length=64)] = None,
    category: Annotated[str | None, Query(max_length=32)] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> ExpenseClaimPendingListResponse:
    """Return submitted-but-not-decided claims, newest first.

    Mounted BEFORE ``GET /{claim_id}`` so FastAPI matches the literal
    ``/pending`` segment before the path-parameter route. The service
    requires ``expenses.approve``; the router surfaces the 403
    envelope on a worker probe.

    The cursor is the opaque domain-side ``"<submitted_at-iso>|<id>"``
    pair from :func:`~app.domain.expenses.approval._encode_pending_cursor`,
    wrapped in the standard base64 envelope from
    :mod:`app.api.pagination`.
    """
    raw_cursor = decode_cursor(cursor)
    try:
        rows, next_raw = list_pending(
            session,
            ctx,
            claimant_user_id=claimant_user_id,
            property_id=property_id,
            category=category,
            limit=limit,
            cursor=raw_cursor,
        )
    except ApprovalPermissionDenied as exc:
        raise _http_for_claim_error(exc) from exc
    except ValueError as exc:
        # Malformed pending cursor (the domain decoder raises
        # plain ``ValueError`` — the cursor is opaque to callers,
        # so a bad shape is a tampered query).
        raise _http(422, "invalid_cursor", message=str(exc)) from exc
    return ExpenseClaimPendingListResponse(
        data=[ExpenseClaimPayload.from_view(v) for v in rows],
        next_cursor=encode_cursor(next_raw) if next_raw is not None else None,
        has_more=next_raw is not None,
    )


@router.get(
    "/{claim_id}",
    response_model=ExpenseClaimPayload,
    operation_id="get_expense_claim",
    summary="Read a single expense claim",
)
def get_expense_claim_route(
    claim_id: str,
    ctx: _Ctx,
    session: _Db,
) -> ExpenseClaimPayload:
    """Return the claim identified by ``claim_id`` or 404."""
    try:
        view = get_claim(session, ctx, claim_id=claim_id)
    except (ClaimNotFound, ClaimPermissionDenied) as exc:
        raise _http_for_claim_error(exc) from exc
    return ExpenseClaimPayload.from_view(view)


@router.patch(
    "/{claim_id}",
    response_model=ExpenseClaimPayload,
    operation_id="patch_expense_claim",
    summary="Partial update of a draft expense claim",
)
def patch_expense_claim_route(
    claim_id: str,
    body: ExpenseClaimUpdate,
    ctx: _Ctx,
    session: _Db,
) -> ExpenseClaimPayload:
    """Rewrite the fields the caller supplied; draft-only.

    ``ExpenseClaimUpdate`` is all-optional; the service consumes
    ``model_dump(exclude_unset=True)`` so an omitted key is left
    untouched. The 409 ``claim_not_editable`` envelope fires when
    the caller PATCHes anything past the draft state.
    """
    try:
        view = update_claim(session, ctx, claim_id=claim_id, body=body)
    except (
        ClaimNotFound,
        ClaimNotEditable,
        ClaimPermissionDenied,
        CurrencyInvalid,
        PurchaseDateInFuture,
    ) as exc:
        raise _http_for_claim_error(exc) from exc
    return ExpenseClaimPayload.from_view(view)


@router.delete(
    "/{claim_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="cancel_expense_claim",
    summary="Cancel a claim (soft-delete draft, reject submitted)",
)
def cancel_expense_claim_route(
    claim_id: str,
    ctx: _Ctx,
    session: _Db,
) -> Response:
    """Cancel the claim per the worker-side state machine.

    See :func:`~app.domain.expenses.claims.cancel_claim` for the
    transition table. Returns 204 with no body — the spec §12
    DELETE convention.
    """
    try:
        cancel_claim(session, ctx, claim_id=claim_id)
    except (
        ClaimNotFound,
        ClaimPermissionDenied,
        ClaimStateTransitionInvalid,
    ) as exc:
        raise _http_for_claim_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{claim_id}/submit",
    response_model=ExpenseClaimPayload,
    operation_id="submit_expense_claim",
    summary="Transition a draft claim to submitted",
)
def submit_expense_claim_route(
    claim_id: str,
    ctx: _Ctx,
    session: _Db,
) -> ExpenseClaimPayload:
    """Drive the claim from ``draft`` to ``submitted``.

    409 ``claim_state_transition_invalid`` fires when the claim is
    not in ``draft``; the SPA should refresh its view if a worker
    thinks a previous submit was lost.
    """
    try:
        view = submit_claim(session, ctx, claim_id=claim_id)
    except (
        ClaimNotFound,
        ClaimPermissionDenied,
        ClaimStateTransitionInvalid,
    ) as exc:
        raise _http_for_claim_error(exc) from exc
    return ExpenseClaimPayload.from_view(view)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@router.post(
    "/{claim_id}/attachments",
    status_code=status.HTTP_201_CREATED,
    response_model=ExpenseAttachmentPayload,
    operation_id="attach_expense_receipt",
    summary="Register a previously-uploaded blob as a receipt",
)
def attach_expense_receipt_route(
    claim_id: str,
    body: AttachReceiptRequest,
    ctx: _Ctx,
    session: _Db,
    storage: _Storage,
) -> ExpenseAttachmentPayload:
    """Attach a blob to a draft claim.

    The bytes flow through ``POST /uploads`` first; this endpoint
    only registers the metadata row. The service revalidates the
    asserted mime / size against the receipt allow-list + 10 MB cap
    and confirms ``storage.exists(blob_hash)``.
    """
    try:
        view = attach_receipt(
            session,
            ctx,
            claim_id=claim_id,
            blob_hash=body.blob_hash,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            storage=storage,
            kind=body.kind,
            pages=body.pages,
        )
    except (
        ClaimNotFound,
        ClaimNotEditable,
        ClaimPermissionDenied,
        BlobMissing,
        BlobMimeNotAllowed,
        BlobTooLarge,
        TooManyAttachments,
    ) as exc:
        raise _http_for_claim_error(exc) from exc
    return ExpenseAttachmentPayload.from_view(view)


@router.delete(
    "/{claim_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    operation_id="detach_expense_receipt",
    summary="Remove an attachment from a draft claim",
)
def detach_expense_receipt_route(
    claim_id: str,
    attachment_id: str,
    ctx: _Ctx,
    session: _Db,
) -> Response:
    """Detach a receipt; only valid while the claim is in draft.

    The blob itself is NOT deleted from storage — see the
    :func:`~app.domain.expenses.claims.detach_receipt` docstring for
    the GC rationale.
    """
    try:
        detach_receipt(
            session,
            ctx,
            claim_id=claim_id,
            attachment_id=attachment_id,
        )
    except (
        ClaimNotFound,
        ClaimNotEditable,
        ClaimPermissionDenied,
    ) as exc:
        raise _http_for_claim_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{claim_id}/attachments",
    response_model=ExpenseAttachmentListResponse,
    operation_id="list_expense_attachments",
    summary="List receipts attached to a claim",
)
def list_expense_attachments_route(
    claim_id: str,
    ctx: _Ctx,
    session: _Db,
) -> ExpenseAttachmentListResponse:
    """Return every attachment on the claim.

    Mirror of the inline ``ExpenseClaimPayload.attachments`` list —
    the dedicated endpoint exists for clients that prefer the smaller
    payload (a re-render after an attach / detach round-trip doesn't
    need the full claim shape). Reads run through
    :func:`~app.domain.expenses.claims.get_claim` so the same authz
    and 404 path applies.
    """
    try:
        view = get_claim(session, ctx, claim_id=claim_id)
    except (ClaimNotFound, ClaimPermissionDenied) as exc:
        raise _http_for_claim_error(exc) from exc
    return ExpenseAttachmentListResponse(
        data=[ExpenseAttachmentPayload.from_view(a) for a in view.attachments]
    )


# ---------------------------------------------------------------------------
# Manager actions
# ---------------------------------------------------------------------------


@router.post(
    "/{claim_id}/approve",
    response_model=ExpenseClaimPayload,
    operation_id="approve_expense_claim",
    summary="Approve a submitted claim (optional inline edits)",
)
def approve_expense_claim_route(
    claim_id: str,
    ctx: _Ctx,
    session: _Db,
    body: ApprovalEdits | None = None,
) -> ExpenseClaimPayload:
    """Drive the claim from ``submitted`` to ``approved``.

    The body is optional — ``null`` / omitted means "approve as-is".
    A non-null body supplies inline edits the approver wants applied
    in the same transition; see
    :func:`~app.domain.expenses.approval.approve_claim` for the
    field-level rules.
    """
    try:
        view = approve_claim(session, ctx, claim_id=claim_id, edits=body)
    except (
        ClaimNotFound,
        ClaimNotApprovable,
        ApprovalPermissionDenied,
        CurrencyInvalid,
        PurchaseDateInFuture,
    ) as exc:
        raise _http_for_claim_error(exc) from exc
    return ExpenseClaimPayload.from_view(view)


@router.post(
    "/{claim_id}/reject",
    response_model=ExpenseClaimPayload,
    operation_id="reject_expense_claim",
    summary="Reject a submitted claim with a non-empty reason",
)
def reject_expense_claim_route(
    claim_id: str,
    body: RejectBody,
    ctx: _Ctx,
    session: _Db,
) -> ExpenseClaimPayload:
    """Drive the claim from ``submitted`` to ``rejected``.

    Empty / whitespace-only ``reason_md`` is rejected with 422 by
    the DTO's ``min_length=1`` rule; the service-level guard fires
    for Python callers bypassing the DTO.
    """
    try:
        view = reject_claim(session, ctx, claim_id=claim_id, reason_md=body.reason_md)
    except (
        ClaimNotFound,
        ClaimNotApprovable,
        ApprovalPermissionDenied,
    ) as exc:
        raise _http_for_claim_error(exc) from exc
    except ValueError as exc:
        # Defence-in-depth — an empty reason_md surfaces here when
        # the DTO is bypassed. 422 stays consistent with the DTO
        # path.
        raise _http(422, "reject_reason_required", message=str(exc)) from exc
    return ExpenseClaimPayload.from_view(view)


@router.post(
    "/{claim_id}/reimburse",
    response_model=ExpenseClaimPayload,
    operation_id="reimburse_expense_claim",
    summary="Mark an approved claim as reimbursed",
)
def reimburse_expense_claim_route(
    claim_id: str,
    body: ReimburseBody,
    ctx: _Ctx,
    session: _Db,
) -> ExpenseClaimPayload:
    """Drive the claim from ``approved`` to ``reimbursed``.

    ``body.via`` records the channel used; ``body.paid_at`` defaults
    to the server clock at transition time. A future ``paid_at``
    surfaces as 422 via the service-level skew guard (vanilla
    :class:`ValueError`).
    """
    try:
        view = mark_reimbursed(session, ctx, claim_id=claim_id, body=body)
    except (
        ClaimNotFound,
        ClaimNotReimbursable,
        ReimbursePermissionDenied,
    ) as exc:
        raise _http_for_claim_error(exc) from exc
    except ValueError as exc:
        # Future-paid_at guard raises plain ValueError; map to 422
        # so callers see a structured envelope instead of a 500.
        raise _http(422, "paid_at_in_future", message=str(exc)) from exc
    return ExpenseClaimPayload.from_view(view)


# ---------------------------------------------------------------------------
# OCR autofill placeholder (cd-95zb)
# ---------------------------------------------------------------------------


@router.post(
    "/autofill",
    operation_id="autofill_expense_claim",
    summary="OCR-autofill an expense claim from receipt images (placeholder)",
)
def autofill_expense_claim_route() -> None:
    """Return 501 until cd-95zb wires the OCR pipeline.

    The mocks already chain "upload receipt" → "autofill claim" via
    this URL; the placeholder gives them a stable target while the
    real implementation lands. No body is consumed today — the
    follow-up will accept ``multipart/form-data`` per spec §12
    "Time, payroll, expenses".
    """
    raise _http(
        status.HTTP_501_NOT_IMPLEMENTED,
        "autofill_not_implemented",
        message=(
            "OCR-autofill is wired in cd-95zb; until then upload receipts "
            "via POST /expenses/{id}/attachments and fill the fields manually."
        ),
    )
