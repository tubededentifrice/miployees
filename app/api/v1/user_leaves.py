"""User-leaves HTTP router (cd-oydd) — ``/user_leaves`` + sub-resources.

Mounted inside ``/w/<slug>/api/v1`` by the app factory. Surface per
``docs/specs/12-rest-api.md`` §"Users / work roles / settings":

```
GET    /user_leaves               # ?user_id=…&from=…&to=…&approved=true|false
POST   /user_leaves
PATCH  /user_leaves/{id}
POST   /user_leaves/{id}/approve
POST   /user_leaves/{id}/reject
DELETE /user_leaves/{id}          # soft delete
```

Tags: ``identity`` + ``user_leaves`` so the OpenAPI surface clusters
the verbs alongside the rest of the identity context (matching the
sibling user_work_roles / property_work_role_assignments routers).

**Authz at the wire.** Every verb authenticates against an active
:class:`~app.tenancy.WorkspaceContext`; the actual capability gate
runs in the domain service. This is **deliberately different** from
the ``property_work_role_assignments`` router (which carries the
gate as a route ``Depends`` on a single fixed capability): leaves
have a per-target capability shape — self-target uses
``leaves.create_self`` / no-cap, cross-user uses
``leaves.view_others`` / ``leaves.edit_others`` — so the right
seam is the service, where ``target_user_id`` is known. The router
maps :class:`~app.domain.identity.user_leaves.UserLeavePermissionDenied`
to the §12 403 envelope.

**State machine.** Pending → approved (via approve), pending →
rejected (via reject; soft-deletes the row), pending → deleted
(via DELETE; soft-deletes), approved → deleted (manager revoke
of an approved row). Approved → rejected is **not** allowed: the
manager must DELETE the approved row. The service surfaces every
"wrong state" as :class:`UserLeaveTransitionForbidden`, mapped to
409 here.

See ``docs/specs/06-tasks-and-scheduling.md`` §"user_leave",
``docs/specs/05-employees-and-roles.md`` §"Action catalog",
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.domain.identity.user_leaves import (
    UserLeaveCategory,
    UserLeaveCreate,
    UserLeaveInvariantViolated,
    UserLeaveListFilter,
    UserLeaveNotFound,
    UserLeavePermissionDenied,
    UserLeaveTransitionForbidden,
    UserLeaveUpdate,
    UserLeaveView,
    approve_leave,
    create_leave,
    delete_leave,
    list_leaves,
    reject_leave,
    update_leave,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "UserLeaveCreateRequest",
    "UserLeaveListResponse",
    "UserLeaveRejectRequest",
    "UserLeaveResponse",
    "UserLeaveUpdateRequest",
    "build_user_leaves_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

_MAX_ID_LEN = 64
_MAX_NOTE_LEN = 20_000


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class UserLeaveCreateRequest(BaseModel):
    """Request body for ``POST /user_leaves``.

    ``workspace_id`` is **deliberately absent** — the service derives
    it from the :class:`WorkspaceContext`. ``user_id`` defaults to the
    caller; managers send it explicitly to author a leave on someone
    else's behalf.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    starts_on: date
    ends_on: date
    category: UserLeaveCategory
    note_md: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)

    @model_validator(mode="after")
    def _validate_window(self) -> UserLeaveCreateRequest:
        """Reject ``ends_on < starts_on`` at the DTO layer.

        Mirrors :class:`~app.domain.identity.user_leaves.UserLeaveCreate`
        so a malformed window surfaces as a 422 from FastAPI's
        validation envelope rather than as a 500 from the service-
        layer raise. Same-day leaves (``starts_on == ends_on``) are
        valid per §06 "user_leave".
        """
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on must be on or after starts_on")
        return self


class UserLeaveUpdateRequest(BaseModel):
    """Request body for ``PATCH /user_leaves/{id}``.

    Explicit-sparse — only sent fields land. ``user_id`` is frozen
    after create (re-keying would orphan the audit chain); approval
    state is mutated through the dedicated approve / reject
    sub-resources, not through PATCH.
    """

    model_config = ConfigDict(extra="forbid")

    starts_on: date | None = None
    ends_on: date | None = None
    category: UserLeaveCategory | None = None
    note_md: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)


class UserLeaveRejectRequest(BaseModel):
    """Optional body for ``POST /user_leaves/{id}/reject``.

    ``reason_md`` is folded into the row's ``note_md`` so the worker
    sees the rejection rationale alongside their original request.
    A request without a body is fine — the worker simply sees a
    rejected row with no extra context.
    """

    model_config = ConfigDict(extra="forbid")

    reason_md: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)


class UserLeaveResponse(BaseModel):
    """Response shape for user_leave operations."""

    id: str
    workspace_id: str
    user_id: str
    starts_on: date
    ends_on: date
    category: UserLeaveCategory
    approved_at: datetime | None
    approved_by: str | None
    note_md: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class UserLeaveListResponse(BaseModel):
    """Collection envelope for ``GET /user_leaves``.

    Shape matches §12 "Pagination" verbatim — ``{data, next_cursor,
    has_more}``.
    """

    data: list[UserLeaveResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Query dependencies
# ---------------------------------------------------------------------------


_UserIdFilter = Annotated[
    str | None,
    Query(
        max_length=_MAX_ID_LEN,
        description=(
            "Narrow the listing to one user. Omit for the manager "
            "inbox view (requires ``leaves.view_others``)."
        ),
    ),
]

_FromFilter = Annotated[
    date | None,
    Query(
        alias="from",
        description=(
            "Inclusive lower bound on ``starts_on`` (ISO date). "
            "Combine with ``to`` to slice a date window."
        ),
    ),
]

_ToFilter = Annotated[
    date | None,
    Query(
        alias="to",
        description=(
            "Inclusive upper bound on ``ends_on`` (ISO date). Combine "
            "with ``from`` to slice a date window."
        ),
    ),
]

_ApprovedFilter = Annotated[
    bool | None,
    Query(
        description=(
            "``true`` returns only approved leaves; ``false`` returns "
            "only pending leaves. Omit for both states."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view_to_response(view: UserLeaveView) -> UserLeaveResponse:
    return UserLeaveResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        user_id=view.user_id,
        starts_on=view.starts_on,
        ends_on=view.ends_on,
        category=view.category,
        approved_at=view.approved_at,
        approved_by=view.approved_by,
        note_md=view.note_md,
        created_at=view.created_at,
        updated_at=view.updated_at,
        deleted_at=view.deleted_at,
    )


def _http_for_invariant(exc: UserLeaveInvariantViolated) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": "user_leave_invariant", "message": str(exc)},
    )


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "user_leave_not_found"},
    )


def _http_for_permission_denied(exc: UserLeavePermissionDenied) -> HTTPException:
    """Map a domain :class:`UserLeavePermissionDenied` to 403.

    The detail body matches the §12 envelope shape used by the
    :func:`app.authz.Permission` dependency: ``{"error":
    "permission_denied", "action_key": "<key>"}``. The action key is
    pulled from the underlying exception message (the service raises
    ``UserLeavePermissionDenied(str(PermissionDenied))`` which
    stringifies to the action key alone).
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "permission_denied", "action_key": str(exc)},
    )


def _http_for_transition(exc: UserLeaveTransitionForbidden) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "user_leave_transition_forbidden", "message": str(exc)},
    )


def _approved_to_status(
    approved: bool | None,
) -> Literal["approved", "pending"] | None:
    """Translate the wire ``?approved=`` query param into the service status filter."""
    if approved is None:
        return None
    return "approved" if approved else "pending"


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_user_leaves_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the CRUD + state surface."""
    api = APIRouter(prefix="/user_leaves", tags=["identity", "user_leaves"])

    @api.get(
        "",
        response_model=UserLeaveListResponse,
        operation_id="user_leaves.list",
        summary="List user_leave rows in the caller's workspace",
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
        user_id: _UserIdFilter = None,
        from_: _FromFilter = None,
        to: _ToFilter = None,
        approved: _ApprovedFilter = None,
    ) -> UserLeaveListResponse:
        """Cursor-paginated listing with optional filters.

        ``from_`` is the ``?from=`` query alias (Python keyword
        clash). The wire param stays ``from`` — see the
        :data:`_FromFilter` dependency annotation.
        """
        after_id = decode_cursor(cursor)
        filters = UserLeaveListFilter(
            user_id=user_id,
            status=_approved_to_status(approved),
            starts_after=from_,
            ends_before=to,
        )
        try:
            views = list_leaves(
                session,
                ctx,
                filters=filters,
                limit=limit,
                after_id=after_id,
            )
        except UserLeavePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc

        page = paginate(views, limit=limit, key_getter=lambda v: v.id)
        return UserLeaveListResponse(
            data=[_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=UserLeaveResponse,
        operation_id="user_leaves.create",
        summary="Create a user_leave row",
    )
    def create(
        body: UserLeaveCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserLeaveResponse:
        """Insert a new user_leave row.

        Self-submit (``user_id`` omitted or equal to the caller) is
        gated on ``leaves.create_self`` and lands pending unless the
        caller is owner / manager. Cross-user create is gated on
        ``leaves.edit_others`` and always lands auto-approved.
        """
        service_body = UserLeaveCreate.model_validate(body.model_dump())
        try:
            view = create_leave(session, ctx, body=service_body)
        except UserLeavePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        except UserLeaveInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.patch(
        "/{leave_id}",
        response_model=UserLeaveResponse,
        operation_id="user_leaves.update",
        summary="Partial update of a pending user_leave row",
    )
    def update(
        leave_id: str,
        body: UserLeaveUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserLeaveResponse:
        """Update mutable fields on a pending leave.

        Pending-only — an approved leave rejects with 409. The
        requester or a holder of ``leaves.edit_others`` may mutate.
        """
        sent = body.model_fields_set
        service_body = UserLeaveUpdate.model_validate(
            {f: getattr(body, f) for f in sent}
        )
        try:
            view = update_leave(session, ctx, leave_id=leave_id, body=service_body)
        except UserLeaveNotFound as exc:
            raise _http_for_not_found() from exc
        except UserLeavePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        except UserLeaveTransitionForbidden as exc:
            raise _http_for_transition(exc) from exc
        except UserLeaveInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.post(
        "/{leave_id}/approve",
        response_model=UserLeaveResponse,
        operation_id="user_leaves.approve",
        summary="Approve a pending user_leave row",
    )
    def approve(
        leave_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> UserLeaveResponse:
        """Stamp ``approved_at`` + ``approved_by`` on a pending row.

        Always requires ``leaves.edit_others``. An already-approved
        row collapses to 409.
        """
        try:
            view = approve_leave(session, ctx, leave_id=leave_id)
        except UserLeaveNotFound as exc:
            raise _http_for_not_found() from exc
        except UserLeavePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        except UserLeaveTransitionForbidden as exc:
            raise _http_for_transition(exc) from exc
        return _view_to_response(view)

    @api.post(
        "/{leave_id}/reject",
        response_model=UserLeaveResponse,
        operation_id="user_leaves.reject",
        summary="Reject (soft-delete) a pending user_leave row",
    )
    def reject(
        leave_id: str,
        ctx: _Ctx,
        session: _Db,
        body: UserLeaveRejectRequest | None = None,
    ) -> UserLeaveResponse:
        """Soft-delete a pending row with optional rejection reason.

        §06 doesn't pin a persistent ``rejected`` state on
        ``user_leave``; v1 ships rejection as a tombstone + folded-in
        ``note_md`` so the worker keeps the rationale visible. The
        ``user_leave.rejected`` audit row preserves the transition.
        Always requires ``leaves.edit_others``.
        """
        reason = body.reason_md if body is not None else None
        try:
            view = reject_leave(session, ctx, leave_id=leave_id, reason_md=reason)
        except UserLeaveNotFound as exc:
            raise _http_for_not_found() from exc
        except UserLeavePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        except UserLeaveTransitionForbidden as exc:
            raise _http_for_transition(exc) from exc
        return _view_to_response(view)

    @api.delete(
        "/{leave_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="user_leaves.delete",
        summary="Soft-delete a user_leave row (worker withdraw / manager revoke)",
    )
    def delete(
        leave_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Stamp ``deleted_at`` and return 204.

        Authorisation: requester or ``leaves.edit_others``. A
        repeated DELETE on an already-deleted row surfaces 404 (the
        tombstone filter hides the row from :func:`_load_row`); the
        spec leaves the choice between 404 and 204-idempotent open
        and the loud-on-double-click reading is what we want here so
        a UI bug doesn't silently mint multiple
        ``user_leave.deleted`` audit rows.
        """
        try:
            delete_leave(session, ctx, leave_id=leave_id)
        except UserLeaveNotFound as exc:
            raise _http_for_not_found() from exc
        except UserLeavePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


router = build_user_leaves_router()
