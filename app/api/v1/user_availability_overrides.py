"""User-availability-overrides HTTP router (cd-uqw1).

Mounted inside ``/w/<slug>/api/v1`` by the app factory. Surface per
``docs/specs/12-rest-api.md`` §"Users / work roles / settings":

```
GET    /user_availability_overrides   # ?user_id=…&from=…&to=…&approved=true|false
POST   /user_availability_overrides
PATCH  /user_availability_overrides/{id}
POST   /user_availability_overrides/{id}/approve
POST   /user_availability_overrides/{id}/reject
DELETE /user_availability_overrides/{id}          # soft delete
```

Tags: ``identity`` + ``user_availability_overrides`` so the OpenAPI
surface clusters the verbs alongside the rest of the identity context
(matching the sibling ``user_leaves`` router).

**Authz at the wire.** Every verb authenticates against an active
:class:`~app.tenancy.WorkspaceContext`; the actual capability gate
runs in the domain service. This is **deliberately different** from
the ``property_work_role_assignments`` router (which carries the
gate as a route ``Depends`` on a single fixed capability): availability
overrides have a per-target capability shape — self-target uses
``availability_overrides.create_self`` / no-cap, cross-user uses
``availability_overrides.view_others`` /
``availability_overrides.edit_others`` — so the right seam is the
service, where ``target_user_id`` is known. The router maps
:class:`~app.domain.identity.user_availability_overrides.UserAvailabilityOverridePermissionDenied`
to the §12 403 envelope.

**State machine.** Pending → approved (via approve), pending →
rejected (via reject; soft-deletes the row), pending → deleted
(via DELETE; soft-deletes), approved → deleted (manager revoke
of an approved row). Approved → rejected is **not** allowed: the
manager must DELETE the approved row. The service surfaces every
"wrong state" as :class:`UserAvailabilityOverrideTransitionForbidden`,
mapped to 409 here.

See ``docs/specs/06-tasks-and-scheduling.md`` §"user_availability_overrides",
``docs/specs/05-employees-and-roles.md`` §"Action catalog",
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.adapters.db.availability.repositories import (
    SqlAlchemyCapabilityChecker,
    SqlAlchemyUserAvailabilityOverrideRepository,
)
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.domain.identity.user_availability_overrides import (
    UserAvailabilityOverrideCreate,
    UserAvailabilityOverrideInvariantViolated,
    UserAvailabilityOverrideListFilter,
    UserAvailabilityOverrideNotFound,
    UserAvailabilityOverridePermissionDenied,
    UserAvailabilityOverrideTransitionForbidden,
    UserAvailabilityOverrideUpdate,
    UserAvailabilityOverrideView,
    approve_override,
    create_override,
    delete_override,
    list_overrides,
    reject_override,
    update_override,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "UserAvailabilityOverrideCreateRequest",
    "UserAvailabilityOverrideListResponse",
    "UserAvailabilityOverrideRejectRequest",
    "UserAvailabilityOverrideResponse",
    "UserAvailabilityOverrideUpdateRequest",
    "build_user_availability_overrides_router",
    "make_seam_pair",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

_MAX_ID_LEN = 64
_MAX_REASON_LEN = 20_000


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class UserAvailabilityOverrideCreateRequest(BaseModel):
    """Request body for ``POST /user_availability_overrides``.

    ``workspace_id`` is **deliberately absent** — the service derives
    it from the :class:`WorkspaceContext`. ``user_id`` defaults to the
    caller; managers send it explicitly to author an override on
    someone else's behalf.

    ``starts_local`` / ``ends_local`` are paired (BOTH-OR-NEITHER per
    §06). Setting ``available=False`` requires both null — a
    not-working override has no hours.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    date: date
    available: bool
    starts_local: time | None = None
    ends_local: time | None = None
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LEN)

    @model_validator(mode="after")
    def _validate_hours(self) -> UserAvailabilityOverrideCreateRequest:
        """Enforce BOTH-OR-NEITHER + ``ends_local > starts_local``.

        Mirrors the service-layer
        :class:`~app.domain.identity.user_availability_overrides.UserAvailabilityOverrideCreate`
        DTO so a malformed shape surfaces as a 422 from FastAPI's
        validation envelope rather than as a 500 from the service-
        layer raise.
        """
        starts = self.starts_local
        ends = self.ends_local
        if (starts is None) != (ends is None):
            raise ValueError(
                "starts_local and ends_local must both be set or both be null"
            )
        if starts is not None and ends is not None and ends <= starts:
            raise ValueError("ends_local must be after starts_local")
        if not self.available and (starts is not None or ends is not None):
            raise ValueError(
                "available=false overrides must not carry hours; clear "
                "starts_local / ends_local"
            )
        return self


class UserAvailabilityOverrideUpdateRequest(BaseModel):
    """Request body for ``PATCH /user_availability_overrides/{id}``.

    Explicit-sparse — only sent fields land. ``user_id`` and ``date``
    are frozen after create (re-keying would orphan the audit chain
    and could collide with the unique constraint); approval state is
    mutated through the dedicated approve / reject sub-resources.
    """

    model_config = ConfigDict(extra="forbid")

    available: bool | None = None
    starts_local: time | None = None
    ends_local: time | None = None
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LEN)


class UserAvailabilityOverrideRejectRequest(BaseModel):
    """Optional body for ``POST /user_availability_overrides/{id}/reject``.

    ``reason_md`` is folded into the row's ``reason`` so the worker
    sees the rejection rationale alongside their original request.
    A request without a body is fine — the worker simply sees a
    rejected row with no extra context.
    """

    model_config = ConfigDict(extra="forbid")

    reason_md: str | None = Field(default=None, max_length=_MAX_REASON_LEN)


class UserAvailabilityOverrideResponse(BaseModel):
    """Response shape for user_availability_override operations."""

    id: str
    workspace_id: str
    user_id: str
    date: date
    available: bool
    starts_local: time | None
    ends_local: time | None
    reason: str | None
    approval_required: bool
    approved_at: datetime | None
    approved_by: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class UserAvailabilityOverrideListResponse(BaseModel):
    """Collection envelope for ``GET /user_availability_overrides``.

    Shape matches §12 "Pagination" verbatim — ``{data, next_cursor,
    has_more}``.
    """

    data: list[UserAvailabilityOverrideResponse]
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
            "inbox view (requires ``availability_overrides.view_others``)."
        ),
    ),
]

_FromFilter = Annotated[
    date | None,
    Query(
        alias="from",
        description=(
            "Inclusive lower bound on ``date`` (ISO date). Combine "
            "with ``to`` to slice a date window."
        ),
    ),
]

_ToFilter = Annotated[
    date | None,
    Query(
        alias="to",
        description=(
            "Inclusive upper bound on ``date`` (ISO date). Combine "
            "with ``from`` to slice a date window."
        ),
    ),
]

_ApprovedFilter = Annotated[
    bool | None,
    Query(
        description=(
            "``true`` returns only approved overrides; ``false`` "
            "returns only pending overrides. Omit for both states."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view_to_response(
    view: UserAvailabilityOverrideView,
) -> UserAvailabilityOverrideResponse:
    return UserAvailabilityOverrideResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        user_id=view.user_id,
        date=view.date,
        available=view.available,
        starts_local=view.starts_local,
        ends_local=view.ends_local,
        reason=view.reason,
        approval_required=view.approval_required,
        approved_at=view.approved_at,
        approved_by=view.approved_by,
        created_at=view.created_at,
        updated_at=view.updated_at,
        deleted_at=view.deleted_at,
    )


def _http_for_invariant(
    exc: UserAvailabilityOverrideInvariantViolated,
) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": "user_availability_override_invariant",
            "message": str(exc),
        },
    )


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "user_availability_override_not_found"},
    )


def _http_for_permission_denied(
    exc: UserAvailabilityOverridePermissionDenied,
) -> HTTPException:
    """Map a domain :class:`UserAvailabilityOverridePermissionDenied` to 403.

    The detail body matches the §12 envelope shape used by the
    :func:`app.authz.dep.Permission` dependency: ``{"error":
    "permission_denied", "action_key": "<key>"}``.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "permission_denied", "action_key": str(exc)},
    )


def _http_for_transition(
    exc: UserAvailabilityOverrideTransitionForbidden,
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "user_availability_override_transition_forbidden",
            "message": str(exc),
        },
    )


def _approved_to_status(
    approved: bool | None,
) -> Literal["approved", "pending"] | None:
    """Translate the wire ``?approved=`` query param into the service status filter."""
    if approved is None:
        return None
    return "approved" if approved else "pending"


def make_seam_pair(
    session: Session, ctx: WorkspaceContext
) -> tuple[
    SqlAlchemyUserAvailabilityOverrideRepository,
    SqlAlchemyCapabilityChecker,
]:
    """Construct the SA-backed repo + capability checker for the request.

    Both seams (cd-r5j2) wrap the same ``(session, ctx)`` pair the
    rest of the route would otherwise pass through to the service.
    Bundling them in one helper keeps every endpoint's wiring to a
    single line and pins the cross-seam contract: the audit writer
    rides ``repo.session`` (same UoW), and the checker honours
    ``ctx.workspace_id`` for every action key the service touches.

    Public (no leading underscore) so the sibling
    :mod:`app.api.v1.me_schedule` router — which dispatches a self-only
    subset of the same surface — can reuse the wiring without taking
    a dependency on a conventionally module-private name. The cd-2upg
    leaves seam will land its own ``make_seam_pair`` adjacent to the
    leaves router; both share the ``(session, ctx)`` shape because the
    underlying :class:`SqlAlchemyCapabilityChecker` is the one piece
    that crosses both surfaces.
    """
    return (
        SqlAlchemyUserAvailabilityOverrideRepository(session),
        SqlAlchemyCapabilityChecker(session, ctx),
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_user_availability_overrides_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the CRUD + state surface."""
    api = APIRouter(
        prefix="/user_availability_overrides",
        tags=["identity", "user_availability_overrides"],
    )

    @api.get(
        "",
        response_model=UserAvailabilityOverrideListResponse,
        operation_id="user_availability_overrides.list",
        summary="List user_availability_override rows in the caller's workspace",
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
    ) -> UserAvailabilityOverrideListResponse:
        """Cursor-paginated listing with optional filters.

        ``from_`` is the ``?from=`` query alias (Python keyword
        clash). The wire param stays ``from`` — see the
        :data:`_FromFilter` dependency annotation.
        """
        after_id = decode_cursor(cursor)
        filters = UserAvailabilityOverrideListFilter(
            user_id=user_id,
            status=_approved_to_status(approved),
            from_date=from_,
            to_date=to,
        )
        repo, checker = make_seam_pair(session, ctx)
        try:
            views = list_overrides(
                repo,
                checker,
                ctx,
                filters=filters,
                limit=limit,
                after_id=after_id,
            )
        except UserAvailabilityOverridePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc

        page = paginate(views, limit=limit, key_getter=lambda v: v.id)
        return UserAvailabilityOverrideListResponse(
            data=[_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=UserAvailabilityOverrideResponse,
        operation_id="user_availability_overrides.create",
        summary="Create a user_availability_override row",
    )
    def create(
        body: UserAvailabilityOverrideCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserAvailabilityOverrideResponse:
        """Insert a new user_availability_override row.

        Self-submit (``user_id`` omitted or equal to the caller) is
        gated on ``availability_overrides.create_self``. Cross-user
        create is gated on ``availability_overrides.edit_others`` and
        always lands auto-approved. Server computes
        ``approval_required`` per §06 "Approval logic (hybrid model)";
        when ``False``, the row also auto-approves.
        """
        service_body = UserAvailabilityOverrideCreate.model_validate(body.model_dump())
        repo, checker = make_seam_pair(session, ctx)
        try:
            view = create_override(repo, checker, ctx, body=service_body)
        except UserAvailabilityOverridePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        except UserAvailabilityOverrideInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.patch(
        "/{override_id}",
        response_model=UserAvailabilityOverrideResponse,
        operation_id="user_availability_overrides.update",
        summary="Partial update of a pending user_availability_override row",
    )
    def update(
        override_id: str,
        body: UserAvailabilityOverrideUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserAvailabilityOverrideResponse:
        """Update mutable fields on a pending override.

        Pending-only — an approved override rejects with 409. The
        requester or a holder of ``availability_overrides.edit_others``
        may mutate.
        """
        sent = body.model_fields_set
        service_body = UserAvailabilityOverrideUpdate.model_validate(
            {f: getattr(body, f) for f in sent}
        )
        repo, checker = make_seam_pair(session, ctx)
        try:
            view = update_override(
                repo, checker, ctx, override_id=override_id, body=service_body
            )
        except UserAvailabilityOverrideNotFound as exc:
            raise _http_for_not_found() from exc
        except UserAvailabilityOverridePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        except UserAvailabilityOverrideTransitionForbidden as exc:
            raise _http_for_transition(exc) from exc
        except UserAvailabilityOverrideInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.post(
        "/{override_id}/approve",
        response_model=UserAvailabilityOverrideResponse,
        operation_id="user_availability_overrides.approve",
        summary="Approve a pending user_availability_override row",
    )
    def approve(
        override_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> UserAvailabilityOverrideResponse:
        """Stamp ``approved_at`` + ``approved_by`` on a pending row.

        Always requires ``availability_overrides.edit_others``. An
        already-approved row collapses to 409.
        """
        repo, checker = make_seam_pair(session, ctx)
        try:
            view = approve_override(repo, checker, ctx, override_id=override_id)
        except UserAvailabilityOverrideNotFound as exc:
            raise _http_for_not_found() from exc
        except UserAvailabilityOverridePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        except UserAvailabilityOverrideTransitionForbidden as exc:
            raise _http_for_transition(exc) from exc
        return _view_to_response(view)

    @api.post(
        "/{override_id}/reject",
        response_model=UserAvailabilityOverrideResponse,
        operation_id="user_availability_overrides.reject",
        summary="Reject (soft-delete) a pending user_availability_override row",
    )
    def reject(
        override_id: str,
        ctx: _Ctx,
        session: _Db,
        body: UserAvailabilityOverrideRejectRequest | None = None,
    ) -> UserAvailabilityOverrideResponse:
        """Soft-delete a pending row with optional rejection reason.

        §06 doesn't pin a persistent ``rejected`` state on
        ``user_availability_override``; v1 ships rejection as a
        tombstone + folded-in ``reason`` so the worker keeps the
        rationale visible. Always requires
        ``availability_overrides.edit_others``.
        """
        reason = body.reason_md if body is not None else None
        repo, checker = make_seam_pair(session, ctx)
        try:
            view = reject_override(
                repo, checker, ctx, override_id=override_id, reason_md=reason
            )
        except UserAvailabilityOverrideNotFound as exc:
            raise _http_for_not_found() from exc
        except UserAvailabilityOverridePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        except UserAvailabilityOverrideTransitionForbidden as exc:
            raise _http_for_transition(exc) from exc
        return _view_to_response(view)

    @api.delete(
        "/{override_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="user_availability_overrides.delete",
        summary=(
            "Soft-delete a user_availability_override row "
            "(worker withdraw / manager revoke)"
        ),
    )
    def delete(
        override_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Stamp ``deleted_at`` and return 204.

        Authorisation: requester or
        ``availability_overrides.edit_others``. A repeated DELETE on
        an already-deleted row surfaces 404 (the tombstone filter
        hides the row from :func:`_load_row`); the spec leaves the
        choice between 404 and 204-idempotent open and the
        loud-on-double-click reading is what we want here.
        """
        repo, checker = make_seam_pair(session, ctx)
        try:
            delete_override(repo, checker, ctx, override_id=override_id)
        except UserAvailabilityOverrideNotFound as exc:
            raise _http_for_not_found() from exc
        except UserAvailabilityOverridePermissionDenied as exc:
            raise _http_for_permission_denied(exc) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


router = build_user_availability_overrides_router()
