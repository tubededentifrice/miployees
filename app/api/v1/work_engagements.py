"""Work-engagements HTTP router — ``/work_engagements`` (spec §12).

Mounted inside ``/w/<slug>/api/v1`` by the app factory. v1 surface:

* ``GET /work_engagements`` — cursor-paginated list of engagements
  (optional ``user_id`` filter).
* ``GET /work_engagements/{id}`` — read one engagement.
* ``PATCH /work_engagements/{id}`` — partial update of the mutable
  fields.
* ``POST /work_engagements/{id}/archive`` — archive one engagement.
* ``POST /work_engagements/{id}/reinstate`` — reverse archive.

The archive / reinstate paths here are **engagement-keyed** — they
target a specific engagement row by id and do not sweep the user's
``user_work_role`` rows. The user-centric sweep lives on
``POST /users/{id}/archive`` (see
:mod:`app.api.v1.users`) which is the right tool for full off-
boarding; the engagement-keyed variant exists so a manager can end
a single payroll pipeline without touching the rest of the user's
workspace-scoped state.

Every route tags ``identity`` + ``work_engagements``. Reads gate on
``scope.view``; mutations on ``work_roles.manage`` (§05 action
catalog default-allow: ``owners, managers``). No ``POST`` to create
a new engagement — engagements are seeded on invite-accept and
managed through PATCH + archive / reinstate; re-creating requires a
dedicated flow that does not exist on v1 yet.

See ``docs/specs/02-domain-model.md`` §"work_engagement",
``docs/specs/05-employees-and-roles.md`` §"Work engagement",
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz import Permission
from app.domain.identity.work_engagements import (
    EngagementKind,
    WorkEngagementInvariantViolated,
    WorkEngagementNotFound,
    WorkEngagementUpdate,
    WorkEngagementView,
    archive_work_engagement,
    get_work_engagement,
    list_work_engagements,
    reinstate_work_engagement,
    update_work_engagement,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "WorkEngagementListResponse",
    "WorkEngagementResponse",
    "WorkEngagementUpdateRequest",
    "build_work_engagements_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class WorkEngagementUpdateRequest(BaseModel):
    """Request body for ``PATCH /work_engagements/{id}``.

    Mirrors :class:`~app.domain.identity.work_engagements.WorkEngagementUpdate`
    — explicit-sparse. Frozen columns (``id``, ``user_id``,
    ``workspace_id``, ``started_on``) are intentionally absent; the
    service rejects any attempt to mutate them by the ``extra=forbid``
    Pydantic config below.
    """

    model_config = ConfigDict(extra="forbid")

    engagement_kind: EngagementKind | None = None
    supplier_org_id: str | None = Field(default=None, max_length=64)
    pay_destination_id: str | None = Field(default=None, max_length=64)
    reimbursement_destination_id: str | None = Field(default=None, max_length=64)
    notes_md: str | None = Field(default=None, max_length=20_000)


class WorkEngagementResponse(BaseModel):
    """Response shape for engagement reads + writes."""

    id: str
    user_id: str
    workspace_id: str
    engagement_kind: str
    supplier_org_id: str | None
    pay_destination_id: str | None
    reimbursement_destination_id: str | None
    started_on: date
    archived_on: date | None
    notes_md: str
    created_at: datetime
    updated_at: datetime


class WorkEngagementListResponse(BaseModel):
    """Collection envelope for ``GET /work_engagements``."""

    data: list[WorkEngagementResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Query / path dependencies
# ---------------------------------------------------------------------------


_UserIdFilter = Annotated[
    str | None,
    Query(
        max_length=64,
        description=(
            "Narrow the engagement listing to a specific user. "
            "Matches the spec §12 '?user_id=…' filter verbatim."
        ),
    ),
]


_ActiveFilter = Annotated[
    Literal["true", "false"] | None,
    Query(
        description=(
            "When ``true``, exclude archived engagements "
            "(``archived_on IS NOT NULL``). When ``false`` or omitted, "
            "include them. Mirrors the §12 roster view boolean filter."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view_to_response(view: WorkEngagementView) -> WorkEngagementResponse:
    return WorkEngagementResponse(
        id=view.id,
        user_id=view.user_id,
        workspace_id=view.workspace_id,
        engagement_kind=view.engagement_kind,
        supplier_org_id=view.supplier_org_id,
        pay_destination_id=view.pay_destination_id,
        reimbursement_destination_id=view.reimbursement_destination_id,
        started_on=view.started_on,
        archived_on=view.archived_on,
        notes_md=view.notes_md,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "work_engagement_not_found"},
    )


def _http_for_invariant(exc: WorkEngagementInvariantViolated) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "work_engagement_invariant", "message": str(exc)},
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_work_engagements_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for engagement CRUD."""
    api = APIRouter(prefix="/work_engagements", tags=["identity", "work_engagements"])

    manage_gate = Depends(Permission("work_roles.manage", scope_kind="workspace"))
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))

    @api.get(
        "",
        response_model=WorkEngagementListResponse,
        operation_id="work_engagements.list",
        summary="List work engagements in the caller's workspace",
        dependencies=[view_gate],
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
        user_id: _UserIdFilter = None,
        active: _ActiveFilter = None,
    ) -> WorkEngagementListResponse:
        """Cursor-paginated engagement list with optional filters."""
        after_id = decode_cursor(cursor)
        include_archived = active != "true"
        views = list_work_engagements(
            session,
            ctx,
            limit=limit,
            after_id=after_id,
            user_id=user_id,
            include_archived=include_archived,
        )
        page = paginate(
            views,
            limit=limit,
            key_getter=lambda v: v.id,
        )
        return WorkEngagementListResponse(
            data=[_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.get(
        "/{engagement_id}",
        response_model=WorkEngagementResponse,
        operation_id="work_engagements.read",
        summary="Read a work engagement by id",
        dependencies=[view_gate],
    )
    def read(
        engagement_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkEngagementResponse:
        """Return one engagement view or 404."""
        try:
            view = get_work_engagement(session, ctx, engagement_id=engagement_id)
        except WorkEngagementNotFound as exc:
            raise _http_for_not_found() from exc
        return _view_to_response(view)

    @api.patch(
        "/{engagement_id}",
        response_model=WorkEngagementResponse,
        operation_id="work_engagements.update",
        summary="Partial update of a work engagement",
        dependencies=[manage_gate],
    )
    def update(
        engagement_id: str,
        body: WorkEngagementUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkEngagementResponse:
        """PATCH the mutable engagement fields (kind, supplier, pay pointers, notes)."""
        sent = body.model_fields_set
        service_body = WorkEngagementUpdate.model_validate(
            {f: getattr(body, f) for f in sent}
        )
        try:
            view = update_work_engagement(
                session, ctx, engagement_id=engagement_id, body=service_body
            )
        except WorkEngagementNotFound as exc:
            raise _http_for_not_found() from exc
        except WorkEngagementInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.post(
        "/{engagement_id}/archive",
        response_model=WorkEngagementResponse,
        operation_id="work_engagements.archive",
        summary="Archive a single engagement — idempotent",
        dependencies=[manage_gate],
    )
    def archive(
        engagement_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkEngagementResponse:
        """Set ``archived_on`` on one engagement row."""
        try:
            view = archive_work_engagement(session, ctx, engagement_id=engagement_id)
        except WorkEngagementNotFound as exc:
            raise _http_for_not_found() from exc
        return _view_to_response(view)

    @api.post(
        "/{engagement_id}/reinstate",
        response_model=WorkEngagementResponse,
        operation_id="work_engagements.reinstate",
        summary="Reverse archive on a single engagement — idempotent",
        dependencies=[manage_gate],
    )
    def reinstate(
        engagement_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkEngagementResponse:
        """Clear ``archived_on`` on one engagement row.

        Fires 422 when the user already has a different active
        engagement in this workspace — the partial UNIQUE on
        ``(user_id, workspace_id) WHERE archived_on IS NULL`` would
        otherwise reject at flush time with an opaque 500.
        """
        try:
            view = reinstate_work_engagement(session, ctx, engagement_id=engagement_id)
        except WorkEngagementNotFound as exc:
            raise _http_for_not_found() from exc
        except WorkEngagementInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    return api


router = build_work_engagements_router()
