"""Time context router — shifts clock-in / clock-out + manager edits.

Mounted by the app factory under ``/w/<slug>/api/v1/time``. All
routes require an active :class:`~app.tenancy.WorkspaceContext`.

Routes (cd-whl):

* ``POST /shifts/open`` — worker opens a shift for themselves (or a
  manager opens one for someone else via ``time.edit_others``).
* ``POST /shifts/{shift_id}/close`` — worker closes their own shift
  or a manager closes someone else's via ``time.edit_others``.
* ``PATCH /shifts/{shift_id}`` — manager-only retroactive amend.
* ``GET /shifts`` — list shifts in the workspace (filtered by
  ``user_id`` / ``starts_from`` / ``starts_until`` / ``open_only``).
* ``GET /shifts/{shift_id}`` — read a single shift.

The handlers are thin: unpack the DTO, call the domain service, map
typed errors to HTTP. The UoW (:func:`app.api.deps.db_session`) owns
the transaction boundary; domain code never commits itself.

Module name shadows the stdlib ``time`` module locally — this is a
relative-import-only context module under ``app.api.v1`` so no import
collision is possible.

See ``docs/specs/09-time-payroll-expenses.md`` §"Bookings",
§"Owner and manager adjustments";
``docs/specs/12-rest-api.md`` §"REST API".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.domain.time.shifts import (
    ShiftAlreadyOpen,
    ShiftBoundaryInvalid,
    ShiftClose,
    ShiftEdit,
    ShiftEditForbidden,
    ShiftNotFound,
    ShiftOpen,
    ShiftView,
    close_shift,
    edit_shift,
    get_shift,
    list_open_shifts,
    list_shifts,
    open_shift,
)
from app.tenancy import WorkspaceContext

__all__ = ["router"]


router = APIRouter(tags=["time"])


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ShiftPayload(BaseModel):
    """HTTP projection of :class:`~app.domain.time.shifts.ShiftView`.

    A Pydantic model rather than re-exporting the frozen dataclass so
    FastAPI's OpenAPI generator emits a named component schema the
    SPA can pattern-match on. Mirrors the read shape of the domain
    view one-to-one — no filtering, no derived fields.
    """

    id: str
    workspace_id: str
    user_id: str
    starts_at: datetime
    ends_at: datetime | None
    property_id: str | None
    source: str
    notes_md: str | None
    approved_by: str | None
    approved_at: datetime | None

    @classmethod
    def from_view(cls, view: ShiftView) -> ShiftPayload:
        """Copy a :class:`ShiftView` into its HTTP payload shape."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            user_id=view.user_id,
            starts_at=view.starts_at,
            ends_at=view.ends_at,
            property_id=view.property_id,
            source=view.source,
            notes_md=view.notes_md,
            approved_by=view.approved_by,
            approved_at=view.approved_at,
        )


class ShiftListResponse(BaseModel):
    """Response body for ``GET /shifts``.

    Always-present ``items`` key so the SPA can treat the response
    as paginated-able from day one — adding a ``next_cursor`` field
    later is non-breaking.
    """

    items: list[ShiftPayload]


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _http_for_shift_error(exc: Exception) -> HTTPException:
    """Map a domain shift error to the router's HTTP response shape.

    Keeps the mapping centralised so every route returns the same
    ``{"error": "<code>"}`` envelope for the same domain type —
    the SPA / CLI can switch on ``body.detail.error`` without
    parsing the status code.
    """
    if isinstance(exc, ShiftNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        )
    if isinstance(exc, ShiftAlreadyOpen):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "already_open",
                "existing_shift_id": exc.existing_shift_id,
            },
        )
    if isinstance(exc, ShiftBoundaryInvalid):
        # Use the literal 422 rather than the starlette constant —
        # starlette renamed ``HTTP_422_UNPROCESSABLE_ENTITY`` →
        # ``HTTP_422_UNPROCESSABLE_CONTENT`` in 2024 and emits a
        # deprecation warning on the old name. The integer is stable
        # across versions. (Same trick used in
        # :mod:`app.authz.enforce._misuse_to_http`.)
        return HTTPException(
            status_code=422,
            detail={"error": "invalid_window", "message": str(exc)},
        )
    if isinstance(exc, ShiftEditForbidden):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden"},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/shifts/open",
    status_code=status.HTTP_201_CREATED,
    response_model=ShiftPayload,
    operation_id="time.open_shift",
    summary="Open (clock-in) a shift",
)
def post_open_shift(
    body: ShiftOpen,
    ctx: _Ctx,
    session: _Db,
) -> ShiftPayload:
    """Open a fresh shift for the caller (or the body's ``user_id``)."""
    try:
        view = open_shift(
            session,
            ctx,
            user_id=body.user_id,
            property_id=body.property_id,
            source=body.source,
            notes_md=body.notes_md,
        )
    except (ShiftAlreadyOpen, ShiftEditForbidden) as exc:
        raise _http_for_shift_error(exc) from exc

    return ShiftPayload.from_view(view)


@router.post(
    "/shifts/{shift_id}/close",
    response_model=ShiftPayload,
    operation_id="time.close_shift",
    summary="Close (clock-out) a shift",
)
def post_close_shift(
    shift_id: str,
    body: ShiftClose,
    ctx: _Ctx,
    session: _Db,
) -> ShiftPayload:
    """Close the shift identified by ``shift_id``."""
    try:
        view = close_shift(
            session,
            ctx,
            shift_id=shift_id,
            ends_at=body.ends_at,
        )
    except (ShiftNotFound, ShiftBoundaryInvalid, ShiftEditForbidden) as exc:
        raise _http_for_shift_error(exc) from exc

    return ShiftPayload.from_view(view)


@router.patch(
    "/shifts/{shift_id}",
    response_model=ShiftPayload,
    operation_id="time.edit_shift",
    summary="Manager edit of a shift",
)
def patch_edit_shift(
    shift_id: str,
    body: ShiftEdit,
    ctx: _Ctx,
    session: _Db,
) -> ShiftPayload:
    """Patch the mutable fields of a shift."""
    kwargs: dict[str, Any] = {}
    # Forward only fields the client actually sent so "None ==
    # leave untouched" semantics hold. The PATCH DTO is all-optional
    # with ``None`` defaults, so we walk ``model_fields_set`` to
    # know which were explicit.
    for field in body.model_fields_set:
        kwargs[field] = getattr(body, field)

    try:
        view = edit_shift(session, ctx, shift_id=shift_id, **kwargs)
    except (ShiftNotFound, ShiftBoundaryInvalid, ShiftEditForbidden) as exc:
        raise _http_for_shift_error(exc) from exc

    return ShiftPayload.from_view(view)


@router.get(
    "/shifts",
    response_model=ShiftListResponse,
    operation_id="time.list_shifts",
    summary="List shifts in the workspace",
)
def get_list_shifts(
    ctx: _Ctx,
    session: _Db,
    user_id: Annotated[str | None, Query(max_length=40)] = None,
    starts_from: Annotated[datetime | None, Query()] = None,
    starts_until: Annotated[datetime | None, Query()] = None,
    open_only: Annotated[bool, Query()] = False,
) -> ShiftListResponse:
    """Return every shift matching the optional filters."""
    if open_only:
        views = list_open_shifts(session, ctx, user_id=user_id)
    else:
        views = list_shifts(
            session,
            ctx,
            user_id=user_id,
            starts_from=starts_from,
            starts_until=starts_until,
        )
    return ShiftListResponse(items=[ShiftPayload.from_view(v) for v in views])


@router.get(
    "/shifts/{shift_id}",
    response_model=ShiftPayload,
    operation_id="time.get_shift",
    summary="Read a single shift",
)
def get_one_shift(
    shift_id: str,
    ctx: _Ctx,
    session: _Db,
) -> ShiftPayload:
    """Return the shift identified by ``shift_id``."""
    try:
        view = get_shift(session, ctx, shift_id=shift_id)
    except ShiftNotFound as exc:
        raise _http_for_shift_error(exc) from exc
    return ShiftPayload.from_view(view)
