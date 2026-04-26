"""``detect_overdue`` — soft-overdue sweeper tick (cd-hurw).

Walks every live task in the caller's workspace whose
``state IN ('scheduled', 'pending', 'in_progress')`` and whose
``ends_at + grace_minutes`` has slipped below ``now``, flips them to
``state='overdue'``, stamps ``overdue_since=now``, and emits one
:class:`~app.events.types.TaskOverdue` per row.

Idempotent by construction: a task already in ``state='overdue'`` is
excluded from the load query (``state IN (...)`` filter), so a second
tick over the same data set inserts no audit, writes no row, fires no
event. The full sweep summary lands as a ``tasks.overdue_tick`` audit
row (one per workspace per tick) so operator dashboards can chart
flip rate + per-property breakdown over time.

Public surface:

* :class:`OverdueReport` — counts the worker returns (and the audit
  payload it writes). Frozen + slotted so the audit writer can
  flatten to JSON deterministically and tests can equality-check
  the full shape.
* :func:`detect_overdue` — the entry point. Signature
  ``(ctx, *, session, now=None, clock=None, grace_minutes=None,
  event_bus=None) -> OverdueReport``.

**Manual-transition safety.** Between the SELECT (load eligible
candidates) and the per-row UPDATE (flip state), a worker / manager
may have manually transitioned the task (start, complete, skip,
cancel). To avoid clobbering a deliberate move, the per-row UPDATE
re-asserts the ``state IN ('scheduled', 'pending', 'in_progress')``
predicate in the WHERE clause: a manual transition lands first; the
sweeper's UPDATE matches zero rows and the deliberate move stands.
The skip is silent (no event, no audit beyond the per-tick summary),
matching the spec's "soft state never overwrites a manual transition
that happened between ticks" invariant.

**Workspace settings (cd-hurw temporary stub).** The grace window
and tick cadence are spec'd as workspace-resolved settings
(``tasks.overdue_grace_minutes`` and ``tasks.overdue_tick_seconds``).
The settings cascade reads from ``workspace.settings_json`` already;
this slice exposes a thin :func:`resolve_overdue_grace_minutes`
helper that reads the key with a sensible default. The richer
cascade (workspace → property → unit → engagement → task) is the
job of cd-settings-cascade — same pattern the completion module's
:data:`~app.domain.tasks.completion.EvidencePolicyResolver` uses
today.

**WorkspaceContext** is threaded through every DB read and event
publish. The worker never reads tenancy from the environment; the
caller (APScheduler tick fan-out, CLI invocation, test) resolves a
context per workspace before calling in.

See ``docs/specs/06-tasks-and-scheduling.md`` §"State machine"
("overdue is soft, never terminal; manual transitions clear
``overdue_since``") and
``docs/specs/16-deployment-operations.md`` §"Worker process".
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from app.adapters.db.tasks.models import Occurrence
from app.adapters.db.workspace.models import Workspace
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import TaskOverdue
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock

__all__ = [
    "DEFAULT_OVERDUE_GRACE_MINUTES",
    "DEFAULT_OVERDUE_TICK_SECONDS",
    "SETTINGS_KEY_OVERDUE_GRACE_MINUTES",
    "SETTINGS_KEY_OVERDUE_TICK_SECONDS",
    "OverdueReport",
    "detect_overdue",
    "resolve_overdue_grace_minutes",
    "resolve_overdue_tick_seconds",
]


# ---------------------------------------------------------------------------
# Constants + setting keys
# ---------------------------------------------------------------------------


# §06 default grace window. The per-workspace setting
# ``tasks.overdue_grace_minutes`` overrides; the constant is the
# fallback when the key is unset (or the workspace row is missing).
# Pinned at 15 min to match spec §06 + the cd-hurw migration's
# backfill. Keeping the literal here means the worker, the migration,
# and the test fixtures all agree on the default without an indirect
# import.
DEFAULT_OVERDUE_GRACE_MINUTES: Final[int] = 15

# §06 default tick cadence. Surfaced as a constant so the
# scheduler-wiring callsite can import it instead of re-deriving the
# 5-minute boundary from the spec.
DEFAULT_OVERDUE_TICK_SECONDS: Final[int] = 300

# Dotted keys inside ``workspace.settings_json``. The §02 settings
# cascade owns the namespace (``tasks.*`` for task-domain knobs); the
# key strings are pinned here so the worker, the API admin surface,
# and the (future) settings-cascade resolver line up.
SETTINGS_KEY_OVERDUE_GRACE_MINUTES: Final[str] = "tasks.overdue_grace_minutes"
SETTINGS_KEY_OVERDUE_TICK_SECONDS: Final[str] = "tasks.overdue_tick_seconds"


# Source states the sweeper will flip. Any other state — ``done``,
# ``skipped``, ``cancelled``, ``approved``, or already ``overdue`` —
# is left untouched. Pulled out so the load-query filter and the
# per-row UPDATE guard reference the same tuple (the manual-transition
# safety invariant only holds when the two predicates agree).
_FLIPPABLE_STATES: Final[tuple[str, ...]] = ("scheduled", "pending", "in_progress")


# ---------------------------------------------------------------------------
# Settings resolvers
# ---------------------------------------------------------------------------


def _resolve_int_setting(
    session: Session,
    *,
    workspace_id: str,
    key: str,
    default: int,
) -> int:
    """Read an integer-valued setting from ``workspace.settings_json``.

    Returns ``default`` for any of:

    * the workspace row is missing (defensive — the tenancy middleware
      should have resolved it);
    * the ``settings_json`` payload is not a dict (corruption);
    * the key is absent;
    * the value is not coercible to a positive integer.

    A non-positive value (zero or negative) collapses to ``default``
    too: a zero grace window or zero tick cadence is almost certainly
    a misconfiguration, and the worker would otherwise either flip
    every just-ended task instantly (grace=0) or never tick (tick=0).
    The conservative posture matches the rest of the worker's
    "missing setting → fall back to spec default" stance.
    """
    # Belt-and-braces: ``workspace`` is the tenancy anchor and not
    # registered with the ORM tenant filter, but a future migration
    # could change that. Wrap in ``tenant_agnostic`` so the SELECT
    # never trips ``TenantFilterMissing`` if a caller forgot to bind
    # a context before calling in.
    with tenant_agnostic():
        settings_json = session.scalar(
            select(Workspace.settings_json).where(Workspace.id == workspace_id)
        )
    if not isinstance(settings_json, dict):
        return default
    raw = settings_json.get(key)
    if isinstance(raw, bool):
        # ``isinstance(True, int)`` is ``True`` in Python; explicitly
        # reject bool so a stray ``"key": true`` in the settings JSON
        # does not silently coerce to ``1``.
        return default
    if isinstance(raw, int) and raw > 0:
        return raw
    return default


def resolve_overdue_grace_minutes(session: Session, *, workspace_id: str) -> int:
    """Resolve ``tasks.overdue_grace_minutes`` for a workspace.

    Reads :attr:`Workspace.settings_json`; falls back to
    :data:`DEFAULT_OVERDUE_GRACE_MINUTES` when the key is unset
    (or the value is not a positive integer). The richer §02
    settings cascade (workspace → property → unit → engagement →
    task) is the job of cd-settings-cascade; this slice surfaces
    the workspace layer because that is the only one the v1
    sweeper needs (the grace is a per-tenant policy, not a
    per-task one).
    """
    return _resolve_int_setting(
        session,
        workspace_id=workspace_id,
        key=SETTINGS_KEY_OVERDUE_GRACE_MINUTES,
        default=DEFAULT_OVERDUE_GRACE_MINUTES,
    )


def resolve_overdue_tick_seconds(session: Session, *, workspace_id: str) -> int:
    """Resolve ``tasks.overdue_tick_seconds`` for a workspace.

    Mirror of :func:`resolve_overdue_grace_minutes` for the cadence
    knob the scheduler uses. Currently unused inside the worker
    body itself — exposed so the scheduler-wiring layer (cd-hurw
    extension to :mod:`app.worker.scheduler`) can read it without
    importing the same dotted key string twice. Falls back to
    :data:`DEFAULT_OVERDUE_TICK_SECONDS`.
    """
    return _resolve_int_setting(
        session,
        workspace_id=workspace_id,
        key=SETTINGS_KEY_OVERDUE_TICK_SECONDS,
        default=DEFAULT_OVERDUE_TICK_SECONDS,
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OverdueReport:
    """Summary counts for one ``detect_overdue`` invocation.

    Frozen + slotted so the audit writer can flatten to JSON
    deterministically and tests can equality-check the full shape.

    * ``flipped_count`` — rows the sweeper transitioned to
      ``overdue`` (i.e. the UPDATE actually landed). The denominator
      for the per-tick fan-out's structured-log summary.
    * ``skipped_already_overdue`` — candidates the load query saw in
      ``state='overdue'`` already. Always zero today (the load query
      excludes that state), kept on the report so a future widening
      that re-enrols stuck rows has a place to surface them without a
      shape change.
    * ``skipped_manual_transition`` — candidates whose per-row UPDATE
      matched zero rows because a concurrent manual transition
      landed between SELECT and UPDATE. The §06 "soft state never
      overwrites a manual transition" invariant materialised in a
      counter.
    * ``per_property_breakdown`` — ``{property_id: count}``, only
      properties with ``flipped_count > 0`` appear. Personal /
      workspace-scoped tasks (``property_id IS NULL``) are bucketed
      under the empty string key so the dict is JSON-serialisable
      without a magic ``None``.
    * ``flipped_task_ids`` — ULIDs of the rows the sweeper flipped,
      so callers (tests, operator dashboards) can walk the set
      without re-querying.
    * ``tick_started_at`` / ``tick_ended_at`` — sweeper bookends.
      Useful for measuring per-tick duration in the audit feed
      without joining heartbeat rows.
    """

    flipped_count: int
    skipped_already_overdue: int
    skipped_manual_transition: int
    per_property_breakdown: Mapping[str, int]
    tick_started_at: datetime
    tick_ended_at: datetime
    flipped_task_ids: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def detect_overdue(
    ctx: WorkspaceContext,
    *,
    session: Session,
    now: datetime | None = None,
    clock: Clock | None = None,
    grace_minutes: int | None = None,
    event_bus: EventBus | None = None,
) -> OverdueReport:
    """Run one sweeper tick for the caller's workspace.

    ``now`` pins the comparison instant; if omitted it is taken from
    ``clock`` (or :class:`~app.util.clock.SystemClock` if ``clock`` is
    also omitted). Both are exposed so tests can drive the worker
    deterministically.

    ``grace_minutes`` overrides the per-workspace setting; if
    ``None`` the worker resolves
    :func:`resolve_overdue_grace_minutes`. Tests pin a deterministic
    grace by passing the kwarg.

    Returns an :class:`OverdueReport`. Writes one
    ``tasks.overdue_tick`` audit row at the end of the run with the
    full count set + per-property breakdown. Publishes one
    :class:`~app.events.types.TaskOverdue` per flipped row.

    Does **not** commit the session; the caller's Unit-of-Work owns
    the transaction boundary (§01 "Key runtime invariants" #3).

    Raises :class:`ValueError` on a non-positive ``grace_minutes``
    override — a zero or negative grace is almost certainly a caller
    bug (would flip every just-ended task instantly).
    """
    if grace_minutes is not None and grace_minutes <= 0:
        raise ValueError(
            f"grace_minutes must be a positive integer; got {grace_minutes}"
        )

    resolved_clock = clock if clock is not None else SystemClock()
    resolved_now = now if now is not None else resolved_clock.now()
    if resolved_now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime in UTC")
    resolved_bus = event_bus if event_bus is not None else default_event_bus
    resolved_grace = (
        grace_minutes
        if grace_minutes is not None
        else resolve_overdue_grace_minutes(session, workspace_id=ctx.workspace_id)
    )

    tick_started_at = resolved_now
    cutoff = resolved_now - timedelta(minutes=resolved_grace)

    # 1. Load eligible candidates. Predicate matches §06: rows in a
    #    flippable state whose ``ends_at`` is on the wrong side of the
    #    cutoff. The ``state IN (...)`` leg is the selective one and
    #    rides the cd-hurw composite index
    #    ``ix_occurrence_workspace_state_overdue_since`` for the
    #    per-tenant scan.
    candidates = list(
        session.scalars(
            select(Occurrence)
            .where(Occurrence.workspace_id == ctx.workspace_id)
            .where(Occurrence.state.in_(_FLIPPABLE_STATES))
            .where(Occurrence.ends_at < cutoff)
            .order_by(Occurrence.id.asc())
        ).all()
    )

    flipped_task_ids: list[str] = []
    per_property_breakdown: dict[str, int] = {}
    skipped_manual_transition = 0
    # ``skipped_already_overdue`` is always zero today (the load
    # query excludes ``state='overdue'``); kept on the report shape
    # for forward-compat. See :class:`OverdueReport`.
    skipped_already_overdue = 0

    for task in candidates:
        # 2. Re-assert the state predicate in the WHERE clause of the
        #    UPDATE. A manual transition landing between SELECT and
        #    UPDATE makes the row's state no longer match — the
        #    UPDATE matches zero rows, the deliberate move stands,
        #    and the sweeper silently skips. This is the
        #    spec-mandated "soft state never overwrites a manual
        #    transition" guard.
        result = session.execute(
            update(Occurrence)
            .where(Occurrence.id == task.id)
            .where(Occurrence.workspace_id == ctx.workspace_id)
            .where(Occurrence.state.in_(_FLIPPABLE_STATES))
            .values(state="overdue", overdue_since=resolved_now)
        )
        # ``Session.execute`` returns ``Result[Any]`` in the public
        # type stubs; bulk-DML paths actually return a
        # :class:`CursorResult` with a concrete ``rowcount``. The
        # narrow assertion is precise, not defensive — a non-cursor
        # result would mean SQLAlchemy's UPDATE seam regressed and we
        # want the failure to be loud (mirrors the same pattern in
        # :func:`app.api.middleware.idempotency._prune_in_session`).
        assert isinstance(result, CursorResult)
        if result.rowcount == 0:
            # Manual transition won the race. No event, no per-row
            # audit — the manual write already produced its own
            # ``task.start`` / ``task.complete`` / ``task.skip`` /
            # ``task.cancel`` row.
            skipped_manual_transition += 1
            continue

        flipped_task_ids.append(task.id)
        bucket_key = task.property_id if task.property_id is not None else ""
        per_property_breakdown[bucket_key] = (
            per_property_breakdown.get(bucket_key, 0) + 1
        )

        # 3. Emit ``task.overdue``. ``slipped_minutes`` is floored
        #    minutes between ``ends_at`` and ``now``; under the load
        #    predicate ``ends_at + grace < now`` the value is at
        #    least ``grace`` (and zero only when ``grace == 0``,
        #    which the resolver rejects). ``ends_at`` may come back
        #    naive on SQLite — coerce to UTC before subtracting so
        #    the arithmetic is portable.
        ends_at_aware = _ensure_utc(task.ends_at)
        slipped_seconds = (resolved_now - ends_at_aware).total_seconds()
        slipped_minutes = max(0, math.floor(slipped_seconds / 60))
        resolved_bus.publish(
            TaskOverdue(
                workspace_id=ctx.workspace_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.audit_correlation_id,
                occurred_at=resolved_now,
                task_id=task.id,
                assigned_user_id=task.assignee_user_id,
                overdue_since=resolved_now,
                slipped_minutes=slipped_minutes,
            )
        )

    tick_ended_at = resolved_clock.now()

    _write_overdue_tick_audit(
        session,
        ctx,
        flipped_count=len(flipped_task_ids),
        skipped_already_overdue=skipped_already_overdue,
        skipped_manual_transition=skipped_manual_transition,
        per_property_breakdown=per_property_breakdown,
        grace_minutes=resolved_grace,
        tick_started_at=tick_started_at,
        tick_ended_at=tick_ended_at,
        clock=resolved_clock,
    )

    return OverdueReport(
        flipped_count=len(flipped_task_ids),
        skipped_already_overdue=skipped_already_overdue,
        skipped_manual_transition=skipped_manual_transition,
        per_property_breakdown=per_property_breakdown,
        tick_started_at=tick_started_at,
        tick_ended_at=tick_ended_at,
        flipped_task_ids=tuple(flipped_task_ids),
    )


# ---------------------------------------------------------------------------
# Audit writer
# ---------------------------------------------------------------------------


def _write_overdue_tick_audit(
    session: Session,
    ctx: WorkspaceContext,
    *,
    flipped_count: int,
    skipped_already_overdue: int,
    skipped_manual_transition: int,
    per_property_breakdown: Mapping[str, int],
    grace_minutes: int,
    tick_started_at: datetime,
    tick_ended_at: datetime,
    clock: Clock,
) -> None:
    """Record the per-tick summary row.

    §06 asks for one ``tasks.overdue_tick`` audit entry with the full
    count set + per-property breakdown so operators can chart flip
    rate (and the breakdown) over time. Anchored on the workspace —
    ``entity_id = workspace_id`` — matching the
    ``schedules.generation_tick`` convention from
    :func:`app.worker.tasks.generator.generate_task_occurrences`.
    """
    write_audit(
        session,
        ctx,
        entity_kind="workspace",
        entity_id=ctx.workspace_id,
        action="tasks.overdue_tick",
        diff={
            "flipped_count": flipped_count,
            "skipped_already_overdue": skipped_already_overdue,
            "skipped_manual_transition": skipped_manual_transition,
            "per_property_breakdown": dict(per_property_breakdown),
            "grace_minutes": grace_minutes,
            "tick_started_at": tick_started_at.isoformat(),
            "tick_ended_at": tick_ended_at.isoformat(),
        },
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_utc(value: datetime) -> datetime:
    """Narrow a round-tripped ``DateTime(timezone=True)`` to aware UTC.

    SQLite strips tzinfo off ``DateTime(timezone=True)`` columns on
    read; PostgreSQL preserves it. The column is always written as
    aware UTC, so a naive read is a UTC value that has lost its
    zone. The :class:`TaskOverdue` event validator and the slipped-
    minutes arithmetic both require an aware datetime; coerce here
    before either consumes the value.
    """
    from datetime import UTC

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
