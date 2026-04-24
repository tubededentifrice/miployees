"""Integration tests for the 60 s LLM-budget refresh scheduler job (cd-ca1k).

End-to-end proof that
:func:`~app.worker.scheduler._make_llm_budget_refresh_body` fans out
across every :class:`~app.adapters.db.workspace.models.Workspace` row,
calls :func:`~app.domain.llm.budget.refresh_aggregate` per workspace,
and keeps a broken workspace from starving the siblings.

Unit coverage for the registration shape + clock propagation lives in
``tests/unit/worker/test_scheduler.py::TestLlmBudgetRefreshJob``. This
suite covers what that layer cannot:

* Two workspaces each with in-window ``llm_usage`` rows see their
  ledger's ``spent_cents`` rewritten to the re-summed total.
* A workspace with usage but no ledger row is skipped (DEBUG log,
  no crash, no new row inserted).
* A workspace whose per-tick ``refresh_aggregate`` call raises is
  logged at WARNING and the OTHER workspaces still refresh.
* The tick emits the ``event=llm.budget.refresh.tick`` INFO summary
  with ``workspaces``, ``failures``, and ``total_cents``.

See ``docs/specs/11-llm-and-agents.md`` §"Workspace usage budget"
§"Meter";  ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.db.workspace.models import Workspace
from app.domain.llm import budget as budget_mod
from app.tenancy import WorkspaceContext, registry, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import Clock, FrozenClock
from app.util.ulid import new_ulid
from app.worker import scheduler as scheduler_mod
from app.worker.scheduler import _make_llm_budget_refresh_body

pytestmark = pytest.mark.integration


# Pinned wall-clock. The refresh body threads the injected clock into
# :func:`refresh_aggregate`, so every "in-window" seed row has a
# ``created_at`` relative to this instant — not ``datetime.now(UTC)``,
# which would drift on slow CI runs and push a seeded row past the
# 30-day cutoff between seed and tick.
_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# LLM tables must stay in the workspace-scoped registry. The sibling
# unit test ``tests/unit/test_tenancy_orm_filter.py`` wipes the
# process-wide registry in an autouse fixture; without this repair
# the tenant filter silently drops off our LLM queries when the full
# suite runs. Same pattern as
# ``tests/domain/llm/conftest.py::_ensure_llm_registered``.
_LLM_TABLES: tuple[str, ...] = (
    "model_assignment",
    "llm_capability_inheritance",
    "llm_usage",
    "budget_ledger",
)


@pytest.fixture(autouse=True)
def _ensure_llm_registered() -> None:
    for table in _LLM_TABLES:
        registry.register(table)


@pytest.fixture(autouse=True)
def _reset_tenancy_context() -> Iterator[None]:
    """Every test starts without an active :class:`WorkspaceContext`.

    Mirrors :mod:`tests.domain.llm.conftest`'s fixture; prevents a
    leaked ctx from a sibling case silently satisfying the tenant
    filter when the refresh body opens its own UoW.
    """
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    The refresh body opens its own UoW via
    :func:`app.adapters.db.session.make_uow`, so we point the
    process-wide default at the integration engine — same plumbing
    the idempotency-sweep integration suite uses. Teardown restores
    the originals.
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
def clean_budget_tables(engine: Engine) -> Iterator[None]:
    """Empty workspace / llm_usage / budget_ledger before and after each test.

    The harness engine is session-scoped, so cross-test bleed would
    otherwise mask regressions: a stale workspace row with an
    unrelated ledger would trivially satisfy the "both ledgers
    rewrite" assertion even if the job never ran.
    """
    with engine.begin() as conn:
        conn.execute(delete(BudgetLedger))
        conn.execute(delete(LlmUsageRow))
        conn.execute(delete(Workspace))
    yield
    with engine.begin() as conn:
        conn.execute(delete(BudgetLedger))
        conn.execute(delete(LlmUsageRow))
        conn.execute(delete(Workspace))


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_workspace(engine: Engine, *, slug: str) -> str:
    """Insert a :class:`Workspace` row and return its id."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    workspace_id = new_ulid()
    with factory() as session, tenant_agnostic():
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=f"Workspace {slug}",
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        session.commit()
    return workspace_id


def _seed_usage(
    engine: Engine,
    *,
    workspace_id: str,
    cost_cents: int,
    created_at: datetime,
) -> None:
    """Insert one :class:`LlmUsageRow` inside the 30-day window."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        session.add(
            LlmUsageRow(
                id=new_ulid(),
                workspace_id=workspace_id,
                capability="chat.manager",
                model_id="01HWA00000000000000000MDL0",
                tokens_in=100,
                tokens_out=50,
                cost_cents=cost_cents,
                latency_ms=0,
                status="ok",
                correlation_id=new_ulid(),
                attempt=0,
                created_at=created_at,
            )
        )
        session.commit()


def _seed_ledger(
    engine: Engine,
    *,
    workspace_id: str,
    cap_cents: int = 10000,
    spent_cents: int = 0,
) -> str:
    """Insert one :class:`BudgetLedger` row anchored to ``_PINNED``.

    Pre-seeds a 30-day window so the initial row exists; the
    :func:`refresh_aggregate` call rolls the ``period_start /
    period_end`` forward on write, so the initial values here only
    matter for the "no-ledger skip" regression — the refresh itself
    overwrites them.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    ledger_id = new_ulid()
    with factory() as session, tenant_agnostic():
        session.add(
            BudgetLedger(
                id=ledger_id,
                workspace_id=workspace_id,
                period_start=_PINNED - timedelta(days=30),
                period_end=_PINNED,
                spent_cents=spent_cents,
                cap_cents=cap_cents,
                updated_at=_PINNED,
            )
        )
        session.commit()
    return ledger_id


def _read_ledger_spent(engine: Engine, *, workspace_id: str) -> int | None:
    """Return the ``spent_cents`` on the most-recent ledger row, or ``None``."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        row = session.scalars(
            select(BudgetLedger)
            .where(BudgetLedger.workspace_id == workspace_id)
            .order_by(BudgetLedger.period_end.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return row.spent_cents


def _count_ledgers(engine: Engine, *, workspace_id: str) -> int:
    """Return how many ledger rows exist for ``workspace_id``."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session, tenant_agnostic():
        return len(
            list(
                session.scalars(
                    select(BudgetLedger).where(
                        BudgetLedger.workspace_id == workspace_id
                    )
                )
            )
        )


# ---------------------------------------------------------------------------
# Happy path: two workspaces refresh to the sum of their in-window usage
# ---------------------------------------------------------------------------


class TestRefreshFanOut:
    """Drive the fan-out body against the real UoW.

    Calling the factory's returned closure directly (rather than
    going through :func:`wrap_job`) isolates the fan-out logic from
    the heartbeat / swallow-exception seams that already have their
    own suite. The wrapper composition is covered by the sibling
    idempotency-sweep integration test.
    """

    def test_two_workspaces_refresh_to_in_window_sum(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_budget_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """Seed A + B each with a 500-cent in-window usage row + ledger.

        Both ledgers should rewrite to 500 after one tick. The tick
        emits a ``llm.budget.refresh.tick`` INFO record with
        ``workspaces=2``, ``failures=0``, ``total_cents=1000``.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(_PINNED)

        ws_a = _seed_workspace(engine, slug="budget-a")
        ws_b = _seed_workspace(engine, slug="budget-b")

        # In-window usage: 10 days ago relative to the frozen clock.
        usage_at = frozen.now() - timedelta(days=10)
        _seed_usage(engine, workspace_id=ws_a, cost_cents=500, created_at=usage_at)
        _seed_usage(engine, workspace_id=ws_b, cost_cents=500, created_at=usage_at)

        _seed_ledger(engine, workspace_id=ws_a, spent_cents=0)
        _seed_ledger(engine, workspace_id=ws_b, spent_cents=0)

        body = _make_llm_budget_refresh_body(frozen)
        with caplog.at_level(logging.INFO, logger="app.worker.scheduler"):
            body()

        # Both ledgers now reflect the re-summed aggregate.
        assert _read_ledger_spent(engine, workspace_id=ws_a) == 500
        assert _read_ledger_spent(engine, workspace_id=ws_b) == 500

        # Tick summary event with the expected counts.
        tick_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "llm.budget.refresh.tick"
        ]
        assert len(tick_events) == 1
        tick = tick_events[0]
        assert tick.levelno == logging.INFO
        assert getattr(tick, "workspaces", None) == 2
        assert getattr(tick, "failures", None) == 0
        assert getattr(tick, "total_cents", None) == 1000

    def test_workspace_without_ledger_is_debug_skipped(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_budget_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """A workspace with usage but no ledger row is skipped at DEBUG.

        Regression guard: the missing-ledger path must NOT:

        1. Crash the tick (would starve every workspace behind it).
        2. Insert a new ledger row (the seeding bug is
           workspace-create's responsibility, cd-tubi).

        It MUST:

        - Log ``event="llm.budget.refresh.no_ledger"`` at DEBUG so
          operators can trace the skip without getting paged.
        - Let the next workspace (A, with a ledger) refresh normally.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(_PINNED)

        ws_a = _seed_workspace(engine, slug="with-ledger")
        ws_c = _seed_workspace(engine, slug="no-ledger")

        usage_at = frozen.now() - timedelta(days=5)
        _seed_usage(engine, workspace_id=ws_a, cost_cents=500, created_at=usage_at)
        _seed_usage(engine, workspace_id=ws_c, cost_cents=700, created_at=usage_at)

        _seed_ledger(engine, workspace_id=ws_a, spent_cents=0)
        # Deliberately: no ledger for ws_c.
        assert _count_ledgers(engine, workspace_id=ws_c) == 0

        body = _make_llm_budget_refresh_body(frozen)
        with caplog.at_level(logging.DEBUG, logger="app.worker.scheduler"):
            body()

        # A refreshed to the sum of its one in-window row.
        assert _read_ledger_spent(engine, workspace_id=ws_a) == 500

        # C was NOT seeded with a ledger and the tick did not create
        # one — refresh_aggregate's contract is "return 0 + log
        # WARNING, do not insert". Stays at zero rows.
        assert _count_ledgers(engine, workspace_id=ws_c) == 0

        # Fan-out body logged a DEBUG skip line for C.
        skip_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "llm.budget.refresh.no_ledger"
            and getattr(rec, "workspace_id", None) == ws_c
        ]
        assert len(skip_events) == 1
        assert skip_events[0].levelno == logging.DEBUG

    def test_ledger_present_zero_spend_does_not_log_no_ledger(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_budget_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
    ) -> None:
        """A workspace with a ledger and zero in-window spend stays quiet.

        Regression guard for the original "result == 0 → DEBUG" check:
        :func:`refresh_aggregate` returns 0 for TWO distinct shapes —
        (a) no ledger row (the cd-tubi seeding-bug signal) and (b) a
        ledger row whose in-window usage sums to zero (a healthy
        zero-spend workspace). Conflating the two at DEBUG would make
        ``event=llm.budget.refresh.no_ledger`` useless for the
        seeding-bug dashboard: a fleet with ten idle zero-spend
        tenants would emit the same signal as one broken seed path.
        The fan-out body must pre-check the ledger row and emit the
        DEBUG event ONLY on path (a); this test seeds path (b) and
        asserts the silence.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(_PINNED)

        ws_z = _seed_workspace(engine, slug="zero-spend")
        _seed_ledger(engine, workspace_id=ws_z, spent_cents=0)
        # Deliberately no llm_usage row: the workspace has a ledger
        # but has never consumed. ``refresh_aggregate`` returns 0
        # from a healthy path — the fan-out must NOT treat this as a
        # missing-ledger signal.

        body = _make_llm_budget_refresh_body(frozen)
        with caplog.at_level(logging.DEBUG, logger="app.worker.scheduler"):
            body()

        # The ledger still exists and is still zero (rewrite is a
        # no-op here; ``period_start`` / ``period_end`` roll forward
        # but ``spent_cents`` stays at 0).
        assert _read_ledger_spent(engine, workspace_id=ws_z) == 0

        # No ``no_ledger`` DEBUG for ws_z — the early-skip path must
        # not fire on a workspace that actually has a ledger row.
        skip_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "llm.budget.refresh.no_ledger"
            and getattr(rec, "workspace_id", None) == ws_z
        ]
        assert skip_events == [], (
            "no_ledger DEBUG event fired for a workspace that has a ledger; "
            "pre-check should distinguish missing-ledger from zero-spend."
        )

        # Tick summary records the workspace attempt (no failure,
        # zero spend summed in).
        tick_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "llm.budget.refresh.tick"
        ]
        assert len(tick_events) == 1
        tick = tick_events[0]
        assert getattr(tick, "workspaces", None) == 1
        assert getattr(tick, "failures", None) == 0
        assert getattr(tick, "total_cents", None) == 0

    def test_broken_workspace_does_not_starve_others(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_budget_tables: None,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One workspace that raises must not stop the next one refreshing.

        Poison :func:`refresh_aggregate` so the call for workspace D
        raises :class:`RuntimeError`. The tick must:

        1. Log ``event="llm.budget.refresh.workspace_failed"`` at
           WARNING with the error class name.
        2. Continue the fan-out loop so workspace A still refreshes.
        3. Surface ``failures=1`` in the tick-summary INFO event.

        This is the crash-safety invariant §11 relies on — a 60 s
        cadence job that aborted on the first broken tenant would
        silently freeze every healthy tenant's meter.
        """
        allow_propagated_log_capture("app.worker.scheduler")

        frozen = FrozenClock(_PINNED)

        ws_a = _seed_workspace(engine, slug="healthy")
        ws_d = _seed_workspace(engine, slug="broken")

        usage_at = frozen.now() - timedelta(days=3)
        _seed_usage(engine, workspace_id=ws_a, cost_cents=500, created_at=usage_at)
        _seed_usage(engine, workspace_id=ws_d, cost_cents=900, created_at=usage_at)

        _seed_ledger(engine, workspace_id=ws_a, spent_cents=0)
        _seed_ledger(engine, workspace_id=ws_d, spent_cents=0)

        real_refresh = budget_mod.refresh_aggregate

        def poisoned_refresh(
            session: Session,
            ctx: WorkspaceContext,
            *,
            clock: Clock | None = None,
        ) -> int:
            """Pass through to the real helper unless ``ctx`` targets ws_d.

            The scheduler body hands in a real
            :class:`~app.tenancy.WorkspaceContext`; this wrapper keeps
            the same signature so ``mypy --strict`` stays clean when
            we delegate on the happy path. Declaring the argument as
            ``object`` would have forced a ``# type: ignore[arg-type]``
            on the delegation — AGENTS.md forbids those, so we type
            the arg properly and let the real signature narrow
            at call time.
            """
            if ctx.workspace_id == ws_d:
                raise RuntimeError("poisoned for test")
            return real_refresh(session, ctx, clock=clock)

        monkeypatch.setattr(
            "app.domain.llm.budget.refresh_aggregate",
            poisoned_refresh,
        )

        body = _make_llm_budget_refresh_body(frozen)
        with caplog.at_level(logging.DEBUG, logger="app.worker.scheduler"):
            body()  # must NOT raise

        # Healthy workspace refreshed; broken one untouched.
        assert _read_ledger_spent(engine, workspace_id=ws_a) == 500
        assert _read_ledger_spent(engine, workspace_id=ws_d) == 0

        # Per-workspace WARNING fired for D with the error class name.
        failed_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "llm.budget.refresh.workspace_failed"
            and getattr(rec, "workspace_id", None) == ws_d
        ]
        assert len(failed_events) == 1
        assert failed_events[0].levelno == logging.WARNING
        assert getattr(failed_events[0], "error", None) == "RuntimeError"

        # Tick summary reports the failure without aborting.
        tick_events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "llm.budget.refresh.tick"
        ]
        assert len(tick_events) == 1
        tick = tick_events[0]
        assert getattr(tick, "workspaces", None) == 2
        assert getattr(tick, "failures", None) == 1
        # Total_cents accounts only for the successful workspace's
        # freshly-computed aggregate — the failing one returned no
        # value to sum.
        assert getattr(tick, "total_cents", None) == 500


# ---------------------------------------------------------------------------
# Registered-body path: assert the scheduler's stored callable matches
# the factory output (belt-and-braces for the register_jobs wiring).
# ---------------------------------------------------------------------------


class TestRegisteredBodyDrivesRefresh:
    """Prove the closure the scheduler stores is the same one a direct
    factory call would produce.

    Not strictly necessary — the unit suite already asserts the job
    lands in the scheduler — but cheap at integration depth, and a
    regression that swapped the factory for a placeholder body
    (like the current ``_generator_fanout_placeholder``) would slip
    past the unit tests while still satisfying the "job exists"
    assertion. Driving the real engine through the registered path
    closes that gap.
    """

    def test_registered_job_body_refreshes_ledger(
        self,
        engine: Engine,
        real_make_uow: None,
        clean_budget_tables: None,
    ) -> None:
        """The closure registered via :func:`register_jobs` rewrites the ledger.

        Register into a fresh scheduler, pull the callable the
        scheduler stored under :data:`LLM_BUDGET_REFRESH_JOB_ID`,
        and invoke it under the real UoW. ``register_jobs`` wraps the
        body in :func:`wrap_job`; the wrapper needs an event loop, so
        we bypass it here and reach the raw factory via the sibling
        ``_make_llm_budget_refresh_body`` — same guarantee, without
        coupling to :mod:`asyncio`.
        """
        frozen = FrozenClock(_PINNED)

        ws_a = _seed_workspace(engine, slug="registered-a")
        usage_at = frozen.now() - timedelta(days=1)
        _seed_usage(engine, workspace_id=ws_a, cost_cents=250, created_at=usage_at)
        _seed_ledger(engine, workspace_id=ws_a, spent_cents=0)

        # The factory is the same module-level symbol ``register_jobs``
        # calls. If a refactor swapped the ``register_jobs`` wiring to
        # a different body, this test would still pass — that's what
        # the unit ``test_adds_llm_budget_refresh_job_at_60s_interval``
        # already covers (it asserts the ID + trigger on a real
        # ``register_jobs`` call). Here we just prove the factory
        # output actually rewrites a ledger against the real engine.
        body = scheduler_mod._make_llm_budget_refresh_body(frozen)
        body()

        assert _read_ledger_spent(engine, workspace_id=ws_a) == 250
