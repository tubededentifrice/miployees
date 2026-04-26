"""Unit tests for :mod:`app.worker.scheduler`.

Covers the public seam without a running event loop:

* :func:`create_scheduler` returns an :class:`AsyncIOScheduler`
  seeded with the injected clock.
* :func:`register_jobs` wires the expected job ids.
* :func:`start` / :func:`stop` are idempotent.
* :func:`wrap_job` runs the body, logs start/end, swallows
  exceptions, and upserts the heartbeat on success.

The heartbeat-path assertions use a monkey-patched
:func:`app.worker.scheduler._write_heartbeat` so the tests don't
need a DB — the heartbeat module itself has its own unit suite.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.util.clock import FrozenClock
from app.worker import scheduler as scheduler_mod
from app.worker.scheduler import (
    GENERATOR_JOB_ID,
    HEARTBEAT_JOB_ID,
    IDEMPOTENCY_SWEEP_JOB_ID,
    LLM_BUDGET_REFRESH_INTERVAL_SECONDS,
    LLM_BUDGET_REFRESH_JOB_ID,
    OVERDUE_DETECT_INTERVAL_SECONDS,
    OVERDUE_DETECT_JOB_ID,
    USER_WORKSPACE_REFRESH_INTERVAL_SECONDS,
    USER_WORKSPACE_REFRESH_JOB_ID,
    create_scheduler,
    register_jobs,
    registered_job_ids,
    start,
    stop,
    wrap_job,
)

# ---------------------------------------------------------------------------
# create_scheduler / register_jobs
# ---------------------------------------------------------------------------


class TestCreateScheduler:
    def test_returns_asyncio_scheduler_not_started(self) -> None:
        """Fresh scheduler is an AsyncIO one and is NOT running."""
        sched = create_scheduler()
        assert isinstance(sched, AsyncIOScheduler)
        assert sched.running is False

    def test_clock_stashed_for_wrap_job(self) -> None:
        """The injected clock is reachable via the private attribute.

        Kept as a white-box assertion because ``wrap_job`` depends
        on it — a refactor that moves the storage seam needs to
        update both sides in lockstep.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        sched = create_scheduler(clock=clock)
        assert sched._crewday_clock is clock


class TestRegisterJobs:
    def test_registers_expected_ids(self) -> None:
        """Standard job set: heartbeat + generator + idempotency-sweep
        + llm-budget + user_workspace-refresh + overdue-detect.
        """
        sched = create_scheduler()
        register_jobs(sched)
        # Compare as a set so inserting a future job doesn't break
        # this assertion just by landing a new alphabetical neighbour.
        # The ``registered_job_ids`` helper already returns a sorted
        # tuple, so duplicates would still surface via the companion
        # ``test_is_idempotent_under_replace_existing`` path.
        assert set(registered_job_ids(sched)) == {
            GENERATOR_JOB_ID,
            IDEMPOTENCY_SWEEP_JOB_ID,
            HEARTBEAT_JOB_ID,
            LLM_BUDGET_REFRESH_JOB_ID,
            OVERDUE_DETECT_JOB_ID,
            USER_WORKSPACE_REFRESH_JOB_ID,
        }

    def test_is_idempotent_under_replace_existing(self) -> None:
        """Re-registering on the same scheduler does not raise.

        Covers the not-yet-started path: on a STOPPED scheduler,
        APScheduler's ``replace_existing=True`` does NOT dedupe — it
        appends to ``_pending_jobs`` unchecked, and the duplicate only
        trips ``ConflictingIdError`` (suppressed by ``replace_existing``)
        at :meth:`start` time. :func:`register_jobs` therefore calls
        :meth:`remove_job` first; this test proves the call works by
        asserting both ``registered_job_ids`` (which scans ``get_jobs``
        and would surface the duplicate as ``('generator', 'generator',
        'heartbeat', 'heartbeat')``) and the raw ``get_jobs`` length
        after two rounds.
        """
        sched = create_scheduler()
        register_jobs(sched)
        register_jobs(sched)  # must not raise
        # Job count unchanged — ``replace_existing=True`` is not
        # enough on its own; the explicit ``remove_job`` keeps the
        # pending list to exactly one entry per id. Set equality here
        # mirrors ``test_registers_expected_ids``; the raw
        # ``len(sched.get_jobs())`` below is what actually catches a
        # duplicate (a stale entry would make the list longer than
        # the set).
        ids = registered_job_ids(sched)
        assert set(ids) == {
            GENERATOR_JOB_ID,
            IDEMPOTENCY_SWEEP_JOB_ID,
            HEARTBEAT_JOB_ID,
            LLM_BUDGET_REFRESH_JOB_ID,
            OVERDUE_DETECT_JOB_ID,
            USER_WORKSPACE_REFRESH_JOB_ID,
        }
        assert len(ids) == 6
        assert len(sched.get_jobs()) == 6


# ---------------------------------------------------------------------------
# Overdue sweeper job (cd-hurw)
# ---------------------------------------------------------------------------


class TestOverdueDetectJob:
    """Registration shape for the 5-minute overdue-sweeper job.

    The body's per-workspace fan-out (skip demo-expired tenants,
    isolate broken workspaces, sum flipped counts) is covered
    end-to-end in ``tests/integration/test_tasks_overdue_tick.py``
    against a real engine — the unit layer pins the registration
    metadata so a future refactor cannot silently change the
    operator-visible cadence.
    """

    def test_adds_overdue_detect_job_at_5min_interval(self) -> None:
        """Job is registered with the pinned interval + coalesce knobs."""
        from apscheduler.triggers.interval import IntervalTrigger

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(OVERDUE_DETECT_JOB_ID)
        assert job is not None, (
            f"{OVERDUE_DETECT_JOB_ID} not registered by register_jobs"
        )

        # IntervalTrigger at 300 s (5 min).
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval.total_seconds() == 300.0
        assert OVERDUE_DETECT_INTERVAL_SECONDS == 300

        # Wrapper knobs: misfire grace = interval (one-tick-late is
        # idempotent; two-ticks-late is a stuck-scheduler signal),
        # single instance, coalesce on.
        assert job.misfire_grace_time == OVERDUE_DETECT_INTERVAL_SECONDS
        assert job.coalesce is True
        assert job.max_instances == 1


# ---------------------------------------------------------------------------
# LLM budget refresh job (cd-ca1k)
# ---------------------------------------------------------------------------


class TestLlmBudgetRefreshJob:
    """Registration shape + clock propagation for the 60 s refresh job.

    The body's fan-out behaviour (skip missing ledger, isolate broken
    workspaces, sum total_cents) is covered end-to-end in
    ``tests/integration/test_worker_llm_budget.py`` against a real
    engine — the unit layer only asserts what can be proven without a
    DB: the registration metadata, idempotent re-registration, and
    that the injected clock reaches the closure.
    """

    def test_adds_llm_budget_refresh_job_at_60s_interval(self) -> None:
        """Job is registered with the pinned interval + coalesce settings.

        Ties the concrete APScheduler trigger shape to the spec's
        60 s cadence. ``coalesce=True`` + ``max_instances=1`` + a 90 s
        ``misfire_grace_time`` are the three knobs the task description
        enumerates; asserting the exact values here pins them so a
        future registration refactor has to update this test in
        lockstep (and surface the operator-visible cadence change).
        """
        from apscheduler.triggers.interval import IntervalTrigger

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(LLM_BUDGET_REFRESH_JOB_ID)
        assert job is not None, (
            f"{LLM_BUDGET_REFRESH_JOB_ID} not registered by register_jobs"
        )

        # Trigger: IntervalTrigger at 60 s.
        assert isinstance(job.trigger, IntervalTrigger)
        # APScheduler stores the interval as a :class:`datetime.timedelta`;
        # compare the total seconds to stay readable.
        assert job.trigger.interval.total_seconds() == 60.0
        assert LLM_BUDGET_REFRESH_INTERVAL_SECONDS == 60

        # Wrapper knobs: misfire grace 90 s, coalesce on, single
        # instance. A late restart up to 90 s catches up; beyond that
        # the next tick picks up and the skipped window is recovered
        # by the next refresh (the function is idempotent — it
        # rewrites the same sum).
        assert job.misfire_grace_time == 90
        assert job.coalesce is True
        assert job.max_instances == 1

    def test_is_idempotent(self) -> None:
        """Re-registering keeps exactly one budget-refresh job.

        Same invariant as :class:`TestRegisterJobs.
        test_is_idempotent_under_replace_existing` but pinned on
        the new job id — a regression that missed the new id in the
        ``remove_job`` loop would leave duplicate pending entries
        here without the suite-wide set assertion catching it. The
        head-level count already catches duplicates; this narrower
        test makes the regression signature obvious.
        """
        sched = create_scheduler()
        register_jobs(sched)
        register_jobs(sched)

        matching = [j for j in sched.get_jobs() if j.id == LLM_BUDGET_REFRESH_JOB_ID]
        assert len(matching) == 1

        # Trigger shape survives the re-register.
        from apscheduler.triggers.interval import IntervalTrigger

        assert isinstance(matching[0].trigger, IntervalTrigger)
        assert matching[0].trigger.interval.total_seconds() == 60.0

    def test_uses_resolved_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Injected :class:`FrozenClock` propagates into the refresh body.

        The factory closes over the scheduler's clock at registration
        time, not at tick time (matches the idempotency-sweep pattern).
        Without this assertion a future refactor that reached for
        :class:`~app.util.clock.SystemClock` inside the body would
        silently trip every FrozenClock-driven test by falling back to
        the OS clock — a hazard that cost the generator-fan-out work
        an iteration. We prove propagation by patching
        :func:`app.domain.llm.budget.refresh_aggregate` and observing
        the clock kwarg the body hands in.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))

        seen_clocks: list[object] = []

        def fake_refresh_aggregate(
            session: object,
            ctx: object,
            *,
            clock: object | None = None,
        ) -> int:
            seen_clocks.append(clock)
            return 0

        monkeypatch.setattr(
            "app.domain.llm.budget.refresh_aggregate",
            fake_refresh_aggregate,
        )

        # Fabricate a single workspace row so the body's SELECT
        # returns one tenant to dispatch into the patched
        # ``refresh_aggregate``. A direct monkeypatch on the execute
        # path keeps the test DB-free — the scheduler body only
        # reaches SQLAlchemy through ``session.execute(select(...))``.
        class _FakeRow:
            def __init__(self, id: str, slug: str) -> None:
                self.id = id
                self.slug = slug

        fake_rows = [_FakeRow("01HWA00000000000000000WSP1", "ws-one")]

        class _FakeResult:
            def all(self) -> list[_FakeRow]:
                return list(fake_rows)

        class _FakeNestedTx:
            """Trivial context manager standing in for ``session.begin_nested()``.

            The real call opens a SAVEPOINT; here the body just needs
            a context manager that enters / exits cleanly so the
            ``with session.begin_nested(): ...`` block around the
            per-workspace refresh runs. We don't need rollback
            semantics because the happy-path test never raises.
            """

            def __enter__(self) -> _FakeNestedTx:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        class _FakeSession:
            """Minimal stand-in for ``sqlalchemy.orm.Session`` — only the
            methods the body touches are implemented. The ``isinstance``
            guard on ``Session`` in the body is patched below so the
            fake session survives the runtime type check.
            """

            def execute(self, _stmt: object) -> _FakeResult:
                return _FakeResult()

            def scalar(self, _stmt: object) -> str:
                """Stand-in for ``session.scalar(select(BudgetLedger.id)...)``.

                The body pre-checks ledger presence before calling
                :func:`refresh_aggregate` — a truthy return (any
                non-``None`` value) tells the body the ledger exists
                and the refresh path should fire. Returning a fixed
                ULID-shaped sentinel keeps the test DB-free while
                guiding the body past the ``no_ledger`` early-skip.
                """
                return "01HWA00000000000000000LGR0"

            def begin_nested(self) -> _FakeNestedTx:
                return _FakeNestedTx()

        class _FakeUow:
            """Context-manager shim imitating :class:`UnitOfWorkImpl`."""

            def __enter__(self) -> _FakeSession:
                return _FakeSession()

            def __exit__(self, *exc: object) -> None:
                return None

        # Patch the seams the body pulls from:
        #   * ``make_uow`` — hand back the fake UoW.
        #   * ``Session`` — isinstance check flips to ``_FakeSession``.
        monkeypatch.setattr(scheduler_mod, "make_uow", lambda: _FakeUow())
        import sqlalchemy.orm as _orm_mod

        monkeypatch.setattr(_orm_mod, "Session", _FakeSession)

        body = scheduler_mod._make_llm_budget_refresh_body(clock)
        body()

        # The body dispatched one ``refresh_aggregate`` call and
        # handed the patched clock through.
        assert len(seen_clocks) == 1
        assert seen_clocks[0] is clock


# ---------------------------------------------------------------------------
# user_workspace derive-refresh job (cd-yqm4)
# ---------------------------------------------------------------------------


class TestUserWorkspaceRefreshJob:
    """Registration shape + clock propagation for the cd-yqm4 derive-refresh job.

    The body's reconciliation behaviour (insert / delete / source-flip)
    is covered end-to-end against a real engine in the integration
    suite under ``tests/integration/identity/test_user_workspace_refresh.py``;
    the unit layer only asserts what can be proven without a DB:
    registration metadata, idempotent re-registration, and that the
    injected clock reaches the closure.
    """

    def test_adds_job_at_5min_interval(self) -> None:
        """Registered with the pinned interval + coalesce settings."""
        from apscheduler.triggers.interval import IntervalTrigger

        sched = create_scheduler()
        register_jobs(sched)

        job = sched.get_job(USER_WORKSPACE_REFRESH_JOB_ID)
        assert job is not None, (
            f"{USER_WORKSPACE_REFRESH_JOB_ID} not registered by register_jobs"
        )

        # Trigger: IntervalTrigger at the pinned cadence.
        assert isinstance(job.trigger, IntervalTrigger)
        assert (
            job.trigger.interval.total_seconds()
            == USER_WORKSPACE_REFRESH_INTERVAL_SECONDS
        )

        # Wrapper knobs: misfire grace == one full interval, coalesce
        # on, single instance. One tick late is tolerated (idempotent
        # reconcile); two ticks late skip rather than stack.
        assert job.misfire_grace_time == USER_WORKSPACE_REFRESH_INTERVAL_SECONDS
        assert job.coalesce is True
        assert job.max_instances == 1

    def test_is_idempotent(self) -> None:
        """Re-registering keeps exactly one user_workspace_refresh job."""
        sched = create_scheduler()
        register_jobs(sched)
        register_jobs(sched)

        matching = [
            j for j in sched.get_jobs() if j.id == USER_WORKSPACE_REFRESH_JOB_ID
        ]
        assert len(matching) == 1

    def test_uses_resolved_clock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Injected :class:`FrozenClock` propagates into the refresh body.

        The factory closes over the scheduler's clock at registration
        time (matches the LLM-budget-refresh / idempotency-sweep
        pattern). A regression that reached for
        :class:`~app.util.clock.SystemClock` inside the body would
        silently trip every FrozenClock-driven test by falling back to
        the OS clock; we prove propagation by patching
        :func:`reconcile_user_workspace` and observing the ``now`` arg.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))

        seen_now: list[object] = []

        def fake_reconcile(session: object, *, now: object) -> object:
            seen_now.append(now)

            class _Report:
                rows_inserted = 0
                rows_deleted = 0
                rows_source_flipped = 0
                upstream_pairs_seen = 0

            return _Report()

        # Patch the deferred import target — the body imports
        # ``reconcile_user_workspace`` from the domain module, so we
        # patch it there.
        monkeypatch.setattr(
            "app.domain.identity.user_workspace_refresh.reconcile_user_workspace",
            fake_reconcile,
        )

        # Fake UoW + Session so the body never reaches a real DB.
        class _FakeSession:
            pass

        class _FakeUow:
            def __enter__(self) -> _FakeSession:
                return _FakeSession()

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(scheduler_mod, "make_uow", lambda: _FakeUow())
        import sqlalchemy.orm as _orm_mod

        # Flip ``Session`` to ``_FakeSession`` so the body's
        # ``isinstance(session, Session)`` narrowing accepts the fake.
        monkeypatch.setattr(_orm_mod, "Session", _FakeSession)

        body = scheduler_mod._make_user_workspace_refresh_body(clock)
        body()

        # The body dispatched one ``reconcile_user_workspace`` call
        # and handed the patched clock's ``now()`` through.
        assert seen_now == [clock.now()]


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_start_is_idempotent(self) -> None:
        """Calling :func:`start` on a running scheduler is a no-op.

        Drives the coroutine via :func:`asyncio.run` so the AsyncIO
        scheduler's internal loop reference resolves correctly.
        """

        async def _run() -> None:
            sched = create_scheduler()
            start(sched)
            assert sched.running
            # Second start must not raise SchedulerAlreadyRunningError.
            start(sched)
            assert sched.running
            stop(sched)

        asyncio.run(_run())

    def test_stop_is_idempotent(self) -> None:
        """Calling :func:`stop` on a stopped scheduler is a no-op."""
        sched = create_scheduler()
        # Never started — stop must not raise SchedulerNotRunningError.
        stop(sched)
        assert sched.running is False


# ---------------------------------------------------------------------------
# wrap_job
# ---------------------------------------------------------------------------


class TestWrapJob:
    """Cover the three wrapper responsibilities: run, log, heartbeat."""

    def test_runs_body_and_writes_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Successful body → heartbeat upsert keyed by ``job_id``."""
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        seen_calls: list[tuple[str, datetime]] = []

        def fake_write(job_id: str, injected_clock: object) -> None:
            assert injected_clock is clock
            seen_calls.append((job_id, clock.now()))

        monkeypatch.setattr(scheduler_mod, "_write_heartbeat", fake_write)

        body = MagicMock()
        wrapped = wrap_job(body, job_id="test_job", clock=clock)
        asyncio.run(wrapped())

        body.assert_called_once_with()
        assert seen_calls == [("test_job", clock.now())]

    def test_body_exception_is_swallowed_and_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """A raising body logs at ERROR and the tick completes without raising.

        The heartbeat must NOT advance on a failed run — the whole
        point of the staleness window is that a broken job stops
        bumping the row.
        """
        # Alembic's fileConfig can flip ``propagate=False`` on named
        # loggers across the test session. Enable propagation so
        # ``caplog`` sees the records.
        allow_propagated_log_capture("app.worker.scheduler")  # type: ignore[operator]

        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        write_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: write_calls.append(job_id),
        )

        def body() -> None:
            raise RuntimeError("boom")

        wrapped = wrap_job(body, job_id="flaky", clock=clock)
        with caplog.at_level(logging.ERROR, logger="app.worker.scheduler"):
            asyncio.run(wrapped())  # must not raise

        assert write_calls == []
        error_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.tick.error"
        ]
        assert len(error_events) == 1
        assert getattr(error_events[0], "job_id", None) == "flaky"

    def test_base_exception_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SystemExit / KeyboardInterrupt must bubble past the wrapper.

        The shutdown path relies on these propagating — catching
        :class:`BaseException` would wedge the process on Ctrl+C.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: None,
        )

        def body() -> None:
            raise KeyboardInterrupt()

        wrapped = wrap_job(body, job_id="ctrl_c", clock=clock)
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(wrapped())

    def test_heartbeat_can_be_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``heartbeat=False`` opts a job out of the upsert."""
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        write_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: write_calls.append(job_id),
        )

        body = MagicMock()
        wrapped = wrap_job(body, job_id="silent", clock=clock, heartbeat=False)
        asyncio.run(wrapped())

        body.assert_called_once_with()
        assert write_calls == []

    def test_heartbeat_failure_does_not_raise(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """A heartbeat-write crash logs at ERROR and the tick returns.

        The scheduler must survive a transient DB outage — the next
        tick retries and the staleness window escalates if the DB
        stays down.
        """
        allow_propagated_log_capture("app.worker.scheduler")  # type: ignore[operator]

        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))

        def failing_write(job_id: str, _clock: object) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(scheduler_mod, "_write_heartbeat", failing_write)

        body = MagicMock()
        wrapped = wrap_job(body, job_id="hb_flap", clock=clock)
        with caplog.at_level(logging.ERROR, logger="app.worker.scheduler"):
            asyncio.run(wrapped())  # must not raise

        hb_errors = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.heartbeat.error"
        ]
        assert len(hb_errors) == 1

    def test_async_body_is_awaited_on_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``async def`` body is awaited — not silently skipped.

        Regression guard for the ``asyncio.to_thread(func)`` trap:
        calling ``to_thread`` on a coroutine function returns an
        un-awaited coroutine object and the body never executes. The
        heartbeat would still upsert, so ``/readyz`` would stay green
        while the real work vanished into a :class:`RuntimeWarning`.
        Downstream tasks planning an async body (LLM fan-out, async
        HTTP clients) rely on this path.
        """
        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: None,
        )

        run_count = 0

        async def async_body() -> None:
            nonlocal run_count
            run_count += 1

        wrapped = wrap_job(async_body, job_id="async_tick", clock=clock)
        asyncio.run(wrapped())

        assert run_count == 1

    def test_async_body_exception_is_swallowed_and_logged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """A raising ``async def`` body is caught the same as a sync one.

        Parity assertion: the ``Exception`` handler must fire for
        awaitable bodies too, otherwise a single async job crash
        would escape into APScheduler's own error handling and lose
        the ``worker.tick.error`` event marker.
        """
        allow_propagated_log_capture("app.worker.scheduler")  # type: ignore[operator]

        clock = FrozenClock(datetime(2026, 4, 24, 12, 0, tzinfo=UTC))
        write_calls: list[str] = []
        monkeypatch.setattr(
            scheduler_mod,
            "_write_heartbeat",
            lambda job_id, _clock: write_calls.append(job_id),
        )

        async def async_body() -> None:
            raise RuntimeError("async boom")

        wrapped = wrap_job(async_body, job_id="async_flaky", clock=clock)
        with caplog.at_level(logging.ERROR, logger="app.worker.scheduler"):
            asyncio.run(wrapped())  # must not raise

        assert write_calls == []
        error_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.tick.error"
        ]
        assert len(error_events) == 1
        assert getattr(error_events[0], "job_id", None) == "async_flaky"
