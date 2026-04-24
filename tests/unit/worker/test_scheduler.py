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
        """Standard job set: heartbeat + generator + idempotency-sweep."""
        sched = create_scheduler()
        register_jobs(sched)
        # Compare as a set so inserting a future job (cd-yqm4
        # user_workspace derive-refresh, the generator fan-out, any
        # other daily sweep) doesn't break this assertion just by
        # landing a new alphabetical neighbour. The
        # ``registered_job_ids`` helper already returns a sorted
        # tuple, so duplicates would still surface via the companion
        # ``test_is_idempotent_under_replace_existing`` path.
        assert set(registered_job_ids(sched)) == {
            GENERATOR_JOB_ID,
            IDEMPOTENCY_SWEEP_JOB_ID,
            HEARTBEAT_JOB_ID,
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
        }
        assert len(ids) == 3
        assert len(sched.get_jobs()) == 3


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
