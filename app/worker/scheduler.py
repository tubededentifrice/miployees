"""APScheduler bootstrap + ``register_jobs`` hook.

The single seam through which downstream tasks (cd-j9l7 idempotency
sweep, cd-ca1k LLM-budget refresh, cd-dcl2 occurrence-generator fan-out,
cd-yqm4 user_workspace derive-refresh — all live) plug into the shared
scheduler.
Two entry-points exercise the same ``register_jobs`` call:

* **Inline (default)** — the FastAPI factory's lifespan hook starts
  the scheduler inside the web process when
  ``settings.worker == "internal"`` (§16 "Worker process"). No extra
  container is needed for single-VPS deployments (Recipe A of §16).
* **Standalone** — ``python -m app.worker`` boots an AsyncIO loop,
  registers the same job set, and handles SIGTERM / SIGINT for a
  graceful shutdown. The external Recipe B / D compose files
  (``worker:`` service) invoke this entrypoint.

**Scheduler class.** :class:`~apscheduler.schedulers.asyncio.AsyncIOScheduler`
— FastAPI's request loop is asyncio, and a sibling
:class:`~apscheduler.schedulers.background.BackgroundScheduler`
would create a second event loop in the same process with no
coordinated lifecycle. AsyncIO-native keeps start / stop consistent
across both entry-points.

**Job wrapping.** Every registered job goes through
:func:`wrap_job`, which:

1. Opens a fresh :class:`~app.adapters.db.session.UnitOfWorkImpl` per
   tick (never share a session across ticks — SQLAlchemy sessions
   are not safe for concurrent use and APScheduler may schedule
   overlapping runs if a tick overshoots its interval).
2. Logs ``worker.tick.start`` / ``worker.tick.end`` with the job id.
3. Catches every :class:`Exception` (never :class:`BaseException` —
   ``KeyboardInterrupt`` / ``SystemExit`` must propagate so the
   process can shut down cleanly). A crashing job logs at ERROR with
   the traceback and the next tick still runs.
4. On success, upserts the deployment-wide
   :class:`~app.adapters.db.ops.models.WorkerHeartbeat` row keyed by
   the job id — ``/readyz`` reads ``MAX(heartbeat_at)``, so any
   healthy tick is enough to flip readiness green.

**Idempotent start / stop.** Calling :func:`start` on an already-
running scheduler is a no-op (not a crash); :func:`stop` on a
stopped scheduler is likewise a no-op. Both paths short-circuit on
:attr:`AsyncIOScheduler.running` so a lifespan hook that double-
fires (process supervisor restarts, test fixtures) does not raise.

See ``docs/specs/01-architecture.md`` §"Worker" and
``docs/specs/16-deployment-operations.md`` §"Worker process",
§"Healthchecks".
"""

from __future__ import annotations

import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, Final

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.adapters.db.session import make_uow
from app.observability.metrics import (
    WORKER_JOB_DURATION_SECONDS,
    WORKER_JOBS_TOTAL,
    sanitize_label,
)
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.logging import new_request_id, reset_request_id, set_request_id
from app.worker.heartbeat import upsert_heartbeat

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

__all__ = [
    "GENERATOR_JOB_ID",
    "HEARTBEAT_JOB_ID",
    "HEARTBEAT_JOB_INTERVAL_SECONDS",
    "IDEMPOTENCY_SWEEP_JOB_ID",
    "LLM_BUDGET_REFRESH_INTERVAL_SECONDS",
    "LLM_BUDGET_REFRESH_JOB_ID",
    "OVERDUE_DETECT_INTERVAL_SECONDS",
    "OVERDUE_DETECT_JOB_ID",
    "USER_WORKSPACE_REFRESH_INTERVAL_SECONDS",
    "USER_WORKSPACE_REFRESH_JOB_ID",
    "create_scheduler",
    "register_jobs",
    "start",
    "stop",
    "wrap_job",
]

_log = logging.getLogger(__name__)


# Stable job id for the always-on heartbeat tick. Matches the string
# ``/readyz``'s freshness window tolerates (it reads
# ``MAX(heartbeat_at)``, not a specific name — any registered job
# bumps the same table — so choosing a descriptive id is a clarity
# thing, not a correctness thing). Pinned so tests and operators can
# grep for the row: "the bare-minimum liveness proof is
# ``scheduler_heartbeat``."
HEARTBEAT_JOB_ID: str = "scheduler_heartbeat"

# The heartbeat tick runs every 30 s, giving ``/readyz``'s 60 s
# staleness window a 2x safety margin against one skipped tick
# (scheduler pause during migration, momentary DB reconnect). Aligned
# with :mod:`app.api.health`'s ``_HEARTBEAT_STALE_AFTER`` comment.
HEARTBEAT_JOB_INTERVAL_SECONDS: int = 30

# Stable job id for the hourly generator tick (cd-dcl2). The
# per-workspace fan-out is built by
# :func:`_make_generator_fanout_body` and registered on the cron
# cadence ``CronTrigger(minute=0)`` — every workspace gets a
# :func:`~app.worker.tasks.generator.generate_task_occurrences` call
# under a system-actor :class:`WorkspaceContext`.
GENERATOR_JOB_ID: str = "generate_task_occurrences"

# Stable job id for the daily ``idempotency_key`` TTL sweep (cd-j9l7).
# Spec §12 "Idempotency" pins the TTL at 24 h; the sweep callable
# (:func:`app.api.middleware.idempotency.prune_expired_idempotency_keys`)
# removes rows older than that so the table never grows unbounded.
IDEMPOTENCY_SWEEP_JOB_ID: str = "idempotency_sweep"

# Stable job id for the 60 s LLM-budget aggregate refresh (cd-ca1k).
# Spec §11 "Workspace usage budget" §"Meter" pins the cadence:
# ``workspace_usage.cost_30d_cents`` is re-summed from the last 30
# days of ``llm_usage`` every 60 s so the cached aggregate never
# trails the meter by more than that window. The fan-out body
# iterates every workspace with a ``budget_ledger`` row, building a
# system-actor :class:`~app.tenancy.WorkspaceContext` per workspace
# and calling :func:`~app.domain.llm.budget.refresh_aggregate`.
LLM_BUDGET_REFRESH_JOB_ID: str = "llm_budget_refresh_aggregate"

# Interval for the LLM-budget refresh. Spec §11 pins 60 s; also
# matches the 60 s freshness promise surfaced on the admin /
# settings usage tile ("a cap edit reflects in the cached aggregate
# within 60 s"). Pulled out as a module-level constant so tests can
# import it rather than re-derive the number from the spec.
LLM_BUDGET_REFRESH_INTERVAL_SECONDS: int = 60

# Stable job id for the soft-overdue sweeper tick (cd-hurw). The
# per-workspace fan-out built by :func:`_make_overdue_fanout_body`
# calls :func:`~app.worker.tasks.overdue.detect_overdue` once per
# workspace under a system-actor :class:`WorkspaceContext`.
OVERDUE_DETECT_JOB_ID: str = "detect_overdue"

# Interval for the overdue sweeper. Spec §06 + cd-hurw pin 5 minutes
# (and surface the cadence as a per-workspace setting
# ``tasks.overdue_tick_seconds`` for future tuning). Pulled out as a
# module-level constant so tests and the scheduler-wiring code share
# the same number without re-deriving it from the spec.
OVERDUE_DETECT_INTERVAL_SECONDS: int = 300


# Stable job id for the ``user_workspace`` derive-refresh tick (cd-yqm4).
# The reconciler in
# :mod:`app.domain.identity.user_workspace_refresh` walks every active
# upstream (workspace-scoped role_grants, property-scoped role_grants
# resolved through ``property_workspace``, work_engagements; plus the
# forward-compat seams for ``org_workspace``) and brings the derived
# junction in line. Spec §02 "user_workspace" pins the table as
# derived; this job is the canonical reconciler.
USER_WORKSPACE_REFRESH_JOB_ID: str = "user_workspace_refresh"

# Interval for the user_workspace derive-refresh tick. The §02 spec
# does not pin a specific cadence — only that the worker keeps the
# junction reconciled. Five minutes is the default: small enough that
# a worker tick is the next thing the user sees after a grant is
# minted in the API (login redirect lands within one tick), large
# enough that the fan-out's full-table scan does not dominate the
# workload on a fleet with thousands of workspaces. Tests can pin
# this constant if they want to exercise the cadence boundary.
USER_WORKSPACE_REFRESH_INTERVAL_SECONDS: int = 300


# Job-body type. Downstream tasks supply either a synchronous callable
# (most of today's jobs — pure SQL + logging) or an ``async def``
# coroutine function (for jobs that need to ``await`` an async client
# such as the future LLM fan-out). The wrapper below branches:
#
# * sync bodies run inside :func:`asyncio.to_thread` so a blocking DB
#   op never starves the event loop — :meth:`AsyncIOScheduler.add_job`
#   without an explicit executor would otherwise run ``def`` jobs on
#   the loop itself and block every other coroutine.
# * async bodies are awaited directly on the loop; running them through
#   :func:`asyncio.to_thread` would call the coroutine function, return
#   an un-awaited coroutine object, and silently skip the body (logged
#   only as a ``RuntimeWarning`` — the heartbeat upsert would still
#   succeed and ``/readyz`` would stay green while the work vanished).
JobBody = Callable[[], None] | Callable[[], Awaitable[None]]


def create_scheduler(*, clock: Clock | None = None) -> AsyncIOScheduler:
    """Return a fresh :class:`AsyncIOScheduler` — not yet started.

    No jobs are added here; call :func:`register_jobs` next. The
    ``clock`` is stashed on the scheduler instance (under a pinned
    attribute name, not the standard APScheduler timezone field) so
    the job wrappers can reach it for heartbeat timestamps. We do not
    override APScheduler's internal clock because the library's own
    scheduling math is driven off the OS clock regardless; injecting
    a :class:`~app.util.clock.FrozenClock` only affects our business
    logic (the heartbeat column and any job body that reads it), not
    APScheduler's internal "when should I fire next" decisions.

    The scheduler defaults to UTC so trigger times are unambiguous
    across deployments — matches §01's "Time is UTC at rest" rule.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Stash the clock on the instance so ``wrap_job`` can reach it
    # without every downstream caller having to thread it through.
    # ``_crewday_clock`` uses a leading underscore + crewday prefix
    # so it does not collide with APScheduler's own attributes if the
    # library adds a ``clock`` hook in a future release.
    scheduler._crewday_clock = resolved_clock
    return scheduler


def _clock_for(scheduler: AsyncIOScheduler) -> Clock:
    """Return the :class:`Clock` stashed on ``scheduler`` or the system clock.

    A scheduler built outside :func:`create_scheduler` (unexpected,
    but possible if a caller wires APScheduler directly) falls back
    to :class:`SystemClock` rather than raising — the heartbeat is
    still correct, it just can't be driven by a test fixture.
    """
    clock = getattr(scheduler, "_crewday_clock", None)
    if isinstance(clock, Clock):
        return clock
    return SystemClock()


def wrap_job(
    func: JobBody,
    *,
    job_id: str,
    clock: Clock,
    heartbeat: bool = True,
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Return an async wrapper APScheduler can register as a job.

    The wrapper:

    1. Logs ``worker.tick.start`` at INFO with the job id.
    2. Runs ``func`` — detected once at wrap time:

       * ``async def`` / returns an awaitable → awaited directly on
         the event loop.
       * plain ``def`` → run via :func:`asyncio.to_thread` so a
         blocking DB op never starves the event loop.

       Mixing the two in one scheduler is supported, but each job's
       shape is pinned at registration and doesn't change per tick.
    3. On success, opens a fresh UoW and upserts the heartbeat row
       keyed by ``job_id`` (unless ``heartbeat=False`` for jobs that
       opt out — e.g. future jobs that write a per-tenant heartbeat
       of their own).
    4. Swallows + logs every :class:`Exception`. A job that keeps
       raising does not take the whole scheduler down; its heartbeat
       stops advancing and ``/readyz`` goes red via the staleness
       window — the natural escalation signal.
    5. Logs ``worker.tick.end`` at INFO with ``ok: True|False`` so
       operators can grep for stuck jobs.

    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) is
    deliberately NOT caught — the process shutdown path needs those
    to propagate so the scheduler can run its own cleanup.
    """
    import asyncio  # local import — asyncio is only needed when called
    import time as _time

    is_coroutine = inspect.iscoroutinefunction(func)
    job_label = sanitize_label(job_id)

    async def _runner() -> None:
        # Bind a fresh request_id per tick so structured-log records
        # the body emits get correlated end-to-end (the §16
        # "Observability / Logs" key contract). Worker ticks are the
        # subprocess-equivalent of an HTTP request — they need the
        # same id discipline so an operator scraping the JSON stream
        # can isolate one tick's lines from another's.
        request_id_token = set_request_id(new_request_id())
        start = _time.perf_counter()
        _log.info(
            "worker tick starting",
            extra={"event": "worker.tick.start", "job_id": job_id},
        )
        ok = False
        try:
            try:
                if is_coroutine:
                    # ``func`` is ``async def`` — invoke and await on
                    # the event loop. ``asyncio.to_thread`` would call
                    # the coroutine function, hand back an un-awaited
                    # coroutine object, and silently skip the body.
                    result = func()
                    if inspect.isawaitable(result):
                        await result
                else:
                    # Sync body — offload to the default executor so a
                    # blocking DB op does not pin the event loop.
                    await asyncio.to_thread(func)
                ok = True
            except Exception:
                # The job body's own logging (if any) fires first;
                # this backstop guarantees a record even if the body
                # swallowed.
                _log.exception(
                    "worker tick failed",
                    extra={"event": "worker.tick.error", "job_id": job_id},
                )

            if ok and heartbeat:
                try:
                    await asyncio.to_thread(_write_heartbeat, job_id, clock)
                except Exception:
                    # A heartbeat write failure is itself a signal —
                    # log and move on. The next successful tick will
                    # try again; if every tick fails the heartbeat
                    # goes stale and ``/readyz`` catches it.
                    _log.exception(
                        "worker heartbeat write failed",
                        extra={
                            "event": "worker.heartbeat.error",
                            "job_id": job_id,
                        },
                    )
                    ok = False

            duration = _time.perf_counter() - start
            WORKER_JOB_DURATION_SECONDS.labels(job=job_label).observe(duration)
            WORKER_JOBS_TOTAL.labels(
                job=job_label,
                status="ok" if ok else "error",
            ).inc()

            _log.info(
                "worker tick finished",
                extra={
                    "event": "worker.tick.end",
                    "job_id": job_id,
                    "ok": ok,
                },
            )
        finally:
            # Always restore — even if the heartbeat / metric path
            # raised — so the request id ContextVar does not leak
            # into the next tick scheduled on the same task.
            reset_request_id(request_id_token)

    return _runner


def _write_heartbeat(job_id: str, clock: Clock) -> None:
    """Upsert a fresh ``worker_heartbeat`` row for ``job_id``.

    Opens its own :class:`~app.adapters.db.session.UnitOfWorkImpl` so
    the heartbeat commit is independent of the job body's session —
    a job that failed halfway through its own transaction must not
    roll back the heartbeat row (and vice versa).
    """
    now = clock.now()
    with make_uow() as session:
        upsert_heartbeat(session, worker_name=job_id, now=now)


def register_jobs(
    scheduler: AsyncIOScheduler,
    *,
    clock: Clock | None = None,
) -> None:
    """Register the standard job set on ``scheduler``.

    Downstream tasks (cd-j9l7, cd-yqm4, the per-workspace occurrence
    generator fan-out) extend this function by adding one call to
    :meth:`scheduler.add_job` per job, each wrapped in
    :func:`wrap_job`. Keeping the registration in one function lets
    the lifespan hook and the ``__main__`` entrypoint share the same
    job set without copying the body.

    Idempotent — re-invoking ``register_jobs`` on the same scheduler
    (test fixtures, supervised restart, module reload) removes any
    existing job with the same id before the re-add. Note that
    APScheduler's ``replace_existing=True`` only deduplicates when
    the scheduler is actually running (started jobs live in the
    jobstore); on a not-yet-started scheduler the pending-jobs
    buffer is append-only, so an explicit :meth:`remove_job` is
    required. We do both so a started and a pending scheduler
    behave the same.
    """
    resolved_clock = clock if clock is not None else _clock_for(scheduler)

    # Drop any pre-existing entries for the ids we're about to add
    # so the registration is idempotent regardless of scheduler
    # state (see docstring). :class:`JobLookupError` is the expected
    # path when the id is not present (first register_jobs call);
    # we suppress it narrowly rather than swallowing ``Exception``
    # so a genuinely broken jobstore still surfaces.
    for pending_id in (
        HEARTBEAT_JOB_ID,
        GENERATOR_JOB_ID,
        IDEMPOTENCY_SWEEP_JOB_ID,
        LLM_BUDGET_REFRESH_JOB_ID,
        OVERDUE_DETECT_JOB_ID,
        USER_WORKSPACE_REFRESH_JOB_ID,
    ):
        with contextlib.suppress(JobLookupError):
            scheduler.remove_job(pending_id)

    # --- Always-on heartbeat ---
    # The simplest-possible job: write the heartbeat row and return.
    # ``/readyz`` reads ``MAX(heartbeat_at)`` across the whole table
    # so every wrapped job contributes to readiness, but this job
    # exists so the worker has SOMETHING to bump even when no
    # domain-level tick fires (e.g. during the window between the
    # scheduler starting and the hourly generator's first run).
    scheduler.add_job(
        wrap_job(_heartbeat_only_body, job_id=HEARTBEAT_JOB_ID, clock=resolved_clock),
        trigger=IntervalTrigger(seconds=HEARTBEAT_JOB_INTERVAL_SECONDS),
        id=HEARTBEAT_JOB_ID,
        name=HEARTBEAT_JOB_ID,
        replace_existing=True,
        # Fire immediately on scheduler start so ``/readyz`` flips
        # green within the first tick window rather than waiting the
        # full interval — important for container restart smoke tests
        # and for the integration suite in this change.
        next_run_time=None,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=HEARTBEAT_JOB_INTERVAL_SECONDS,
    )

    # --- Hourly occurrence generator fan-out (cd-dcl2) ---
    # Single-workspace callable in ``app/worker/tasks/generator.py``;
    # the per-tick fan-out across workspaces is built by
    # :func:`_make_generator_fanout_body`. Cron-anchored at the top
    # of every hour for the same operator-dashboard reasons cited in
    # the idempotency-sweep block (cron cadence is stable across
    # container restarts; an interval trigger would re-anchor on
    # every ``scheduler.start()``).
    scheduler.add_job(
        wrap_job(
            _make_generator_fanout_body(resolved_clock),
            job_id=GENERATOR_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=CronTrigger(minute=0),
        id=GENERATOR_JOB_ID,
        name=GENERATOR_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        # Tolerate a scheduler restart that misses the top-of-hour
        # firing by up to 10 min — running the tick late is strictly
        # better than skipping it, because the generator's own
        # idempotency (partial unique on ``(schedule_id,
        # scheduled_for_local)``) makes a late run safe.
        misfire_grace_time=600,
    )

    # --- Daily ``idempotency_key`` TTL sweep (cd-j9l7) ---
    # Spec §12 "Idempotency" pins the cache TTL at 24 h. Without a
    # periodic sweep the table grows unbounded — every retry adds a
    # row and nothing deletes them. We schedule a CRON trigger at
    # 03:00 UTC rather than an ``IntervalTrigger(hours=24)`` for two
    # reasons:
    #   1. Cron-based cadence is stable across container restarts; an
    #      interval trigger re-anchors on each ``scheduler.start()``
    #      so a deployment that restarts at noon would end up sweeping
    #      at noon every day — harmless, but harder to reason about
    #      from an operator dashboard that expects a fixed slot.
    #   2. 03:00 UTC lands in the lowest-traffic window for the
    #      North-Atlantic / European user base §16 assumes; the bulk
    #      ``DELETE ... WHERE created_at < cutoff`` takes a brief row
    #      lock on the backing index, so running it at the quiet hour
    #      keeps the p99 of a concurrent ``POST`` retry low.
    # ``misfire_grace_time=3600`` covers a scheduler restart around
    # 03:00 — running the sweep up to an hour late is strictly better
    # than skipping the day entirely. The callable is itself
    # idempotent (``DELETE`` where ``created_at < cutoff`` over rows
    # all older than the cutoff reaches zero after one run) so a
    # duplicate run is free.
    scheduler.add_job(
        wrap_job(
            _make_idempotency_sweep_body(resolved_clock),
            job_id=IDEMPOTENCY_SWEEP_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=CronTrigger(hour=3, minute=0),
        id=IDEMPOTENCY_SWEEP_JOB_ID,
        name=IDEMPOTENCY_SWEEP_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    # --- 60 s LLM budget aggregate refresh (cd-ca1k) ---
    # Spec §11 "Workspace usage budget" §"Meter" pins a 60 s cadence.
    # The fan-out body iterates every workspace, builds a system-actor
    # :class:`~app.tenancy.WorkspaceContext`, and calls
    # :func:`~app.domain.llm.budget.refresh_aggregate`. The per-workspace
    # call is idempotent (it rewrites ``spent_cents`` from the last 30
    # days of ``llm_usage``), so a misfire that runs late or a coalesced
    # tick is strictly safe.
    #
    # ``misfire_grace_time=90`` — one tick late is tolerated (idempotent
    # rewrite) but a two-tick-late run is a signal the scheduler is
    # stuck and a skip is preferable to a stacked catch-up.
    # ``coalesce=True`` + ``max_instances=1`` keep a slow refresh from
    # stacking ticks on an overloaded DB.
    scheduler.add_job(
        wrap_job(
            _make_llm_budget_refresh_body(resolved_clock),
            job_id=LLM_BUDGET_REFRESH_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=LLM_BUDGET_REFRESH_INTERVAL_SECONDS),
        id=LLM_BUDGET_REFRESH_JOB_ID,
        name=LLM_BUDGET_REFRESH_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=90,
    )

    # --- 5 min soft-overdue sweeper fan-out (cd-hurw) ---
    # Single-workspace callable in ``app/worker/tasks/overdue.py``;
    # the per-tick fan-out across workspaces is built by
    # :func:`_make_overdue_fanout_body`. Interval-anchored at 5 min;
    # the cadence matches the spec default and the
    # ``tasks.overdue_tick_seconds`` workspace setting (whose
    # per-tenant override is the cd-settings-cascade follow-up — the
    # scheduler wires the deployment-wide default for now). The
    # detect_overdue body is itself idempotent (the load query
    # excludes ``state='overdue'`` rows and the per-row UPDATE
    # re-asserts the source-state predicate so a manual transition
    # between ticks is preserved), so a misfire that runs late or a
    # coalesced tick is strictly safe.
    scheduler.add_job(
        wrap_job(
            _make_overdue_fanout_body(resolved_clock),
            job_id=OVERDUE_DETECT_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=OVERDUE_DETECT_INTERVAL_SECONDS),
        id=OVERDUE_DETECT_JOB_ID,
        name=OVERDUE_DETECT_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        # ``misfire_grace_time = OVERDUE_DETECT_INTERVAL_SECONDS`` —
        # one tick late is fine (the body is idempotent), two-ticks
        # late is a signal the scheduler is stuck and a skip is
        # preferable to a stacked catch-up that hammers the DB on an
        # already-strained host.
        misfire_grace_time=OVERDUE_DETECT_INTERVAL_SECONDS,
    )

    # --- 5 min user_workspace derive-refresh (cd-yqm4) ---
    # The reconciler in
    # :mod:`app.domain.identity.user_workspace_refresh` is the
    # canonical writer for the derived junction (§02
    # "user_workspace"). Domain services (signup, grant, invite,
    # remove_member) write the upstream rows; this tick brings
    # ``user_workspace`` in line.
    #
    # ``misfire_grace_time = USER_WORKSPACE_REFRESH_INTERVAL_SECONDS``
    # — one tick late is fine (the reconciler is idempotent: it
    # rewrites the same set), but two-ticks-late is a signal the
    # scheduler is stuck and a skip is preferable to a stacked
    # catch-up. ``coalesce=True`` + ``max_instances=1`` keep a slow
    # reconcile from stacking ticks on an overloaded DB.
    scheduler.add_job(
        wrap_job(
            _make_user_workspace_refresh_body(resolved_clock),
            job_id=USER_WORKSPACE_REFRESH_JOB_ID,
            clock=resolved_clock,
        ),
        trigger=IntervalTrigger(seconds=USER_WORKSPACE_REFRESH_INTERVAL_SECONDS),
        id=USER_WORKSPACE_REFRESH_JOB_ID,
        name=USER_WORKSPACE_REFRESH_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=USER_WORKSPACE_REFRESH_INTERVAL_SECONDS,
    )


def _heartbeat_only_body() -> None:
    """No-op job body — the heartbeat upsert runs after it returns.

    Exists as a distinct function (rather than ``lambda: None``) so
    the scheduler's log output shows the module-qualified name in
    stack traces if something upstream instruments the call.
    """


def _make_idempotency_sweep_body(clock: Clock) -> Callable[[], None]:
    """Build the daily ``idempotency_key`` TTL-sweep body (cd-j9l7).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — the cutoff
    is ``clock.now() - TTL``, which MUST be driven by the same clock
    the heartbeat uses. Otherwise a :class:`~app.util.clock.FrozenClock`
    under test (or a future simulated-time deployment) would have a
    deterministic heartbeat timestamp and a non-deterministic sweep
    cutoff — easy to mis-diagnose and pointless to tolerate given how
    cheap the closure is.

    The returned body is a thin adapter around
    :func:`app.api.middleware.idempotency.prune_expired_idempotency_keys`:
    the callable opens its own UoW (and therefore its own
    transaction) when no session is passed, so the scheduler wrapper
    does not need to thread one through. The sweeper returns the
    number of rows deleted; we log it at INFO with
    ``event=idempotency.sweep`` so operators can correlate the
    table's steady-state size with the sweep cadence.

    The middleware import is deferred into the closure body so module
    import order stays robust — the middleware module drags in
    :mod:`starlette.middleware`, :mod:`app.api.errors`, and
    :mod:`app.tenancy.middleware`, none of which the standalone
    ``python -m app.worker`` entrypoint otherwise needs.
    """

    def _body() -> None:
        from app.api.middleware.idempotency import prune_expired_idempotency_keys

        deleted = prune_expired_idempotency_keys(now=clock.now())
        _log.info(
            "idempotency sweep completed",
            extra={"event": "idempotency.sweep", "deleted": deleted},
        )

    return _body


# Pinned system-actor identifiers for worker-initiated fan-outs
# (LLM-budget refresh, occurrence-generator tick, future tenant-
# scoped sweeps). ``WorkspaceContext`` requires non-empty ULIDs on
# ``actor_id`` + ``audit_correlation_id``; the fan-out paths write
# either zero audit rows (refresh_aggregate) or workspace-anchored
# audit rows whose provenance the spec already pins by
# ``actor_kind = 'system'`` (generator's
# ``schedules.generation_tick``, ``schedules.skipped_for_closure``).
# A zero-ULID string satisfies the dataclass invariant without lying
# about provenance: any downstream seam that eventually reads these
# fields (e.g. a future worker-audit writer) sees an all-zero
# sentinel that operators can pattern-match on.
#
# Matches the convention in :func:`app.auth.signup._agnostic_audit_ctx`
# (system actor with zero-ULID ids) — kept module-private so callers
# don't accidentally construct the sentinel outside the scheduler's
# fan-out loops.
_SYSTEM_ACTOR_ZERO_ULID: Final[str] = "00000000000000000000000000"


def _system_actor_context(
    *,
    workspace_id: str,
    workspace_slug: str,
) -> WorkspaceContext:
    """Build a system-actor :class:`WorkspaceContext` for a worker fan-out.

    Shared by every worker fan-out body that needs a per-workspace
    context the tenant filter will accept (LLM-budget refresh,
    occurrence-generator tick, future tenant-scoped sweeps).
    ``actor_grant_role`` uses ``"manager"`` to mirror the established
    system-actor convention in the auth modules
    (:func:`app.auth.signup._agnostic_audit_ctx`,
    :func:`app.auth.recovery._agnostic_audit_ctx`,
    :func:`app.auth.magic_link._agnostic_audit_ctx`, and the passkey
    and session ``actor_kind="system"`` sites). The field is unused
    for ``actor_kind="system"`` rows in audit writes; picking the same
    canonical value across every system-actor context lets operators
    ``grep`` one shape when triaging a "which ctx fired this?" thread.
    """
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=_SYSTEM_ACTOR_ZERO_ULID,
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=_SYSTEM_ACTOR_ZERO_ULID,
        principal_kind="system",
    )


def _demo_expired_workspace_ids(
    session: Session,
    workspace_ids: list[str],
    *,
    now: datetime,
) -> set[str]:
    """Return the subset of ``workspace_ids`` whose demo TTL has passed.

    §24 "Demo mode" / "Garbage collection" is the spec of record:
    every ``demo_workspace`` row carries an ``expires_at``; once it
    is in the past the workspace is awaiting GC and the generator
    must skip it (running materialisation on a soon-to-be-purged
    tenant is wasted work and would race with the ``demo_gc`` sweep).

    The :class:`DemoWorkspace` table does not exist yet — cd-otv3 +
    cd-h0ja are the open follow-ups that land it. Until then this
    helper returns an empty set, the fan-out treats every workspace
    as live, and the count surfaced in the tick summary stays at
    zero. Once the model is in place the missing-module branch falls
    away and the SELECT below picks up the filter without further
    work in the fan-out.

    Resolved via :mod:`importlib` rather than a static ``from ...
    import ...`` because the demo package does not exist on disk
    today — a static import would either hard-fail at module load
    time or force ``# type: ignore`` to placate ``mypy --strict``.
    Both are worse than this seam: ``importlib.import_module`` raises
    :class:`ModuleNotFoundError` at call time (a subclass of
    :class:`ImportError`, narrowed below), no other exception class
    is swallowed, and the helper stays type-safe.
    """
    if not workspace_ids:
        return set()

    import importlib

    try:
        demo_module = importlib.import_module("app.adapters.db.demo.models")
    except ModuleNotFoundError:
        return set()

    # The model class itself stays attribute-resolved — ``getattr``
    # is the only safe form for a runtime-only import. The
    # ``DemoWorkspace`` mapper is mandatory once the package
    # exists; an :class:`AttributeError` here would be a packaging
    # bug we want to surface, not swallow.
    demo_workspace = demo_module.DemoWorkspace

    # ``demo_workspace.id`` is a 1:1 FK to ``workspace.id`` (§24
    # "Entity"), so the predicate is a simple ``id IN ...`` plus the
    # ``expires_at`` cutoff. ``demo_workspace`` is the demo tenancy
    # anchor — it carries no ``workspace_id`` of its own — so the
    # SELECT runs inside ``tenant_agnostic`` (the caller already
    # holds that bracket via the Workspace enumeration).
    stmt = (
        select(demo_workspace.id)
        .where(demo_workspace.id.in_(workspace_ids))
        .where(demo_workspace.expires_at < now)
    )
    # ``demo_workspace`` came in via :mod:`importlib`, so the column
    # type stays ``Any`` from mypy's view. Belt-and-braces filter the
    # scalars to the input set: we promise a ``set[str]`` whose every
    # element appears in ``workspace_ids``, so a future schema change
    # that returned a wrapped type (e.g. a ULID dataclass) cannot
    # silently flag a live workspace as expired through equality
    # surprise. A string that fails the membership check is dropped
    # — fail-open is the right default for a sweep skip filter.
    candidate_set = set(workspace_ids)
    return {
        candidate_id
        for candidate_id in session.scalars(stmt).all()
        if isinstance(candidate_id, str) and candidate_id in candidate_set
    }


def _make_generator_fanout_body(clock: Clock) -> Callable[[], None]:
    """Build the hourly occurrence-generator fan-out body (cd-dcl2).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — the same
    rationale the sibling :func:`_make_llm_budget_refresh_body` and
    :func:`_make_idempotency_sweep_body` cite. The generator's
    ``now`` is derived from ``clock.now()`` inside each per-workspace
    call; reusing the scheduler's clock keeps the heartbeat timestamp
    and the generation horizon aligned under :class:`FrozenClock`.

    The returned body:

    1. Opens its own UoW (one per tick) via
       :func:`app.adapters.db.session.make_uow`. Sibling bodies do
       the same (``_make_idempotency_sweep_body``,
       ``_make_llm_budget_refresh_body``). The outer UoW commits on
       clean exit and rolls back on any uncaught exception; the
       per-workspace ``begin_nested`` SAVEPOINT below scopes the
       rollback so a broken tenant does not lose successful sibling
       writes.
    2. Enumerates every :class:`~app.adapters.db.workspace.models.Workspace`
       under :func:`tenant_agnostic`; the ``workspace`` table is the
       tenancy anchor and carries no ``workspace_id`` of its own.
       Demo-expired tenants (§24 "Garbage collection") are filtered
       upfront via :func:`_demo_expired_workspace_ids` so the per-
       workspace loop only touches live tenants.
    3. For each live workspace, binds a system-actor
       :class:`WorkspaceContext` to the ``current`` ContextVar and
       calls :func:`~app.worker.tasks.generator.generate_task_occurrences`.
       Wrapped in ``try / except Exception`` + ``begin_nested`` so a
       single raising workspace logs at WARNING and the loop
       continues — the spec's "per-workspace errors must not abort
       the tick" invariant.
    4. Emits structured log events:

       * ``event="worker.generator.workspace.tick"`` (INFO) — per
         workspace, with ``workspace_id``, ``workspace_slug``,
         ``schedules_walked``, ``tasks_created``,
         ``skipped_duplicate``, ``skipped_for_closure``. The
         per-workspace payload the cd-dcl2 acceptance criteria
         pin for log-based attribution.
       * ``event="worker.generator.workspace.failed"`` (WARNING) —
         per workspace, with ``workspace_id`` + the exception class
         name. Full traceback would be noisy at hourly cadence;
         operators can re-run with DEBUG logging if the root cause
         needs deeper plumbing.
       * ``event="worker.generator.tick.summary"`` (INFO) — once per
         tick, with ``total_workspaces`` (live + demo-expired
         enumerated), ``total_workspaces_skipped`` (demo-expired,
         not walked), ``total_workspaces_failed``,
         ``total_schedules_walked`` (sum of
         :attr:`GenerationReport.schedules_walked`),
         ``total_tasks_created`` (sum of
         :attr:`GenerationReport.tasks_created`),
         ``total_skipped_duplicate`` and ``total_skipped_for_closure``
         (sums of the matching :class:`GenerationReport` fields). The
         per-component split is the reason the per-workspace event
         pins the same shape — operator dashboards plot rate of
         duplicate skips (idempotency proof) separately from closure
         skips (suppression proof).

    The import of :func:`generate_task_occurrences` is deferred into
    the closure body so module import order stays robust: the
    generator drags in :mod:`dateutil.rrule`,
    :mod:`app.adapters.db.tasks.models`, and the audit writer, none
    of which the standalone ``python -m app.worker`` entrypoint
    needs to start the heartbeat-only deployment.
    """

    def _body() -> None:
        # Deferred imports — see factory docstring rationale. Keep
        # them narrow so a worker process whose generator path fails
        # to import still boots the heartbeat + idempotency-sweep
        # ticks (the standalone worker's import surface stays lean).
        from sqlalchemy.orm import Session as _Session

        from app.adapters.db.workspace.models import Workspace
        from app.tenancy import tenant_agnostic
        from app.tenancy.current import reset_current, set_current
        from app.worker.tasks.generator import generate_task_occurrences

        now = clock.now()

        total_workspaces = 0
        total_workspaces_skipped = 0
        total_workspaces_failed = 0
        total_schedules_walked = 0
        total_tasks_created = 0
        total_skipped_duplicate = 0
        total_skipped_for_closure = 0

        with make_uow() as session:
            # Same isinstance narrowing the LLM-budget body uses —
            # ``UnitOfWorkImpl.__enter__`` returns a ``DbSession``
            # protocol; the fan-out hands the concrete ``Session``
            # to :func:`generate_task_occurrences`.
            assert isinstance(session, _Session)

            with tenant_agnostic():
                # ``workspace`` is NOT in the tenant-filter registry
                # (it is the tenancy anchor; see
                # ``app/adapters/db/workspace/__init__.py``); the
                # ``tenant_agnostic`` block is belt-and-braces in
                # case a future migration registers the table.
                rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())
                workspace_ids = [row.id for row in rows]
                expired_ids = _demo_expired_workspace_ids(
                    session, workspace_ids, now=now
                )

            for row in rows:
                workspace_id = row.id
                workspace_slug = row.slug
                total_workspaces += 1

                if workspace_id in expired_ids:
                    # §24 "Garbage collection" — workspaces past
                    # ``expires_at`` are awaiting the ``demo_gc``
                    # sweep; running materialisation on them is
                    # wasted work and would race the GC. Counted
                    # toward ``total_workspaces_skipped`` so the
                    # tick summary keeps the demo-fleet attrition
                    # observable.
                    total_workspaces_skipped += 1
                    continue

                ctx = _system_actor_context(
                    workspace_id=workspace_id,
                    workspace_slug=workspace_slug,
                )
                # Tenant filter reads the ``current`` ContextVar on
                # every scoped SELECT; the generator's ``Schedule``
                # / ``TaskTemplate`` / ``Occurrence`` reads + writes
                # would raise
                # :class:`~app.tenancy.orm_filter.TenantFilterMissing`
                # without an active context. ``try / finally``
                # guarantees the token is reset even if the body
                # raises before the SAVEPOINT catches — without it
                # the ContextVar would leak into the next iteration
                # and the next workspace's run would see a stale
                # ctx.
                token = set_current(ctx)
                try:
                    try:
                        # Per-workspace SAVEPOINT scopes the rollback
                        # of any partial occurrence inserts to this
                        # tenant — sibling workspaces' successful
                        # writes ride the outer transaction unharmed.
                        # The same pattern the LLM-budget refresh
                        # body uses.
                        with session.begin_nested():
                            report = generate_task_occurrences(
                                ctx,
                                session=session,
                                clock=clock,
                            )
                    except Exception as exc:
                        # SAVEPOINT already rolled back by the context
                        # manager; the outer transaction is still
                        # usable. Log at WARNING with the exception
                        # class name — full traceback would be noisy
                        # at hourly cadence.
                        total_workspaces_failed += 1
                        _log.warning(
                            "worker.generator.workspace.failed",
                            extra={
                                "event": "worker.generator.workspace.failed",
                                "workspace_id": workspace_id,
                                "workspace_slug": workspace_slug,
                                "error": type(exc).__name__,
                            },
                        )
                        continue
                finally:
                    reset_current(token)

                total_schedules_walked += report.schedules_walked
                total_tasks_created += report.tasks_created
                total_skipped_duplicate += report.skipped_duplicate
                total_skipped_for_closure += report.skipped_for_closure

                _log.info(
                    "worker.generator.workspace.tick",
                    extra={
                        "event": "worker.generator.workspace.tick",
                        "workspace_id": workspace_id,
                        "workspace_slug": workspace_slug,
                        "schedules_walked": report.schedules_walked,
                        "tasks_created": report.tasks_created,
                        "skipped_duplicate": report.skipped_duplicate,
                        "skipped_for_closure": report.skipped_for_closure,
                    },
                )

        _log.info(
            "worker.generator.tick.summary",
            extra={
                "event": "worker.generator.tick.summary",
                "total_workspaces": total_workspaces,
                "total_workspaces_skipped": total_workspaces_skipped,
                "total_workspaces_failed": total_workspaces_failed,
                "total_schedules_walked": total_schedules_walked,
                "total_tasks_created": total_tasks_created,
                "total_skipped_duplicate": total_skipped_duplicate,
                "total_skipped_for_closure": total_skipped_for_closure,
            },
        )

    return _body


def _make_overdue_fanout_body(clock: Clock) -> Callable[[], None]:
    """Build the 5-minute soft-overdue sweeper fan-out body (cd-hurw).

    Mirror of :func:`_make_generator_fanout_body` for the overdue
    sweeper: enumerate every live workspace, bind a system-actor
    :class:`WorkspaceContext`, run
    :func:`~app.worker.tasks.overdue.detect_overdue` per tenant inside
    a SAVEPOINT so a single broken workspace does not roll back its
    siblings' updates. Demo-expired workspaces are skipped (same §24
    rationale the generator fan-out cites).

    Structured-log emission:

    * ``event="worker.overdue.workspace.tick"`` (INFO) — per workspace,
      with ``workspace_id``, ``workspace_slug``, ``flipped_count``,
      ``skipped_already_overdue``, ``skipped_manual_transition``. The
      per-workspace payload operator dashboards key on for "which
      tenants are stacking overdue tasks?".
    * ``event="worker.overdue.workspace.failed"`` (WARNING) — per
      workspace, with ``workspace_id`` + the exception class name.
    * ``event="worker.overdue.tick.summary"`` (INFO) — once per tick,
      with ``total_workspaces``, ``total_workspaces_skipped`` (demo-
      expired), ``total_workspaces_failed``, ``total_flipped``,
      ``total_skipped_manual_transition``. Sums of the matching
      :class:`OverdueReport` fields.

    The :func:`detect_overdue` import is deferred into the closure
    body so module import order stays robust — same pattern the
    sibling generator fan-out uses.
    """

    def _body() -> None:
        from sqlalchemy.orm import Session as _Session

        from app.adapters.db.workspace.models import Workspace
        from app.tenancy import tenant_agnostic
        from app.tenancy.current import reset_current, set_current
        from app.worker.tasks.overdue import detect_overdue

        now = clock.now()

        total_workspaces = 0
        total_workspaces_skipped = 0
        total_workspaces_failed = 0
        total_flipped = 0
        total_skipped_manual_transition = 0

        with make_uow() as session:
            assert isinstance(session, _Session)

            with tenant_agnostic():
                rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())
                workspace_ids = [row.id for row in rows]
                expired_ids = _demo_expired_workspace_ids(
                    session, workspace_ids, now=now
                )

            for row in rows:
                workspace_id = row.id
                workspace_slug = row.slug
                total_workspaces += 1

                if workspace_id in expired_ids:
                    total_workspaces_skipped += 1
                    continue

                ctx = _system_actor_context(
                    workspace_id=workspace_id,
                    workspace_slug=workspace_slug,
                )
                token = set_current(ctx)
                try:
                    try:
                        with session.begin_nested():
                            report = detect_overdue(
                                ctx,
                                session=session,
                                clock=clock,
                            )
                    except Exception as exc:
                        total_workspaces_failed += 1
                        _log.warning(
                            "worker.overdue.workspace.failed",
                            extra={
                                "event": "worker.overdue.workspace.failed",
                                "workspace_id": workspace_id,
                                "workspace_slug": workspace_slug,
                                "error": type(exc).__name__,
                            },
                        )
                        continue
                finally:
                    reset_current(token)

                total_flipped += report.flipped_count
                total_skipped_manual_transition += report.skipped_manual_transition

                _log.info(
                    "worker.overdue.workspace.tick",
                    extra={
                        "event": "worker.overdue.workspace.tick",
                        "workspace_id": workspace_id,
                        "workspace_slug": workspace_slug,
                        "flipped_count": report.flipped_count,
                        "skipped_already_overdue": report.skipped_already_overdue,
                        "skipped_manual_transition": (report.skipped_manual_transition),
                    },
                )

        _log.info(
            "worker.overdue.tick.summary",
            extra={
                "event": "worker.overdue.tick.summary",
                "total_workspaces": total_workspaces,
                "total_workspaces_skipped": total_workspaces_skipped,
                "total_workspaces_failed": total_workspaces_failed,
                "total_flipped": total_flipped,
                "total_skipped_manual_transition": total_skipped_manual_transition,
            },
        )

    return _body


def _make_llm_budget_refresh_body(clock: Clock) -> Callable[[], None]:
    """Build the 60 s LLM-budget refresh body (cd-ca1k).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — the
    ``refresh_aggregate`` window bounds are derived from
    ``clock.now() - 30d``, which MUST be driven by the same clock
    the heartbeat uses. A :class:`~app.util.clock.FrozenClock` under
    test otherwise would have a deterministic heartbeat timestamp
    and a non-deterministic refresh window; trivially cheap closure,
    no reason to tolerate the mismatch.

    The returned body:

    1. Opens its own UoW (one per tick) via
       :func:`app.adapters.db.session.make_uow`. ``wrap_job`` does
       not hand in a session — every sibling body opens its own
       (``_make_idempotency_sweep_body`` does the same). This keeps
       the per-tick transaction boundary explicit: a broken workspace
       does not poison the session for its siblings because the
       outer UoW rolls back only on an un-caught exception (this
       body catches per-workspace).
    2. Queries every :class:`~app.adapters.db.workspace.models.Workspace`
       row. No ``archived_at`` column exists yet (tracked as a
       Beads follow-up); for now "active" == "row exists".
    3. For each workspace, constructs a system-actor
       :class:`WorkspaceContext` and calls
       :func:`~app.domain.llm.budget.refresh_aggregate`. Wrapped in
       ``try / except Exception`` so a single broken workspace
       doesn't starve the fan-out.
    4. Emits structured log events the operator dashboard keys on:

       * ``event="llm.budget.refresh.no_ledger"`` (DEBUG) — per
         workspace, when :func:`refresh_aggregate` returns 0 and no
         ledger row exists. Logged at DEBUG because the workspace-
         create handler seeds the ledger (cd-tubi); the DEBUG line
         lets an operator trace the skip without alerting.
       * ``event="llm.budget.refresh.workspace_failed"`` (WARNING) —
         per workspace, with the exception class name. The exception
         is swallowed here; the outer tick continues.
       * ``event="llm.budget.refresh.tick"`` (INFO) — once per tick,
         with ``workspaces`` (count attempted), ``failures`` (count
         raising), and ``total_cents`` (sum of freshly-computed
         aggregates across every workspace that returned a value).
         Health metric for operator dashboards — NOT a cap check.

    The import of :func:`refresh_aggregate` is deferred into the
    closure body so module import order stays robust: the budget
    module drags in :mod:`app.adapters.db.llm.models`, which is
    unnecessary for the standalone ``python -m app.worker``
    entrypoint that only needs the scheduler seam.
    """

    def _body() -> None:
        # Deferred imports — see factory docstring rationale. Keep
        # them narrow so a worker process that fails to start the
        # budget job still boots (the heartbeat + idempotency sweep
        # ride a separate import path). ``make_uow`` is already
        # imported at module scope (the heartbeat writer uses it);
        # the budget / workspace / tenancy imports are the ones we
        # defer to keep the standalone worker's import surface lean.
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from app.adapters.db.llm.models import BudgetLedger
        from app.adapters.db.workspace.models import Workspace
        from app.domain.llm.budget import refresh_aggregate
        from app.tenancy import tenant_agnostic
        from app.tenancy.current import reset_current, set_current

        workspaces_attempted = 0
        failures = 0
        # ``total_cents`` is the sum of freshly-computed aggregates
        # across every workspace the tick actually refreshed. Python
        # ``int`` is arbitrary-precision so overflow is impossible;
        # downstream log serializers see a plain integer.
        total_cents = 0

        with make_uow() as session:
            # ``UnitOfWorkImpl.__enter__`` returns the concrete
            # :class:`~sqlalchemy.orm.Session` under a :class:`DbSession`
            # protocol annotation; :func:`refresh_aggregate` wants the
            # concrete class. Narrow with an ``isinstance`` assertion
            # — same pattern as
            # :func:`app.api.middleware.idempotency.prune_expired_idempotency_keys`.
            assert isinstance(session, Session)

            # ``workspace`` is NOT in the tenant-filter registry
            # (it is the tenancy anchor; see
            # ``app/adapters/db/workspace/__init__.py``) — no
            # ``workspace_id`` predicate is injected, so a plain
            # SELECT returns every tenant row. Wrap in
            # ``tenant_agnostic()`` as belt-and-braces in case a
            # future migration registers the table.
            # justification: scheduler fan-out must enumerate every
            # tenant's workspace row before binding a per-workspace
            # ctx; the ``workspace`` table is the tenancy anchor and
            # carries no ``workspace_id`` column of its own.
            with tenant_agnostic():
                rows = list(session.execute(select(Workspace.id, Workspace.slug)).all())

            for row in rows:
                workspace_id = row.id
                workspace_slug = row.slug
                workspaces_attempted += 1
                ctx = _system_actor_context(
                    workspace_id=workspace_id,
                    workspace_slug=workspace_slug,
                )
                # Tenant filter reads the ``current`` ContextVar on
                # every scoped SELECT; :func:`refresh_aggregate`'s
                # ``_sum_usage_cents`` hits the workspace-scoped
                # ``llm_usage`` table and would raise
                # :class:`~app.tenancy.orm_filter.TenantFilterMissing`
                # without an active context. Scope the bind to the
                # single ``refresh_aggregate`` call so a later fan-out
                # step (or the tick's INFO log emit) never runs with
                # a lingering per-workspace ctx. ``try / finally``
                # guarantees the token is reset even if the body
                # below raises before the SAVEPOINT catches — without
                # it the ContextVar would leak into the next iteration
                # and the next workspace's refresh would see a stale
                # ctx.
                token = set_current(ctx)
                try:
                    # Per-workspace SAVEPOINT wraps BOTH the ledger
                    # pre-check AND the refresh call. Two reasons:
                    #
                    # 1. A crashing workspace must not roll back a
                    #    sibling's UPDATE. The outer UoW owns the
                    #    top-level transaction (commit on clean exit,
                    #    rollback on exception); the nested
                    #    ``session.begin_nested()`` lets us roll back
                    #    only this workspace's work on failure while
                    #    keeping the preceding workspaces' updates
                    #    live. Without this scope, a single poisoned
                    #    refresh would take down the entire fan-out's
                    #    progress at commit time.
                    # 2. If the pre-check SELECT itself raises (bad
                    #    connection, malformed row, tenant-filter
                    #    misconfiguration), we MUST treat it as a
                    #    per-workspace failure — not an unhandled
                    #    exception that skips the tick-summary INFO
                    #    emit and leaves the ``/readyz`` heartbeat
                    #    silently disconnected from actual progress.
                    try:
                        with session.begin_nested():
                            # Pre-check the ledger row existence BEFORE
                            # calling :func:`refresh_aggregate`. The
                            # domain function returns ``0`` for two
                            # distinct shapes:
                            #   (a) no ledger row — the seeding bug
                            #       cd-tubi tracks; the workspace-
                            #       create handler has not yet run
                            #       (or has a bug).
                            #   (b) a ledger row whose in-window usage
                            #       sums to zero — a perfectly healthy
                            #       zero-spend workspace.
                            # Conflating the two at the DEBUG log level
                            # makes ``event=llm.budget.refresh.no_ledger``
                            # useless for the seeding-bug dashboard
                            # cd-tubi is meant to drive — a fleet with
                            # ten healthy zero-spend tenants would page
                            # the same signal as a single broken seed
                            # path. Pre-checking disambiguates the two
                            # signals and skips the domain call (and
                            # its redundant
                            # ``llm.budget.ledger_missing_on_refresh``
                            # WARNING) entirely on path (a).
                            #
                            # The ledger probe is itself a workspace-
                            # scoped SELECT — ``budget_ledger`` is in
                            # the tenancy registry — so it runs INSIDE
                            # the ``set_current`` / ``reset_current``
                            # bracket, not before.
                            ledger_exists = (
                                session.scalar(
                                    select(BudgetLedger.id)
                                    .where(BudgetLedger.workspace_id == workspace_id)
                                    .limit(1)
                                )
                                is not None
                            )
                            if not ledger_exists:
                                result = None
                            else:
                                result = refresh_aggregate(session, ctx, clock=clock)
                    except Exception as exc:
                        # SAVEPOINT already rolled back by the context
                        # manager; the outer transaction is still
                        # usable. Log at WARNING with the exception
                        # class name (full traceback would be noisy
                        # at 60 s cadence; operators can ``grep`` for
                        # the event and re-run with DEBUG logging if
                        # the root cause needs deeper plumbing).
                        failures += 1
                        _log.warning(
                            "llm.budget.refresh.workspace_failed",
                            extra={
                                "event": "llm.budget.refresh.workspace_failed",
                                "workspace_id": workspace_id,
                                "error": type(exc).__name__,
                            },
                        )
                        continue
                finally:
                    reset_current(token)

                if result is None:
                    # Pre-check saw a missing ledger — the
                    # seeding-bug signal (cd-tubi). DEBUG so the
                    # fan-out trace stays complete without paging
                    # on a fleet of healthy tenants.
                    _log.debug(
                        "llm.budget.refresh.no_ledger",
                        extra={
                            "event": "llm.budget.refresh.no_ledger",
                            "workspace_id": workspace_id,
                        },
                    )
                    continue

                total_cents += result

        _log.info(
            "llm.budget.refresh.tick",
            extra={
                "event": "llm.budget.refresh.tick",
                "workspaces": workspaces_attempted,
                "failures": failures,
                "total_cents": total_cents,
            },
        )

    return _body


def _make_user_workspace_refresh_body(clock: Clock) -> Callable[[], None]:
    """Build the user_workspace derive-refresh body (cd-yqm4).

    Factory rather than a bare module-level function so the body
    closes over the scheduler's injected :class:`Clock` — same
    rationale the sibling :func:`_make_llm_budget_refresh_body` and
    :func:`_make_idempotency_sweep_body` cite. The reconciler stamps
    new ``user_workspace.added_at`` rows with ``clock.now()``; under
    a :class:`~app.util.clock.FrozenClock` the heartbeat timestamp
    and the freshly-inserted ``added_at`` MUST line up so test
    fixtures can assert on the exact pair.

    The returned body:

    1. Opens its own UoW (one per tick) via
       :func:`app.adapters.db.session.make_uow`. Sibling bodies do
       the same (idempotency sweep, LLM-budget refresh, generator
       fan-out). The UoW commits on clean exit.
    2. Calls :func:`reconcile_user_workspace`, which runs under
       :func:`tenant_agnostic` internally — the reconciler operates
       across every workspace in a single pass, not per-workspace,
       so there is no fan-out loop here.
    3. Emits one structured-log event per tick:
       ``event="worker.identity.user_workspace.tick.summary"`` (INFO),
       carrying ``rows_inserted`` / ``rows_deleted`` /
       ``rows_source_flipped`` / ``upstream_pairs_seen``. Operator
       dashboards plot insert + delete rates separately so a sudden
       spike in deletes (mass revoke) is distinguishable from a
       backfill.

    Per-tenant SAVEPOINT isolation is unnecessary here because the
    reconciler is a single SQL pass — there is no per-workspace work
    that could fail in isolation. A SQL error fails the whole tick
    (the outer UoW rolls back), the next tick retries, and the
    heartbeat staleness window catches a permanently-stuck
    reconcile.

    The :func:`reconcile_user_workspace` import is deferred into the
    closure body so module import order stays robust: the
    reconciler drags in :mod:`app.adapters.db.places.models` and
    :mod:`app.adapters.db.workspace.models`, neither of which the
    standalone ``python -m app.worker`` entrypoint otherwise needs
    to start the heartbeat-only deployment.
    """

    def _body() -> None:
        # Deferred imports — see factory docstring rationale.
        from sqlalchemy.orm import Session as _Session

        from app.domain.identity.user_workspace_refresh import (
            reconcile_user_workspace,
        )

        now = clock.now()

        with make_uow() as session:
            # ``UnitOfWorkImpl.__enter__`` returns a ``DbSession``
            # protocol; the reconciler wants the concrete ``Session``.
            # Same isinstance narrowing the LLM-budget and generator
            # fan-out bodies use.
            assert isinstance(session, _Session)
            report = reconcile_user_workspace(session, now=now)

        _log.info(
            "worker.identity.user_workspace.tick.summary",
            extra={
                "event": "worker.identity.user_workspace.tick.summary",
                "rows_inserted": report.rows_inserted,
                "rows_deleted": report.rows_deleted,
                "rows_source_flipped": report.rows_source_flipped,
                "upstream_pairs_seen": report.upstream_pairs_seen,
            },
        )

    return _body


def start(scheduler: AsyncIOScheduler) -> None:
    """Start ``scheduler`` if it isn't already running.

    Idempotent: calling :func:`start` twice — or on a scheduler that
    another lifespan hook already started — is a no-op. APScheduler
    itself raises :class:`SchedulerAlreadyRunningError` on a double
    start, which would otherwise turn a benign supervisor restart
    into a boot-blocking exception.
    """
    if scheduler.running:
        _log.debug(
            "scheduler already running; start is a no-op",
            extra={"event": "worker.scheduler.start_noop"},
        )
        return
    scheduler.start()
    _log.info(
        "scheduler started",
        extra={"event": "worker.scheduler.started"},
    )


def stop(scheduler: AsyncIOScheduler, *, wait: bool = False) -> None:
    """Stop ``scheduler`` if it's running; no-op otherwise.

    ``wait`` is forwarded to :meth:`AsyncIOScheduler.shutdown` — the
    default ``False`` returns immediately without draining pending
    runs, which is what a SIGTERM handler wants (a supervised
    process restart must not hang on a slow job body). The lifespan
    hook in the FastAPI factory can override with ``wait=True`` if
    graceful shutdown is preferred and the ASGI shutdown deadline
    is generous enough.
    """
    if not scheduler.running:
        _log.debug(
            "scheduler not running; stop is a no-op",
            extra={"event": "worker.scheduler.stop_noop"},
        )
        return
    scheduler.shutdown(wait=wait)
    _log.info(
        "scheduler stopped",
        extra={"event": "worker.scheduler.stopped", "waited": wait},
    )


# ---------------------------------------------------------------------------
# Diagnostic helpers — used by tests and the ``__main__`` entrypoint
# ---------------------------------------------------------------------------


def registered_job_ids(scheduler: AsyncIOScheduler) -> tuple[str, ...]:
    """Return the sorted tuple of job ids currently registered.

    Exists so the unit tests can assert shape without reaching into
    APScheduler internals. :meth:`AsyncIOScheduler.get_jobs` is the
    documented API; we wrap it so the return is a deterministic
    tuple (the underlying list is insertion-ordered, which is fine,
    but sorting makes the test assertion trivially stable under a
    future reordering of the register_jobs body).
    """
    jobs: list[Any] = scheduler.get_jobs()
    return tuple(sorted(job.id for job in jobs))
