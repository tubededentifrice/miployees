"""Integration tests for the daily ``idempotency_sweep`` scheduler job.

End-to-end proof that the APScheduler-registered sweep
(cd-j9l7) wires :func:`~app.api.middleware.idempotency.prune_expired_idempotency_keys`
through :func:`~app.worker.scheduler.wrap_job` against the real UoW
seam — deleting expired rows, advancing the ``worker_heartbeat``
keyed by ``worker_name='idempotency_sweep'``, and emitting an
``event=idempotency.sweep`` INFO record.

Unit coverage for the standalone callable (TTL semantics, batch
deletion, empty table) lives in
``tests/unit/api/middleware/test_idempotency.py``. This suite covers
what that layer can't:

* The job id, trigger cadence, and wrapper hooks registered by
  :func:`~app.worker.scheduler.register_jobs`.
* The sweep body's full round-trip through ``wrap_job``: body runs,
  rows deleted, heartbeat upserted, deletion count logged.
* The happy-path equivalence between a job-driven sweep and a
  direct call — same transaction semantics, same row-count.

See ``docs/specs/12-rest-api.md`` §"Idempotency",
``docs/specs/16-deployment-operations.md`` §"Worker process" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.ops.models import IdempotencyKey, WorkerHeartbeat
from app.api.middleware.idempotency import IDEMPOTENCY_TTL_HOURS
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from app.worker.scheduler import (
    IDEMPOTENCY_SWEEP_JOB_ID,
    create_scheduler,
    register_jobs,
    wrap_job,
)
from app.worker.scheduler import (
    _make_idempotency_sweep_body as make_idempotency_sweep_body,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    The sweep body calls :func:`prune_expired_idempotency_keys` with
    no ``db_session`` — the function opens its own UoW via
    :func:`app.adapters.db.session.make_uow`. Same plumbing
    ``tests/integration/test_worker_scheduler.py`` patches for the
    heartbeat path; we reuse the pattern so the two integration
    suites drive the real scheduler seam identically.
    """
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def clean_sweep_tables(engine: Engine) -> Iterator[None]:
    """Empty ``idempotency_key`` + ``worker_heartbeat`` before and after each test.

    Mirrors ``tests/integration/test_worker_scheduler.py::clean_heartbeat``:
    the harness engine is session-scoped so cross-test bleed would
    otherwise mask regressions (a stale heartbeat row from an earlier
    test would trivially satisfy the "row exists after the sweep"
    assertion even if the job never ran).
    """
    with engine.begin() as conn:
        conn.execute(delete(IdempotencyKey))
        conn.execute(delete(WorkerHeartbeat))
    yield
    with engine.begin() as conn:
        conn.execute(delete(IdempotencyKey))
        conn.execute(delete(WorkerHeartbeat))


def _seed_row(
    engine: Engine,
    *,
    token_id: str,
    key: str,
    created_at: datetime,
) -> str:
    """Insert one :class:`IdempotencyKey` row and return its id.

    The minimal shape the TTL sweep needs: ``created_at`` drives
    the cutoff; the other columns are non-null and filled with
    stable placeholder values. Bypasses the middleware so the test
    controls the ``created_at`` column directly — the middleware
    always stamps ``clock.now()``, which would force an unreliable
    ``freeze_time``-style monkeypatch here.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    row_id = new_ulid()
    with factory() as session:
        session.add(
            IdempotencyKey(
                id=row_id,
                token_id=token_id,
                key=key,
                status=200,
                body_hash="a" * 64,
                body=b"{}",
                headers={"content-type": "application/json"},
                created_at=created_at,
            )
        )
        session.commit()
    return row_id


def _count_rows(engine: Engine) -> int:
    """Return the total row count in ``idempotency_key``."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        return len(list(session.scalars(select(IdempotencyKey))))


def _read_heartbeat(engine: Engine) -> WorkerHeartbeat | None:
    """Return the heartbeat row for the sweep job, or ``None``."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        return session.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == IDEMPOTENCY_SWEEP_JOB_ID
            )
        ).first()


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------


class TestRegisterJobs:
    """The sweep job is registered by :func:`register_jobs` with the
    expected id, cron trigger, and wrapper hooks."""

    def test_registered_with_daily_cron_trigger(self) -> None:
        """Sweep lands under ``IDEMPOTENCY_SWEEP_JOB_ID`` with a
        ``CronTrigger(hour=3, minute=0)`` and room for a late restart.

        Pinning the trigger shape here rather than only in the unit
        suite keeps the cadence visible at the integration layer —
        the place operators' runbooks actually cite.
        """
        scheduler = create_scheduler()
        register_jobs(scheduler)
        job = scheduler.get_job(IDEMPOTENCY_SWEEP_JOB_ID)
        assert job is not None, "idempotency_sweep job not registered"

        assert isinstance(job.trigger, CronTrigger)
        # Cron fields are indexed by name; mapping them via
        # ``job.trigger.fields`` matches the APScheduler public API.
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == "3"
        assert fields["minute"] == "0"

        # ``misfire_grace_time`` buys a late restart up to one hour.
        # Below 60 minutes would fail to cover a slow container
        # restart near 03:00 UTC.
        assert job.misfire_grace_time is not None
        assert job.misfire_grace_time >= 3600

        # ``max_instances=1`` + ``coalesce=True`` — a stuck tick
        # must not stack up. Matches the convention the heartbeat
        # and generator placeholders use.
        assert job.max_instances == 1
        assert job.coalesce is True


# ---------------------------------------------------------------------------
# End-to-end: seeded row → job body → deletion + heartbeat + log
# ---------------------------------------------------------------------------


class TestSweepJobEndToEnd:
    """Drive the sweep body through :func:`wrap_job` against the real UoW.

    Running the body via the wrapper (rather than calling the
    sweeper directly) is what cd-j9l7 acceptance criterion #4 asks
    for — the heartbeat, log, and exception-swallow seams all sit in
    :func:`wrap_job`, and only a wrapper-driven tick proves the whole
    composition.
    """

    def test_expired_row_deleted_heartbeat_advances_count_logged(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_sweep_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """Seed an expired row; one wrapped tick:

        1. Deletes the row.
        2. Writes a :class:`WorkerHeartbeat` keyed by
           :data:`IDEMPOTENCY_SWEEP_JOB_ID`.
        3. Emits one ``event=idempotency.sweep`` INFO record with
           ``deleted >= 1``.

        Alembic's ``fileConfig`` flips ``propagate=False`` on the
        scheduler logger during the session-scoped migration; the
        shared helper restores propagation for the duration of this
        test so ``caplog`` can see the sweep's INFO event.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        # Fixed clock so both the sweep cutoff AND the heartbeat
        # timestamp are deterministic. The sweep body now threads
        # this clock through to ``prune_expired_idempotency_keys``
        # (see :func:`~app.worker.scheduler._make_idempotency_sweep_body`),
        # so the seeded ``created_at`` below must be computed
        # relative to ``frozen.now()`` — not ``datetime.now(UTC)``,
        # which would otherwise be live-wall-clock and drift the row
        # off the expected side of the cutoff whenever the test runs
        # at a real time later than the frozen instant.
        frozen = FrozenClock(datetime(2026, 4, 24, 3, 0, tzinfo=UTC))

        # Seed a row one hour past the TTL (frozen-clock reference).
        expired_at = frozen.now() - timedelta(hours=IDEMPOTENCY_TTL_HOURS + 1)
        _seed_row(
            engine,
            token_id="01HXTOK00000000000000SWEEP",
            key="expired-key",
            created_at=expired_at,
        )
        assert _count_rows(engine) == 1

        wrapped = wrap_job(
            make_idempotency_sweep_body(frozen),
            job_id=IDEMPOTENCY_SWEEP_JOB_ID,
            clock=frozen,
        )

        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            asyncio.run(wrapped())

        # 1. Deletion landed.
        assert _count_rows(engine) == 0

        # 2. Heartbeat row written under the sweep's worker_name
        #    with the injected-clock timestamp.
        heartbeat = _read_heartbeat(engine)
        assert heartbeat is not None, "heartbeat row not written"
        # SQLite strips tzinfo off ``DateTime(timezone=True)`` on
        # read; Postgres keeps it. Compare in naive-UTC space to
        # stay portable (see :mod:`app.worker.tasks.generator`'s
        # ``_as_naive_utc`` for the same reasoning).
        expected = frozen.now().astimezone(UTC).replace(tzinfo=None)
        actual = heartbeat.heartbeat_at
        if actual.tzinfo is not None:
            actual = actual.astimezone(UTC).replace(tzinfo=None)
        assert actual == expected

        # Postgres parallel: the column type is
        # ``DateTime(timezone=True)``, so the stored row MUST
        # round-trip with ``tzinfo`` attached. SQLite always strips
        # it on read — the dialect-specific assertion only runs on
        # the PG shard, gated by ``CREWDAY_TEST_DB=postgres``.
        # Regression guard: a future migration flipping the column
        # to ``timezone=False`` would silently pass the naive-UTC
        # check above without this second assertion.
        if os.environ.get("CREWDAY_TEST_DB", "").lower() == "postgres":
            assert heartbeat.heartbeat_at.tzinfo is not None, (
                "Postgres must preserve tzinfo on heartbeat_at — "
                "check the column type on WorkerHeartbeat.heartbeat_at"
            )

        # 3. Exactly one ``event=idempotency.sweep`` INFO record
        #    with a positive deletion count.
        sweep_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "idempotency.sweep"
        ]
        assert len(sweep_events) == 1
        assert sweep_events[0].levelno == logging.INFO
        assert getattr(sweep_events[0], "deleted", None) == 1

        # The wrapper's end event must fire with ok=True — proves
        # the body didn't raise and the heartbeat seam ran.
        end_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "worker.tick.end"
            and getattr(rec, "job_id", None) == IDEMPOTENCY_SWEEP_JOB_ID
        ]
        assert len(end_events) == 1
        assert getattr(end_events[0], "ok", None) is True

    def test_fresh_row_is_preserved(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_sweep_tables: None,
    ) -> None:
        """Row inside the 24 h window survives the sweep.

        Regression guard: an over-eager cutoff (e.g. UTC / local
        off-by-one, wrong sign on the delta) would silently drop
        live replay-cache entries and defeat the idempotency
        contract.
        """
        # Frozen clock: the sweep body threads this through to
        # ``prune_expired_idempotency_keys``, so "fresh" is measured
        # against the frozen instant.
        frozen = FrozenClock(datetime(2026, 4, 24, 3, 0, tzinfo=UTC))
        # 1 hour ago (frozen-clock reference) — well inside the 24 h TTL.
        fresh_at = frozen.now() - timedelta(hours=1)
        _seed_row(
            engine,
            token_id="01HXTOK00000000000000FRESH",
            key="fresh-key",
            created_at=fresh_at,
        )

        wrapped = wrap_job(
            make_idempotency_sweep_body(frozen),
            job_id=IDEMPOTENCY_SWEEP_JOB_ID,
            clock=frozen,
        )
        asyncio.run(wrapped())

        # Row preserved.
        assert _count_rows(engine) == 1

        # Heartbeat still advanced — the tick was a successful
        # no-op deletion, not a skipped job.
        assert _read_heartbeat(engine) is not None

    def test_empty_table_still_heartbeats_and_logs_zero(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_sweep_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """Empty table → deleted=0, heartbeat still upserts.

        Edge case operators will hit on day one and after every
        quiet window: the sweep must still prove liveness via the
        heartbeat and emit a log record so the absence of sweep
        activity isn't indistinguishable from the job never firing.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(datetime(2026, 4, 24, 3, 0, tzinfo=UTC))
        wrapped = wrap_job(
            make_idempotency_sweep_body(frozen),
            job_id=IDEMPOTENCY_SWEEP_JOB_ID,
            clock=frozen,
        )
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            asyncio.run(wrapped())

        assert _count_rows(engine) == 0
        assert _read_heartbeat(engine) is not None

        sweep_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "idempotency.sweep"
        ]
        assert len(sweep_events) == 1
        assert getattr(sweep_events[0], "deleted", None) == 0
