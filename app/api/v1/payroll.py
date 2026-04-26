"""Payroll context router (§01 "Context map", §12 "Time, payroll, expenses").

Mounted inside ``/w/<slug>/api/v1/payroll`` by the app factory.
Surface (cd-ea7):

```
GET    /users/{user_id}/pay-rules    # paginated, newest-first
POST   /users/{user_id}/pay-rules
GET    /pay-rules/{rule_id}
PATCH  /pay-rules/{rule_id}
DELETE /pay-rules/{rule_id}          # soft-retire (sets effective_to=now)
```

Every route requires an active :class:`~app.tenancy.WorkspaceContext`
and gates on ``pay_rules.edit`` at workspace scope (§05 action
catalog default-allow: ``owners, managers``). Reads gate too —
pay rates are compensation-PII (§15) and the v1 surface is
owner/manager-only end-to-end. The domain service layer also
re-asserts the same capability so non-HTTP transports (CLI, agent,
worker) get the same gate without re-implementing it.

The router is a thin DTO passthrough over the domain service in
:mod:`app.domain.payroll.rules`. Three error mappings carry weight:

* :class:`~app.domain.payroll.rules.PayRuleNotFound` → 404 (unknown
  id, soft-retired rows are still found via :func:`get_rule` — the
  service does not distinguish wrong-workspace from really-missing).
* :class:`~app.domain.payroll.rules.PayRuleInvariantViolated` → 422
  (validation failure: bad currency, multiplier out of range,
  bad window).
* :class:`~app.domain.payroll.rules.PayRuleLocked` → 409 (rule is
  consumed by a paid payslip; callers author a successor row with a
  later ``effective_from`` instead).

Routes follow the §12 "Pagination" envelope verbatim — listings
return ``{data, next_cursor, has_more}``; non-list reads + writes
return the bare resource shape.

See ``docs/specs/09-time-payroll-expenses.md`` §"Pay rules",
``docs/specs/02-domain-model.md`` §"pay_rule",
``docs/specs/12-rest-api.md`` §"Time, payroll, expenses".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.db.payroll.repositories import SqlAlchemyPayRuleRepository
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz.dep import Permission
from app.domain.payroll.rules import (
    BASE_CENTS_MAX,
    PayRuleCreate,
    PayRuleInvariantViolated,
    PayRuleLocked,
    PayRuleNotFound,
    PayRuleUpdate,
    PayRuleView,
    create_rule,
    cursor_for_view,
    get_rule,
    list_rules,
    soft_delete_rule,
    update_rule,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "PayRuleCreateRequest",
    "PayRuleListResponse",
    "PayRuleResponse",
    "PayRuleUpdateRequest",
    "build_payroll_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


_MAX_ID_LEN = 64


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class _PayRuleBodyRequest(BaseModel):
    """Shared mutable body for :class:`PayRuleCreateRequest` and update."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(..., min_length=3, max_length=3)
    base_cents_per_hour: int = Field(..., ge=0, le=BASE_CENTS_MAX)
    overtime_multiplier: Decimal = Field(default=Decimal("1.5"))
    night_multiplier: Decimal = Field(default=Decimal("1.25"))
    weekend_multiplier: Decimal = Field(default=Decimal("1.5"))
    effective_from: datetime
    effective_to: datetime | None = None


class PayRuleCreateRequest(_PayRuleBodyRequest):
    """Request body for ``POST /users/{user_id}/pay-rules``.

    ``user_id`` lives on the URL path; including it on the body
    would let a caller mismatch the two and silently target the
    wrong user.
    """


class PayRuleUpdateRequest(_PayRuleBodyRequest):
    """Request body for ``PATCH /pay-rules/{rule_id}``.

    Full-replacement update — v1 does not yet expose a per-field
    PATCH on pay rules. Same shape as
    :class:`PayRuleCreateRequest` minus the path-bound ``user_id``.
    """


class PayRuleResponse(BaseModel):
    """Response shape for pay-rule operations."""

    id: str
    workspace_id: str
    user_id: str
    currency: str
    base_cents_per_hour: int
    overtime_multiplier: Decimal
    night_multiplier: Decimal
    weekend_multiplier: Decimal
    effective_from: datetime
    effective_to: datetime | None
    created_by: str | None
    created_at: datetime


class PayRuleListResponse(BaseModel):
    """Collection envelope for the per-user pay-rule listing.

    Matches §12 "Pagination" verbatim — ``{data, next_cursor,
    has_more}``.
    """

    data: list[PayRuleResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view_to_response(view: PayRuleView) -> PayRuleResponse:
    return PayRuleResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        user_id=view.user_id,
        currency=view.currency,
        base_cents_per_hour=view.base_cents_per_hour,
        overtime_multiplier=view.overtime_multiplier,
        night_multiplier=view.night_multiplier,
        weekend_multiplier=view.weekend_multiplier,
        effective_from=view.effective_from,
        effective_to=view.effective_to,
        created_by=view.created_by,
        created_at=view.created_at,
    )


def _request_to_create(body: PayRuleCreateRequest) -> PayRuleCreate:
    return PayRuleCreate.model_validate(body.model_dump())


def _request_to_update(body: PayRuleUpdateRequest) -> PayRuleUpdate:
    return PayRuleUpdate.model_validate(body.model_dump())


def _http_for_invariant(exc: PayRuleInvariantViolated) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "pay_rule_invariant", "message": str(exc)},
    )


def _http_for_locked(exc: PayRuleLocked) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "pay_rule_locked", "message": str(exc)},
    )


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "pay_rule_not_found"},
    )


_UserIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=_MAX_ID_LEN,
        description="Owner of the pay-rule chain — usually a ``user.id`` ULID.",
    ),
]
_RuleIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=_MAX_ID_LEN,
        description="ULID of the target ``pay_rule`` row.",
    ),
]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_payroll_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the payroll surface."""
    api = APIRouter(tags=["payroll"])

    edit_gate = Depends(Permission("pay_rules.edit", scope_kind="workspace"))

    @api.get(
        "/users/{user_id}/pay-rules",
        response_model=PayRuleListResponse,
        operation_id="payroll.pay_rules.list",
        summary="List a user's pay rules — newest effective_from first",
        dependencies=[edit_gate],
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        user_id: _UserIdPath,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> PayRuleListResponse:
        """Cursor-paginated listing for ``(workspace, user_id)``."""
        after_cursor = decode_cursor(cursor)
        try:
            views = list_rules(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                user_id=user_id,
                limit=limit,
                after_cursor=after_cursor,
            )
        except ValueError as exc:
            # The repo's cursor-split raises ``ValueError`` on a
            # tampered cursor that base64-decodes cleanly but
            # doesn't carry the ``"<isoformat>|<id>"`` shape. Map
            # to 422 so the surface mirrors :func:`decode_cursor`'s
            # ``invalid_cursor`` error envelope.
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_cursor", "message": str(exc)},
            ) from exc
        page = paginate(
            views,
            limit=limit,
            key_getter=cursor_for_view,
        )
        return PayRuleListResponse(
            data=[_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "/users/{user_id}/pay-rules",
        status_code=status.HTTP_201_CREATED,
        response_model=PayRuleResponse,
        operation_id="payroll.pay_rules.create",
        summary="Create a pay rule for a user",
        dependencies=[edit_gate],
    )
    def create(
        body: PayRuleCreateRequest,
        ctx: _Ctx,
        session: _Db,
        user_id: _UserIdPath,
    ) -> PayRuleResponse:
        """Insert a new ``pay_rule`` row for ``user_id``.

        Domain validators reject: currency outside the ISO-4217
        allow-list, multipliers outside ``[1.0, 5.0]``, zero-or-negative
        windows. All three surface as 422 ``pay_rule_invariant``.
        """
        try:
            view = create_rule(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                user_id=user_id,
                body=_request_to_create(body),
            )
        except PayRuleInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.get(
        "/pay-rules/{rule_id}",
        response_model=PayRuleResponse,
        operation_id="payroll.pay_rules.get",
        summary="Read a single pay rule",
        dependencies=[edit_gate],
    )
    def get_(
        ctx: _Ctx,
        session: _Db,
        rule_id: _RuleIdPath,
    ) -> PayRuleResponse:
        try:
            view = get_rule(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                rule_id=rule_id,
            )
        except PayRuleNotFound as exc:
            raise _http_for_not_found() from exc
        return _view_to_response(view)

    @api.patch(
        "/pay-rules/{rule_id}",
        response_model=PayRuleResponse,
        operation_id="payroll.pay_rules.update",
        summary="Replace the mutable body of a pay rule",
        dependencies=[edit_gate],
    )
    def update(
        body: PayRuleUpdateRequest,
        ctx: _Ctx,
        session: _Db,
        rule_id: _RuleIdPath,
    ) -> PayRuleResponse:
        """Full-replacement update.

        Refused with 409 ``pay_rule_locked`` if any payslip in a
        paid pay_period already cites this row — historical
        evidence is fixed; callers author a successor row with a
        later ``effective_from`` instead.
        """
        try:
            view = update_rule(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                rule_id=rule_id,
                body=_request_to_update(body),
            )
        except PayRuleNotFound as exc:
            raise _http_for_not_found() from exc
        except PayRuleLocked as exc:
            raise _http_for_locked(exc) from exc
        except PayRuleInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.delete(
        "/pay-rules/{rule_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="payroll.pay_rules.delete",
        summary="Soft-retire a pay rule (stamp effective_to = now)",
        dependencies=[edit_gate],
    )
    def delete(
        ctx: _Ctx,
        session: _Db,
        rule_id: _RuleIdPath,
    ) -> Response:
        """Stamp ``effective_to`` so the rule no longer applies forward.

        Pay rules are never hard-deleted (§09 §"Labour-law
        compliance"); historical payslips keep a live FK to the
        row. Refused with 409 if the rule is consumed by a paid
        payslip. No response body per §12 "Deletion".
        """
        try:
            soft_delete_rule(
                SqlAlchemyPayRuleRepository(session),
                ctx,
                rule_id=rule_id,
            )
        except PayRuleNotFound as exc:
            raise _http_for_not_found() from exc
        except PayRuleLocked as exc:
            raise _http_for_locked(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


router = build_payroll_router()
