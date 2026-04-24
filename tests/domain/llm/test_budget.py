"""Unit tests for :mod:`app.domain.llm.budget` (cd-irng).

Covers every acceptance criterion pinned by the Beads task:

1. First call on a fresh workspace passes; aggregate ticks up
   post-call.
2. Pre-flight refuses when ``projected + cached > cap``.
3. Refused call writes NO ``llm_usage`` row.
4. Aggregate respects the 30-day window — a 31-day-old call no
   longer contributes.
5. :func:`record_usage` is idempotent on ``(workspace_id,
   correlation_id, attempt)``.
6. Lowering the cap below the current aggregate immediately refuses
   the next call (within the 60 s refresh).
7. Free-tier model contributes 0 to the aggregate; row still
   written for telemetry.
8. Demo workspaces seed cap = 10 cents (exercised via a fixture —
   the workspace-create handler isn't wired yet, tracked as a
   follow-up Beads task in cd-irng's summary).

Also exercises:

* Unknown ``api_model_id`` → ``(0, 0)`` + a WARNING log entry.
* :func:`warm_start_aggregate` materialises from ``llm_usage``.
* :func:`refresh_aggregate` rolls the ledger's window forward.
* Missing ledger row — :func:`check_budget` fails closed and logs
  WARNING + INFO (seeding bug signal, not "no cap means unlimited").
* Property-style concurrency (only on PG): 50 concurrent
  :func:`record_usage` calls converge on a single aggregate without
  drift. Skipped on SQLite because the sibling SAVEPOINT fixture
  serialises writers by construction; the Postgres shard is the
  real atomicity check.

See ``docs/specs/11-llm-and-agents.md`` §"Workspace usage budget".
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.domain.llm.budget import (
    WINDOW_DAYS,
    WINDOW_LABEL,
    BudgetExceeded,
    LlmUsage,
    UsageStatus,
    check_budget,
    estimate_cost_cents,
    record_usage,
    refresh_aggregate,
    reset_unknown_model_dedup_for_tests,
    warm_start_aggregate,
)
from app.tenancy import tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.domain.llm.conftest import build_context, seed_workspace

# Pinned wall-clock. Shared with the router conftest's ``_PINNED`` so
# the ULID prefix ordering matches across cases.
_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_unknown_model_dedup() -> Iterator[None]:
    """Clear :mod:`app.domain.llm.budget`'s unknown-model dedup set per case.

    :func:`estimate_cost_cents` dedupes the "unknown model" WARNING
    on ``(workspace_id, api_model_id)`` for the process lifetime so a
    busy workspace doesn't flood the log. Without this reset, a test
    that triggered the WARNING once would leave the set populated,
    and a second case asserting on the same WARNING would see a
    DEBUG line instead — flake under test ordering.
    """
    reset_unknown_model_dedup_for_tests()
    yield
    reset_unknown_model_dedup_for_tests()


# ---------------------------------------------------------------------------
# Helpers — budget-specific row factories
# ---------------------------------------------------------------------------


def _seed_ledger(
    session: Session,
    *,
    workspace_id: str,
    cap_cents: int,
    spent_cents: int = 0,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    now: datetime = _PINNED,
) -> BudgetLedger:
    """Insert a :class:`BudgetLedger` row with scenario-specific overrides.

    Production seeds its ledger via
    :func:`app.auth.signup.provision_workspace_and_owner_seat` (cd-tubi),
    which calls :func:`app.domain.llm.budget.new_ledger_row` so every
    fresh workspace lands a ledger in the same transaction as the
    :class:`~app.adapters.db.workspace.models.Workspace` row per §11
    "Cap".

    This helper deliberately stays around because the budget tests
    below need to seed rows with a non-default ``cap_cents`` (e.g.
    10-cent demo cap, 100 000-cent concurrency stress cap), a
    pre-populated ``spent_cents`` (the "near-cap" refusal case —
    :class:`TestCheckBudget.test_refuses_when_sum_exceeds_cap`), or a
    custom ``period_start / period_end`` (:class:`Test30DayWindow`'s
    "seed a stale window; refresh rolls it forward"). The
    :func:`new_ledger_row` seam is intentionally pinned to the
    fresh-workspace shape (zero spend, ``[now, now+30d]`` rolling
    window) — widening it to accept every test fixture's knobs would
    leak test-only parameters into the prod signup call site, which
    is exactly the coupling the ledger seam exists to prevent.

    The trailing ``[now-30d, now]`` default here is a legacy of the
    pre-cd-tubi scaffolding (tests seed "the 30 days ending now" to
    pre-populate ``spent_cents``); callers that care about the exact
    bounds override explicitly.
    """
    start = (
        period_start if period_start is not None else now - timedelta(days=WINDOW_DAYS)
    )
    end = period_end if period_end is not None else now
    row = BudgetLedger(
        id=new_ulid(),
        workspace_id=workspace_id,
        period_start=start,
        period_end=end,
        spent_cents=spent_cents,
        cap_cents=cap_cents,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _seed_usage_row(
    session: Session,
    *,
    workspace_id: str,
    cost_cents: int,
    created_at: datetime,
    capability: str = "chat.manager",
    correlation_id: str | None = None,
    attempt: int = 0,
    status: str = "ok",
) -> LlmUsageRow:
    """Insert an :class:`LlmUsageRow` directly (bypasses :func:`record_usage`).

    Used to pre-seed historical usage for the 30-day-window test.
    """
    row = LlmUsageRow(
        id=new_ulid(),
        workspace_id=workspace_id,
        capability=capability,
        model_id="01HWA00000000000000000MDL0",
        tokens_in=0,
        tokens_out=0,
        cost_cents=cost_cents,
        latency_ms=0,
        status=status,
        correlation_id=correlation_id or new_ulid(),
        attempt=attempt,
        created_at=created_at,
    )
    session.add(row)
    session.flush()
    return row


def _build_usage(
    *,
    cost_cents: int,
    capability: str = "chat.manager",
    correlation_id: str | None = None,
    attempt: int = 0,
    status: str = "ok",
    api_model_id: str = "01HWA00000000000000000MDLA",
) -> LlmUsage:
    """Build a domain :class:`LlmUsage` value with sensible defaults.

    ``status`` is accepted as :class:`str` (rather than the narrow
    :data:`UsageStatus` Literal) so tests can pass invalid literals
    to exercise the refusal / unknown-status paths. The call site
    below re-narrows via :func:`typing.cast` — ``mypy --strict``
    knows the value reaches the dataclass as a :data:`UsageStatus`,
    and runtime sees whatever the test wrote. This replaces the
    earlier ``# type: ignore[arg-type]`` which hid the narrowing
    choice.
    """
    return LlmUsage(
        prompt_tokens=100,
        completion_tokens=50,
        cost_cents=cost_cents,
        provider_model_id=api_model_id,
        api_model_id=api_model_id,
        assignment_id="01HWA00000000000000000ASGN",
        capability=capability,
        correlation_id=correlation_id or new_ulid(),
        attempt=attempt,
        status=cast(UsageStatus, status),
    )


# ---------------------------------------------------------------------------
# estimate_cost_cents
# ---------------------------------------------------------------------------


class TestEstimateCostCents:
    """Pricing helper: known / unknown / free-tier paths."""

    def test_known_model_uses_pricing_table(self) -> None:
        pricing = {"gpt-4o-mini": (15, 60)}  # cents per 1M tokens
        # 1000 in + 500 out → 1000*15/1M + 500*60/1M = 0 + 0 (integer
        # floor). Use larger counts so the floor doesn't clip to zero.
        cost = estimate_cost_cents(
            1_000_000,
            1_000_000,
            api_model_id="gpt-4o-mini",
            pricing=pricing,
        )
        # 1M * 15 + 1M * 60 = 75_000_000; // 1M = 75 cents.
        assert cost == 75

    def test_unknown_model_returns_zero_with_warning(
        self,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        # Alembic's fileConfig disables app loggers by default; re-enable
        # capture for the budget module's logger name.
        allow_propagated_log_capture("app.domain.llm.budget")  # type: ignore[operator]
        with caplog.at_level(logging.WARNING, logger="app.domain.llm.budget"):
            cost = estimate_cost_cents(
                100,
                200,
                api_model_id="not-in-registry",
                pricing={},
                workspace_id="ws-test",
            )
        assert cost == 0
        assert any(r.message == "llm.pricing.unknown_model" for r in caplog.records), (
            "expected WARNING for unknown model; got "
            f"{[r.message for r in caplog.records]}"
        )

    def test_free_tier_returns_zero_without_warning(
        self,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        allow_propagated_log_capture("app.domain.llm.budget")  # type: ignore[operator]
        with caplog.at_level(logging.WARNING, logger="app.domain.llm.budget"):
            cost = estimate_cost_cents(
                1_000_000,
                1_000_000,
                api_model_id="google/gemma-3-27b-it:free",
                pricing={},
            )
        assert cost == 0
        # Free-tier short-circuit: no WARNING (the demo seeds these
        # deliberately; a warning per call would drown the operator).
        assert not any(r.message == "llm.pricing.unknown_model" for r in caplog.records)

    def test_free_tier_logs_debug_for_observability(
        self,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """:free models log at DEBUG so the free-tier code path is traceable.

        Operators diagnosing "is my demo workspace hitting free-tier
        models?" need a deterministic log line. WARNING would spam;
        silence would leave the branch invisible. DEBUG splits the
        difference.
        """
        allow_propagated_log_capture("app.domain.llm.budget")  # type: ignore[operator]
        with caplog.at_level(logging.DEBUG, logger="app.domain.llm.budget"):
            estimate_cost_cents(
                100,
                100,
                api_model_id="google/gemma-3-27b-it:free",
                pricing={},
                workspace_id="ws-demo",
            )
        assert any(r.message == "llm.pricing.free_tier" for r in caplog.records), (
            "expected DEBUG log for free-tier path; got "
            f"{[r.message for r in caplog.records]}"
        )

    def test_unknown_model_warning_is_deduped_per_process(
        self,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """Second hit on the same ``(workspace, api_model_id)`` drops to DEBUG.

        §11 "Pricing source" falls back to ``(0, 0)`` on an unknown
        model; that can happen thousands of times per minute on a
        busy workspace. The first miss logs at WARNING (operator
        signal); subsequent misses log at DEBUG (code-path trace).
        """
        allow_propagated_log_capture("app.domain.llm.budget")  # type: ignore[operator]
        with caplog.at_level(logging.DEBUG, logger="app.domain.llm.budget"):
            # First call — WARNING.
            estimate_cost_cents(
                100,
                100,
                api_model_id="mystery-model",
                pricing={},
                workspace_id="ws-dedup",
            )
            # Second call with identical pair — DEBUG.
            estimate_cost_cents(
                100,
                100,
                api_model_id="mystery-model",
                pricing={},
                workspace_id="ws-dedup",
            )

        warnings = [
            r
            for r in caplog.records
            if r.message == "llm.pricing.unknown_model" and r.levelno == logging.WARNING
        ]
        debugs = [
            r
            for r in caplog.records
            if r.message == "llm.pricing.unknown_model" and r.levelno == logging.DEBUG
        ]
        assert len(warnings) == 1, (
            f"expected exactly one WARNING across two calls; got {len(warnings)}"
        )
        assert len(debugs) == 1, f"expected one DEBUG dedup line; got {len(debugs)}"

    def test_unknown_model_dedup_keys_on_workspace_plus_model(
        self,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """Different ``(workspace_id, api_model_id)`` pairs each log a WARNING.

        Dedup is per-pair so the operator sees a loud signal on
        every new workspace / model combination that hits an
        unregistered model.
        """
        allow_propagated_log_capture("app.domain.llm.budget")  # type: ignore[operator]
        with caplog.at_level(logging.WARNING, logger="app.domain.llm.budget"):
            estimate_cost_cents(
                100,
                100,
                api_model_id="mystery-a",
                pricing={},
                workspace_id="ws-1",
            )
            estimate_cost_cents(
                100,
                100,
                api_model_id="mystery-a",
                pricing={},
                workspace_id="ws-2",
            )
            estimate_cost_cents(
                100,
                100,
                api_model_id="mystery-b",
                pricing={},
                workspace_id="ws-1",
            )
        warnings = [
            r
            for r in caplog.records
            if r.message == "llm.pricing.unknown_model" and r.levelno == logging.WARNING
        ]
        # Three distinct pairs → three WARNINGs.
        assert len(warnings) == 3


# ---------------------------------------------------------------------------
# BudgetExceeded — shape the API / CLI seam depends on
# ---------------------------------------------------------------------------


class TestBudgetExceededShape:
    """The exception carries the §11 refusal envelope as plain data."""

    def test_to_dict_matches_spec_shape(self) -> None:
        """``to_dict()`` emits ``{error, capability, window, message}``.

        This is the body the FastAPI handler (cd-6bcl) serialises as
        the 402 response verbatim — the test pins the contract so a
        future rename catches at test time rather than on the wire.
        """
        exc = BudgetExceeded(capability="chat.manager", workspace_id="ws-x")
        body = exc.to_dict()
        assert set(body.keys()) == {"error", "capability", "window", "message"}
        assert body["error"] == "budget_exceeded"
        assert body["capability"] == "chat.manager"
        assert body["window"] == WINDOW_LABEL
        assert isinstance(body["message"], str) and body["message"]

    def test_to_dict_omits_workspace_id_privacy(self) -> None:
        """``workspace_id`` does NOT cross the wire (§15 "Privacy")."""
        exc = BudgetExceeded(capability="chat.manager", workspace_id="ws-secret")
        body = exc.to_dict()
        assert "workspace_id" not in body
        # The attribute is still available to the caller that raised
        # the exception (for logging / audit) — only the serialised
        # form strips it.
        assert exc.workspace_id == "ws-secret"

    def test_str_yields_message(self) -> None:
        """``str(exc)`` returns the human-facing message."""
        exc = BudgetExceeded(
            capability="chat.manager",
            workspace_id="ws-x",
            message="custom message",
        )
        assert str(exc) == "custom message"


# ---------------------------------------------------------------------------
# check_budget — pre-flight refusal (§11 "At-cap behaviour")
# ---------------------------------------------------------------------------


class TestCheckBudget:
    """AC #1 / #2 / #6: pre-flight envelope check."""

    def test_first_call_on_fresh_workspace_passes(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """AC #1 (pre-flight portion): fresh ledger accepts the call."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            # No exception = passes.
            check_budget(
                db_session,
                ctx,
                capability="chat.manager",
                projected_cost_cents=10,
                clock=clock,
            )
        finally:
            reset_current(token)

    def test_refuses_when_sum_exceeds_cap(
        self,
        db_session: Session,
        clock: FrozenClock,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """AC #2: ``cached + projected > cap`` trips :class:`BudgetExceeded`."""
        allow_propagated_log_capture("app.domain.llm.budget")  # type: ignore[operator]
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(
            db_session,
            workspace_id=ws.id,
            cap_cents=500,
            spent_cents=495,
        )
        token = set_current(ctx)
        try:
            with (
                caplog.at_level(logging.INFO, logger="app.domain.llm.budget"),
                pytest.raises(BudgetExceeded) as excinfo,
            ):
                check_budget(
                    db_session,
                    ctx,
                    capability="chat.manager",
                    projected_cost_cents=10,
                    clock=clock,
                )
            assert excinfo.value.capability == "chat.manager"
            assert excinfo.value.window == WINDOW_LABEL
            assert excinfo.value.workspace_id == ws.id
            # INFO log per §11 "At-cap behaviour".
            assert any(r.message == "llm.budget_exceeded" for r in caplog.records), (
                f"expected INFO log; got {[r.message for r in caplog.records]}"
            )
        finally:
            reset_current(token)

    def test_equal_to_cap_is_allowed(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Boundary: ``spent + projected == cap`` passes (inclusive cap)."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(
            db_session,
            workspace_id=ws.id,
            cap_cents=500,
            spent_cents=490,
        )
        token = set_current(ctx)
        try:
            check_budget(
                db_session,
                ctx,
                capability="chat.manager",
                projected_cost_cents=10,
                clock=clock,
            )
        finally:
            reset_current(token)

    def test_missing_ledger_refuses_with_warning(
        self,
        db_session: Session,
        clock: FrozenClock,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """Fail-closed: a workspace with no ledger row refuses every call."""
        allow_propagated_log_capture("app.domain.llm.budget")  # type: ignore[operator]
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        # NO _seed_ledger call — this workspace was created without the
        # cap seed the §11 handler should have inserted.
        token = set_current(ctx)
        try:
            with (
                caplog.at_level(logging.WARNING, logger="app.domain.llm.budget"),
                pytest.raises(BudgetExceeded),
            ):
                check_budget(
                    db_session,
                    ctx,
                    capability="chat.manager",
                    projected_cost_cents=1,
                    clock=clock,
                )
            assert any(
                r.message == "llm.budget.ledger_missing" for r in caplog.records
            ), (
                "expected ledger_missing WARNING; got "
                f"{[r.message for r in caplog.records]}"
            )
        finally:
            reset_current(token)

    def test_lowering_cap_refuses_next_call(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """AC #6: cap lowered below aggregate → next call refused immediately."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        ledger = _seed_ledger(
            db_session,
            workspace_id=ws.id,
            cap_cents=500,
            spent_cents=400,
        )
        token = set_current(ctx)
        try:
            # First pass: cap is 500, spent 400, projected 10 → passes.
            check_budget(
                db_session,
                ctx,
                capability="chat.manager",
                projected_cost_cents=10,
                clock=clock,
            )
            # Operator lowers the cap to 100 (well below current spend).
            ledger.cap_cents = 100
            db_session.flush()
            # The next call refuses immediately — we don't wait for the
            # 60 s refresh; the ledger read picks up the new cap now.
            with pytest.raises(BudgetExceeded):
                check_budget(
                    db_session,
                    ctx,
                    capability="chat.manager",
                    projected_cost_cents=10,
                    clock=clock,
                )
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# record_usage — post-flight write + ledger bump
# ---------------------------------------------------------------------------


class TestRecordUsage:
    """AC #1 (post-flight) / #3 / #5 / #7: record_usage invariants."""

    def test_happy_path_writes_row_and_bumps_ledger(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """AC #1: aggregate ticks up post-call."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record_usage(
                db_session,
                ctx,
                _build_usage(cost_cents=25),
                clock=clock,
            )
            db_session.flush()
            # Row landed.
            rows = (
                db_session.execute(
                    select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws.id)
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].cost_cents == 25
            # Ledger bumped.
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 25
        finally:
            reset_current(token)

    def test_refused_status_writes_no_row(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """AC #3: refused calls do NOT write ``llm_usage``."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record_usage(
                db_session,
                ctx,
                _build_usage(cost_cents=0, status="refused"),
                clock=clock,
            )
            rows = (
                db_session.execute(
                    select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws.id)
                )
                .scalars()
                .all()
            )
            assert rows == []
            # Ledger untouched.
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 0
        finally:
            reset_current(token)

    def test_idempotent_on_correlation_attempt(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """AC #5: retry with same ``(correlation_id, attempt)`` is a no-op."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            correlation_id = new_ulid()
            usage = _build_usage(cost_cents=25, correlation_id=correlation_id)

            record_usage(db_session, ctx, usage, clock=clock)
            db_session.flush()

            # Retry — same triple. Must not double-count.
            record_usage(db_session, ctx, usage, clock=clock)
            db_session.flush()

            rows = (
                db_session.execute(
                    select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws.id)
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 25
        finally:
            reset_current(token)

    def test_different_attempt_writes_second_row(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Retries with a bumped ``attempt`` DO write a second row.

        The idempotency key is ``(workspace_id, correlation_id,
        attempt)`` — a fallback-chain walker that ticks ``attempt``
        on each rung expects each rung to land as its own row (§11
        "Failure modes" ``X-LLM-Fallback-Attempts``).
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            correlation_id = new_ulid()
            record_usage(
                db_session,
                ctx,
                _build_usage(
                    cost_cents=10,
                    correlation_id=correlation_id,
                    attempt=0,
                ),
                clock=clock,
            )
            record_usage(
                db_session,
                ctx,
                _build_usage(
                    cost_cents=15,
                    correlation_id=correlation_id,
                    attempt=1,
                ),
                clock=clock,
            )
            db_session.flush()

            rows = (
                db_session.execute(
                    select(LlmUsageRow)
                    .where(LlmUsageRow.workspace_id == ws.id)
                    .order_by(LlmUsageRow.attempt.asc())
                )
                .scalars()
                .all()
            )
            assert [r.attempt for r in rows] == [0, 1]
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 25
        finally:
            reset_current(token)

    def test_free_tier_row_lands_with_zero_cost(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """AC #7: free-tier model writes the row, contributes 0 to aggregate."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record_usage(
                db_session,
                ctx,
                _build_usage(
                    cost_cents=0,
                    api_model_id="google/gemma-3-27b-it:free",
                ),
                clock=clock,
            )
            db_session.flush()

            rows = (
                db_session.execute(
                    select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws.id)
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].cost_cents == 0
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 0  # Still zero — free-tier.
        finally:
            reset_current(token)

    def test_default_attempt_collides_with_first_write(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Caller that forgets ``attempt`` lands on attempt=0 twice → dedup.

        The idempotency triple is ``(workspace_id, correlation_id,
        attempt)`` and :data:`LlmUsage.attempt` defaults to 0. A
        caller that issues two *distinct* writes with the same
        correlation id but forgets to bump ``attempt`` is effectively
        claiming they are retries of the same logical call. The
        second write is a no-op — the unique catches it and the
        ledger stays at the first cost.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            correlation_id = new_ulid()
            # Both calls omit ``attempt`` — default 0 applies.
            record_usage(
                db_session,
                ctx,
                _build_usage(cost_cents=10, correlation_id=correlation_id),
                clock=clock,
            )
            db_session.flush()
            record_usage(
                db_session,
                ctx,
                _build_usage(cost_cents=15, correlation_id=correlation_id),
                clock=clock,
            )
            db_session.flush()

            rows = (
                db_session.execute(
                    select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws.id)
                )
                .scalars()
                .all()
            )
            # Only the first wrote; second hit the unique.
            assert len(rows) == 1
            assert rows[0].cost_cents == 10
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 10
        finally:
            reset_current(token)

    def test_record_usage_writes_no_audit_log_row(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """``record_usage`` is telemetry, not a state change — no audit row.

        Per §11 "Agent audit trail": the caller that triggered the
        LLM call (an agent action, admin console prompt, digest
        worker) writes one audit_log row describing the action.
        Writing a second one here would double-count the row in the
        /admin/audit feed. Refusals likewise don't hit audit_log
        (§11 "At-cap behaviour"): they're operational telemetry, not
        state changes.

        This test pins the negative invariant so a future
        over-eager audit write would fail loudly.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record_usage(
                db_session,
                ctx,
                _build_usage(cost_cents=20),
                clock=clock,
            )
            db_session.flush()
            audit_rows = (
                db_session.execute(
                    select(AuditLog).where(AuditLog.workspace_id == ws.id)
                )
                .scalars()
                .all()
            )
            assert audit_rows == [], (
                "record_usage must not write audit rows; caller owns "
                f"the audit trail. Got {[r.action for r in audit_rows]}"
            )
        finally:
            reset_current(token)

    def test_check_budget_refusal_writes_no_audit_log_row(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """§11 "At-cap behaviour": refusals do NOT hit audit_log.

        INFO log only (asserted in ``test_refuses_when_sum_exceeds_cap``).
        This case pins the absence of the audit row so a regression
        that tries to "remember every refusal" gets caught here.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500, spent_cents=495)
        token = set_current(ctx)
        try:
            with pytest.raises(BudgetExceeded):
                check_budget(
                    db_session,
                    ctx,
                    capability="chat.manager",
                    projected_cost_cents=10,
                    clock=clock,
                )
            db_session.flush()
            audit_rows = (
                db_session.execute(
                    select(AuditLog).where(AuditLog.workspace_id == ws.id)
                )
                .scalars()
                .all()
            )
            assert audit_rows == []
        finally:
            reset_current(token)

    def test_check_budget_missing_ledger_logs_warning_once(
        self,
        db_session: Session,
        clock: FrozenClock,
        caplog: pytest.LogCaptureFixture,
        allow_propagated_log_capture: object,
    ) -> None:
        """Fail-closed path: one WARNING + one INFO on the refusal.

        A busy workspace without a ledger shouldn't spam the log
        with millions of WARNING + INFO pairs, but the first call
        must log loudly so the operator surface can flag the
        seeding bug.
        """
        allow_propagated_log_capture("app.domain.llm.budget")  # type: ignore[operator]
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        token = set_current(ctx)
        try:
            with (
                caplog.at_level(logging.INFO, logger="app.domain.llm.budget"),
                pytest.raises(BudgetExceeded),
            ):
                check_budget(
                    db_session,
                    ctx,
                    capability="chat.manager",
                    projected_cost_cents=1,
                    clock=clock,
                )
            warnings = [
                r for r in caplog.records if r.message == "llm.budget.ledger_missing"
            ]
            infos = [r for r in caplog.records if r.message == "llm.budget_exceeded"]
            # Exactly one of each for this refusal — the WARNING
            # flags the seeding bug, the INFO tracks the refusal.
            assert len(warnings) == 1
            assert len(infos) == 1
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# warm_start_aggregate / refresh_aggregate — worker hooks
# ---------------------------------------------------------------------------


class TestAggregateRefresh:
    """AC #4: 30-day window + worker refresh."""

    def test_warm_start_sums_last_30_days(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """AC #4: a 31-day-old call does NOT contribute."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            # Seed three usage rows at different ages.
            now = clock.now()
            _seed_usage_row(
                db_session,
                workspace_id=ws.id,
                cost_cents=10,
                created_at=now - timedelta(days=1),
            )
            _seed_usage_row(
                db_session,
                workspace_id=ws.id,
                cost_cents=20,
                created_at=now - timedelta(days=15),
            )
            # 31-day-old row — outside the window.
            _seed_usage_row(
                db_session,
                workspace_id=ws.id,
                cost_cents=99,
                created_at=now - timedelta(days=31),
            )
            db_session.flush()

            new_total = warm_start_aggregate(db_session, ctx, clock=clock)
            db_session.flush()

            assert new_total == 30  # 10 + 20; the 31-day-old row dropped.
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 30
        finally:
            reset_current(token)

    def test_warm_start_skips_refused_rows(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Refused rows never contribute to the aggregate (belt-and-braces)."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            now = clock.now()
            _seed_usage_row(
                db_session,
                workspace_id=ws.id,
                cost_cents=15,
                created_at=now - timedelta(hours=1),
                status="ok",
            )
            _seed_usage_row(
                db_session,
                workspace_id=ws.id,
                cost_cents=100,  # Shouldn't matter — refused.
                created_at=now - timedelta(hours=2),
                status="refused",
            )
            db_session.flush()

            new_total = warm_start_aggregate(db_session, ctx, clock=clock)
            assert new_total == 15
        finally:
            reset_current(token)

    def test_warm_start_is_idempotent(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Running :func:`warm_start_aggregate` twice doesn't double-count.

        The function re-reads the last 30 days of :class:`LlmUsage`
        and writes the sum back to ``spent_cents`` (overwrite, not
        increment). A second run on the same window must land the
        same total — process restart loops / HA failover hand-off
        can both trigger this path, and either doubling or accidental
        zeroing would mis-state the budget.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            now = clock.now()
            _seed_usage_row(
                db_session,
                workspace_id=ws.id,
                cost_cents=40,
                created_at=now - timedelta(hours=2),
            )
            db_session.flush()

            first = warm_start_aggregate(db_session, ctx, clock=clock)
            db_session.flush()
            second = warm_start_aggregate(db_session, ctx, clock=clock)
            db_session.flush()

            assert first == second == 40
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 40
        finally:
            reset_current(token)

    def test_warm_start_after_midnight_sums_last_30_days(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Startup right after midnight — window is ``now - 30d`` sharp.

        The window is anchored on :func:`Clock.now()` with a
        timedelta, so midnight is a non-event: the same trailing 30
        days applies whether the process started at 23:59 or 00:01.
        Seeds a row at ``now - 30 days + 1 minute`` (just inside the
        window) and another at ``now - 30 days - 1 minute`` (just
        outside) and confirms only the former contributes.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            now = clock.now()
            # Just inside the window by one minute.
            _seed_usage_row(
                db_session,
                workspace_id=ws.id,
                cost_cents=7,
                created_at=now - timedelta(days=WINDOW_DAYS) + timedelta(minutes=1),
            )
            # Just outside by one minute.
            _seed_usage_row(
                db_session,
                workspace_id=ws.id,
                cost_cents=99,
                created_at=now - timedelta(days=WINDOW_DAYS) - timedelta(minutes=1),
            )
            db_session.flush()

            total = warm_start_aggregate(db_session, ctx, clock=clock)
            assert total == 7
        finally:
            reset_current(token)

    def test_refresh_rolls_window_forward(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """The ledger's ``period_start / period_end`` track the rolling window."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(
            db_session,
            workspace_id=ws.id,
            cap_cents=500,
            # Seed with a stale window — we'll advance the clock and
            # assert refresh rewrites it to match.
            period_start=_PINNED - timedelta(days=40),
            period_end=_PINNED - timedelta(days=10),
        )
        token = set_current(ctx)
        try:
            # Advance the clock by 5 days. The refresh should point the
            # ledger's window at [now-30d, now).
            clock.advance(timedelta(days=5))
            refresh_aggregate(db_session, ctx, clock=clock)
            db_session.flush()

            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            # SQLite strips the tzinfo on round-trip (it stores naive
            # text); Postgres preserves it. Compare the naive form so
            # the assertion works on both backends — the UTC invariant
            # is enforced by the ``DateTime(timezone=True)`` column on
            # the Postgres side and by the clock's own UTC contract on
            # the write side.
            expected_end = clock.now().replace(tzinfo=None)
            expected_start = (clock.now() - timedelta(days=WINDOW_DAYS)).replace(
                tzinfo=None
            )
            actual_end = ledger.period_end.replace(tzinfo=None)
            actual_start = ledger.period_start.replace(tzinfo=None)
            assert actual_end == expected_end
            assert actual_start == expected_start
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Demo seed value — AC #8
# ---------------------------------------------------------------------------


class TestDemoSeed:
    """AC #8: demo workspaces seed cap = 10 cents (0.10 USD).

    The workspace-create handler isn't wired yet; this test asserts
    the demo-seed *value* via a helper so the eventual handler
    change is caught by the test when it lands. Tracked in the
    cd-irng follow-up Beads task.
    """

    def test_demo_cap_is_ten_cents(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        ws = seed_workspace(db_session, slug="demo-ws")
        ctx = build_context(ws.id, slug="demo-ws")
        # Simulate the demo seed: cap_cents = 10 (0.10 USD, §11 demo
        # overrides / §24).
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=10)
        token = set_current(ctx)
        try:
            # A 1-cent call passes.
            check_budget(
                db_session,
                ctx,
                capability="chat.manager",
                projected_cost_cents=1,
                clock=clock,
            )
            # An 11-cent call does not.
            with pytest.raises(BudgetExceeded):
                check_budget(
                    db_session,
                    ctx,
                    capability="chat.manager",
                    projected_cost_cents=11,
                    clock=clock,
                )
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Concurrency — property-style atomicity check
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("CREWDAY_TEST_DB", "sqlite").lower() != "postgres",
    reason=(
        "SQLite's SAVEPOINT fixture + single-file DB serialises writers "
        "by construction — the Postgres shard is the real atomicity "
        "check; tracked by cd-irng's test plan."
    ),
)
class TestConcurrency:
    """N concurrent :func:`record_usage` calls don't drift the aggregate."""

    @pytest.fixture
    def concurrency_setup(self, engine: Engine) -> Iterator[tuple[str, str]]:
        """Seed a workspace + ledger in its own transaction, committed.

        Concurrent writers need a committed baseline to race against —
        the per-test SAVEPOINT fixture would rollback everything at
        teardown, so we use a separate committed transaction and clean
        it up by hand.
        """
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        install_tenant_filter(factory)
        session = factory()
        try:
            with tenant_agnostic():
                ws = seed_workspace(session)
                session.commit()
                ws_id = ws.id
            ctx = build_context(ws_id)
            token = set_current(ctx)
            try:
                _seed_ledger(session, workspace_id=ws_id, cap_cents=100_000)
                session.commit()
            finally:
                reset_current(token)
            yield ws_id, ctx.audit_correlation_id
        finally:
            # Sweep — delete the workspace row so the row CASCADE wipes
            # the ledger + usage children. Tenant-agnostic since we're
            # hitting the anchor.
            from app.adapters.db.workspace.models import Workspace

            with tenant_agnostic():
                session.execute(
                    Workspace.__table__.delete().where(Workspace.id == ws_id)
                )
                session.commit()
            session.close()

    def test_fifty_concurrent_calls_converge(
        self,
        engine: Engine,
        concurrency_setup: tuple[str, str],
        clock: FrozenClock,
    ) -> None:
        """50 concurrent :func:`record_usage` calls → ledger = 50 * cost."""
        ws_id, _audit = concurrency_setup
        N = 50
        per_call_cost = 7

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        install_tenant_filter(factory)

        errors: list[BaseException] = []

        def worker(i: int) -> None:
            session = factory()
            try:
                ctx = build_context(ws_id)
                token = set_current(ctx)
                try:
                    record_usage(
                        session,
                        ctx,
                        _build_usage(
                            cost_cents=per_call_cost,
                            correlation_id=f"CORR-{i:03d}",
                            attempt=0,
                        ),
                        clock=clock,
                    )
                    session.commit()
                finally:
                    reset_current(token)
            except BaseException as exc:
                # Propagate to the main thread — join() can't surface
                # worker exceptions directly, so we capture them in a
                # list the main thread asserts on.
                errors.append(exc)
                session.rollback()
            finally:
                session.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"workers raised: {errors!r}"

        verify = factory()
        try:
            ctx = build_context(ws_id)
            token = set_current(ctx)
            try:
                ledger = verify.execute(
                    select(BudgetLedger).where(BudgetLedger.workspace_id == ws_id)
                ).scalar_one()
                assert ledger.spent_cents == N * per_call_cost
                rows = (
                    verify.execute(
                        select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws_id)
                    )
                    .scalars()
                    .all()
                )
                assert len(rows) == N
            finally:
                reset_current(token)
        finally:
            verify.close()

    def test_concurrent_retries_idempotent(
        self,
        engine: Engine,
        concurrency_setup: tuple[str, str],
        clock: FrozenClock,
    ) -> None:
        """N workers racing the same triple → exactly one row + one bump."""
        ws_id, _audit = concurrency_setup
        N = 20
        shared_correlation = "CORR-SHARED"
        per_call_cost = 13

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        install_tenant_filter(factory)

        errors: list[BaseException] = []

        def worker() -> None:
            session = factory()
            try:
                ctx = build_context(ws_id)
                token = set_current(ctx)
                try:
                    record_usage(
                        session,
                        ctx,
                        _build_usage(
                            cost_cents=per_call_cost,
                            correlation_id=shared_correlation,
                            attempt=0,
                        ),
                        clock=clock,
                    )
                    session.commit()
                finally:
                    reset_current(token)
            except BaseException as exc:
                # Propagate to the main thread — join() can't surface
                # worker exceptions directly, so we capture them in a
                # list the main thread asserts on.
                errors.append(exc)
                session.rollback()
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"workers raised: {errors!r}"

        verify = factory()
        try:
            ctx = build_context(ws_id)
            token = set_current(ctx)
            try:
                rows = (
                    verify.execute(
                        select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws_id)
                    )
                    .scalars()
                    .all()
                )
                assert len(rows) == 1  # Exactly one — unique caught the rest.
                ledger = verify.execute(
                    select(BudgetLedger).where(BudgetLedger.workspace_id == ws_id)
                ).scalar_one()
                assert ledger.spent_cents == per_call_cost  # One bump.
            finally:
                reset_current(token)
        finally:
            verify.close()


# ---------------------------------------------------------------------------
# cd-wjpl — agent-trail telemetry fields round-trip through record_usage
# ---------------------------------------------------------------------------


class TestNewTelemetryFields:
    """cd-wjpl: the six new optional columns land on the ORM row verbatim.

    Pins the write contract between the domain :class:`LlmUsage` and
    the ORM :class:`LlmUsageRow` — the cd-wjpl migration added
    ``assignment_id`` / ``fallback_attempts`` / ``finish_reason`` /
    ``actor_user_id`` / ``token_id`` / ``agent_label`` and
    :func:`record_usage` now carries them through.
    """

    def test_all_telemetry_fields_round_trip(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Every new cd-wjpl column reads back the value written."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            usage = LlmUsage(
                prompt_tokens=100,
                completion_tokens=50,
                cost_cents=12,
                provider_model_id="01HWA00000000000000000MDLA",
                api_model_id="01HWA00000000000000000MDLA",
                assignment_id="01HWA00000000000000000ASGN",
                capability="chat.manager",
                correlation_id="01HWA00000000000000000CR01",
                attempt=1,
                status="ok",
                latency_ms=420,
                fallback_attempts=2,
                finish_reason="stop",
                actor_user_id="01HWA00000000000000000USR0",
                token_id="01HWA00000000000000000TOK0",
                agent_label="manager-chat",
            )
            record_usage(db_session, ctx, usage, clock=clock)
            db_session.flush()

            row = db_session.execute(
                select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws.id)
            ).scalar_one()
            assert row.assignment_id == "01HWA00000000000000000ASGN"
            assert row.fallback_attempts == 2
            assert row.finish_reason == "stop"
            assert row.actor_user_id == "01HWA00000000000000000USR0"
            assert row.token_id == "01HWA00000000000000000TOK0"
            assert row.agent_label == "manager-chat"
        finally:
            reset_current(token)

    def test_defaults_keep_backwards_compatibility(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """A caller that omits the new fields still writes cleanly.

        :class:`LlmUsage` defaults every cd-wjpl field to a null-safe
        value (``0`` for ``fallback_attempts``, ``None`` for the
        four nullable columns). Pre-cd-wjpl callers that haven't
        been updated must still round-trip — the /admin/usage feed
        tolerates NULL on every new column.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            # ``_build_usage`` does not set any of the new fields —
            # exercises the default path end-to-end.
            record_usage(
                db_session,
                ctx,
                _build_usage(cost_cents=3),
                clock=clock,
            )
            db_session.flush()

            row = db_session.execute(
                select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws.id)
            ).scalar_one()
            assert row.fallback_attempts == 0
            assert row.finish_reason is None
            assert row.actor_user_id is None
            assert row.token_id is None
            assert row.agent_label is None
            # Non-empty ``_build_usage`` assignment_id stays populated.
            assert row.assignment_id == "01HWA00000000000000000ASGN"
        finally:
            reset_current(token)

    def test_cost_cents_must_be_zero_when_status_is_error(self) -> None:
        """cd-z8h1: non-zero cost on ``status="error"`` trips the invariant.

        :func:`record_usage` bumps the ledger by ``cost_cents`` on
        every non-refused status — an accidental non-zero cost on a
        terminal failure would silently inflate the 30-day meter
        (§11 "Cost tracking" never bills a provider failure). The
        invariant is enforced at construction so the offending call
        site surfaces in the traceback rather than a line inside the
        shared helper.
        """
        with pytest.raises(ValueError, match="cost_cents must be 0"):
            LlmUsage(
                prompt_tokens=10,
                completion_tokens=0,
                cost_cents=5,  # non-zero on an error — must trip.
                provider_model_id="x",
                api_model_id="x",
                assignment_id="",
                capability="chat.manager",
                correlation_id="corr",
                attempt=0,
                status="error",
            )

    def test_cost_cents_must_be_zero_when_status_is_timeout(self) -> None:
        """Same invariant applies to ``status="timeout"``."""
        with pytest.raises(ValueError, match="cost_cents must be 0"):
            LlmUsage(
                prompt_tokens=10,
                completion_tokens=0,
                cost_cents=3,
                provider_model_id="x",
                api_model_id="x",
                assignment_id="",
                capability="chat.manager",
                correlation_id="corr",
                attempt=0,
                status="timeout",
            )

    def test_cost_cents_must_be_zero_when_status_is_refused(self) -> None:
        """And to ``status="refused"`` — refusals never bill."""
        with pytest.raises(ValueError, match="cost_cents must be 0"):
            LlmUsage(
                prompt_tokens=0,
                completion_tokens=0,
                cost_cents=1,
                provider_model_id="x",
                api_model_id="x",
                assignment_id="",
                capability="chat.manager",
                correlation_id="corr",
                attempt=0,
                status="refused",
            )

    def test_zero_cost_on_non_ok_status_passes(self) -> None:
        """Zero cost on a terminal failure is the documented convention.

        Asserts the invariant does not false-positive the common
        terminal-error path the /admin/usage feed relies on.
        """
        # No exception — `cost_cents=0` on error / timeout / refused
        # is the documented write shape.
        for status in ("error", "timeout", "refused"):
            LlmUsage(
                prompt_tokens=0,
                completion_tokens=0,
                cost_cents=0,
                provider_model_id="x",
                api_model_id="x",
                assignment_id="",
                capability="chat.manager",
                correlation_id="corr",
                attempt=0,
                status=cast(UsageStatus, status),
            )

    def test_non_zero_cost_on_ok_status_passes(self) -> None:
        """Non-zero cost on a successful call is the billable happy path."""
        usage = LlmUsage(
            prompt_tokens=10,
            completion_tokens=5,
            cost_cents=7,
            provider_model_id="x",
            api_model_id="x",
            assignment_id="",
            capability="chat.manager",
            correlation_id="corr",
            attempt=0,
            status="ok",
        )
        assert usage.cost_cents == 7

    def test_empty_assignment_id_coerces_to_null(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Empty-string ``assignment_id`` lands as NULL on the ORM row.

        The domain dataclass uses empty-string as the "resolver
        bypassed" sentinel (§11 "Deployment-scope capabilities" +
        admin smoke path); the DB column contract is NULL. The
        adapter coerces so the /admin/usage query's
        ``WHERE assignment_id IS NULL`` picks up the bypass cleanly.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            usage = LlmUsage(
                prompt_tokens=0,
                completion_tokens=0,
                cost_cents=0,
                provider_model_id="01HWA00000000000000000MDLA",
                api_model_id="01HWA00000000000000000MDLA",
                assignment_id="",  # sentinel
                capability="chat.manager",
                correlation_id=new_ulid(),
                attempt=0,
                status="ok",
            )
            record_usage(db_session, ctx, usage, clock=clock)
            db_session.flush()

            row = db_session.execute(
                select(LlmUsageRow).where(LlmUsageRow.workspace_id == ws.id)
            ).scalar_one()
            assert row.assignment_id is None
        finally:
            reset_current(token)
