"""``occurrence`` assignment service (§06 task kind).

The §06 "Assignment algorithm" is more than ``task.assigned_user_id =
?``. Given a materialised :class:`~app.adapters.db.tasks.models.Occurrence`
the service resolves the best candidate by walking five deterministic
steps:

1. **Primary + ordered backups.** When the parent
   :class:`~app.adapters.db.tasks.models.Schedule` carries a
   ``default_assignee`` or ``backup_assignee_user_ids`` list, walk
   ``[default_assignee, *backup_assignee_user_ids]`` in order. The
   first entry that passes the availability precedence stack *and*
   the rota filter wins. ``assignment_source`` lands on the audit
   row as ``"primary"`` or ``"backup[N]"`` so the manager can tell
   at a glance that e.g. the second backup was used.
2. **Candidate pool.** When step 1 did not find a winner (or the
   task has no parent schedule), build a pool of users who hold a
   matching ``user_work_role`` (or are generalists), pass the same
   availability + rota filters, and were not already tried in step
   1.
3. **Unique pick.** A single-candidate pool is a direct assignment
   with ``assignment_source = "candidate_pool"``.
4. **Tiebreakers.** With more than one candidate, prefer the user
   with the fewest tasks at the same property in the 7-day window
   around ``scheduled_for_local``; break ties by rotation (oldest
   last-task-at-this-property wins).
5. **Zero candidates.** Leave ``assignee_user_id = NULL``; surface
   in the daily digest via :class:`TaskPrimaryUnavailable` (step 1
   was attempted) or :class:`TaskUnassigned` (pool was empty from
   the start).

## Injectable ports

Real availability / rota / pool semantics depend on tables that are
not yet in the schema. The real body plugs in once cd-5kv4
(``user_work_role`` table) + the downstream availability migrations
land — ``property_work_role_assignment``, ``schedule_ruleset``,
``user_leave``, ``user_availability_override``,
``user_weekly_availability``, ``public_holiday``. The service
exposes every such touchpoint as an injectable
callable with a default that matches the permissive "tables haven't
landed" reality:

* :data:`AvailabilityPort` — decides whether a user is free on a
  given property-local date + time. Default :func:`_always_available`
  returns an :class:`AvailabilityVerdict` with ``available=True``.
* :data:`RotaPort` — decides whether a user's rota for the property
  covers the occurrence's weekday + local window. Default
  :func:`_always_covers` returns ``True`` (every user behaves as a
  §05 generalist).
* :data:`CandidatePoolPort` — returns the user-id pool matching the
  task's ``expected_role_id`` + property, minus the caller's
  ``exclude`` list. Default :func:`_empty_pool` returns an empty
  tuple — the pool branch is inert until the work-role tables land.
* :data:`WorkloadPort` — counts a user's tasks in the 7-day window
  at the same property and resolves their last-task-here timestamp.
  Default :func:`_default_workload` queries the existing
  ``occurrence`` table (which **does** exist).

The injection seam lets cd-5kv4 (work_role table) and the downstream
availability migrations plug in real queries without another
service-wide refactor.

## Public surface

* :func:`assign_task` — auto-assigns a task, with an
  ``override_user_id`` shortcut for the manager UI.
* :func:`reassign_task` — explicit move to a new user; emits
  :class:`TaskReassigned` with the previous + new user.
* :func:`unassign_task` — explicit clear; emits
  :class:`TaskUnassigned` with a caller-supplied reason.
* :func:`availability_for` — the default availability-stack entry
  point, exposed here so the scheduler UI (§14) can reuse the
  exact same rule as the assignment algorithm.
* :func:`build_assignment_hook` — returns an
  :data:`~app.domain.tasks.oneoff.AssignmentHook` that the one-off
  + generator call sites can plug in; keeps event emission on the
  caller side so we don't double-fire ``task.assigned``.

The service never commits — the caller's Unit-of-Work owns
transaction boundaries (§01 "Key runtime invariants" #3). Every
mutation writes one audit row carrying ``assignment_source`` and
``candidate_count`` so the owner/manager surface has a single
structured artefact to render.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Availability
precedence stack", §"Assignment algorithm", §"Pull-back logic for
before_checkin tasks".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.tasks.models import Occurrence, Schedule
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import (
    TaskAssigned,
    TaskPrimaryUnavailable,
    TaskReassigned,
    TaskUnassigned,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock

__all__ = [
    "AssignmentResult",
    "AssignmentSource",
    "AvailabilityPort",
    "AvailabilityVerdict",
    "CandidatePoolPort",
    "RotaPort",
    "TaskAlreadyAssigned",
    "TaskNotFound",
    "WorkloadPort",
    "assign_task",
    "availability_for",
    "build_assignment_hook",
    "reassign_task",
    "unassign_task",
]


# ---------------------------------------------------------------------------
# Types + ports
# ---------------------------------------------------------------------------


AssignmentSource = Literal[
    "manual",
    "primary",
    "backup",
    "candidate_pool",
    "unassigned",
]
"""How an occurrence ended up with (or without) an assignee.

``"manual"`` — caller passed an explicit ``override_user_id`` into
:func:`assign_task` (manager UI / NL agent override).
``"primary"`` — the parent schedule's ``default_assignee`` was picked.
``"backup"`` — one of the schedule's ``backup_assignee_user_ids``
   entries was picked; the index within that list rides on
   :attr:`AssignmentResult.backup_index`.
``"candidate_pool"`` — fell through to the role-scoped pool.
``"unassigned"`` — every path exhausted; the task row's
   ``assignee_user_id`` stays ``NULL``.
"""


@dataclass(frozen=True, slots=True)
class AvailabilityVerdict:
    """Structured "is this user free?" answer.

    Kept as a dataclass rather than a bare :class:`bool` so a future
    availability port can carry richer context (the governing rule —
    ``"leave"`` / ``"weekly_off"`` — or a reduced-hours window for
    a public-holiday ``scheduling_effect = reduced``) without
    widening every call site. Today the service only reads
    :attr:`available`; additional fields are present but unused.
    """

    available: bool
    reason: str | None = None


# (session, ctx, user_id, local_dt, property_id) -> verdict.
AvailabilityPort = Callable[
    [Session, WorkspaceContext, str, datetime, str | None],
    AvailabilityVerdict,
]

# (session, ctx, user_id, property_id, local_dt) -> rota covers?
RotaPort = Callable[
    [Session, WorkspaceContext, str, str | None, datetime],
    bool,
]

# (session, ctx, expected_role_id, property_id, exclude) -> user ids.
CandidatePoolPort = Callable[
    [Session, WorkspaceContext, str | None, str | None, Sequence[str]],
    Sequence[str],
]


class WorkloadPort(Protocol):
    """Workload / rotation snapshot used by the tiebreaker step.

    Split into a :class:`typing.Protocol` rather than two loose
    callables because the tiebreaker reads both fields on the same
    candidate — bundling them lets a backend cache the "tasks at
    this property in the last week" window once per candidate
    rather than twice.
    """

    def count_tasks_in_window(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        user_id: str,
        property_id: str | None,
        local_dt: datetime,
        window_days: int = 7,
    ) -> int:
        """Tasks assigned to ``user_id`` at ``property_id`` inside a
        ``window_days``-wide window centred on ``local_dt``.

        ``window_days`` is the **total** width of the window, matching
        the spec's "7-day window around ``scheduled_for_local``"
        (§06 "Assignment algorithm" step 4). A default of ``7`` means
        three-and-a-half days on either side; odd widths split evenly
        (``window_days=7`` → ±3.5d so the window is exactly 7 days)."""

    def last_task_at(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        user_id: str,
        property_id: str | None,
    ) -> datetime | None:
        """Most-recent :class:`Occurrence.scheduled_for_local` (parsed)
        for ``user_id`` at ``property_id``; ``None`` when the user has
        no history there (→ rotation "oldest wins" treats them as
        maximally stale)."""


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    """Public shape returned by every entry point.

    Frozen + slotted so callers can cheap-compare in tests and the
    audit writer can reflect straight into ``diff['after']`` without
    mutating the payload.

    ``candidate_count`` is the size of the candidate pool the
    algorithm considered — ``0`` when step 1 succeeded or no pool
    was built, ``>0`` when the candidate_pool branch ran.
    ``backup_index`` is set only when ``source == "backup"`` and
    gives the 0-based position within
    ``schedule.backup_assignee_user_ids`` that won.
    """

    task_id: str
    assigned_user_id: str | None
    source: AssignmentSource
    candidate_count: int
    backup_index: int | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TaskNotFound(LookupError):
    """The task id is unknown in the caller's workspace.

    404-equivalent. Raised by every entry point when the row is not
    visible (wrong workspace, soft-deleted, or straight-up missing).
    """


class TaskAlreadyAssigned(ValueError):
    """:func:`reassign_task` was called on an unassigned task.

    422-equivalent — reassignment is a *move*, and a move requires a
    current holder. Callers of an explicit reassign UI must use
    :func:`assign_task` (auto or override) when no previous assignee
    exists.
    """


# ---------------------------------------------------------------------------
# Default port implementations
# ---------------------------------------------------------------------------


def _always_available(
    session: Session,
    ctx: WorkspaceContext,
    user_id: str,
    local_dt: datetime,
    property_id: str | None,
) -> AvailabilityVerdict:
    """Default availability port.

    Returns ``available=True`` for every user: the §06 availability
    tables (``user_leave``, ``user_availability_override``,
    ``public_holiday``, ``user_weekly_availability``) are not yet in
    the schema, so a partial implementation would lie to callers.
    The real body replaces this default when the tables land via
    follow-up migrations.
    """
    _ = session, ctx, user_id, local_dt, property_id
    return AvailabilityVerdict(available=True, reason=None)


def _always_covers(
    session: Session,
    ctx: WorkspaceContext,
    user_id: str,
    property_id: str | None,
    local_dt: datetime,
) -> bool:
    """Default rota port — every user is treated as a §05 generalist.

    A generalist skips the rota filter per the spec's "Rota
    composition" paragraph. Until ``property_work_role_assignment``
    lands every candidate behaves as a generalist; this default
    captures that honestly rather than inventing a rota check with
    no table behind it.
    """
    _ = session, ctx, user_id, property_id, local_dt
    return True


def _empty_pool(
    session: Session,
    ctx: WorkspaceContext,
    expected_role_id: str | None,
    property_id: str | None,
    exclude: Sequence[str],
) -> Sequence[str]:
    """Default candidate-pool port.

    Returns an empty tuple: ``user_work_role`` lands with cd-5kv4 but
    ``property_work_role_assignment`` is still pending — without both
    tables we cannot build a meaningful pool. Downstream tests inject
    a deterministic pool to exercise steps 2-4.
    """
    _ = session, ctx, expected_role_id, property_id, exclude
    return ()


@dataclass(frozen=True, slots=True)
class _DefaultWorkload:
    """Default :class:`WorkloadPort` backed by the ``occurrence`` table.

    Both helpers filter on ``workspace_id`` (via the caller's ``ctx``)
    and ``property_id`` so cross-property work doesn't leak into the
    tiebreaker. ``scheduled_for_local`` is parsed via
    :func:`datetime.fromisoformat` — the column holds property-local
    ISO-8601 strings written by the scheduler worker and the one-off
    service.
    """

    def count_tasks_in_window(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        user_id: str,
        property_id: str | None,
        local_dt: datetime,
        window_days: int = 7,
    ) -> int:
        if property_id is None:
            return 0
        rows = session.scalars(
            select(Occurrence.scheduled_for_local).where(
                Occurrence.workspace_id == ctx.workspace_id,
                Occurrence.assignee_user_id == user_id,
                Occurrence.property_id == property_id,
                Occurrence.scheduled_for_local.is_not(None),
            )
        ).all()
        anchor = local_dt.replace(tzinfo=None)
        # ``window_days`` is the **total** width of the window (spec
        # §06 "Assignment algorithm" step 4: "fewest tasks in the
        # 7-day window around ``scheduled_for_local``"); split it
        # evenly on either side so the default ``7`` yields a window
        # exactly 7 days wide.
        half_window = timedelta(days=window_days) / 2
        lower_bound = anchor - half_window
        upper_bound = anchor + half_window
        count = 0
        for raw in rows:
            if raw is None:
                continue
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                # Malformed rows are skipped rather than crashing the
                # tiebreaker; the generator's write path enforces the
                # shape on new inserts so a bad row is a migration
                # artefact, not a runtime expectation.
                continue
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            if lower_bound <= parsed <= upper_bound:
                count += 1
        return count

    def last_task_at(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        user_id: str,
        property_id: str | None,
    ) -> datetime | None:
        if property_id is None:
            return None
        rows = session.scalars(
            select(Occurrence.scheduled_for_local).where(
                Occurrence.workspace_id == ctx.workspace_id,
                Occurrence.assignee_user_id == user_id,
                Occurrence.property_id == property_id,
                Occurrence.scheduled_for_local.is_not(None),
            )
        ).all()
        latest: datetime | None = None
        for raw in rows:
            if raw is None:
                continue
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if parsed.tzinfo is not None:
                parsed = parsed.replace(tzinfo=None)
            if latest is None or parsed > latest:
                latest = parsed
        return latest


# Singleton default — the ports are pure-function shaped, so a
# module-level instance keeps the caller-side code clean.
_DEFAULT_WORKLOAD: WorkloadPort = _DefaultWorkload()


def availability_for(
    session: Session,
    ctx: WorkspaceContext,
    user_id: str,
    local_dt: datetime,
    property_id: str | None,
    *,
    port: AvailabilityPort | None = None,
) -> AvailabilityVerdict:
    """Public availability entry point — re-used by the scheduler UI.

    The manager's §14 scheduler needs the same "is this user free?"
    answer as the assignment algorithm; exposing it here keeps one
    source of truth. Defaults to the module's permissive
    :func:`_always_available`; pass ``port`` to inject a real check.
    """
    resolved = port if port is not None else _always_available
    return resolved(session, ctx, user_id, local_dt, property_id)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_task(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
) -> Occurrence:
    row = session.scalar(
        select(Occurrence).where(
            Occurrence.id == task_id,
            Occurrence.workspace_id == ctx.workspace_id,
        )
    )
    if row is None:
        raise TaskNotFound(f"task {task_id!r} not visible in workspace")
    return row


def _load_schedule(session: Session, schedule_id: str) -> Schedule | None:
    return session.scalar(
        select(Schedule).where(
            Schedule.id == schedule_id,
            Schedule.deleted_at.is_(None),
        )
    )


def _parse_local(task: Occurrence) -> datetime:
    """Resolve the occurrence's anchor timestamp for availability checks.

    Prefers ``scheduled_for_local`` (property-local naive ISO-8601)
    because the spec's availability rules are defined in the
    property's local frame. Falls back to the UTC ``starts_at``
    column (stripped of tzinfo) when the local column is missing —
    pre-cd-22e rows do not populate it.
    """
    if task.scheduled_for_local:
        try:
            parsed = datetime.fromisoformat(task.scheduled_for_local)
        except ValueError:
            parsed = task.starts_at
    else:
        parsed = task.starts_at
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PortBundle:
    """Pinned copy of the caller's port choices, with defaults applied.

    Every entry point accepts optional port overrides; resolving them
    into this immutable bundle once up-front keeps the algorithm code
    readable and guarantees a single resolution per call (a caller
    passing ``available=None`` doesn't get a fresh default on every
    candidate check).
    """

    available: AvailabilityPort
    rota: RotaPort
    pool: CandidatePoolPort
    workload: WorkloadPort


def _bundle(
    available: AvailabilityPort | None,
    rota: RotaPort | None,
    pool: CandidatePoolPort | None,
    workload: WorkloadPort | None,
) -> _PortBundle:
    return _PortBundle(
        available=available if available is not None else _always_available,
        rota=rota if rota is not None else _always_covers,
        pool=pool if pool is not None else _empty_pool,
        workload=workload if workload is not None else _DEFAULT_WORKLOAD,
    )


def _user_is_candidate(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    property_id: str | None,
    local_dt: datetime,
    ports: _PortBundle,
) -> bool:
    """Apply the availability + rota filters to one user id.

    Split out because both the primary/backup walk and the
    candidate-pool branch need the same "does this user pass?"
    check; keeping the two in lock-step avoids a class of bugs where
    the pool would accept someone the primary walk rejected.
    """
    verdict = ports.available(session, ctx, user_id, local_dt, property_id)
    if not verdict.available:
        return False
    return ports.rota(session, ctx, user_id, property_id, local_dt)


def _pick_from_primary_and_backups(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task: Occurrence,
    local_dt: datetime,
    ports: _PortBundle,
) -> tuple[str | None, AssignmentSource, int | None, tuple[str, ...]]:
    """Walk primary + ordered backups.

    Returns ``(user_id, source, backup_index, tried_ids)``:

    * ``user_id is not None`` — a winner was found; ``source`` is
      ``"primary"`` or ``"backup"`` (with ``backup_index`` set).
    * ``user_id is None`` — no winner; ``source`` is
      ``"candidate_pool"`` as a placeholder (the caller moves on
      to step 2). ``tried_ids`` carries the ids we tested so the
      pool step can exclude them.

    A task without a parent schedule returns
    ``(None, "candidate_pool", None, ())`` — there is no primary or
    backup list to walk.
    """
    if task.schedule_id is None:
        return None, "candidate_pool", None, ()
    schedule = _load_schedule(session, task.schedule_id)
    if schedule is None:
        return None, "candidate_pool", None, ()

    primary = schedule.assignee_user_id
    backups = tuple(schedule.backup_assignee_user_ids or ())
    tried: list[str] = []

    if primary:
        tried.append(primary)
        if _user_is_candidate(
            session,
            ctx,
            user_id=primary,
            property_id=task.property_id,
            local_dt=local_dt,
            ports=ports,
        ):
            return primary, "primary", None, tuple(tried)

    for index, user_id in enumerate(backups):
        if user_id in tried:
            # Defence-in-depth: the schedule writer rejects duplicates
            # (see :class:`ScheduleCreate`), but a hand-edited row
            # could slip one through. Skip silently to avoid a double
            # availability probe on the same user.
            continue
        tried.append(user_id)
        if _user_is_candidate(
            session,
            ctx,
            user_id=user_id,
            property_id=task.property_id,
            local_dt=local_dt,
            ports=ports,
        ):
            return user_id, "backup", index, tuple(tried)

    return None, "candidate_pool", None, tuple(tried)


def _pick_from_pool(
    session: Session,
    ctx: WorkspaceContext,
    *,
    task: Occurrence,
    local_dt: datetime,
    exclude: Sequence[str],
    ports: _PortBundle,
) -> tuple[str | None, int]:
    """Build + filter the candidate pool; apply tiebreakers.

    Returns ``(chosen_user_id, candidate_count)`` — ``candidate_count``
    is the size of the post-filter pool the caller audits, and is
    ``0`` when the pool was empty from the start or every user was
    filtered out.
    """
    raw_pool = ports.pool(
        session, ctx, task.expected_role_id, task.property_id, tuple(exclude)
    )
    exclude_set = set(exclude)
    filtered: list[str] = []
    for user_id in raw_pool:
        if user_id in exclude_set:
            continue
        if not _user_is_candidate(
            session,
            ctx,
            user_id=user_id,
            property_id=task.property_id,
            local_dt=local_dt,
            ports=ports,
        ):
            continue
        filtered.append(user_id)

    if not filtered:
        return None, 0
    if len(filtered) == 1:
        return filtered[0], 1

    # Tiebreakers. ``count_tasks_in_window`` drives the primary sort
    # (fewest wins); ``last_task_at`` drives the rotation tiebreaker
    # (oldest / ``None`` wins). ``user_id`` breaks final ties so the
    # result is deterministic under equal workload + no-history.
    _EPOCH = datetime(1970, 1, 1)

    def _sort_key(user_id: str) -> tuple[int, datetime, str]:
        count = ports.workload.count_tasks_in_window(
            session,
            ctx,
            user_id=user_id,
            property_id=task.property_id,
            local_dt=local_dt,
        )
        last = ports.workload.last_task_at(
            session, ctx, user_id=user_id, property_id=task.property_id
        )
        # ``None`` → maximally stale (epoch). The sort then treats
        # "never worked here" as "oldest", which matches the spec's
        # rotation intent ("pick the user whose last task at this
        # property is the oldest").
        return count, last if last is not None else _EPOCH, user_id

    filtered.sort(key=_sort_key)
    return filtered[0], len(filtered)


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def _publish_assigned(
    bus: EventBus,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    task_id: str,
    assigned_to: str,
) -> None:
    bus.publish(
        TaskAssigned(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=clock.now(),
            task_id=task_id,
            assigned_to=assigned_to,
        )
    )


def _publish_reassigned(
    bus: EventBus,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    task_id: str,
    previous_user_id: str | None,
    new_user_id: str,
) -> None:
    bus.publish(
        TaskReassigned(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=clock.now(),
            task_id=task_id,
            previous_user_id=previous_user_id,
            new_user_id=new_user_id,
        )
    )


def _publish_unassigned(
    bus: EventBus,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    task_id: str,
    reason: str,
) -> None:
    bus.publish(
        TaskUnassigned(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=clock.now(),
            task_id=task_id,
            reason=reason,
        )
    )


def _publish_primary_unavailable(
    bus: EventBus,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    task_id: str,
    candidate_count: int,
) -> None:
    bus.publish(
        TaskPrimaryUnavailable(
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.audit_correlation_id,
            occurred_at=clock.now(),
            task_id=task_id,
            candidate_count=candidate_count,
        )
    )


def _audit_assignment(
    session: Session,
    ctx: WorkspaceContext,
    clock: Clock,
    *,
    task: Occurrence,
    result: AssignmentResult,
    action: str,
    previous_user_id: str | None,
    reason: str | None,
) -> None:
    after: dict[str, object] = {
        "assigned_user_id": result.assigned_user_id,
        "assignment_source": result.source,
        "candidate_count": result.candidate_count,
    }
    if result.backup_index is not None:
        after["backup_index"] = result.backup_index
    if reason is not None:
        after["reason"] = reason
    diff: dict[str, object] = {
        "before": {"assigned_user_id": previous_user_id},
        "after": after,
    }
    write_audit(
        session,
        ctx,
        entity_kind="task",
        entity_id=task.id,
        action=action,
        diff=diff,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def assign_task(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    *,
    override_user_id: str | None = None,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
    available: AvailabilityPort | None = None,
    rota: RotaPort | None = None,
    pool: CandidatePoolPort | None = None,
    workload: WorkloadPort | None = None,
) -> AssignmentResult:
    """Assign (or reassign auto-style) a task to the best candidate.

    Flow:

    1. If ``override_user_id`` is set, skip the algorithm and write
       the override through. ``assignment_source = "manual"``,
       ``task.assigned`` (or ``task.reassigned`` when the previous
       assignee differs) fires.
    2. Else walk primary + backups (§06 step 1). A winner wires
       ``assignment_source = "primary" | "backup"`` (with
       :attr:`AssignmentResult.backup_index` set for backups) and
       emits ``task.assigned``.
    3. Else build the candidate pool excluding the tried primary /
       backups (§06 step 2). One candidate → direct assign; >1 →
       tiebreakers; 0 → leave unassigned + emit
       :class:`TaskPrimaryUnavailable` (step 1 attempted) or
       :class:`TaskUnassigned` (no step 1 to begin with).

    The task row's ``assignee_user_id`` is updated in place; the
    caller's Unit of Work commits the change. One audit row
    (``task.assigned`` action) lands per invocation, carrying
    ``assignment_source``, ``candidate_count``, and the before/after
    assignee.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    ports = _bundle(available, rota, pool, workload)

    task = _load_task(session, ctx, task_id)
    previous_user_id = task.assignee_user_id

    # --- Manual override path. -------------------------------------
    if override_user_id is not None:
        task.assignee_user_id = override_user_id
        session.flush()
        result = AssignmentResult(
            task_id=task.id,
            assigned_user_id=override_user_id,
            source="manual",
            candidate_count=0,
        )
        _audit_assignment(
            session,
            ctx,
            resolved_clock,
            task=task,
            result=result,
            action="task.assigned",
            previous_user_id=previous_user_id,
            reason=None,
        )
        if previous_user_id is not None and previous_user_id != override_user_id:
            _publish_reassigned(
                resolved_bus,
                ctx,
                resolved_clock,
                task_id=task.id,
                previous_user_id=previous_user_id,
                new_user_id=override_user_id,
            )
        else:
            _publish_assigned(
                resolved_bus,
                ctx,
                resolved_clock,
                task_id=task.id,
                assigned_to=override_user_id,
            )
        return result

    local_dt = _parse_local(task)

    # --- Step 1: primary + ordered backups. ------------------------
    chosen, source, backup_index, tried = _pick_from_primary_and_backups(
        session, ctx, task=task, local_dt=local_dt, ports=ports
    )
    step1_attempted = bool(tried)

    if chosen is not None:
        task.assignee_user_id = chosen
        session.flush()
        result = AssignmentResult(
            task_id=task.id,
            assigned_user_id=chosen,
            source=source,
            candidate_count=0,
            backup_index=backup_index,
        )
        _audit_assignment(
            session,
            ctx,
            resolved_clock,
            task=task,
            result=result,
            action="task.assigned",
            previous_user_id=previous_user_id,
            reason=None,
        )
        _publish_assigned(
            resolved_bus,
            ctx,
            resolved_clock,
            task_id=task.id,
            assigned_to=chosen,
        )
        return result

    # --- Step 2-4: candidate pool + tiebreakers. -------------------
    pool_pick, candidate_count = _pick_from_pool(
        session,
        ctx,
        task=task,
        local_dt=local_dt,
        exclude=tried,
        ports=ports,
    )

    if pool_pick is not None:
        task.assignee_user_id = pool_pick
        session.flush()
        result = AssignmentResult(
            task_id=task.id,
            assigned_user_id=pool_pick,
            source="candidate_pool",
            candidate_count=candidate_count,
        )
        _audit_assignment(
            session,
            ctx,
            resolved_clock,
            task=task,
            result=result,
            action="task.assigned",
            previous_user_id=previous_user_id,
            reason=None,
        )
        _publish_assigned(
            resolved_bus,
            ctx,
            resolved_clock,
            task_id=task.id,
            assigned_to=pool_pick,
        )
        return result

    # --- Step 5: zero candidates. ----------------------------------
    # ``task.assignee_user_id`` stays ``NULL``. If the task already
    # had an assignee, clear it — an auto-assign run that finds no
    # candidate should not silently keep a stale holder.
    if previous_user_id is not None:
        task.assignee_user_id = None
        session.flush()

    result = AssignmentResult(
        task_id=task.id,
        assigned_user_id=None,
        source="unassigned",
        candidate_count=candidate_count,
    )
    reason = (
        "primary_and_backups_unavailable" if step1_attempted else "candidate_pool_empty"
    )
    _audit_assignment(
        session,
        ctx,
        resolved_clock,
        task=task,
        result=result,
        action="task.unassigned",
        previous_user_id=previous_user_id,
        reason=reason,
    )
    if step1_attempted:
        _publish_primary_unavailable(
            resolved_bus,
            ctx,
            resolved_clock,
            task_id=task.id,
            candidate_count=candidate_count,
        )
    else:
        _publish_unassigned(
            resolved_bus,
            ctx,
            resolved_clock,
            task_id=task.id,
            reason=reason,
        )
    return result


def reassign_task(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    new_user_id: str,
    *,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssignmentResult:
    """Move a task from its current assignee to ``new_user_id``.

    Distinct from :func:`assign_task` + ``override_user_id`` because
    the semantics are *move*, not *assign*: the task must already
    have a holder, and the emitted event is :class:`TaskReassigned`
    with both old + new ids. A manager UI "change assignee" button
    routes here; the NL intake agent's "reassign to X" action routes
    here; a creation-time explicit assignee goes through
    :func:`assign_task`.

    Raises :exc:`TaskAlreadyAssigned` (the name is inverted —
    reassigning requires a previous assignee) when ``assignee_user_id``
    is currently ``None``.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus

    task = _load_task(session, ctx, task_id)
    previous_user_id = task.assignee_user_id
    if previous_user_id is None:
        raise TaskAlreadyAssigned(
            f"task {task_id!r} has no current assignee; use assign_task() "
            "with override_user_id to pin one from scratch"
        )

    if previous_user_id == new_user_id:
        # Idempotent — no-op write, no audit, no event. A manager who
        # drags a task onto its existing assignee should not see a
        # noisy audit trail or a toast.
        return AssignmentResult(
            task_id=task.id,
            assigned_user_id=new_user_id,
            source="manual",
            candidate_count=0,
        )

    task.assignee_user_id = new_user_id
    session.flush()
    result = AssignmentResult(
        task_id=task.id,
        assigned_user_id=new_user_id,
        source="manual",
        candidate_count=0,
    )
    _audit_assignment(
        session,
        ctx,
        resolved_clock,
        task=task,
        result=result,
        action="task.reassigned",
        previous_user_id=previous_user_id,
        reason=None,
    )
    _publish_reassigned(
        resolved_bus,
        ctx,
        resolved_clock,
        task_id=task.id,
        previous_user_id=previous_user_id,
        new_user_id=new_user_id,
    )
    return result


def unassign_task(
    session: Session,
    ctx: WorkspaceContext,
    task_id: str,
    *,
    reason: str,
    clock: Clock | None = None,
    event_bus: EventBus | None = None,
) -> AssignmentResult:
    """Explicitly clear a task's assignee.

    Used by the manager "unassign" UI and by the stay-lifecycle pull-
    back path when ``max_advance_days`` is exhausted. ``reason`` is a
    short free-form string the caller supplies and lands on both the
    audit diff and the emitted :class:`TaskUnassigned` event so
    digests + toasts can render a meaningful explanation.

    Idempotent when the task already has no assignee: no write, no
    audit, no event — same no-op contract as
    :func:`reassign_task` with a no-op move.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_bus = event_bus if event_bus is not None else default_event_bus

    task = _load_task(session, ctx, task_id)
    previous_user_id = task.assignee_user_id

    if previous_user_id is None:
        return AssignmentResult(
            task_id=task.id,
            assigned_user_id=None,
            source="unassigned",
            candidate_count=0,
        )

    task.assignee_user_id = None
    session.flush()
    result = AssignmentResult(
        task_id=task.id,
        assigned_user_id=None,
        source="unassigned",
        candidate_count=0,
    )
    _audit_assignment(
        session,
        ctx,
        resolved_clock,
        task=task,
        result=result,
        action="task.unassigned",
        previous_user_id=previous_user_id,
        reason=reason,
    )
    _publish_unassigned(
        resolved_bus,
        ctx,
        resolved_clock,
        task_id=task.id,
        reason=reason,
    )
    return result


# ---------------------------------------------------------------------------
# Hook integration
# ---------------------------------------------------------------------------


def build_assignment_hook(
    *,
    available: AvailabilityPort | None = None,
    rota: RotaPort | None = None,
    pool: CandidatePoolPort | None = None,
    workload: WorkloadPort | None = None,
    clock: Clock | None = None,
) -> Callable[[Session, WorkspaceContext, str], str | None]:
    """Return an assignment callable matching :data:`oneoff.AssignmentHook`.

    The one-off service (:mod:`app.domain.tasks.oneoff`) owns its
    own ``task.created`` + ``task.assigned`` event fanout; wiring
    :func:`assign_task` in directly would double-emit. The hook
    therefore calls the algorithm with a **private, no-op event
    bus** — the row still receives its ``assignee_user_id`` update
    and an audit row (``task.assigned`` carrying
    ``assignment_source``) lands, but the caller retains control
    of the outward-facing event stream.

    Returns the chosen user id (or ``None`` when the algorithm left
    the task unassigned), matching :data:`AssignmentHook`'s
    ``Callable[[Session, WorkspaceContext, str], str | None]``
    signature.
    """
    silent_bus = EventBus()

    def _hook(
        session: Session,
        ctx: WorkspaceContext,
        task_id: str,
    ) -> str | None:
        result = assign_task(
            session,
            ctx,
            task_id,
            clock=clock,
            event_bus=silent_bus,
            available=available,
            rota=rota,
            pool=pool,
            workload=workload,
        )
        return result.assigned_user_id

    return _hook
