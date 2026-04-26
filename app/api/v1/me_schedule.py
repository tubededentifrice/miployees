"""Self-service ``/me/*`` HTTP router (cd-6uij) — schedule + leaves + overrides.

Mounted inside ``/w/<slug>/api/v1`` by the app factory. Surface per
``docs/specs/12-rest-api.md`` §"Self-service shortcuts":

```
GET    /me/schedule                       # self-only calendar feed
POST   /me/leaves                         # self-only leave create
GET    /me/availability_overrides         # self-only override list
POST   /me/availability_overrides         # self-only override create
```

Tags: ``identity`` + ``me`` so the OpenAPI surface clusters the verbs
alongside the rest of the identity context (matching the sibling
``user_leaves`` / ``user_availability_overrides`` routers, which tag
themselves under ``identity`` + their own resource tag).

**Self-only by construction.** Each POST forces ``user_id =
ctx.actor_id`` before delegating to the underlying domain service.
The ``user_id`` field is **deliberately absent** from the wire
request body (Pydantic ``extra="forbid"`` rejects an explicit
``user_id`` field with a 422). The cleaner shape — refusing the
field at the schema layer — is preferable to a route-level 403 check
because it keeps the semantic clear: this surface only ever speaks
for the caller.

The :func:`get_self_schedule` aggregator is workspace-scoped: every
SELECT keys on ``ctx.actor_id`` + ``ctx.workspace_id`` so a worker
cannot leak another user's data through the feed.

See ``docs/specs/12-rest-api.md`` §"Self-service shortcuts",
``docs/specs/06-tasks-and-scheduling.md`` §"user_leave",
§"user_availability_overrides", §"Weekly availability".
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from app.api.v1.user_availability_overrides import (
    UserAvailabilityOverrideListResponse,
    UserAvailabilityOverrideResponse,
)
from app.api.v1.user_availability_overrides import (
    _http_for_invariant as _http_for_override_invariant,
)
from app.api.v1.user_availability_overrides import (
    _view_to_response as _override_view_to_response,
)
from app.api.v1.user_availability_overrides import (
    make_seam_pair as make_override_seam_pair,
)
from app.api.v1.user_leaves import (
    UserLeaveResponse,
)
from app.api.v1.user_leaves import (
    _http_for_invariant as _http_for_leave_invariant,
)
from app.api.v1.user_leaves import _view_to_response as _leave_view_to_response
from app.domain.identity.me_schedule import (
    PendingItems,
    PublicHolidayView,
    SchedulePayload,
    TaskRefView,
    WeeklySlotView,
    aggregate_schedule,
)
from app.domain.identity.user_availability_overrides import (
    UserAvailabilityOverrideCreate,
    UserAvailabilityOverrideInvariantViolated,
    UserAvailabilityOverrideListFilter,
    UserAvailabilityOverridePermissionDenied,
    create_override,
    list_overrides,
)
from app.domain.identity.user_leaves import (
    UserLeaveCategory,
    UserLeaveCreate,
    UserLeaveInvariantViolated,
    UserLeavePermissionDenied,
    create_leave,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "MeAvailabilityOverrideCreateRequest",
    "MeLeaveCreateRequest",
    "MePendingItemsResponse",
    "MePublicHolidayResponse",
    "MeScheduleResponse",
    "MeTaskRefResponse",
    "MeWeeklySlotResponse",
    "build_me_schedule_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

_MAX_NOTE_LEN = 20_000
_MAX_REASON_LEN = 20_000


# ---------------------------------------------------------------------------
# Wire-facing shapes — request bodies
# ---------------------------------------------------------------------------


class MeLeaveCreateRequest(BaseModel):
    """Request body for ``POST /me/leaves``.

    ``user_id`` is **deliberately absent**: the router forces
    ``user_id = ctx.actor_id`` before delegating to
    :func:`~app.domain.identity.user_leaves.create_leave`. An explicit
    ``user_id`` in the body lands as a 422 ``unknown_field`` from
    Pydantic ``extra="forbid"`` — the cleanest way to keep the "self
    only" invariant honest at the wire.
    """

    model_config = ConfigDict(extra="forbid")

    starts_on: date
    ends_on: date
    category: UserLeaveCategory
    note_md: str | None = Field(default=None, max_length=_MAX_NOTE_LEN)

    @model_validator(mode="after")
    def _validate_window(self) -> MeLeaveCreateRequest:
        """Reject ``ends_on < starts_on`` at the DTO layer.

        Mirrors :class:`~app.domain.identity.user_leaves.UserLeaveCreate`
        so a malformed window surfaces as a 422 from FastAPI's
        validation envelope rather than as a 500 from the service-
        layer raise.
        """
        if self.ends_on < self.starts_on:
            raise ValueError("ends_on must be on or after starts_on")
        return self


class MeAvailabilityOverrideCreateRequest(BaseModel):
    """Request body for ``POST /me/availability_overrides``.

    ``user_id`` is **deliberately absent** for the same reason as
    :class:`MeLeaveCreateRequest`. Hours pairing + backwards-window
    rejection mirror
    :class:`~app.domain.identity.user_availability_overrides.UserAvailabilityOverrideCreate`.
    """

    model_config = ConfigDict(extra="forbid")

    date: date
    available: bool
    starts_local: time | None = None
    ends_local: time | None = None
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LEN)

    @model_validator(mode="after")
    def _validate_hours(self) -> MeAvailabilityOverrideCreateRequest:
        """Enforce BOTH-OR-NEITHER + ``ends_local > starts_local``."""
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


# ---------------------------------------------------------------------------
# Wire-facing shapes — schedule response
# ---------------------------------------------------------------------------


class MeWeeklySlotResponse(BaseModel):
    """One row of the caller's standing weekly availability pattern."""

    weekday: int
    starts_local: time | None
    ends_local: time | None


class MeTaskRefResponse(BaseModel):
    """Lightweight reference to an :class:`Occurrence` assigned to the caller."""

    id: str
    scheduled_for_local: str


class MePublicHolidayResponse(BaseModel):
    """Read projection of a :class:`PublicHoliday` row covering the window."""

    id: str
    name: str
    date: date
    country: str | None
    scheduling_effect: str
    reduced_starts_local: time | None
    reduced_ends_local: time | None
    payroll_multiplier: Decimal | None


class MePendingItemsResponse(BaseModel):
    """Pending leaves + overrides bucketed away from the live precedence stack."""

    leaves: list[UserLeaveResponse]
    overrides: list[UserAvailabilityOverrideResponse]


class MeScheduleResponse(BaseModel):
    """Aggregated calendar feed for the caller across ``[from, to]``.

    The ``from`` / ``to`` fields use Pydantic aliases so the wire
    payload reads naturally (``from`` is a Python keyword on the
    response side too, even though the wire shape is stable).
    """

    model_config = ConfigDict(populate_by_name=True)

    from_date: date = Field(serialization_alias="from", validation_alias="from")
    to_date: date = Field(serialization_alias="to", validation_alias="to")
    rota: list[MeWeeklySlotResponse]
    tasks: list[MeTaskRefResponse]
    leaves: list[UserLeaveResponse]
    overrides: list[UserAvailabilityOverrideResponse]
    holidays: list[MePublicHolidayResponse]
    pending: MePendingItemsResponse


# ---------------------------------------------------------------------------
# Query dependencies
# ---------------------------------------------------------------------------


_FromQuery = Annotated[
    date | None,
    Query(
        alias="from",
        description=(
            "Inclusive lower bound on the schedule window (ISO date). "
            "Defaults to today."
        ),
    ),
]


_ToQuery = Annotated[
    date | None,
    Query(
        alias="to",
        description=(
            "Inclusive upper bound on the schedule window (ISO date). "
            "Defaults to today + 14 days."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _weekly_slot_to_response(slot: WeeklySlotView) -> MeWeeklySlotResponse:
    return MeWeeklySlotResponse(
        weekday=slot.weekday,
        starts_local=slot.starts_local,
        ends_local=slot.ends_local,
    )


def _task_ref_to_response(ref: TaskRefView) -> MeTaskRefResponse:
    return MeTaskRefResponse(id=ref.id, scheduled_for_local=ref.scheduled_for_local)


def _holiday_to_response(view: PublicHolidayView) -> MePublicHolidayResponse:
    return MePublicHolidayResponse(
        id=view.id,
        name=view.name,
        date=view.date,
        country=view.country,
        scheduling_effect=view.scheduling_effect,
        reduced_starts_local=view.reduced_starts_local,
        reduced_ends_local=view.reduced_ends_local,
        payroll_multiplier=view.payroll_multiplier,
    )


def _pending_to_response(pending: PendingItems) -> MePendingItemsResponse:
    return MePendingItemsResponse(
        leaves=[_leave_view_to_response(v) for v in pending.leaves],
        overrides=[_override_view_to_response(v) for v in pending.overrides],
    )


def _payload_to_response(payload: SchedulePayload) -> MeScheduleResponse:
    return MeScheduleResponse(
        from_date=payload.from_date,
        to_date=payload.to_date,
        rota=[_weekly_slot_to_response(s) for s in payload.rota],
        tasks=[_task_ref_to_response(t) for t in payload.tasks],
        leaves=[_leave_view_to_response(v) for v in payload.leaves],
        overrides=[_override_view_to_response(v) for v in payload.overrides],
        holidays=[_holiday_to_response(h) for h in payload.holidays],
        pending=_pending_to_response(payload.pending),
    )


def _http_for_window() -> HTTPException:
    """Return a 422 envelope when the caller sends a backwards window."""
    return HTTPException(
        status_code=422,
        detail={
            "error": "invalid_field",
            "field": "to",
            "message": "to must be on or after from",
        },
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_me_schedule_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for the self-service surface."""
    api = APIRouter(prefix="/me", tags=["identity", "me"])

    @api.get(
        "/schedule",
        response_model=MeScheduleResponse,
        operation_id="me.schedule.get",
        summary="Aggregated calendar feed for the caller",
    )
    def get_schedule(
        ctx: _Ctx,
        session: _Db,
        from_: _FromQuery = None,
        to: _ToQuery = None,
    ) -> MeScheduleResponse:
        """Return rota / tasks / approved leaves / overrides / holidays / pending.

        ``from_`` is the ``?from=`` query alias (Python keyword
        clash). The wire param stays ``from`` — see the
        :data:`_FromQuery` dependency annotation. Defaults are
        ``[today, today+14d]`` per §12 "Self-service shortcuts".
        """
        if from_ is not None and to is not None and to < from_:
            raise _http_for_window()
        payload = aggregate_schedule(
            session,
            ctx,
            from_date=from_,
            to_date=to,
        )
        return _payload_to_response(payload)

    @api.post(
        "/leaves",
        status_code=status.HTTP_201_CREATED,
        response_model=UserLeaveResponse,
        operation_id="me.leaves.create",
        summary="Create a leave for the caller (always self-target)",
    )
    def create_self_leave(
        body: MeLeaveCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserLeaveResponse:
        """Forward to :func:`create_leave` with ``user_id = ctx.actor_id``.

        Always lands pending per spec §12 "Self-service shortcuts" —
        ``creates user_leave with approval_required always true``.
        The router passes ``force_pending=True`` so even a manager
        self-submitting through ``/me/leaves`` lands pending; a
        manager wanting to retroactively self-log + auto-approve
        uses the generic ``POST /user_leaves`` endpoint.
        """
        service_body = UserLeaveCreate(
            user_id=ctx.actor_id,
            starts_on=body.starts_on,
            ends_on=body.ends_on,
            category=body.category,
            note_md=body.note_md,
        )
        try:
            view = create_leave(session, ctx, body=service_body, force_pending=True)
        except UserLeavePermissionDenied as exc:
            # ``leaves.create_self`` is auto-allowed to ``all_workers``
            # in the default catalog; a 403 here implies a deployment
            # that revoked it explicitly. Re-raised through the §12
            # 403 envelope so the SPA renders the right banner.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "permission_denied", "action_key": str(exc)},
            ) from exc
        except UserLeaveInvariantViolated as exc:
            raise _http_for_leave_invariant(exc) from exc
        return _leave_view_to_response(view)

    @api.get(
        "/availability_overrides",
        response_model=UserAvailabilityOverrideListResponse,
        operation_id="me.availability_overrides.list",
        summary="List the caller's user_availability_override rows",
    )
    def list_self_overrides(
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> UserAvailabilityOverrideListResponse:
        """Cursor-paginated listing keyed to ``ctx.actor_id``.

        Per spec §12 "Self-service shortcuts": ``self-only list of
        every user_availability_override (any approval state)``. The
        domain helper :func:`list_overrides` is invoked with
        ``user_id = ctx.actor_id`` so a worker cannot widen the
        listing to cross-user; the underlying capability check
        ``availability_overrides.create_self`` is permissive for
        self-target reads (a self-keyed listing is always allowed
        by ``_gate_or_self``).
        """
        after_id = decode_cursor(cursor)
        filters = UserAvailabilityOverrideListFilter(user_id=ctx.actor_id)
        repo, checker = make_override_seam_pair(session, ctx)
        views = list_overrides(
            repo,
            checker,
            ctx,
            filters=filters,
            limit=limit,
            after_id=after_id,
        )
        page = paginate(views, limit=limit, key_getter=lambda v: v.id)
        return UserAvailabilityOverrideListResponse(
            data=[_override_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "/availability_overrides",
        status_code=status.HTTP_201_CREATED,
        response_model=UserAvailabilityOverrideResponse,
        operation_id="me.availability_overrides.create",
        summary="Create an availability override for the caller (always self-target)",
    )
    def create_self_override(
        body: MeAvailabilityOverrideCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserAvailabilityOverrideResponse:
        """Forward to :func:`create_override` with ``user_id = ctx.actor_id``.

        Server computes ``approval_required`` per the §06 "Approval
        logic (hybrid model)" matrix — adding hours auto-approves,
        narrowing or removing requires manager sign-off. The resolved
        state lands on the response so the UI does not need to
        re-derive it.
        """
        service_body = UserAvailabilityOverrideCreate(
            user_id=ctx.actor_id,
            date=body.date,
            available=body.available,
            starts_local=body.starts_local,
            ends_local=body.ends_local,
            reason=body.reason,
        )
        repo, checker = make_override_seam_pair(session, ctx)
        try:
            view = create_override(repo, checker, ctx, body=service_body)
        except UserAvailabilityOverridePermissionDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "permission_denied", "action_key": str(exc)},
            ) from exc
        except UserAvailabilityOverrideInvariantViolated as exc:
            raise _http_for_override_invariant(exc) from exc
        return _override_view_to_response(view)

    return api


router = build_me_schedule_router()
