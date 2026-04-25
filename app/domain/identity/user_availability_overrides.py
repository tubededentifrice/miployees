"""User-availability-override CRUD + hybrid approval state machine (cd-uqw1).

The :class:`~app.adapters.db.availability.models.UserAvailabilityOverride`
row carries a worker's date-specific tweak to their weekly availability
pattern. Per ``docs/specs/06-tasks-and-scheduling.md`` §"user_availability_overrides"
and §"Availability precedence stack", **only approved** overrides
substitute for the weekly pattern — pending overrides do not affect
the candidate pool. Adding hours self-approves; reducing hours requires
owner / manager approval.

This module is the single write seam for the row. The HTTP router in
:mod:`app.api.v1.user_availability_overrides` is a thin DTO passthrough.

Public surface:

* **DTOs** — :class:`UserAvailabilityOverrideCreate`,
  :class:`UserAvailabilityOverrideUpdate`,
  :class:`UserAvailabilityOverrideListFilter`,
  :class:`UserAvailabilityOverrideView`.
* **Service functions** — :func:`list_overrides` (cursor-paginated,
  with filters), :func:`get_override`, :func:`create_override`,
  :func:`update_override`, :func:`approve_override`,
  :func:`reject_override`, :func:`delete_override`.
* **Errors** — :class:`UserAvailabilityOverrideNotFound`,
  :class:`UserAvailabilityOverrideInvariantViolated`,
  :class:`UserAvailabilityOverridePermissionDenied`,
  :class:`UserAvailabilityOverrideTransitionForbidden`.

**Capabilities.** Writes gate through :func:`app.authz.require`:

* ``availability_overrides.create_self`` — self-submit (auto-allowed
  to ``all_workers``).
* ``availability_overrides.edit_others`` — manager retroactive create
  / edit / approve / reject / delete on someone else's row (managers +
  owners by default).
* ``availability_overrides.view_others`` — listing or reading other
  users' rows (managers + owners by default).

There is no separate ``availability_overrides.approve`` /
``availability_overrides.manage`` key: approve / reject collapse to
``edit_others`` because every manager who can edit on behalf of a
worker can also stamp the approval — same shape as the sibling
``leaves.*`` keys.

**Hybrid approval (§06 "Approval logic (hybrid model)").** On create,
the server computes ``approval_required`` by comparing the override
against the user's weekly pattern row for that date's weekday:

* Weekly off + override available=true (add work day) → not required.
* Weekly working + override available=false (remove work day) → required.
* Weekly working + override extends or matches the window → not required.
* Weekly working + override narrows the window → required.
* Weekly off + override available=false (confirm off) → not required.

When ``approval_required`` is ``False``, the row lands with
``approved_at = now`` + ``approved_by = ctx.actor_id`` so it enters
the precedence stack immediately. When ``True``, ``approved_at`` /
``approved_by`` stay null until an owner/manager explicitly approves.

**Auto-approve on owner/manager submit.** When the caller already
holds ``availability_overrides.edit_others``, :func:`create_override`
stamps ``approved_at`` regardless of ``approval_required`` — a
manager scheduling a date-specific override shouldn't have to walk
through their own approval queue. The check routes through
:func:`app.authz.require` so the auto-approve trigger and every
other ``edit_others`` gate share the same authority.

**Reject = soft-delete with reason.** §06 doesn't pin a persistent
``rejected`` state on ``user_availability_override`` (the schema only
carries ``approved_at`` / ``approved_by``). Rather than carve a new
column or overload ``approved_by = NULL`` as a marker,
:func:`reject_override` soft-deletes the row (stamps ``deleted_at``)
and folds the rejection reason into ``reason`` if provided. The
``user_availability_override.rejected`` audit row preserves the full
state transition for the worker's complaints inbox.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation writes
one :mod:`app.audit` row in the same transaction.

**Tenancy.** The ORM tenant filter auto-narrows every SELECT on
``user_availability_override``; the service re-asserts the
``workspace_id = ctx.workspace_id`` predicate explicitly as
defence-in-depth.

See ``docs/specs/06-tasks-and-scheduling.md`` §"user_availability_overrides",
§"Approval logic (hybrid model)", §"Availability precedence stack";
``docs/specs/05-employees-and-roles.md`` §"Action catalog" rows
``availability_overrides.create_self`` /
``availability_overrides.edit_others`` /
``availability_overrides.view_others``;
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.availability.models import (
    UserAvailabilityOverride,
    UserWeeklyAvailability,
)
from app.audit import write_audit
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "UserAvailabilityOverrideCreate",
    "UserAvailabilityOverrideInvariantViolated",
    "UserAvailabilityOverrideListFilter",
    "UserAvailabilityOverrideNotFound",
    "UserAvailabilityOverridePermissionDenied",
    "UserAvailabilityOverrideTransitionForbidden",
    "UserAvailabilityOverrideUpdate",
    "UserAvailabilityOverrideView",
    "approve_override",
    "create_override",
    "delete_override",
    "get_override",
    "list_overrides",
    "reject_override",
    "update_override",
]


_MAX_REASON_LEN = 20_000
_MAX_ID_LEN = 64


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UserAvailabilityOverrideNotFound(LookupError):
    """The target ``user_availability_override`` row is invisible to the caller.

    404-equivalent. Fired when the id is unknown, soft-deleted, or
    lives in a different workspace — all three collapse to the same
    surface per §01 "tenant surface is not enumerable".
    """


class UserAvailabilityOverrideInvariantViolated(ValueError):
    """Write would violate a §06 "user_availability_overrides" invariant.

    422-equivalent. Thrown when ``starts_local`` / ``ends_local`` are
    half-set, the window is backwards, or another override already
    occupies the (user, date) pair.
    """


class UserAvailabilityOverridePermissionDenied(PermissionError):
    """Caller lacks the capability for the attempted action.

    403-equivalent. Wraps the underlying :class:`~app.authz.PermissionDenied`
    so the router maps a single domain exception to the
    ``user_availability_override``-specific 403 envelope.
    """


class UserAvailabilityOverrideTransitionForbidden(ValueError):
    """The override is not in a state the requested transition accepts.

    409-equivalent. Fires on:

    * editing a non-pending override (``approved_at`` set or row
      tombstoned);
    * approving an already-approved override;
    * rejecting an already-rejected (soft-deleted) override.

    Idempotency at the HTTP layer is the router's call — the service
    surfaces every "wrong state" as this single exception so the
    router can decide whether to short-circuit to 200 or surface 409.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class UserAvailabilityOverrideCreate(BaseModel):
    """Request body for :func:`create_override`.

    ``user_id`` defaults to the caller (``ctx.actor_id``) when ``None``.
    Workers self-requesting omit the field; managers creating a
    retroactive entry on someone else's behalf send it explicitly.

    ``starts_local`` and ``ends_local`` are paired — both set or both
    null per the §06 BOTH-OR-NEITHER invariant. When ``available`` is
    ``True`` and both are null, the assignment algorithm falls back to
    the weekly pattern's hours; when ``available`` is ``False``, the
    pair must stay null (a "not working" override has no hours).
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    date: date
    available: bool
    starts_local: time | None = None
    ends_local: time | None = None
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LEN)

    @model_validator(mode="after")
    def _validate_hours(self) -> UserAvailabilityOverrideCreate:
        """Enforce BOTH-OR-NEITHER + ``ends_local > starts_local``.

        Mirrors the DB CHECK ``(starts IS NULL AND ends IS NULL) OR
        (starts IS NOT NULL AND ends IS NOT NULL)`` so a 422 lands at
        the boundary instead of a generic IntegrityError at flush
        time. Also rejects backwards windows (``ends <= starts``) and
        the half-set "available=False with hours" shape because §06
        treats a not-working override as carrying no hours.
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


class UserAvailabilityOverrideUpdate(BaseModel):
    """Partial-update body for :func:`update_override`.

    Explicit-sparse — only sent fields land. ``user_id`` and ``date``
    are deliberately frozen because re-keying a row to a different
    ``(user, date)`` pair would orphan its audit chain and could
    collide with the ``UNIQUE(workspace_id, user_id, date)`` invariant.
    Callers wanting to move an override should soft-delete and
    re-create.

    ``approval_required`` is **not** mutable through PATCH — it's a
    derived field stamped at create time. Callers wanting to change
    the approval shape edit ``starts_local`` / ``ends_local`` /
    ``available`` and the resulting state remains pinned to the
    original ``approval_required`` value (the spec doesn't ask for a
    re-derive on PATCH; pending stays pending until approve or
    reject).
    """

    model_config = ConfigDict(extra="forbid")

    available: bool | None = None
    starts_local: time | None = None
    ends_local: time | None = None
    reason: str | None = Field(default=None, max_length=_MAX_REASON_LEN)


class UserAvailabilityOverrideListFilter(BaseModel):
    """Cursor-page filter for :func:`list_overrides`.

    Fields default to ``None`` so an unfiltered listing is the empty
    filter. The router translates the §12 ``?approved=true|false``
    query param into ``status`` on the way through; that
    self-documenting alias survives in the wire shape because callers
    routinely think in "approved / pending" rather than the
    DB-internal nullability of ``approved_at``.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    status: Literal["approved", "pending"] | None = None
    from_date: date | None = None
    to_date: date | None = None


@dataclass(frozen=True, slots=True)
class UserAvailabilityOverrideView:
    """Immutable read projection of a ``user_availability_override`` row.

    Returned by every service read + write. Both ``approval_required``
    and ``approved_at`` are surfaced — the former records "did this
    override originally need approval?" (so the audit log can replay
    the decision without re-running the resolver); the latter is the
    live "is this override active in the precedence stack?" flag. The
    view deliberately omits ``deleted_at`` from the wire-only path —
    the router keeps it visible because reject leaves the row
    soft-deleted with a non-null ``deleted_at`` the worker should
    see in their complaints inbox.
    """

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_view(row: UserAvailabilityOverride) -> UserAvailabilityOverrideView:
    return UserAvailabilityOverrideView(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        date=row.date,
        available=row.available,
        starts_local=row.starts_local,
        ends_local=row.ends_local,
        reason=row.reason,
        approval_required=row.approval_required,
        approved_at=row.approved_at,
        approved_by=row.approved_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _view_to_diff_dict(view: UserAvailabilityOverrideView) -> dict[str, Any]:
    """Flatten a view into a JSON-safe audit payload.

    Stringifies dates / times / datetimes so the audit ``diff`` column
    (JSON1 on SQLite, JSONB on Postgres) accepts the payload without a
    custom encoder.
    """
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "user_id": view.user_id,
        "date": view.date.isoformat(),
        "available": view.available,
        "starts_local": (
            view.starts_local.isoformat() if view.starts_local is not None else None
        ),
        "ends_local": (
            view.ends_local.isoformat() if view.ends_local is not None else None
        ),
        "reason": view.reason,
        "approval_required": view.approval_required,
        "approved_at": (
            view.approved_at.isoformat() if view.approved_at is not None else None
        ),
        "approved_by": view.approved_by,
    }


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    override_id: str,
    include_deleted: bool = False,
) -> UserAvailabilityOverride:
    """Return the row or raise :class:`UserAvailabilityOverrideNotFound`."""
    stmt = select(UserAvailabilityOverride).where(
        UserAvailabilityOverride.id == override_id,
        UserAvailabilityOverride.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(UserAvailabilityOverride.deleted_at.is_(None))
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise UserAvailabilityOverrideNotFound(override_id)
    return row


def _load_weekly_for_weekday(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    weekday: int,
) -> UserWeeklyAvailability | None:
    """Return the weekly pattern row for ``(user, weekday)``, if any.

    A user with no row at all for that weekday is treated as "off" by
    the approval-logic walk in :func:`_compute_approval_required` —
    same surface as a row with both ``starts_local`` and ``ends_local``
    null. Centralising the lookup here keeps the approval calculator
    free of SQLAlchemy concerns.
    """
    stmt = select(UserWeeklyAvailability).where(
        UserWeeklyAvailability.workspace_id == ctx.workspace_id,
        UserWeeklyAvailability.user_id == user_id,
        UserWeeklyAvailability.weekday == weekday,
    )
    return session.scalars(stmt).one_or_none()


def _compute_approval_required(
    *,
    weekly: UserWeeklyAvailability | None,
    override_available: bool,
    override_starts: time | None,
    override_ends: time | None,
) -> bool:
    """Walk the §06 hybrid-approval table and return ``approval_required``.

    Pure function — takes the resolved weekly row (``None`` when the
    user has no pattern stored for that weekday, treated as "off")
    plus the override's three approval-relevant fields. The caller
    feeds it the inputs and stamps the result on the row.

    Cases (mirrors §06 "Approval logic (hybrid model)" verbatim):

    1. Weekly off + override available=True → adding a work day → False.
    2. Weekly off + override available=False → confirming off → False.
    3. Weekly working + override available=False → removing day → True.
    4. Weekly working + override available=True with null hours → use
       weekly hours (per §06 invariant) — equivalent → False.
    5. Weekly working + override available=True with hours that
       extend or match → False.
    6. Weekly working + override available=True with hours that
       narrow → True.

    "Narrows" means the override window does not contain the weekly
    window: ``override_starts > weekly.starts_local`` or
    ``override_ends < weekly.ends_local``. "Extends or matches" is the
    biconditional negative.
    """
    weekly_working = (
        weekly is not None
        and weekly.starts_local is not None
        and weekly.ends_local is not None
    )

    if not weekly_working:
        # Cases 1 + 2: weekly off. Override either adds (available=True)
        # or confirms off (available=False) — both auto-approved.
        return False

    if not override_available:
        # Case 3: weekly working, override removes the work day.
        return True

    # Cases 4-6: weekly working, override available=True. ``weekly``
    # is non-None and both starts / ends are non-None per the
    # ``weekly_working`` predicate above (mypy can't see that — the
    # asserts narrow Optional[time] → time without changing semantics).
    assert weekly is not None
    weekly_starts = weekly.starts_local
    weekly_ends = weekly.ends_local
    assert weekly_starts is not None
    assert weekly_ends is not None

    if override_starts is None or override_ends is None:
        # Case 4: null hours fall back to the weekly pattern → no
        # change in coverage → no approval required.
        return False

    # Cases 5 + 6: compare windows. Narrowing on either edge requires
    # approval; extension on both edges (or equal) auto-approves.
    return override_starts > weekly_starts or override_ends < weekly_ends


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

    Wraps :func:`app.authz.require` and translates a caller-bug
    (unknown key / invalid scope) into :class:`RuntimeError` so the
    router can surface it as 500 without confusing it with the 403
    that a genuine :class:`~app.authz.PermissionDenied` produces.
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


def _gate_or_self(
    session: Session,
    ctx: WorkspaceContext,
    *,
    target_user_id: str,
    cross_user_action: str,
) -> None:
    """Pass for the self-target case; require ``cross_user_action`` otherwise.

    Centralises the "requester-or-manager" rule shared by every
    cross-user write in this service. Raising
    :class:`UserAvailabilityOverridePermissionDenied` (not the bare
    :class:`~app.authz.PermissionDenied`) lets the router's error
    map stay narrow — one domain exception type per 403 shape.
    """
    if target_user_id == ctx.actor_id:
        return
    try:
        _require_capability(session, ctx, action_key=cross_user_action)
    except PermissionDenied as exc:
        raise UserAvailabilityOverridePermissionDenied(str(exc)) from exc


def _can_edit_others(session: Session, ctx: WorkspaceContext) -> bool:
    """Return ``True`` iff the caller holds ``availability_overrides.edit_others``.

    Mirrors :func:`app.domain.identity.user_leaves._can_edit_others`:
    the canonical "is this caller a manager / owner" question routes
    through the action catalog so the auto-approve trigger shares its
    authority with every other ``edit_others`` gate in this module.
    """
    try:
        require(
            session,
            ctx,
            action_key="availability_overrides.edit_others",
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
        )
    except PermissionDenied:
        return False
    except (UnknownActionKey, InvalidScope) as exc:
        raise RuntimeError(
            "authz catalog misconfigured for "
            f"'availability_overrides.edit_others': {exc!s}"
        ) from exc
    return True


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_overrides(
    session: Session,
    ctx: WorkspaceContext,
    *,
    filters: UserAvailabilityOverrideListFilter | None = None,
    limit: int,
    after_id: str | None = None,
) -> Sequence[UserAvailabilityOverrideView]:
    """Cursor-paginated listing of live ``user_availability_override`` rows.

    Returns up to ``limit + 1`` rows so the router's
    :func:`~app.api.pagination.paginate` helper can compute
    ``has_more`` without a second query. Rows ordered by ``id ASC``
    (ULID → time-ordered) so the forward cursor is deterministic.

    Authorisation:

    * Listing without a ``user_id`` filter is the manager inbox view
      and requires ``availability_overrides.view_others``.
    * Listing with ``user_id == ctx.actor_id`` is always allowed.
    * Listing with a different ``user_id`` requires
      ``availability_overrides.view_others``.

    Filters:

    * ``status='approved'`` → ``approved_at IS NOT NULL``.
    * ``status='pending'`` → ``approved_at IS NULL``.
    * ``from_date`` / ``to_date`` narrow the date window —
      ``from_date`` filters rows with ``date >= from_date``,
      ``to_date`` filters rows with ``date <= to_date``.
    """
    resolved = filters if filters is not None else UserAvailabilityOverrideListFilter()

    target_user_id = resolved.user_id
    if target_user_id is None:
        # Manager inbox — no per-user filter means cross-user surface.
        try:
            _require_capability(
                session, ctx, action_key="availability_overrides.view_others"
            )
        except PermissionDenied as exc:
            raise UserAvailabilityOverridePermissionDenied(str(exc)) from exc
    else:
        _gate_or_self(
            session,
            ctx,
            target_user_id=target_user_id,
            cross_user_action="availability_overrides.view_others",
        )

    stmt = select(UserAvailabilityOverride).where(
        UserAvailabilityOverride.workspace_id == ctx.workspace_id,
        UserAvailabilityOverride.deleted_at.is_(None),
    )
    if target_user_id is not None:
        stmt = stmt.where(UserAvailabilityOverride.user_id == target_user_id)
    if resolved.status == "approved":
        stmt = stmt.where(UserAvailabilityOverride.approved_at.is_not(None))
    elif resolved.status == "pending":
        stmt = stmt.where(UserAvailabilityOverride.approved_at.is_(None))
    if resolved.from_date is not None:
        stmt = stmt.where(UserAvailabilityOverride.date >= resolved.from_date)
    if resolved.to_date is not None:
        stmt = stmt.where(UserAvailabilityOverride.date <= resolved.to_date)
    if after_id is not None:
        stmt = stmt.where(UserAvailabilityOverride.id > after_id)
    stmt = stmt.order_by(UserAvailabilityOverride.id.asc()).limit(limit + 1)

    rows = session.scalars(stmt).all()
    return [_row_to_view(r) for r in rows]


def get_override(
    session: Session,
    ctx: WorkspaceContext,
    *,
    override_id: str,
) -> UserAvailabilityOverrideView:
    """Return a single :class:`UserAvailabilityOverrideView` or raise on miss.

    Authorisation: requester or ``availability_overrides.view_others``.
    A cross-tenant probe collapses to
    :class:`UserAvailabilityOverrideNotFound` (404, not 403) per §01
    "tenant surface is not enumerable".
    """
    row = _load_row(session, ctx, override_id=override_id)
    _gate_or_self(
        session,
        ctx,
        target_user_id=row.user_id,
        cross_user_action="availability_overrides.view_others",
    )
    return _row_to_view(row)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_override(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: UserAvailabilityOverrideCreate,
    clock: Clock | None = None,
) -> UserAvailabilityOverrideView:
    """Insert a new ``user_availability_override`` row.

    When ``body.user_id`` is ``None`` the caller is requesting the
    override for themselves — gated on
    ``availability_overrides.create_self``. When it differs from
    ``ctx.actor_id`` the caller is creating on behalf of someone else
    (manager retroactive entry), gated on
    ``availability_overrides.edit_others``.

    **Approval-required computation.** The server reads the user's
    weekly pattern for the date's weekday and walks
    :func:`_compute_approval_required`. The result lands on the row's
    ``approval_required`` column verbatim so the audit log can replay
    the decision later.

    **Auto-approve.** Owner / manager-created rows are always
    auto-approved (``approved_at = now``, ``approved_by =
    ctx.actor_id``) regardless of the computed
    ``approval_required`` — a manager scheduling a date-specific
    override shouldn't have to walk through their own approval queue.
    For worker self-submits, ``approval_required = False`` also
    auto-approves (the §06 hybrid model: adding hours doesn't need
    sign-off).
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    target_user_id = body.user_id if body.user_id is not None else ctx.actor_id

    try:
        if target_user_id != ctx.actor_id:
            _require_capability(
                session, ctx, action_key="availability_overrides.edit_others"
            )
        else:
            _require_capability(
                session, ctx, action_key="availability_overrides.create_self"
            )
    except PermissionDenied as exc:
        raise UserAvailabilityOverridePermissionDenied(str(exc)) from exc

    # Defence-in-depth: the DTO already enforces these, but a Python
    # caller bypassing the DTO (``model_construct``) would otherwise
    # land an invalid window at the DB CHECK with an opaque
    # IntegrityError.
    if (body.starts_local is None) != (body.ends_local is None):
        raise UserAvailabilityOverrideInvariantViolated(
            "starts_local and ends_local must both be set or both be null"
        )
    if (
        body.starts_local is not None
        and body.ends_local is not None
        and body.ends_local <= body.starts_local
    ):
        raise UserAvailabilityOverrideInvariantViolated(
            f"ends_local {body.ends_local.isoformat()!r} must be after "
            f"starts_local {body.starts_local.isoformat()!r}"
        )

    # ``date.weekday()`` returns Mon=0..Sun=6 — matches the
    # :class:`UserWeeklyAvailability.weekday` ISO encoding (see the
    # column's CHECK constraint).
    weekly = _load_weekly_for_weekday(
        session,
        ctx,
        user_id=target_user_id,
        weekday=body.date.weekday(),
    )
    approval_required = _compute_approval_required(
        weekly=weekly,
        override_available=body.available,
        override_starts=body.starts_local,
        override_ends=body.ends_local,
    )

    auto_approve = (not approval_required) or _can_edit_others(session, ctx)
    approved_at: datetime | None = now if auto_approve else None
    approved_by: str | None = ctx.actor_id if auto_approve else None

    row_id = new_ulid(clock=clock)
    row = UserAvailabilityOverride(
        id=row_id,
        workspace_id=ctx.workspace_id,
        user_id=target_user_id,
        date=body.date,
        available=body.available,
        starts_local=body.starts_local,
        ends_local=body.ends_local,
        reason=body.reason,
        approval_required=approval_required,
        approved_at=approved_at,
        approved_by=approved_by,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    session.flush()

    view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="user_availability_override",
        entity_id=row.id,
        action="user_availability_override.created",
        diff={"after": _view_to_diff_dict(view), "auto_approved": auto_approve},
        clock=resolved_clock,
    )
    return view


def update_override(
    session: Session,
    ctx: WorkspaceContext,
    *,
    override_id: str,
    body: UserAvailabilityOverrideUpdate,
    clock: Clock | None = None,
) -> UserAvailabilityOverrideView:
    """Partial update of a still-pending override.

    State-machine guard: only pending (``approved_at IS NULL``) rows
    are editable. An approved override whose hours need to shift must
    be rejected (or deleted) and re-submitted, so the assignment audit
    trail stays coherent — silently mutating an approved override
    would flip the candidate pool retroactively.

    Authorisation: requester or ``availability_overrides.edit_others``.

    A zero-delta call (every sent field matches the current value)
    skips the audit write — matches the convention from
    :mod:`app.domain.identity.user_leaves`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, override_id=override_id)

    _gate_or_self(
        session,
        ctx,
        target_user_id=row.user_id,
        cross_user_action="availability_overrides.edit_others",
    )

    if row.approved_at is not None:
        raise UserAvailabilityOverrideTransitionForbidden(
            f"override {override_id!r} is already approved; only pending "
            "overrides may have their fields edited"
        )

    sent = body.model_fields_set
    if not sent:
        return _row_to_view(row)

    # Compute the post-update shape before mutating so the BOTH-OR-NEITHER
    # invariant catches a half-set PATCH (sender clears starts_local
    # without clearing ends_local, etc.) at the boundary instead of at
    # flush time. ``model_fields_set`` distinguishes "field was sent"
    # from "field was not sent" so an explicit JSON ``null`` clears
    # a nullable column while an omitted field preserves it. ``available``
    # is non-nullable on the row, so a sent ``null`` is treated as
    # "unchanged" (the wire shape exposes ``bool | None`` only because
    # FastAPI's PATCH idiom needs a Python default for the optional
    # field; there is no semantic "clear" for a non-nullable bool).
    new_available = (
        body.available
        if "available" in sent and body.available is not None
        else row.available
    )
    new_starts = body.starts_local if "starts_local" in sent else row.starts_local
    new_ends = body.ends_local if "ends_local" in sent else row.ends_local

    if (new_starts is None) != (new_ends is None):
        raise UserAvailabilityOverrideInvariantViolated(
            "starts_local and ends_local must both be set or both be null"
        )
    if new_starts is not None and new_ends is not None and new_ends <= new_starts:
        raise UserAvailabilityOverrideInvariantViolated(
            f"ends_local {new_ends.isoformat()!r} must be after "
            f"starts_local {new_starts.isoformat()!r}"
        )
    if not new_available and (new_starts is not None or new_ends is not None):
        raise UserAvailabilityOverrideInvariantViolated(
            "available=false overrides must not carry hours; clear "
            "starts_local / ends_local"
        )

    before = _row_to_view(row)
    changed = False

    if (
        "available" in sent
        and body.available is not None
        and body.available != row.available
    ):
        row.available = body.available
        changed = True
    if "starts_local" in sent and body.starts_local != row.starts_local:
        row.starts_local = body.starts_local
        changed = True
    if "ends_local" in sent and body.ends_local != row.ends_local:
        row.ends_local = body.ends_local
        changed = True
    if "reason" in sent and body.reason != row.reason:
        row.reason = body.reason
        changed = True

    if not changed:
        return before

    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="user_availability_override",
        entity_id=row.id,
        action="user_availability_override.updated",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def approve_override(
    session: Session,
    ctx: WorkspaceContext,
    *,
    override_id: str,
    clock: Clock | None = None,
) -> UserAvailabilityOverrideView:
    """Stamp ``approved_at`` + ``approved_by`` on a pending override.

    Always requires ``availability_overrides.edit_others`` — a worker
    cannot approve their own request through this surface
    (auto-approve at create time is the supported "manager schedules
    their own override" path, plus the §06 "adding hours" path which
    auto-approves regardless of caller).

    State-machine guards:

    * pending → approved: the happy path.
    * already-approved: :class:`UserAvailabilityOverrideTransitionForbidden`
      (409). Idempotent re-approval would lose the second-approver
      signature.
    * soft-deleted (rejected): :class:`UserAvailabilityOverrideNotFound`
      because :func:`_load_row` filters tombstones.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, override_id=override_id)

    try:
        _require_capability(
            session, ctx, action_key="availability_overrides.edit_others"
        )
    except PermissionDenied as exc:
        raise UserAvailabilityOverridePermissionDenied(str(exc)) from exc

    if row.approved_at is not None:
        raise UserAvailabilityOverrideTransitionForbidden(
            f"override {override_id!r} is already approved"
        )

    before = _row_to_view(row)
    row.approved_at = now
    row.approved_by = ctx.actor_id
    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="user_availability_override",
        entity_id=row.id,
        action="user_availability_override.approved",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def reject_override(
    session: Session,
    ctx: WorkspaceContext,
    *,
    override_id: str,
    reason_md: str | None = None,
    clock: Clock | None = None,
) -> UserAvailabilityOverrideView:
    """Reject a pending override by soft-deleting the row.

    §06 "user_availability_overrides" doesn't carve a ``rejected``
    column on the row; instead, the pragmatic v1 shape soft-deletes
    the row (stamps ``deleted_at``) and folds the rejection
    ``reason_md`` into the row's ``reason`` so the worker's complaints
    inbox keeps the explanation. The
    ``user_availability_override.rejected`` audit row preserves the
    full state transition.

    Always requires ``availability_overrides.edit_others``.

    State-machine guards:

    * pending → rejected: the happy path.
    * already-approved → rejected is **not** allowed via this path;
      the manager must :func:`delete_override` the approved row,
      which writes a different audit action so the candidate-pool
      change is greppable. Surfaces
      :class:`UserAvailabilityOverrideTransitionForbidden`.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, override_id=override_id)

    try:
        _require_capability(
            session, ctx, action_key="availability_overrides.edit_others"
        )
    except PermissionDenied as exc:
        raise UserAvailabilityOverridePermissionDenied(str(exc)) from exc

    if row.approved_at is not None:
        raise UserAvailabilityOverrideTransitionForbidden(
            f"override {override_id!r} is already approved; cannot reject — "
            "delete the row instead"
        )

    before = _row_to_view(row)
    row.deleted_at = now
    row.updated_at = now
    if reason_md is not None and reason_md.strip():
        # Concatenate rather than overwrite so the worker's original
        # request stays visible alongside the rejection rationale.
        # An empty / whitespace-only reason is treated as no reason.
        prefix = f"{row.reason}\n\n" if row.reason else ""
        row.reason = f"{prefix}Rejected: {reason_md}"
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="user_availability_override",
        entity_id=row.id,
        action="user_availability_override.rejected",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
            "reason_md": reason_md,
        },
        clock=resolved_clock,
    )
    return after


def delete_override(
    session: Session,
    ctx: WorkspaceContext,
    *,
    override_id: str,
    clock: Clock | None = None,
) -> UserAvailabilityOverrideView:
    """Soft-delete an override row (the worker's "withdraw request" path).

    Authorisation: requester or
    ``availability_overrides.edit_others``. Despite the name, this is
    the canonical "withdraw / cancel" path — workers use it to take
    back a pending request, managers use it to revoke an approved row
    that should no longer enter the precedence stack.

    Idempotent at the row level: a repeated call surfaces
    :class:`UserAvailabilityOverrideNotFound` because :func:`_load_row`
    filters tombstones.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, override_id=override_id)

    _gate_or_self(
        session,
        ctx,
        target_user_id=row.user_id,
        cross_user_action="availability_overrides.edit_others",
    )

    before = _row_to_view(row)
    row.deleted_at = now
    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="user_availability_override",
        entity_id=row.id,
        action="user_availability_override.deleted",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after
