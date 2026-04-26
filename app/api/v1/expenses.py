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
* ``GET  /pending_reimbursement`` — approved-but-not-reimbursed totals
  (§09 §"Amount owed to the employee"). ``?user_id=me`` resolves to
  the caller; ``?user_id=<uuid>`` or no param requires
  ``expenses.approve``. The no-param form populates a per-user
  ``by_user`` breakdown for the manager Pay page; the per-user form
  leaves it ``null``.

**OCR scan**

* ``POST /scan`` — multipart receipt image in, structured
  ``ExpenseScanResult`` out (each parsed field as
  ``{value, confidence}`` plus an optional ``agent_question``).
  503 ``scan_not_configured`` when the deployment has no OCR
  model wired (``settings.llm_ocr_model`` unset).

The flat shape mirrors spec §12's "Time, payroll, expenses" REST
table verbatim — ``POST /expenses``, ``GET /expenses``, ``POST
/expenses/{id}/submit``, etc. — so the SPA's ``fetchJson("/api/v1/
expenses")`` and ``fetchJson("/api/v1/expenses/" + id + "/" +
decision)`` calls match the router 1:1. ``/pending``,
``/pending_reimbursement``, and ``/scan`` are sibling literal
segments under the same prefix; each is registered before
``/{claim_id}`` so FastAPI's ordered route table matches the
literal first.

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

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.db.expenses.repositories import (
    SqlAlchemyCapabilityChecker,
    SqlAlchemyExpensesRepository,
)
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.llm.ports import LLMClient
from app.adapters.storage.ports import Storage
from app.api.deps import current_workspace_context, db_session, get_llm, get_storage
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    encode_cursor,
)
from app.config import Settings, get_settings
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
    PendingReimbursementUserBreakdown,
    PendingReimbursementView,
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
    pending_reimbursement,
    reject_claim,
    submit_claim,
    update_claim,
)
from app.domain.expenses.autofill import (
    AUTOFILL_CAPABILITY,
    ExtractionMetrics,
    ExtractionParseError,
    ExtractionProviderError,
    ExtractionRateLimited,
    ExtractionTimeout,
    ReceiptExtraction,
    extract_from_bytes,
)
from app.tenancy import WorkspaceContext
from app.util.clock import SystemClock
from app.util.ulid import new_ulid

__all__ = ["router"]


router = APIRouter(tags=["expenses"])


def _optional_llm(request: Request) -> LLMClient | None:
    """Like :func:`app.api.deps.get_llm` but returns ``None`` instead of 503.

    Used by the attach route, where the LLM is optional — autofill
    is a "best effort" extra rather than a hard requirement, so a
    deployment without an LLM still serves attaches normally.

    See :func:`app.api.deps.get_llm` for the strict variant the
    preview ``POST /expenses/scan`` endpoint uses.
    """
    llm: LLMClient | None = getattr(request.app.state, "llm", None)
    return llm


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_Storage = Annotated[Storage, Depends(get_storage)]
_Llm = Annotated[LLMClient, Depends(get_llm)]
_AppSettings = Annotated[Settings, Depends(get_settings)]


def make_seam_pair(
    session: Session, ctx: WorkspaceContext
) -> tuple[SqlAlchemyExpensesRepository, SqlAlchemyCapabilityChecker]:
    """Construct the SA-backed expenses-context seam pair for a request.

    Both seams (cd-v3jp) wrap the same ``(session, ctx)`` pair the rest
    of the route would otherwise pass through to the service. Bundling
    them in one helper keeps every endpoint's wiring to a single line
    and pins the cross-seam contract: the audit writer rides
    ``repo.session`` (same UoW), and the checker honours
    ``ctx.workspace_id`` for every action key the service touches.
    Mirrors :func:`app.api.v1.user_leaves.make_seam_pair`.
    """
    return (
        SqlAlchemyExpensesRepository(session),
        SqlAlchemyCapabilityChecker(session, ctx),
    )


# Mime types accepted by ``POST /expenses/scan``. Mirrors the
# attach allow-list (image/jpeg / png / webp / heic / pdf) so the
# preview endpoint and the actual attach path agree on what counts
# as a receipt. PDF is included because vendors mail invoice PDFs;
# the LLM port handles the multi-page case via its OCR step.
_SCAN_ALLOWED_MIME: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"}
)
# Cap on a single preview upload — matches the attach-side cap so a
# caller can't smuggle a 50 MB image through the preview endpoint to
# burn LLM tokens.
_SCAN_MAX_BYTES: int = 10 * 1024 * 1024


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
# Pending-reimbursement (cd-mh4p)
# ---------------------------------------------------------------------------


class CurrencyTotalPayload(BaseModel):
    """One ``(currency, amount_cents)`` total in the pending pool.

    Mirrors :class:`~app.domain.expenses.claims.CurrencyTotal` 1:1.
    """

    currency: str
    amount_cents: int


class PendingReimbursementUserBreakdownPayload(BaseModel):
    """One row of the workspace-wide pending-reimbursement breakdown.

    Mirrors
    :class:`~app.domain.expenses.claims.PendingReimbursementUserBreakdown`
    1:1; the SPA's ``PendingReimbursementUserBreakdown`` type
    (``app/web/src/types/expense.ts``) lines up field-for-field.
    """

    user_id: str
    user_name: str
    totals_by_currency: list[CurrencyTotalPayload]


class PendingReimbursementResponse(BaseModel):
    """Wire shape for ``GET /pending_reimbursement``.

    Mirrors :class:`~app.domain.expenses.claims.PendingReimbursementView`.
    ``by_user`` is non-null only on the workspace-wide aggregate
    response (the per-user form sets it to ``None``); the SPA's
    ``PendingReimbursement`` type tracks the same nullability.
    """

    user_id: str | None
    claims: list[ExpenseClaimPayload]
    totals_by_currency: list[CurrencyTotalPayload]
    by_user: list[PendingReimbursementUserBreakdownPayload] | None = None

    @classmethod
    def from_view(cls, view: PendingReimbursementView) -> PendingReimbursementResponse:
        """Project a :class:`PendingReimbursementView` into its HTTP payload."""
        by_user_payload: list[PendingReimbursementUserBreakdownPayload] | None
        if view.by_user is None:
            by_user_payload = None
        else:
            by_user_payload = [_user_breakdown_payload(b) for b in view.by_user]
        return cls(
            user_id=view.user_id,
            claims=[ExpenseClaimPayload.from_view(c) for c in view.claims],
            totals_by_currency=[
                CurrencyTotalPayload(currency=t.currency, amount_cents=t.amount_cents)
                for t in view.totals_by_currency
            ],
            by_user=by_user_payload,
        )


def _user_breakdown_payload(
    breakdown: PendingReimbursementUserBreakdown,
) -> PendingReimbursementUserBreakdownPayload:
    """Project a domain breakdown row into its HTTP payload."""
    return PendingReimbursementUserBreakdownPayload(
        user_id=breakdown.user_id,
        user_name=breakdown.user_name,
        totals_by_currency=[
            CurrencyTotalPayload(currency=t.currency, amount_cents=t.amount_cents)
            for t in breakdown.totals_by_currency
        ],
    )


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


def _http(
    status_code: int,
    error: str,
    *,
    headers: dict[str, str] | None = None,
    **extra: object,
) -> HTTPException:
    """Construct the ``{"error": "<code>", ...}`` detail envelope.

    ``headers`` lets retry-aware paths attach ``Retry-After`` (or any
    other response header) without bypassing the standard envelope —
    FastAPI propagates the headers verbatim onto the
    ``problem+json`` response.
    """
    detail: dict[str, object] = {"error": error}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


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
    repo, checker = make_seam_pair(session, ctx)
    try:
        view = create_claim(repo, checker, ctx, body=body)
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
    mine: Annotated[
        bool,
        Query(
            description=(
                "When ``true``, narrow the result to the caller's own "
                "claims (equivalent to ``user_id=<caller>``) without "
                "requiring ``expenses.approve``. Mutually exclusive "
                "with an explicit ``user_id`` — supplying both surfaces "
                "422 ``mine_user_id_conflict``. Defaults to ``false``."
            ),
        ),
    ] = False,
    state: Annotated[str | None, Query(max_length=32)] = None,
    cursor: PageCursorQuery = None,
    limit: LimitQuery = DEFAULT_LIMIT,
) -> ExpenseClaimListResponse:
    """Return a cursor-paginated page of claims.

    ``user_id`` defaults to the caller; targeting another user
    requires ``expenses.approve`` (the service raises
    :class:`ClaimPermissionDenied`, which the router translates to
    403). ``mine=true`` is the explicit "my own claims only" form
    the SPA's worker surface uses (see
    ``app/web/src/pages/employee/expenses/RecentExpenses.tsx``); it
    pins the listing to ``ctx.actor_id`` and skips the manager-cap
    branch so a worker without ``expenses.approve`` always succeeds.
    Combining ``mine=true`` with an explicit ``user_id`` is rejected
    with 422 ``mine_user_id_conflict`` — the two filters answer
    different questions and silently picking one would let a bug
    in the caller leak the wrong listing.

    ``state`` filters by the lifecycle state. The cursor is the
    last-returned claim's id encoded via
    :func:`app.api.pagination.encode_cursor`; the underlying service
    sorts by ``id DESC`` so "next page" means "older claim".
    """
    if mine and user_id is not None:
        raise _http(
            422,
            "mine_user_id_conflict",
            message=(
                "mine=true cannot be combined with an explicit user_id; "
                "drop one of them"
            ),
        )
    target_user_id = ctx.actor_id if mine else user_id
    state_literal = _validate_state_filter(state)
    after_id = decode_cursor(cursor)
    repo, checker = make_seam_pair(session, ctx)
    try:
        rows, next_raw = list_for_user(
            repo,
            checker,
            ctx,
            user_id=target_user_id,
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
    "/pending_reimbursement",
    response_model=PendingReimbursementResponse,
    operation_id="get_pending_reimbursement",
    summary="Pending-reimbursement totals (per-user or workspace aggregate)",
)
def get_pending_reimbursement_route(
    ctx: _Ctx,
    session: _Db,
    user_id: Annotated[
        str | None,
        Query(
            max_length=64,
            description=(
                "Filter target. ``me`` resolves to the caller (no "
                "capability required). A different user id requires "
                "``expenses.approve``. Omit the param entirely to get "
                "the workspace-wide aggregate (also gated on "
                "``expenses.approve``); the response then carries a "
                "``by_user`` breakdown."
            ),
        ),
    ] = None,
) -> PendingReimbursementResponse:
    """Return approved-but-not-yet-reimbursed totals (§09 "Amount owed").

    Sibling literal route to ``/pending``; registered before the
    ``/{claim_id}`` path-parameter route so FastAPI matches the
    literal segment first. The router maps ``user_id=me`` to
    ``ctx.actor_id`` so the SPA's ``OwedToYou`` panel doesn't have
    to know its own id; the manager's ``PayPage`` omits the param
    to get the workspace-wide aggregate (with the ``by_user``
    breakdown populated).

    Authorisation:

    * ``user_id=me`` — every worker may read their own pool.
    * ``user_id=<uuid>`` and ``user_id`` omitted — both gated on
      ``expenses.approve`` (the manager queue capability). A worker
      probe surfaces a 403 ``claim_permission_denied`` envelope.
    """
    resolved_user_id = ctx.actor_id if user_id == "me" else user_id
    repo, checker = make_seam_pair(session, ctx)
    try:
        view = pending_reimbursement(repo, checker, ctx, user_id=resolved_user_id)
    except ClaimPermissionDenied as exc:
        raise _http_for_claim_error(exc) from exc
    return PendingReimbursementResponse.from_view(view)


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
    repo, checker = make_seam_pair(session, ctx)
    try:
        view = get_claim(repo, checker, ctx, claim_id=claim_id)
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
    repo, checker = make_seam_pair(session, ctx)
    try:
        view = update_claim(repo, checker, ctx, claim_id=claim_id, body=body)
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
    repo, checker = make_seam_pair(session, ctx)
    try:
        cancel_claim(repo, checker, ctx, claim_id=claim_id)
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
    repo, checker = make_seam_pair(session, ctx)
    try:
        view = submit_claim(repo, checker, ctx, claim_id=claim_id)
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
    settings: _AppSettings,
    llm: Annotated[LLMClient | None, Depends(_optional_llm)] = None,
) -> ExpenseAttachmentPayload:
    """Attach a blob to a draft claim.

    The bytes flow through ``POST /uploads`` first; this endpoint
    only registers the metadata row. The service revalidates the
    asserted mime / size against the receipt allow-list + 10 MB cap
    and confirms ``storage.exists(blob_hash)``.

    cd-95zb: when ``settings.llm_ocr_model`` is set AND an LLM client
    is wired, the attach pipeline injects the synchronous OCR /
    autofill runner so the first-attachment flow auto-populates the
    claim's worker-typed fields. The runner is gated on both knobs
    so a deployment can disable autofill by clearing either side
    without booting a half-wired state machine. ``llm`` is an
    optional dep so the route still works when no LLM client is
    wired (the runner is just ``None``).
    """
    runner = _build_attach_runner(llm=llm, settings=settings, storage=storage)
    repo, checker = make_seam_pair(session, ctx)
    try:
        view = attach_receipt(
            repo,
            checker,
            ctx,
            claim_id=claim_id,
            blob_hash=body.blob_hash,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
            storage=storage,
            kind=body.kind,
            pages=body.pages,
            extraction_runner=runner,
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


def _build_attach_runner(
    *,
    llm: LLMClient | None,
    settings: Settings,
    storage: Storage,
) -> Callable[..., None] | None:
    """Return an extraction runner closure, or ``None`` to skip autofill.

    Both knobs must be set for the runner to fire:

    * ``llm`` — a usable :class:`LLMClient` (the factory wires
      :class:`OpenRouterClient` only when
      ``settings.openrouter_api_key`` is present).
    * ``settings.llm_ocr_model`` — the deployment-level capability
      gate. ``None`` disables autofill.

    When either is missing the helper returns ``None`` — the
    :func:`~app.domain.expenses.claims.attach_receipt` seam treats
    that as the "no autofill" path and skips the runner entirely.

    The closure captures ``llm`` / ``settings`` / ``storage`` from
    the request scope so the domain layer never has to reach back
    through ``app.state`` for adapter handles.
    """
    if llm is None or settings.llm_ocr_model is None:
        return None

    from app.worker.tasks.receipt_ocr import run_receipt_ocr

    captured_llm = llm
    captured_settings = settings
    captured_storage = storage

    def runner(
        session: Session,
        ctx: WorkspaceContext,
        *,
        claim_id: str,
        attachment_id: str,
    ) -> None:
        # The runner runs in the same UoW as the attach. If it
        # raises, ``attach_receipt`` swallows the exception so the
        # attach + the runner's audit / usage rows survive the
        # commit; see the ``attach_receipt`` docstring for the
        # contract.
        run_receipt_ocr(
            session,
            ctx,
            claim_id=claim_id,
            attachment_id=attachment_id,
            llm=captured_llm,
            storage=captured_storage,
            settings=captured_settings,
        )

    return runner


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
    repo, checker = make_seam_pair(session, ctx)
    try:
        detach_receipt(
            repo,
            checker,
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
    repo, checker = make_seam_pair(session, ctx)
    try:
        view = get_claim(repo, checker, ctx, claim_id=claim_id)
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
# OCR scan (cd-95zb / cd-65ib)
# ---------------------------------------------------------------------------
#
# The wire shape mirrors the SPA's ``ExpenseScanResult`` type
# (``app/web/src/types/expense.ts``) verbatim: each parsed field is an
# ``{value, confidence}`` pair, plus an optional free-text
# ``agent_question`` the upstream model can use to nudge the worker on
# an ambiguous receipt. The route is named ``/scan`` per spec §09's
# "scan a receipt" wording (cd-65ib renamed it from the earlier
# ``/autofill`` path, cd-95zb).


class _AutofillField(BaseModel):
    """One ``{value, confidence}`` cell of an :class:`ExpenseScanResult`.

    ``value`` is generic over the parsed type but pydantic + FastAPI
    OpenAPI only emits a stable schema when the field types are
    concrete; we therefore use one concrete subclass per field below
    rather than a generic ``AutofillField[T]``. The wire shape is
    identical (``{value, confidence}``) across all of them.
    """

    confidence: float = Field(ge=0.0, le=1.0)


class _AutofillFieldStr(_AutofillField):
    value: str


class _AutofillFieldInt(_AutofillField):
    value: int


class ExpenseScanResultPayload(BaseModel):
    """Wire shape for ``POST /expenses/scan``.

    Mirrors the SPA's ``ExpenseScanResult`` type 1:1 — each parsed
    field carries its own ``{value, confidence}`` pair so the worker
    UI can grey out / highlight low-confidence fields without
    recomputing a global threshold. ``agent_question`` is the
    optional free-text nudge the model emits when it cannot resolve
    an ambiguity from the image alone (e.g. "Was this a tip or part
    of the total?"); ``null`` when the model is confident.

    The v1 prompt extracts vendor / amount / currency / purchased_at
    / category. ``note_md`` is reserved for a future prompt revision
    that summarises the receipt; today the route returns an empty
    value with confidence 0.0 so the SPA's review form sees a
    well-formed cell and falls back to its empty-state. Same for
    ``agent_question`` — the v1 prompt does not emit it, so the
    field is always ``null`` until the prompt grows the slot. The
    contract is stable; the data fills in as the prompt evolves.
    """

    vendor: _AutofillFieldStr
    purchased_at: _AutofillFieldStr
    currency: _AutofillFieldStr
    total_amount_cents: _AutofillFieldInt
    category: _AutofillFieldStr
    note_md: _AutofillFieldStr
    agent_question: str | None = None

    @classmethod
    def from_extraction(cls, extraction: ReceiptExtraction) -> ExpenseScanResultPayload:
        """Project a :class:`ReceiptExtraction` into the SPA wire shape.

        The LLM emits a per-field ``confidence`` map keyed under the
        prompt's slot names (``vendor``, ``amount``, ``currency``,
        ``purchased_at``, ``category``); the validator already enforces
        the keys are present (see
        :meth:`ReceiptExtraction._confidence_shape`). The ``amount`` /
        ``total_amount_cents`` rename is intentional — the prompt asks
        for an amount in the receipt's minor unit, the claim's column
        is ``total_amount_cents`` and the SPA review form keys off
        that name.
        """
        conf = extraction.confidence
        return cls(
            vendor=_AutofillFieldStr(
                value=extraction.vendor, confidence=conf["vendor"]
            ),
            purchased_at=_AutofillFieldStr(
                value=extraction.purchased_at.isoformat(),
                confidence=conf["purchased_at"],
            ),
            currency=_AutofillFieldStr(
                value=extraction.currency, confidence=conf["currency"]
            ),
            total_amount_cents=_AutofillFieldInt(
                value=extraction.amount_cents, confidence=conf["amount"]
            ),
            category=_AutofillFieldStr(
                value=extraction.category, confidence=conf["category"]
            ),
            # ``note_md`` and ``agent_question`` are reserved slots —
            # see the class docstring. The empty-string + 0.0 cell keeps
            # the SPA's confidence-gate (`fillIf` in
            # ``app/web/src/pages/employee/expenses/lib/scanDerivation.ts``)
            # happy: it falls below every threshold and the form leaves
            # the note blank for the worker to fill in.
            note_md=_AutofillFieldStr(value="", confidence=0.0),
            agent_question=None,
        )


@router.post(
    "/scan",
    response_model=ExpenseScanResultPayload,
    operation_id="scan_expense_receipt",
    summary="Scan a receipt image and return autofill suggestions",
)
async def scan_expense_receipt_route(
    ctx: _Ctx,
    session: _Db,
    settings: _AppSettings,
    llm: _Llm,
    image: Annotated[UploadFile, File()],
    image_2: Annotated[UploadFile | None, File()] = None,
    hint_currency: Annotated[str | None, Form(min_length=3, max_length=3)] = None,
    hint_vendor: Annotated[str | None, Form(min_length=1, max_length=200)] = None,
) -> ExpenseScanResultPayload:
    """Run a receipt blob through the LLM and return parsed fields.

    "Preview" semantic: the route does NOT create a claim, attach a
    blob, or write any DB row apart from the workspace-scoped
    :class:`~app.adapters.db.llm.models.LlmUsage` ledger row that
    every LLM call costs. It exists for the SPA to surface extracted
    suggestions on the upload screen before the worker commits to a
    claim. To persist the same result, the SPA chains ``POST
    /uploads`` → ``POST /expenses`` → ``POST /expenses/{id}/
    attachments`` and lets the wired ``extraction_runner`` re-run
    the same extraction inside the attach transaction.

    Disabled at the deployment level when ``settings.llm_ocr_model``
    is unset — the response is 503 ``scan_not_configured`` so a
    caller can distinguish "no model assigned" from "transient
    provider error".

    The hint fields (``hint_currency``, ``hint_vendor``) are reserved
    for the v1 endpoint surface; the current pipeline does not yet
    feed them into the prompt. Wiring is tracked alongside cd-e626's
    prompt-tuning work — surfacing them here keeps the contract
    stable for the SPA. **Crucially the prompt body still carries
    only the OCR text** — these hints are never forwarded to the
    LLM, so an attacker who supplies a doctored ``hint_vendor``
    cannot inject prompt content the model sees today.

    Every successful or failed LLM call lands one
    :class:`~app.adapters.db.llm.models.LlmUsage` row (capability
    ``expenses.autofill``) so the workspace usage budget envelope
    stays honest even when callers exercise the preview surface.
    """
    if settings.llm_ocr_model is None:
        # Drain the upload so the multipart parser doesn't leak
        # tempfiles on the disabled-feature path.
        await image.close()
        if image_2 is not None:
            await image_2.close()
        raise _http(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "scan_not_configured",
            message=(
                "Receipt scan is disabled in this deployment "
                "(settings.llm_ocr_model is unset)"
            ),
        )

    # v1 single-image: the second image slot is reserved for the
    # multi-page batch follow-up (a tip-receipt + a meal-receipt
    # pair, etc.). Until cd-* lands the multi-image batching, drain
    # it without hitting the LLM so the caller's contract still
    # accepts the field but the cost stays bounded.
    if image_2 is not None:
        await image_2.close()

    if image.content_type not in _SCAN_ALLOWED_MIME:
        await image.close()
        raise _http(
            422,
            "blob_mime_not_allowed",
            message=(
                f"content_type={image.content_type!r} is not in the receipt "
                f"allow-list ({sorted(_SCAN_ALLOWED_MIME)!r})"
            ),
        )

    image_bytes = await image.read()
    await image.close()

    if len(image_bytes) > _SCAN_MAX_BYTES:
        raise _http(
            422,
            "blob_too_large",
            message=(
                f"size_bytes={len(image_bytes)} exceeds the {_SCAN_MAX_BYTES} byte cap"
            ),
        )
    if not image_bytes:
        raise _http(
            422,
            "blob_empty",
            message="image upload is empty",
        )

    try:
        metrics = extract_from_bytes(image_bytes, llm=llm, settings=settings)
    except ExtractionParseError as exc:
        # Chat call may have landed before the parse failed — record
        # the spent tokens so /admin/usage stays honest. The burnt
        # metrics ride :attr:`ExtractionParseError.burnt_metrics`.
        _record_preview_usage(
            session,
            ctx,
            burnt=exc.burnt_metrics,
            fallback_model_id=settings.llm_ocr_model,
            status="error",
        )
        raise _http(422, "extraction_parse_error", message=str(exc)) from exc
    except ExtractionTimeout as exc:
        _record_preview_usage(
            session,
            ctx,
            burnt=None,
            fallback_model_id=settings.llm_ocr_model,
            status="timeout",
        )
        raise _http(504, "extraction_timeout", message=str(exc)) from exc
    except ExtractionRateLimited as exc:
        _record_preview_usage(
            session,
            ctx,
            burnt=None,
            fallback_model_id=settings.llm_ocr_model,
            status="error",
        )
        # ``Retry-After: 60`` mirrors the §11 fallback-chain hint
        # (60 s window for the next attempt). The exact value is the
        # adapter's responsibility once the §12 model router lands;
        # until then the conservative one-minute floor keeps the SPA
        # from hammering the provider.
        raise _http(
            503,
            "extraction_rate_limited",
            message=str(exc),
            headers={"Retry-After": "60"},
        ) from exc
    except ExtractionProviderError as exc:
        _record_preview_usage(
            session,
            ctx,
            burnt=None,
            fallback_model_id=settings.llm_ocr_model,
            status="error",
        )
        raise _http(503, "extraction_provider_error", message=str(exc)) from exc

    extraction = metrics.extraction
    if extraction is None:  # pragma: no cover - defensive invariant
        # ``extract_from_bytes`` always populates ``extraction`` on
        # the happy path; a ``None`` here implies a future helper
        # bypassed the parse step but didn't raise.
        _record_preview_usage(
            session,
            ctx,
            burnt=metrics,
            fallback_model_id=settings.llm_ocr_model,
            status="error",
        )
        raise _http(
            500,
            "extraction_invariant",
            message="extract_from_bytes returned metrics with no extraction",
        )

    _record_preview_usage(
        session,
        ctx,
        burnt=metrics,
        fallback_model_id=settings.llm_ocr_model,
        status="ok",
    )
    return ExpenseScanResultPayload.from_extraction(extraction)


def _record_preview_usage(
    session: Session,
    ctx: WorkspaceContext,
    *,
    burnt: ExtractionMetrics | None,
    fallback_model_id: str,
    status: str,
) -> None:
    """Insert one :class:`LlmUsageRow` for a preview-endpoint call.

    Mirrors :func:`app.domain.expenses.autofill._record_llm_usage`
    but lives here because the preview route has no claim id to
    audit and the autofill module's helper insists on an ``ok |
    error | timeout`` status enum that the persist path uses.

    ``burnt`` is the metrics snapshot when the LLM call returned a
    body (parse error AFTER the chat reply landed; the success path
    obviously); ``None`` when the call failed before any usage data
    came back (timeout / rate-limit / non-2xx provider error). The
    workspace-scoped ``LlmUsage`` shape backs the §11 budget
    envelope, so a row lands on every outcome.
    """
    row = LlmUsageRow(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        capability=AUTOFILL_CAPABILITY,
        model_id=burnt.model_id if burnt is not None else fallback_model_id,
        tokens_in=burnt.prompt_tokens if burnt is not None else 0,
        tokens_out=burnt.completion_tokens if burnt is not None else 0,
        cost_cents=0,
        latency_ms=burnt.latency_ms if burnt is not None else 0,
        status=status,
        correlation_id=new_ulid(),
        attempt=0,
        assignment_id=None,
        fallback_attempts=0,
        finish_reason=None,
        actor_user_id=ctx.actor_id,
        token_id=None,
        agent_label=None,
        created_at=SystemClock().now(),
    )
    session.add(row)
