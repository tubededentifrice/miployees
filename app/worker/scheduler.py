"""APScheduler bootstrap + ``register_jobs`` hook.

The single seam through which downstream tasks (cd-j9l7 idempotency
sweep, cd-yqm4 user_workspace derive-refresh, the future occurrence
generator fan-out tick) plug into the shared scheduler. Two
entry-points exercise the same ``register_jobs`` call:

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
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.adapters.db.session import make_uow
from app.util.clock import Clock, SystemClock
from app.worker.heartbeat import upsert_heartbeat

__all__ = [
    "GENERATOR_JOB_ID",
    "HEARTBEAT_JOB_ID",
    "HEARTBEAT_JOB_INTERVAL_SECONDS",
    "IDEMPOTENCY_SWEEP_JOB_ID",
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

# Stable job id for the hourly generator tick. Real fan-out across
# workspaces lands with cd-p5's follow-up (the generator itself is a
# single-workspace callable); the registration here seats the hook in
# the scheduler so the cron cadence is observable immediately.
GENERATOR_JOB_ID: str = "generate_task_occurrences"

# Stable job id for the daily ``idempotency_key`` TTL sweep (cd-j9l7).
# Spec §12 "Idempotency" pins the TTL at 24 h; the sweep callable
# (:func:`app.api.middleware.idempotency.prune_expired_idempotency_keys`)
# removes rows older than that so the table never grows unbounded.
IDEMPOTENCY_SWEEP_JOB_ID: str = "idempotency_sweep"


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

    is_coroutine = inspect.iscoroutinefunction(func)

    async def _runner() -> None:
        _log.info(
            "worker tick starting",
            extra={"event": "worker.tick.start", "job_id": job_id},
        )
        ok = False
        try:
            if is_coroutine:
                # ``func`` is ``async def`` — invoke and await on the
                # event loop. ``asyncio.to_thread`` would call the
                # coroutine function, hand back an un-awaited coroutine
                # object, and silently skip the body.
                result = func()
                if inspect.isawaitable(result):
                    await result
            else:
                # Sync body — offload to the default executor so a
                # blocking DB op does not pin the event loop.
                await asyncio.to_thread(func)
            ok = True
        except Exception:
            # The job body's own logging (if any) fires first; this
            # backstop guarantees a record even if the body swallowed.
            _log.exception(
                "worker tick failed",
                extra={"event": "worker.tick.error", "job_id": job_id},
            )

        if ok and heartbeat:
            try:
                await asyncio.to_thread(_write_heartbeat, job_id, clock)
            except Exception:
                # A heartbeat write failure is itself a signal — log
                # and move on. The next successful tick will try
                # again; if every tick fails the heartbeat goes
                # stale and ``/readyz`` catches it.
                _log.exception(
                    "worker heartbeat write failed",
                    extra={
                        "event": "worker.heartbeat.error",
                        "job_id": job_id,
                    },
                )
                ok = False

        _log.info(
            "worker tick finished",
            extra={
                "event": "worker.tick.end",
                "job_id": job_id,
                "ok": ok,
            },
        )

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
    for pending_id in (HEARTBEAT_JOB_ID, GENERATOR_JOB_ID, IDEMPOTENCY_SWEEP_JOB_ID):
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

    # --- Hourly occurrence generator fan-out ---
    # The generator itself is a single-workspace callable (see
    # ``app/worker/tasks/generator.py``). The cross-workspace fan-out
    # lands with the downstream follow-up (tracked as a Beads task
    # by this change). We register the hook now so the cadence is
    # observable and the fan-out task has a seam to plug into —
    # today the body is a no-op that just bumps the heartbeat.
    scheduler.add_job(
        wrap_job(
            _generator_fanout_placeholder,
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


def _heartbeat_only_body() -> None:
    """No-op job body — the heartbeat upsert runs after it returns.

    Exists as a distinct function (rather than ``lambda: None``) so
    the scheduler's log output shows the module-qualified name in
    stack traces if something upstream instruments the call.
    """


def _generator_fanout_placeholder() -> None:
    """Placeholder for the per-workspace fan-out of the generator tick.

    The real body iterates every active workspace, builds a
    :class:`~app.tenancy.WorkspaceContext` per workspace (with a
    system-actor identity), opens a fresh UoW, and calls
    :func:`app.worker.tasks.generator.generate_task_occurrences`.
    Until the fan-out seam lands, this placeholder logs once per
    tick so operators can see the job cadence in logs. The heartbeat
    still advances, so the job-level readiness signal is honest.
    """
    _log.info(
        "generator fan-out placeholder — real fan-out lands with follow-up",
        extra={"event": "worker.generator.placeholder"},
    )


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
