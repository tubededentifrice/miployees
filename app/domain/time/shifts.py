"""Shift clock-in / clock-out service.

The :class:`~app.adapters.db.time.models.Shift` row is the atomic
record of a worker's clocked time. This module is the only place that
opens, closes, edits, or reads shift rows at the domain layer (§01
"Handlers are thin").

Public surface:

* **DTOs** — Pydantic v2 models for each write shape
  (:class:`ShiftOpen`, :class:`ShiftClose`, :class:`ShiftEdit`) plus
  the read projection :class:`ShiftView`. Shape-level validation
  (``ends_at >= starts_at``) lives on the DTO / service boundary so
  the same rule fires for HTTP + Python callers.
* **Service functions** — :func:`open_shift`, :func:`close_shift`,
  :func:`edit_shift`, :func:`list_open_shifts`, :func:`list_shifts`,
  :func:`get_shift`. Every function takes a
  :class:`~app.tenancy.WorkspaceContext` as its first argument; the
  ``workspace_id`` is resolved from the context, never from the
  caller's payload (v1 invariant §01).
* **Errors** — :class:`ShiftNotFound`, :class:`ShiftAlreadyOpen`,
  :class:`ShiftBoundaryInvalid`, :class:`ShiftEditForbidden`. Each
  subclasses the stdlib parent the router's error map points at
  (``LookupError`` → 404, ``ValueError`` → 409 / 422,
  ``PermissionError`` → 403).

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation writes
one :mod:`app.audit` row in the same transaction, then publishes a
:class:`~app.events.ShiftChanged` event AFTER the audit write (so a
failed publish still leaves the audit row in the UoW).

**Capabilities.** Writes gate through :func:`app.authz.require`:

* opening a shift for the caller → ``time.clock_self`` (auto-allowed
  for ``all_workers`` + ``managers`` + ``owners`` via default_allow);
* opening / closing / editing someone else's shift →
  ``time.edit_others`` (managers + owners by default).

See ``docs/specs/09-time-payroll-expenses.md`` §"Bookings" (shift
semantics), §"Owner and manager adjustments" (manager edits),
``docs/specs/02-domain-model.md`` §"shift".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.time.models import _SHIFT_SOURCE_VALUES, Shift
from app.audit import write_audit
from app.authz import (
    InvalidScope,
    PermissionDenied,
    UnknownActionKey,
    require,
)
from app.events import ShiftChanged, bus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ShiftAlreadyOpen",
    "ShiftBoundaryInvalid",
    "ShiftClose",
    "ShiftEdit",
    "ShiftEditForbidden",
    "ShiftNotFound",
    "ShiftOpen",
    "ShiftSource",
    "ShiftView",
    "close_shift",
    "edit_shift",
    "get_shift",
    "list_open_shifts",
    "list_shifts",
    "open_shift",
]


# ---------------------------------------------------------------------------
# Enums (string literals — keep parity with the DB CHECK constraint)
# ---------------------------------------------------------------------------


ShiftSource = Literal["manual", "geofence", "occurrence"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ShiftNotFound(LookupError):
    """The requested shift does not exist in the caller's workspace.

    404-equivalent. Raised by :func:`get_shift`, :func:`close_shift`,
    and :func:`edit_shift` when the id is unknown or not visible to
    the caller's workspace context.
    """


class ShiftAlreadyOpen(ValueError):
    """The user already has an open shift (``ends_at IS NULL``).

    409-equivalent. The invariant is "exactly one open shift per
    user per workspace" — opening a second one would force the
    downstream timesheet / payroll pipeline to guess which one is
    authoritative. The caller must close the existing shift first.
    The attribute :attr:`existing_shift_id` carries the offending
    shift so the UI can link directly to it.
    """

    def __init__(self, *, user_id: str, existing_shift_id: str) -> None:
        self.user_id = user_id
        self.existing_shift_id = existing_shift_id
        super().__init__(
            f"user {user_id!r} already has an open shift "
            f"({existing_shift_id!r}); close it before opening another"
        )


class ShiftBoundaryInvalid(ValueError):
    """``ends_at`` is before ``starts_at`` — a zero-or-negative window.

    422-equivalent. The DB CHECK on :class:`Shift` does not enforce
    this invariant (a closed shift's ``ends_at`` is a separate column
    from ``starts_at`` and the model's v1 slice chose not to add a
    cross-column constraint — see
    ``app/adapters/db/time/models.py``). The service re-validates on
    every close / edit so a bad window never lands in storage.
    """


class ShiftEditForbidden(PermissionError):
    """The caller lacks capability to close / edit a shift they don't own.

    403-equivalent. The service uses :func:`app.authz.require` with
    ``time.edit_others`` to enforce the rule; this exception wraps
    the underlying :class:`~app.authz.PermissionDenied` so the
    router can map it to a ``shift``-specific 403 error body.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


# Caps kept modest to bound audit + DB payload without being
# restrictive in practice. Matches the shape of sibling task-template
# DTOs.
_MAX_NOTES_LEN = 20_000
_MAX_ID_LEN = 40


class ShiftOpen(BaseModel):
    """Request body for ``POST /shifts/open``.

    ``user_id`` defaults to the caller's ``ctx.actor_id`` when the
    service receives ``None`` — workers opening their own shift do
    not pass the field. Managers opening a retroactive shift for
    someone else pass an explicit ``user_id``; the service gates
    that path through ``time.edit_others``.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    property_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    source: ShiftSource = "manual"
    notes_md: str | None = Field(default=None, max_length=_MAX_NOTES_LEN)


class ShiftClose(BaseModel):
    """Request body for ``POST /shifts/{shift_id}/close``.

    ``ends_at`` defaults to ``clock.now()`` when the service receives
    ``None`` — the standard "close at current wall clock" path.
    """

    model_config = ConfigDict(extra="forbid")

    ends_at: datetime | None = None


class ShiftEdit(BaseModel):
    """Request body for ``PATCH /shifts/{shift_id}``.

    PATCH-style: every field is optional. A field omitted from the
    body is left untouched; a field present with ``None`` explicitly
    clears the column where that's allowed (``ends_at`` reopening a
    closed shift is NOT allowed — the service rejects it on a second
    pass). The service re-validates ``ends_at > starts_at`` when
    either edge moves.
    """

    model_config = ConfigDict(extra="forbid")

    starts_at: datetime | None = None
    ends_at: datetime | None = None
    property_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    notes_md: str | None = Field(default=None, max_length=_MAX_NOTES_LEN)


@dataclass(frozen=True, slots=True)
class ShiftView:
    """Immutable read projection of a ``shift`` row.

    Returned by every service read + write. A frozen / slotted
    dataclass (not a Pydantic model) because reads carry audit-
    sensitive fields (``approved_by``, ``approved_at``) that are
    managed by the service, not the caller's payload — the same
    reasoning as :class:`~app.domain.tasks.templates.TaskTemplateView`.
    """

    id: str
    workspace_id: str
    user_id: str
    starts_at: datetime
    ends_at: datetime | None
    property_id: str | None
    source: ShiftSource
    notes_md: str | None
    approved_by: str | None
    approved_at: datetime | None


# ---------------------------------------------------------------------------
# Row ↔ view projection
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime.

    SQLite's ``DateTime(timezone=True)`` column type strips tzinfo on
    read (the dialect has no native TZ support — the timezone flag is
    informational only). Rows round-tripping through a SQLite test
    engine come back naive even though the service wrote a UTC-aware
    value. Postgres preserves the offset faithfully, so this guard is
    a no-op there.

    The cross-backend invariant ("time is UTC at rest", see
    ``AGENTS.md`` §"Application-specific notes") lets us
    unambiguously tag a naive value as UTC without guessing at the
    caller's local offset.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _narrow_source(value: str) -> ShiftSource:
    """Narrow a loaded DB string to the :data:`ShiftSource` literal.

    The DB CHECK constraint already rejects anything else; this
    helper exists purely to satisfy mypy's strict-Literal reading
    without a ``cast``. An unexpected value indicates schema drift —
    raise rather than silently downgrade.
    """
    if value == "manual":
        return "manual"
    if value == "geofence":
        return "geofence"
    if value == "occurrence":
        return "occurrence"
    raise ValueError(f"unknown shift.source {value!r} on loaded row")


def _row_to_view(row: Shift) -> ShiftView:
    """Project a loaded :class:`Shift` row into a read view.

    Datetime columns pass through :func:`_ensure_utc` so SQLite-
    stripped rows come back comparable to ``clock.now()`` without
    forcing the comparison sites to re-stamp by hand.
    """
    return ShiftView(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        starts_at=_ensure_utc(row.starts_at),
        ends_at=_ensure_utc(row.ends_at) if row.ends_at is not None else None,
        property_id=row.property_id,
        source=_narrow_source(row.source),
        notes_md=row.notes_md,
        approved_by=row.approved_by,
        approved_at=(
            _ensure_utc(row.approved_at) if row.approved_at is not None else None
        ),
    )


def _view_to_diff_dict(view: ShiftView) -> dict[str, Any]:
    """Flatten a :class:`ShiftView` into a JSON-safe dict for audit.

    Stringifies the two ``datetime`` columns so the audit row's
    ``diff`` JSON payload stays portable (SQLite JSON1 + PG JSONB
    both accept plain strings but reject native ``datetime``
    objects). Mirrors the helper in
    :mod:`app.domain.tasks.templates`.
    """
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "user_id": view.user_id,
        "starts_at": view.starts_at.isoformat(),
        "ends_at": view.ends_at.isoformat() if view.ends_at is not None else None,
        "property_id": view.property_id,
        "source": view.source,
        "notes_md": view.notes_md,
        "approved_by": view.approved_by,
        "approved_at": (
            view.approved_at.isoformat() if view.approved_at is not None else None
        ),
    }


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

    Wraps :func:`app.authz.require` + translates a caller-bug
    (unknown key / invalid scope) into a ``RuntimeError`` so the
    router layer can surface it as a 500, separate from the 403
    that a genuine ``PermissionDenied`` produces.

    We call through to the authz enforcement seam explicitly here
    (instead of a router-level ``Depends(Permission(...))``) because
    the check depends on *which user_id* the caller is targeting —
    a rule the router can't know until the body parses.
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
        # Catalog wiring bug — surface as RuntimeError, not as a
        # user-visible 403. The router maps this to a 500 envelope.
        raise RuntimeError(
            f"authz catalog misconfigured for {action_key!r}: {exc!s}"
        ) from exc


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    shift_id: str,
) -> Shift:
    """Load ``shift_id`` scoped to the caller's workspace.

    The ORM tenant filter already constrains SELECTs to
    ``ctx.workspace_id``; the explicit predicate below is
    defence-in-depth (matches the convention on
    :mod:`app.domain.identity.role_grants`).
    """
    stmt = select(Shift).where(
        Shift.id == shift_id,
        Shift.workspace_id == ctx.workspace_id,
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise ShiftNotFound(shift_id)
    return row


def get_shift(
    session: Session,
    ctx: WorkspaceContext,
    *,
    shift_id: str,
) -> ShiftView:
    """Return the shift identified by ``shift_id`` or raise :class:`ShiftNotFound`."""
    return _row_to_view(_load_row(session, ctx, shift_id=shift_id))


def list_open_shifts(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
) -> Sequence[ShiftView]:
    """Return every open shift (``ends_at IS NULL``) in the workspace.

    Optionally narrowed to a single ``user_id``. Ordered by
    ``starts_at`` ascending with ``id`` as a tiebreaker inside the
    same millisecond. The ``(user_id, ends_at)`` index answers the
    filtered query in one B-tree scan; the unfiltered query uses the
    workspace index.
    """
    stmt = select(Shift).where(
        Shift.workspace_id == ctx.workspace_id,
        Shift.ends_at.is_(None),
    )
    if user_id is not None:
        stmt = stmt.where(Shift.user_id == user_id)
    stmt = stmt.order_by(Shift.starts_at.asc(), Shift.id.asc())
    rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


def list_shifts(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
    starts_from: datetime | None = None,
    starts_until: datetime | None = None,
) -> Sequence[ShiftView]:
    """Return every shift in the workspace, optionally filtered by range.

    ``starts_from`` / ``starts_until`` are half-open on the
    ``starts_at`` column — ``starts_from <= row.starts_at <
    starts_until``. Returning rows in (``starts_at ASC``, ``id
    ASC``) order so the manager timesheet can paginate
    deterministically.
    """
    stmt = select(Shift).where(Shift.workspace_id == ctx.workspace_id)
    if user_id is not None:
        stmt = stmt.where(Shift.user_id == user_id)
    if starts_from is not None:
        stmt = stmt.where(Shift.starts_at >= starts_from)
    if starts_until is not None:
        stmt = stmt.where(Shift.starts_at < starts_until)
    stmt = stmt.order_by(Shift.starts_at.asc(), Shift.id.asc())
    rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _find_open_shift_id(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
) -> str | None:
    """Return the id of ``user_id``'s open shift in this workspace or ``None``.

    Uses the ``(user_id, ends_at)`` index — a cheap index-only scan.
    """
    stmt = select(Shift.id).where(
        Shift.workspace_id == ctx.workspace_id,
        Shift.user_id == user_id,
        Shift.ends_at.is_(None),
    )
    return session.scalars(stmt).first()


def open_shift(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
    property_id: str | None = None,
    source: ShiftSource = "manual",
    notes_md: str | None = None,
    clock: Clock | None = None,
) -> ShiftView:
    """Open a shift for ``user_id`` (or the caller) and return the fresh row.

    When ``user_id`` is ``None`` the caller opens their own shift —
    the common clock-in path. When ``user_id`` differs from
    ``ctx.actor_id`` the caller is opening a shift on someone else's
    behalf (manager retroactive entry) and must hold
    ``time.edit_others``; otherwise only ``time.clock_self`` is
    required (auto-allowed for any worker via default_allow).

    Invariants:

    * The target user must have NO open shift in this workspace.
      :class:`ShiftAlreadyOpen` if one exists.
    * ``starts_at`` defaults to ``clock.now()`` — the shift is
      opened right now.
    """
    target_user_id = user_id if user_id is not None else ctx.actor_id

    # Re-raise any :class:`PermissionDenied` as the sibling
    # :class:`ShiftEditForbidden` so the router's error map has one
    # domain type per 403 shape — the action_key changes per branch
    # (``time.edit_others`` for cross-user, ``time.clock_self`` for
    # self) but both collapse to the same HTTP envelope.
    try:
        if target_user_id != ctx.actor_id:
            _require_capability(session, ctx, action_key="time.edit_others")
        else:
            _require_capability(session, ctx, action_key="time.clock_self")
    except PermissionDenied as exc:
        raise ShiftEditForbidden(str(exc)) from exc

    existing = _find_open_shift_id(session, ctx, user_id=target_user_id)
    if existing is not None:
        raise ShiftAlreadyOpen(
            user_id=target_user_id,
            existing_shift_id=existing,
        )

    now = (clock if clock is not None else SystemClock()).now()
    if source not in _SHIFT_SOURCE_VALUES:
        # Guardrail — the DTO's ``Literal`` already enforces this,
        # but the function accepts a raw ``str`` default so a bad
        # caller from Python land (not through HTTP) would land here.
        raise ValueError(
            f"shift.source={source!r} is not one of {_SHIFT_SOURCE_VALUES!r}"
        )

    row = Shift(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        user_id=target_user_id,
        starts_at=now,
        ends_at=None,
        property_id=property_id,
        source=source,
        notes_md=notes_md,
        approved_by=None,
        approved_at=None,
    )
    session.add(row)
    session.flush()

    view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="shift",
        entity_id=row.id,
        action="open",
        diff={"after": _view_to_diff_dict(view)},
        clock=clock,
    )
    bus.publish(
        ShiftChanged(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            shift_id=row.id,
            user_id=target_user_id,
            action="opened",
        )
    )
    return view


def close_shift(
    session: Session,
    ctx: WorkspaceContext,
    *,
    shift_id: str,
    ends_at: datetime | None = None,
    clock: Clock | None = None,
) -> ShiftView:
    """Close ``shift_id`` and return the updated row.

    ``ends_at`` defaults to ``clock.now()``. Rules:

    * If the shift already has ``ends_at`` set, re-closing is a
      no-op — we return the existing view without writing an audit
      row. This mirrors the UI's idempotent "close" button: a
      double-click shouldn't error. Callers who want a strict error
      on re-close can call :func:`edit_shift` instead.
    * ``ends_at >= starts_at`` — zero-length shifts are allowed
      (a mis-clicked clock-in followed immediately by a clock-out
      still produces a zero-minute row, which the manager can
      amend or delete). Negative windows raise
      :class:`ShiftBoundaryInvalid`.
    * Closing someone else's shift requires ``time.edit_others``.
    """
    row = _load_row(session, ctx, shift_id=shift_id)

    if row.user_id != ctx.actor_id:
        try:
            _require_capability(session, ctx, action_key="time.edit_others")
        except PermissionDenied as exc:
            raise ShiftEditForbidden(str(exc)) from exc

    # Idempotent re-close short-circuit: once a shift is closed, a
    # second ``close_shift`` call (mis-click / double-tap / stale UI)
    # MUST NOT error, write an audit row, or re-publish a
    # ``ShiftChanged`` event. We check this BEFORE validating the
    # caller's ``ends_at`` so a stale client that sends a junky
    # timestamp against an already-closed shift still gets the
    # documented no-op instead of a 422.
    if row.ends_at is not None:
        return _row_to_view(row)

    now = (clock if clock is not None else SystemClock()).now()
    resolved_ends_at = ends_at if ends_at is not None else now
    row_starts_at = _ensure_utc(row.starts_at)

    if resolved_ends_at < row_starts_at:
        raise ShiftBoundaryInvalid(
            f"ends_at {resolved_ends_at.isoformat()!r} is before starts_at "
            f"{row_starts_at.isoformat()!r}"
        )

    before = _row_to_view(row)
    row.ends_at = resolved_ends_at
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="shift",
        entity_id=row.id,
        action="close",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=clock,
    )
    bus.publish(
        ShiftChanged(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            shift_id=row.id,
            user_id=row.user_id,
            action="closed",
        )
    )
    return after


def edit_shift(
    session: Session,
    ctx: WorkspaceContext,
    *,
    shift_id: str,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    property_id: str | None = None,
    notes_md: str | None = None,
    clock: Clock | None = None,
) -> ShiftView:
    """Manager-only amend of a shift.

    Always requires ``time.edit_others`` — editing your own shift
    through this path is intentional (a worker correcting a
    retroactive ``notes_md`` should go through :func:`close_shift`
    or a dedicated "update notes" path, not a PATCH that bypasses
    the same-user fast-path). A manager amending their own shift is
    still a manager action and goes through the same gate.

    Only fields explicitly passed are changed. ``starts_at`` /
    ``ends_at`` / ``property_id`` / ``notes_md`` accept ``None`` to
    mean "leave untouched"; a dedicated clear path would land in a
    follow-up if the UI needs it.

    Validates ``ends_at > starts_at`` when both edges are set after
    the patch applies (either because the caller moved them both,
    or moved one against the other's stored value).
    """
    # Always a manager op. Even when editing your own shift, this
    # entry point is the manager-surface amend — the §09 "Owner and
    # manager adjustments" path — not the worker self-serve close.
    try:
        _require_capability(session, ctx, action_key="time.edit_others")
    except PermissionDenied as exc:
        raise ShiftEditForbidden(str(exc)) from exc

    row = _load_row(session, ctx, shift_id=shift_id)

    before = _row_to_view(row)

    # Validate the target window BEFORE mutating the persistent row.
    # SQLAlchemy's identity map would otherwise keep the invalid
    # assignment in the session until rollback — safe today because
    # the UoW rolls back on every exception, but a future caller that
    # wraps ``edit_shift`` in a broader ``try/except`` would silently
    # commit a row whose ``ends_at <= starts_at``. Compute-then-write
    # keeps the invariant enforced regardless of the outer
    # transaction story.
    target_starts_at = starts_at if starts_at is not None else row.starts_at
    target_ends_at = ends_at if ends_at is not None else row.ends_at
    if target_ends_at is not None:
        ends_at_utc = _ensure_utc(target_ends_at)
        starts_at_utc = _ensure_utc(target_starts_at)
        if ends_at_utc <= starts_at_utc:
            # Strict ``>`` on the manager-edit path: a zero-length
            # retroactive shift is an authoring mistake, not a
            # legitimate amend (the worker-close path tolerates
            # zero-length as an idempotent edge — see
            # :func:`close_shift`). Reject.
            raise ShiftBoundaryInvalid(
                f"ends_at {ends_at_utc.isoformat()!r} must be strictly "
                f"greater than starts_at {starts_at_utc.isoformat()!r}"
            )

    if starts_at is not None:
        row.starts_at = starts_at
    if ends_at is not None:
        row.ends_at = ends_at
    if property_id is not None:
        row.property_id = property_id
    if notes_md is not None:
        row.notes_md = notes_md

    session.flush()
    after = _row_to_view(row)

    now = (clock if clock is not None else SystemClock()).now()
    write_audit(
        session,
        ctx,
        entity_kind="shift",
        entity_id=row.id,
        action="edit",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=clock,
    )
    bus.publish(
        ShiftChanged(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=now,
            shift_id=row.id,
            user_id=row.user_id,
            action="edited",
        )
    )
    return after
