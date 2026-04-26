"""Integration tests for the in-process APScheduler lifespan.

End-to-end proof that a real scheduler started via the FastAPI
factory's lifespan hook flips ``/readyz`` from 503 → 200 by
writing a ``worker_heartbeat`` row on the first tick.

Unit coverage for the individual moving parts (heartbeat upsert,
wrap_job exception handling, start/stop idempotency) lives in
``tests/unit/worker/``. This suite exists for what that layer
can't assert:

* Lifespan wiring actually starts the scheduler when
  ``settings.worker == "internal"``.
* A scheduled job wrapped by :func:`wrap_job` writes through the
  real UoW seam and the row is visible to ``/readyz``.
* ``settings.worker == "external"`` means lifespan does NOT start
  the scheduler — operators running a separate worker container
  do not double-fire ticks.

See ``docs/specs/16-deployment-operations.md`` §"Worker process",
§"Healthchecks" and ``docs/specs/17-testing-quality.md``
§"Integration".
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.ops.models import WorkerHeartbeat
from app.config import Settings
from app.main import create_app
from app.tenancy.orm_filter import install_tenant_filter
from app.worker.scheduler import HEARTBEAT_JOB_ID

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_internal_settings(db_url: str) -> Settings:
    """:class:`Settings` gating the in-process worker lifespan ON."""
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-scheduler-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile="prod",
        log_level="INFO",
    )


@pytest.fixture
def pinned_external_settings(db_url: str) -> Settings:
    """:class:`Settings` gating the in-process worker lifespan OFF.

    Operators running a sibling ``worker`` container (Recipes B / D)
    set ``CREWDAY_WORKER=external``; the lifespan must not start the
    scheduler in that shape to avoid two processes double-firing
    the same tick.
    """
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-scheduler-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="external",
        profile="prod",
        log_level="INFO",
    )


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    The scheduler's heartbeat writer calls
    :func:`app.adapters.db.session.make_uow` directly (no FastAPI
    dep override applies), so we patch the module-level defaults the
    same way ``test_health.py::real_make_uow`` does. Teardown
    restores the originals.
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
def clean_heartbeat(engine: Engine) -> Iterator[None]:
    """Empty ``worker_heartbeat`` before and after each test.

    Same reasoning as ``tests/integration/test_health.py`` — the
    shared db_session savepoint pattern doesn't fully isolate
    commits on SQLite, so we wipe via the engine directly.
    """
    with engine.begin() as conn:
        conn.execute(delete(WorkerHeartbeat))
    yield
    with engine.begin() as conn:
        conn.execute(delete(WorkerHeartbeat))


# ---------------------------------------------------------------------------
# End-to-end: lifespan → scheduler → heartbeat → /readyz
# ---------------------------------------------------------------------------


class TestSchedulerLifespanInternal:
    def test_heartbeat_advances_and_readyz_flips_green(
        self,
        pinned_internal_settings: Settings,
        real_make_uow: None,
        clean_heartbeat: None,
        engine: Engine,
    ) -> None:
        """On ``worker=internal``, the first scheduled tick must:

        1. Make :attr:`app.state.scheduler` non-None under the
           lifespan.
        2. Insert a :class:`WorkerHeartbeat` row keyed by
           :data:`HEARTBEAT_JOB_ID`.
        3. Flip ``/readyz`` from 503 (no heartbeat) to 200.

        The heartbeat interval is 30 s but the job is scheduled with
        an immediate first run (``IntervalTrigger`` fires on the
        first interval boundary after ``start()``); we poll with a
        short deadline rather than sleeping a fixed duration so the
        test stays fast in the happy path and still gives a clear
        timeout error on a real regression.
        """
        app = create_app(settings=pinned_internal_settings)

        with TestClient(app) as client:
            # Scheduler stashed on app.state by the lifespan hook.
            scheduler = app.state.scheduler
            assert scheduler is not None
            assert scheduler.running

            # Poll for the first heartbeat. The IntervalTrigger in
            # APScheduler 3.x schedules the first run one interval
            # out by default; explicitly trigger the job now so
            # the test doesn't wait 30 s. ``modify_job`` with a past
            # ``next_run_time`` nudges the scheduler to fire on the
            # next event-loop tick.
            scheduler.modify_job(
                HEARTBEAT_JOB_ID,
                next_run_time=datetime.now(UTC),
            )

            # Poll the DB for the row — the scheduler runs on the
            # TestClient's internal loop, so yield control via
            # short `client.get("/healthz")` pings rather than
            # ``time.sleep`` (which would freeze the loop).
            deadline_seconds = 10
            attempts = 0
            while attempts < deadline_seconds * 10:
                row = _read_heartbeat_row(engine)
                if row is not None:
                    break
                # Yield to the scheduler loop via a short request.
                client.get("/healthz")
                # And an event-loop nap.
                asyncio.run(asyncio.sleep(0.1))
                attempts += 1
            else:
                pytest.fail(
                    "scheduler did not bump worker_heartbeat within "
                    f"{deadline_seconds}s"
                )

            # /readyz now returns 200.
            resp = client.get("/readyz")
            assert resp.status_code == 200, resp.json()
            assert resp.json() == {"status": "ok", "checks": []}

        # After lifespan exit the scheduler is stopped.
        assert app.state.scheduler is None

    def test_readyz_is_503_before_first_tick(
        self,
        pinned_internal_settings: Settings,
        real_make_uow: None,
        clean_heartbeat: None,
    ) -> None:
        """Without any tick, ``/readyz`` still reports ``no_heartbeat``.

        Proves the integration test is actually observing the
        scheduler's write — an accidental pre-seeded row would
        make the green test trivially pass.
        """
        app = create_app(settings=pinned_internal_settings)
        # Do NOT use ``with TestClient`` here — no lifespan means
        # no scheduler start, so the row stays missing.
        client = TestClient(app, raise_server_exceptions=False)
        try:
            resp = client.get("/readyz")
        finally:
            client.close()
        assert resp.status_code == 503
        row = next(c for c in resp.json()["checks"] if c["check"] == "worker_heartbeat")
        assert row["detail"] == "no_heartbeat"


class TestSchedulerLifespanExternal:
    def test_external_mode_does_not_start_scheduler(
        self,
        pinned_external_settings: Settings,
        real_make_uow: None,
        clean_heartbeat: None,
        engine: Engine,
    ) -> None:
        """``worker=external`` → lifespan must leave the scheduler off.

        Prevents the double-fire bug where a self-hosted operator
        runs a separate ``worker`` container AND the web lifespan
        also spins up an in-process scheduler — two ticks per cron
        boundary, every idempotent job wasting one run's compute.
        """
        app = create_app(settings=pinned_external_settings)

        with TestClient(app) as client:
            assert app.state.scheduler is None
            # Hit an endpoint so the ASGI lifespan definitely ran.
            resp = client.get("/healthz")
            assert resp.status_code == 200

        # No heartbeat row written — the in-process scheduler never
        # started.
        with engine.connect() as conn:
            row = conn.execute(select(WorkerHeartbeat)).first()
        assert row is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_heartbeat_row(engine: Engine) -> WorkerHeartbeat | None:
    """Read the heartbeat row for :data:`HEARTBEAT_JOB_ID` via a fresh connection.

    The scheduler writes through a committed UoW, so a SELECT on a
    separate engine connection is immediately visible — the test
    doesn't need to share the scheduler's session. We wrap the read
    in a :class:`Session` so ``scalars().first()`` returns the
    typed :class:`WorkerHeartbeat` instance rather than the generic
    ``Row`` a bare ``Connection.execute`` would hand back.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    session = factory()
    try:
        return session.scalars(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_name == HEARTBEAT_JOB_ID
            )
        ).first()
    finally:
        session.close()
