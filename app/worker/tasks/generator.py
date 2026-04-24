"""``generate_task_occurrences`` — hourly scheduler tick.

Walks every live, non-paused, in-window schedule in a workspace,
expands its RRULE + RDATE minus EXDATE in the property's timezone,
and materialises one ``occurrence`` row per new
``(schedule_id, scheduled_for_local)`` pair. Idempotent: a second
run over the same horizon inserts nothing new. Property closures
suppress occurrences and emit a ``schedules.skipped_for_closure``
audit event instead. A summary ``schedules.generation_tick`` row
lands at the end of each invocation.

Public surface:

* :class:`GenerationReport` — counts the worker returns (and the
  audit payload it writes). Shape pinned so §16's operator
  dashboards can rely on it.
* :func:`generate_task_occurrences` — the entry point. Signature:
  ``(ctx, *, session, now=None, clock=None, horizon_days=30,
  expand_checklist=None, assign=None, event_bus=None) ->
  GenerationReport``.

**RRULE expansion.** ``rrulestr`` is anchored at the schedule's
``dtstart_local`` **attached to the property timezone** via
``zoneinfo.ZoneInfo``. Wall-clock recurrence then preserves the
property-local time across DST boundaries (09:00 stays 09:00 even
when UTC offset shifts), which is what the spec wants — a weekly
"Saturdays 09:00" schedule should not drift one hour twice a year.

**Idempotency.** The partial unique index
``UNIQUE(schedule_id, scheduled_for_local) WHERE schedule_id IS NOT
NULL`` (cd-22e migration) is the backstop. The worker still checks
existence before insert so it can report skipped duplicates in
:attr:`GenerationReport.skipped_duplicate` without relying on the
per-dialect integrity-error surface.

**WorkspaceContext** is threaded through every DB read. The worker
never reads tenancy from the environment; the caller (APScheduler
tick, CLI invocation, test) resolves a context per workspace before
calling in.

**Hooks for downstream tasks.**

* ``expand_checklist`` — the cd-p5-checklist-template hook called
  after each insert. Signature
  ``(session, ctx, occurrence_id, template, scheduled_for_local)
  -> None``. The default is a no-op; cd-p5 wires the real body.
* ``assign`` — the cd-8luu assignment-service hook. Signature
  ``(session, ctx, occurrence_id) -> None``. The default is a
  no-op.
* ``event_bus`` — the target for ``task.created`` publications.
  Defaults to the process-global :data:`app.events.bus.bus`. Tests
  inject a fresh :class:`~app.events.bus.EventBus` for isolation.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Generation" / §"Pause
vs active range" / §"Interaction with assignment and generation"
and ``docs/specs/02-domain-model.md`` §"occurrence" / §"task".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property, PropertyClosure
from app.adapters.db.tasks.models import Occurrence, Schedule, TaskTemplate
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import TaskCreated
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "DEFAULT_HORIZON_DAYS",
    "AssignmentHook",
    "ChecklistExpansionHook",
    "GenerationReport",
    "generate_task_occurrences",
]


# ---------------------------------------------------------------------------
# Constants + hook signatures
# ---------------------------------------------------------------------------


# §06 "Generation" step 1 — "now + 30 days". Callers can override via
# the ``horizon_days`` kwarg (which ultimately comes from the
# workspace-resolved ``scheduling.horizon_days`` setting once the
# settings cascade lands; for now the caller passes the resolved
# value in directly).
DEFAULT_HORIZON_DAYS: int = 30

# Guard against pathological RRULEs. A correctly-bounded rule stops
# naturally; an open-ended daily rule over a 30-day horizon yields at
# most ~30 occurrences per schedule. Cap the inner iteration at 10k
# so a malformed unbounded rule cannot starve the tick.
_MAX_OCCURRENCES_PER_SCHEDULE = 10_000


# Called once per newly-inserted occurrence to seed the
# :class:`ChecklistItem` rows from the template's
# ``checklist_template_json`` payload. The default no-op defers to
# cd-p5-checklist-template.
ChecklistExpansionHook = Callable[
    [Session, WorkspaceContext, str, TaskTemplate, datetime],
    None,
]


# Called once per newly-inserted occurrence to pick an assignee per
# §06 "Assignment algorithm". The default no-op defers to cd-8luu.
AssignmentHook = Callable[[Session, WorkspaceContext, str], None]


def _noop_expand_checklist(
    session: Session,
    ctx: WorkspaceContext,
    occurrence_id: str,
    template: TaskTemplate,
    scheduled_for_local: datetime,
) -> None:
    """Default checklist-expansion hook — no-op until cd-p5 lands.

    We deliberately do not attempt a partial implementation here:
    the RRULE-filtered seeding rule (§06 "Seeding is RRULE-filtered")
    is subtle enough that a stopgap would either lie to downstream
    readers or be thrown away when the real implementation arrives.
    """
    _ = session, ctx, occurrence_id, template, scheduled_for_local


def _noop_assign(
    session: Session,
    ctx: WorkspaceContext,
    occurrence_id: str,
) -> None:
    """Default assignment hook — no-op until cd-8luu lands.

    The assignment algorithm (§06 "Assignment algorithm") depends on
    availability precedence, rota composition, and the work-role
    tables (cd-5kv4 landed ``work_role`` + ``user_work_role``; cd-8luu
    wires the real assignment service). Until cd-8luu lands the
    generator leaves ``assignee_user_id`` null and the downstream
    digest surfaces the task as unassigned per §06 step 5.
    """
    _ = session, ctx, occurrence_id


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GenerationReport:
    """Summary counts for one ``generate_task_occurrences`` invocation.

    Frozen + slotted so the audit writer can flatten to JSON
    deterministically and tests can equality-check the full shape.

    * ``schedules_walked`` — how many live, non-paused, in-window
      schedules the run considered. The denominator for the rest.
    * ``tasks_created`` — newly-inserted ``occurrence`` rows.
    * ``skipped_duplicate`` — candidates that already had a row at
      ``(schedule_id, scheduled_for_local)``. Proves idempotency
      without reading the DB a second time.
    * ``skipped_for_closure`` — candidates suppressed because a
      :class:`PropertyClosure` covered the day. One
      ``schedules.skipped_for_closure`` audit row is written per
      skip; the count here matches those rows 1:1.
    * ``new_task_ids`` — the ULIDs of the rows this run inserted,
      so callers (tests, operator dashboards) can walk the set
      without re-querying.
    """

    schedules_walked: int
    tasks_created: int
    skipped_duplicate: int
    skipped_for_closure: int
    new_task_ids: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate_task_occurrences(
    ctx: WorkspaceContext,
    *,
    session: Session,
    now: datetime | None = None,
    clock: Clock | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    expand_checklist: ChecklistExpansionHook | None = None,
    assign: AssignmentHook | None = None,
    event_bus: EventBus | None = None,
) -> GenerationReport:
    """Run one hourly generation tick for the caller's workspace.

    ``now`` pins the horizon's upper bound; if omitted it is taken
    from ``clock`` (or :class:`~app.util.clock.SystemClock` if
    ``clock`` is also omitted). Both are exposed so tests can drive
    the worker deterministically.

    Returns a :class:`GenerationReport` with the counts described in
    its docstring. Writes one ``schedules.generation_tick`` audit
    row at the end of the run and one ``schedules.skipped_for_
    closure`` per suppressed day. Publishes one
    :class:`~app.events.types.TaskCreated` per new row.

    Does **not** commit the session; the caller's Unit-of-Work owns
    the transaction boundary (§01 "Key runtime invariants" #3).

    Raises :class:`ValueError` on an invalid ``horizon_days``
    (non-positive or obviously unreasonable > 10 years) — the
    generator must reject obvious misconfiguration rather than
    silently walking forever.
    """
    if horizon_days <= 0 or horizon_days > 3_650:
        raise ValueError(f"horizon_days must be in 1..3650; got {horizon_days}")

    resolved_clock = clock if clock is not None else SystemClock()
    resolved_now = now if now is not None else resolved_clock.now()
    if resolved_now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime in UTC")

    resolved_expand = (
        expand_checklist if expand_checklist is not None else _noop_expand_checklist
    )
    resolved_assign = assign if assign is not None else _noop_assign
    resolved_bus = event_bus if event_bus is not None else default_event_bus

    # ``resolved_now`` is UTC; the SQL gate below compares against
    # property-local ``active_from`` / ``active_until`` dates. A single
    # workspace can host properties across tz offsets up to ±14 h
    # (Pacific/Auckland at UTC+13, Pacific/Samoa at UTC-11), so the
    # UTC date can lead or lag each property's local date by at most
    # one calendar day. Widening the gate by ±1 day keeps the load
    # small while guaranteeing no schedule whose property-local date
    # is inside its active range is ever silently dropped here — the
    # per-candidate filter inside the loop re-checks in the correct
    # frame.
    utc_today = resolved_now.date()
    sql_gate_lower = utc_today - timedelta(days=1)
    sql_gate_upper = utc_today + timedelta(days=1)
    horizon_end = resolved_now + timedelta(days=horizon_days)

    schedules = _load_eligible_schedules(
        session,
        ctx,
        gate_lower=sql_gate_lower,
        gate_upper=sql_gate_upper,
    )

    new_task_ids: list[str] = []
    skipped_duplicate = 0
    skipped_for_closure = 0

    for schedule in schedules:
        # Pin the schedule's property_id locally so the narrowing
        # below (the None-check on ``property_row``) propagates to
        # every subsequent ``_load_closures`` / audit-writer call —
        # ``schedule.property_id`` is typed ``str | None`` on the
        # ORM, but if ``_load_property`` returned a row the pointer
        # is necessarily non-null (and we'd have bailed otherwise).
        schedule_property_id = schedule.property_id
        property_row = _load_property(session, schedule_property_id)
        if property_row is None or schedule_property_id is None:
            # A schedule whose property disappeared between writes is
            # a degenerate state (property_id ``SET NULL`` on delete).
            # Skip silently — the audit row for the parent-schedule
            # mutation already records the change; emitting a
            # per-tick row would just add noise.
            continue

        tz = _resolve_zone(property_row.timezone)
        template = _load_template(session, ctx, template_id=schedule.template_id)
        if template is None:
            # Mirror the property-missing case — the schedule would
            # already be unusable through the CRUD service's
            # template load-before-write. No new task can be
            # materialised without the template.
            continue

        duration_minutes = _resolve_duration(schedule, template)

        closures = _load_closures(session, property_id=schedule_property_id)

        anchor_local = _parse_local_anchor(schedule.dtstart_local, schedule.dtstart)
        rdates = _parse_rdate_payload(schedule.rdate_local)
        exdates = set(_parse_rdate_payload(schedule.exdate_local))

        active_from = _parse_local_date(schedule.active_from)
        active_until = _parse_local_date(schedule.active_until)

        # Lower bound on the RRULE expansion. Without one a daily
        # schedule with an ancient ``dtstart_local`` would either walk
        # millions of past dates per tick or — worse — silently stop
        # before reaching "now" once the per-schedule occurrence cap
        # kicks in. Anchor at ``active_from`` when it's set (the
        # per-candidate filter would drop anything earlier anyway) and
        # fall back to the RRULE's own ``dtstart`` otherwise. ``.between()``
        # auto-clips to ``dtstart`` so overshooting backwards is safe.
        if active_from is not None:
            window_start_local = datetime.combine(active_from, anchor_local.time())
        else:
            window_start_local = anchor_local
        horizon_end_local = horizon_end.astimezone(tz).replace(tzinfo=None)

        for candidate_local in _expand_rule(
            schedule.rrule_text,
            anchor_local=anchor_local,
            zone=tz,
            window_start_local=window_start_local,
            horizon_end_local=horizon_end_local,
            rdates=rdates,
            exdates=exdates,
        ):
            # Active-range filter. ``active_from`` is a property-local
            # date; the ordinal comparison matches §06 step 1 ("active_
            # from ≤ today").
            candidate_date = candidate_local.date()
            if active_from is not None and candidate_date < active_from:
                continue
            if active_until is not None and candidate_date > active_until:
                continue

            if _covered_by_closure(closures, candidate_local, tz):
                skipped_for_closure += 1
                _write_closure_skip_audit(
                    session,
                    ctx,
                    schedule_id=schedule.id,
                    property_id=schedule_property_id,
                    candidate_local=candidate_local,
                    clock=resolved_clock,
                )
                continue

            scheduled_for_local_iso = _iso_local(candidate_local)
            if _already_materialised(
                session,
                ctx,
                schedule_id=schedule.id,
                scheduled_for_local_iso=scheduled_for_local_iso,
            ):
                skipped_duplicate += 1
                continue

            starts_utc = _to_utc(candidate_local, tz)
            ends_utc = starts_utc + timedelta(minutes=duration_minutes)

            row = Occurrence(
                id=new_ulid(),
                workspace_id=ctx.workspace_id,
                schedule_id=schedule.id,
                template_id=schedule.template_id,
                property_id=schedule_property_id,
                assignee_user_id=schedule.assignee_user_id,
                starts_at=starts_utc,
                ends_at=ends_utc,
                scheduled_for_local=scheduled_for_local_iso,
                originally_scheduled_for=scheduled_for_local_iso,
                state="scheduled",
                cancellation_reason=None,
                created_at=resolved_clock.now(),
            )
            session.add(row)
            session.flush()

            resolved_expand(session, ctx, row.id, template, candidate_local)
            resolved_assign(session, ctx, row.id)

            resolved_bus.publish(
                TaskCreated(
                    workspace_id=ctx.workspace_id,
                    actor_id=ctx.actor_id,
                    correlation_id=ctx.audit_correlation_id,
                    occurred_at=resolved_clock.now(),
                    task_id=row.id,
                )
            )

            new_task_ids.append(row.id)

    _write_generation_tick_audit(
        session,
        ctx,
        schedules_walked=len(schedules),
        tasks_created=len(new_task_ids),
        skipped_duplicate=skipped_duplicate,
        skipped_for_closure=skipped_for_closure,
        horizon_days=horizon_days,
        now=resolved_now,
        clock=resolved_clock,
    )

    return GenerationReport(
        schedules_walked=len(schedules),
        tasks_created=len(new_task_ids),
        skipped_duplicate=skipped_duplicate,
        skipped_for_closure=skipped_for_closure,
        new_task_ids=tuple(new_task_ids),
    )


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def _load_eligible_schedules(
    session: Session,
    ctx: WorkspaceContext,
    *,
    gate_lower: date,
    gate_upper: date,
) -> list[Schedule]:
    """Return live, non-paused, in-window schedules ordered by id.

    §06 "Pause vs active range": ``paused_at`` always wins; active-
    range bounds are only evaluated when ``paused_at`` is null. We
    filter both in SQL to keep the per-tick working set small.

    ``gate_lower`` / ``gate_upper`` are UTC dates widened by one day
    on either side of "now" (see caller) so the SQL-side active-range
    predicate never drops a schedule whose property-local date is
    inside its active range but whose UTC-today differs by a calendar
    day (±14 h TZ offsets in the wild). The authoritative active-
    range check runs per candidate inside the loop, in the property's
    own frame.

    ``active_from`` / ``active_until`` are stored as ISO-8601 text
    (``YYYY-MM-DD``); lexical comparison matches calendar ordering
    for that shape, which keeps the predicate portable between
    SQLite and Postgres without a dialect-specific cast.
    """
    upper_iso = gate_upper.isoformat()
    lower_iso = gate_lower.isoformat()
    stmt = (
        select(Schedule)
        .where(Schedule.workspace_id == ctx.workspace_id)
        .where(Schedule.deleted_at.is_(None))
        .where(Schedule.paused_at.is_(None))
        # "active_from is not in the future (from any property's frame)"
        .where((Schedule.active_from.is_(None)) | (Schedule.active_from <= upper_iso))
        # "active_until is not in the past (from any property's frame)"
        .where((Schedule.active_until.is_(None)) | (Schedule.active_until >= lower_iso))
        .order_by(Schedule.id.asc())
    )
    return list(session.scalars(stmt).all())


def _load_property(session: Session, property_id: str | None) -> Property | None:
    """Load the parent property; return ``None`` on missing.

    ``property_id`` on :class:`Schedule` is nullable (workspace-wide
    schedule) and ``SET NULL`` on delete. The v1 ``Occurrence`` model
    requires a non-null ``property_id``, so a schedule without one
    cannot materialise under the current schema — return ``None``
    and let the caller skip. Once the v1 occurrence model accepts
    null property_ids (workspace-wide tasks), this branch evolves
    with it.
    """
    if property_id is None:
        return None
    # ``property`` is intentionally not workspace-scoped (see
    # ``app/adapters/db/places/__init__.py``); a direct select by id
    # is the accepted read shape for this table.
    stmt = select(Property).where(Property.id == property_id)
    return session.scalars(stmt).one_or_none()


def _load_template(
    session: Session, ctx: WorkspaceContext, *, template_id: str
) -> TaskTemplate | None:
    """Load the parent template (workspace-scoped, live only).

    Mirrors ``_load_template`` in :mod:`app.domain.tasks.schedules`
    but returns ``None`` instead of raising — the generator has to
    keep walking the rest of the schedules if one template was
    concurrently soft-deleted.
    """
    stmt = (
        select(TaskTemplate)
        .where(TaskTemplate.id == template_id)
        .where(TaskTemplate.workspace_id == ctx.workspace_id)
        .where(TaskTemplate.deleted_at.is_(None))
    )
    return session.scalars(stmt).one_or_none()


def _load_closures(session: Session, *, property_id: str) -> list[PropertyClosure]:
    """Load every :class:`PropertyClosure` for ``property_id``.

    ``property_closure`` is not workspace-scoped in the v1 slice
    (see :mod:`app.adapters.db.places`); the property FK enforces
    the boundary via ``property_workspace``. We select every closure
    for this property and let the caller match against occurrence
    dates in-memory — a closure list per property is tiny by nature
    (a handful of rows covering renovation windows, seasonal
    breaks, iCal import noise).
    """
    stmt = (
        select(PropertyClosure)
        .where(PropertyClosure.property_id == property_id)
        .order_by(PropertyClosure.starts_at.asc())
    )
    return list(session.scalars(stmt).all())


def _already_materialised(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    scheduled_for_local_iso: str,
) -> bool:
    """Return ``True`` iff ``(schedule_id, scheduled_for_local)`` already exists.

    The partial unique index (cd-22e migration) backs this check —
    an INSERT race would raise ``IntegrityError``, but the worker
    runs one tick at a time per workspace so the SELECT-then-INSERT
    window is narrow. Pre-flighting lets us report skipped
    duplicates in the report without probing per-dialect
    integrity-error surfaces.
    """
    stmt = (
        select(Occurrence.id)
        .where(Occurrence.workspace_id == ctx.workspace_id)
        .where(Occurrence.schedule_id == schedule_id)
        .where(Occurrence.scheduled_for_local == scheduled_for_local_iso)
        .limit(1)
    )
    return session.scalars(stmt).first() is not None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _resolve_zone(name: str) -> ZoneInfo:
    """Resolve a property's IANA timezone; fall back to UTC on error.

    A property mis-configured with a blank or junk timezone is a
    data bug, but the generator cannot block the rest of the
    workspace on one broken row. Falling back to UTC lets the
    schedule still produce occurrences (at their naive local time
    treated as UTC) while the manager fixes the row. The miss is
    deliberately silent here — the property-CRUD path owns
    validation (cd-i6u / cd-8u5); this is belt-and-braces.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_local_anchor(dtstart_local: str | None, dtstart: datetime) -> datetime:
    """Parse the schedule's local DTSTART into a naive datetime.

    Pre-cd-k4l rows carry only ``dtstart`` (UTC-ish); new rows carry
    ``dtstart_local`` (naive ISO-8601). We prefer the explicit local
    column when present; otherwise we strip the timezone off the
    legacy UTC value and treat it as local, matching the fallback
    convention in :func:`app.domain.tasks.schedules._row_to_view`.
    """
    if dtstart_local:
        parsed = datetime.fromisoformat(dtstart_local.strip())
        if parsed.tzinfo is not None:
            return parsed.replace(tzinfo=None)
        return parsed
    return dtstart.replace(tzinfo=None)


def _parse_rdate_payload(payload: str | None) -> list[datetime]:
    """Parse a line- or semicolon-separated RDATE / EXDATE body.

    Mirrors :func:`app.domain.tasks.schedules._split_rdate_lines` +
    :func:`_parse_rdate_payload` but returns naive datetimes
    directly without raising :class:`InvalidRRule` — the CRUD
    service already rejected malformed bodies at write time; the
    worker just skips any entry it cannot parse to stay robust
    against pre-cd-k4l rows.
    """
    if not payload:
        return []
    lines: list[str] = [payload]
    for sep in ("\n", ";"):
        lines = [piece for chunk in lines for piece in chunk.split(sep)]
    out: list[datetime] = []
    for line in lines:
        clean = line.strip()
        if not clean:
            continue
        try:
            parsed = datetime.fromisoformat(clean)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        out.append(parsed)
    return out


def _parse_local_date(value: str | None) -> date | None:
    """Parse an ISO-8601 date column (``active_from`` / ``active_until``)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _resolve_duration(schedule: Schedule, template: TaskTemplate) -> int:
    """Pick the effective duration minutes.

    §06 "Schedule" — ``schedule.duration_minutes`` is nullable;
    ``NULL`` falls back to ``template.duration_minutes`` and then to
    the cd-chd legacy ``default_duration_min``. The generator must
    pick a concrete integer so ``ends_at`` can be computed.
    """
    if schedule.duration_minutes is not None:
        return schedule.duration_minutes
    if template.duration_minutes is not None:
        return template.duration_minutes
    return template.default_duration_min


# ---------------------------------------------------------------------------
# RRULE expansion + timezone helpers
# ---------------------------------------------------------------------------


def _expand_rule(
    rrule_body: str,
    *,
    anchor_local: datetime,
    zone: ZoneInfo,
    window_start_local: datetime,
    horizon_end_local: datetime,
    rdates: list[datetime],
    exdates: set[datetime],
) -> Iterable[datetime]:
    """Yield every occurrence in ``[window_start_local, horizon_end_local]``.

    We attach ``zone`` to the anchor before handing it to
    :func:`dateutil.rrule.rrulestr` so the RRULE engine preserves
    the local **wall-clock** across DST boundaries (the intent of
    "Saturdays at 09:00" is 09:00 local, even when the UTC offset
    shifts). After expansion we strip the tzinfo back off so the
    emitted datetimes share their shape with the RDATE / EXDATE
    entries (which are always naive-local on the write path).

    ``window_start_local`` bounds the iteration below so an ancient
    ``dtstart_local`` can't push us past
    :data:`_MAX_OCCURRENCES_PER_SCHEDULE` before reaching "now" —
    :meth:`rrule.between` is the expansion method of record because
    it auto-clips at ``dtstart`` (so overshooting backwards is safe)
    and stops iteration at ``horizon_end_local``.

    RDATEs are additive; EXDATEs subtract by exact match. Sorted
    ascending, deduplicated, capped at
    :data:`_MAX_OCCURRENCES_PER_SCHEDULE` for safety.
    """
    anchor_aware = anchor_local.replace(tzinfo=zone)
    try:
        parsed = rrulestr(rrule_body, dtstart=anchor_aware)
    except (ValueError, TypeError):
        # A malformed rule is a data bug — the CRUD service rejects
        # these at write time, but a pre-cd-k4l row could still be
        # degenerate. Skip the schedule rather than abort the tick.
        return []

    seen: set[datetime] = set()
    out: list[datetime] = []

    for extra in rdates:
        if extra > horizon_end_local:
            continue
        if extra < window_start_local:
            continue
        if extra in exdates:
            continue
        if extra not in seen:
            seen.add(extra)
            out.append(extra)

    # ``rrulestr`` returns either an ``rrule`` or an ``rruleset``. Both
    # expose ``between``; it accepts tz-aware bounds when the rule's
    # anchor is aware, so we re-attach the property's zone to the
    # naive window bounds here. ``inc=True`` is symmetric with the
    # RDATE filter above (inclusive on both ends).
    window_start_aware = window_start_local.replace(tzinfo=zone)
    horizon_end_aware = horizon_end_local.replace(tzinfo=zone)
    try:
        rrule_hits: list[datetime] = list(
            parsed.between(window_start_aware, horizon_end_aware, inc=True)
        )
    except (ValueError, TypeError):
        return []

    for occ in rrule_hits[:_MAX_OCCURRENCES_PER_SCHEDULE]:
        # Strip the zone — the whole rest of the pipeline treats
        # occurrences as naive property-local datetimes.
        naive = occ.replace(tzinfo=None) if occ.tzinfo is not None else occ
        if naive in exdates or naive in seen:
            continue
        seen.add(naive)
        out.append(naive)

    out.sort()
    return out


def _to_utc(candidate_local: datetime, zone: ZoneInfo) -> datetime:
    """Attach ``zone`` to ``candidate_local`` and project to UTC.

    ``candidate_local`` is a naive property-local wall-clock. We
    localise it via :meth:`datetime.replace(tzinfo=zone)` — the
    ``zoneinfo`` stdlib resolves the correct UTC offset for the
    given wall-clock, including the standard/DST transition. A
    wall-clock that falls **in** the spring-forward gap (e.g.
    ``02:30`` on DST-start in Europe/Paris) does not exist; we
    accept the ``zoneinfo`` stdlib's interpretation (it picks the
    standard-time offset) rather than re-implementing the DST
    handling here. The spec treats such authoring as a caller bug
    — a schedule at 02:30 local never fires in a tz that skips
    that wall-clock twice a year.
    """
    aware = candidate_local.replace(tzinfo=zone)
    return aware.astimezone(ZoneInfo("UTC"))


def _iso_local(candidate_local: datetime) -> str:
    """Render a naive property-local datetime as the idempotency key.

    Always ``YYYY-MM-DDTHH:MM:SS`` — zero-padded down to seconds so
    two candidates with the same wall-clock hash to the same key
    regardless of how they were parsed. Microseconds are dropped
    (RRULE expansion does not produce sub-second values).
    """
    return candidate_local.replace(microsecond=0).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Closure matching
# ---------------------------------------------------------------------------


def _covered_by_closure(
    closures: list[PropertyClosure],
    candidate_local: datetime,
    zone: ZoneInfo,
) -> bool:
    """Return ``True`` iff any closure covers ``candidate_local``'s day.

    §06 "Interaction with assignment and generation": a
    :class:`PropertyClosure` covers ``scheduled_for_local.date()``
    when the candidate day falls inside its ``[starts_at, ends_at)``
    UTC window (viewed in the property frame). The v1 model stores
    closures as UTC datetimes; we project the candidate to UTC and
    check inclusion. The spec's ``unit_id`` filter lands with cd-8u5
    — until then every closure on the property applies to every
    task on the property.
    """
    if not closures:
        return False
    candidate_utc = _to_utc(candidate_local, zone)
    # SQLite strips tzinfo off ``DateTime(timezone=True)`` columns on
    # read; Postgres keeps it. Normalise both sides to naive-UTC for
    # comparison so the predicate is portable and the inclusion check
    # cannot throw ``TypeError`` on one backend only.
    candidate_naive_utc = candidate_utc.replace(tzinfo=None)
    for closure in closures:
        start = _as_naive_utc(closure.starts_at)
        end = _as_naive_utc(closure.ends_at)
        if start <= candidate_naive_utc < end:
            return True
    return False


def _as_naive_utc(value: datetime) -> datetime:
    """Return ``value`` as a naive-UTC datetime.

    If ``value`` is already naive (SQLite round-trip), return it
    verbatim — the column writes are always UTC-aware, so a naive
    read is a UTC value that has lost its zone. If ``value`` is
    aware, convert to UTC and strip the zone.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Audit writers
# ---------------------------------------------------------------------------


def _write_closure_skip_audit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedule_id: str,
    property_id: str,
    candidate_local: datetime,
    clock: Clock,
) -> None:
    """Record one ``schedules.skipped_for_closure`` row per suppressed day.

    Payload shape is deliberate: ``schedule_id`` + ``property_id``
    so the audit feed can be filtered by either, plus the
    property-local timestamp that was suppressed so the manager can
    reconcile against the closure window.
    """
    write_audit(
        session,
        ctx,
        entity_kind="schedule",
        entity_id=schedule_id,
        action="schedules.skipped_for_closure",
        diff={
            "schedule_id": schedule_id,
            "property_id": property_id,
            "scheduled_for_local": _iso_local(candidate_local),
        },
        clock=clock,
    )


def _write_generation_tick_audit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    schedules_walked: int,
    tasks_created: int,
    skipped_duplicate: int,
    skipped_for_closure: int,
    horizon_days: int,
    now: datetime,
    clock: Clock,
) -> None:
    """Record the per-tick summary row.

    §06 "Generation" asks for one ``schedules.generation_tick`` audit
    entry with the full count set so operators can chart throughput
    over time. The payload mirrors :class:`GenerationReport` plus
    the ``horizon_days`` + ``tick_at`` context.
    """
    write_audit(
        session,
        ctx,
        # Anchored on the workspace so dashboards can pivot by
        # tenant; ``entity_id = workspace_id`` matches the convention
        # used by other workspace-level audit entries.
        entity_kind="workspace",
        entity_id=ctx.workspace_id,
        action="schedules.generation_tick",
        diff={
            "schedules_walked": schedules_walked,
            "tasks_created": tasks_created,
            "skipped_duplicate": skipped_duplicate,
            "skipped_for_closure": skipped_for_closure,
            "horizon_days": horizon_days,
            "tick_at": now.isoformat(),
        },
        clock=clock,
    )
