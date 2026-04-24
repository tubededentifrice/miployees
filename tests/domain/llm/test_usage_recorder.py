"""Unit tests for :mod:`app.domain.llm.usage_recorder` (cd-wjpl).

The recorder is a pure orchestration seam — no I/O beyond the
delegated :func:`~app.domain.llm.budget.record_usage` call. The tests
therefore assert on the ORM row's shape (all six cd-wjpl telemetry
columns) and on the delegation contract (refusal short-circuit,
idempotency guard, fallback-attempts round-trip, attribution
modes).

Covers:

* Happy path — first-rung success, every telemetry column populated.
* Fallback success — ``fallback_attempts > 0`` round-trips; ledger
  bumps by the cost of the rung that finally worked.
* Terminal error — row written for /admin/usage feed visibility,
  ledger NOT bumped (caller passes ``cost_cents=0`` on error by
  convention — §11 "Cost tracking" never accrues a non-billable
  provider failure to the meter).
* Delegated-token attribution — all three §11 "Agent audit trail"
  fields populated.
* Passkey-session attribution — only ``actor_user_id`` populated.
* Service-initiated attribution — all three NULL (daily digest
  worker, health check).
* Idempotency — same ``(workspace_id, correlation_id, attempt)``
  tuple dedupes at the DB level (inherited from ``record_usage``).
* Refused status — defensive branch in ``record_usage`` catches it;
  the recorder does NOT crash (callers MUST NOT reach this path,
  but a bypass mustn't destabilise the session).

See ``docs/specs/11-llm-and-agents.md`` §"Client abstraction",
§"Cost tracking", §"Agent audit trail".
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.domain.llm.router import ModelPick
from app.domain.llm.usage_recorder import (
    AgentAttribution,
    RecordedCall,
    record,
)
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.domain.llm.conftest import build_context, seed_workspace

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers — recorder-specific factories
# ---------------------------------------------------------------------------


def _seed_ledger(
    session: Session,
    *,
    workspace_id: str,
    cap_cents: int,
    spent_cents: int = 0,
) -> BudgetLedger:
    """Insert a :class:`BudgetLedger` row for the 30-day envelope.

    Mirrors the helper in ``test_budget.py`` — kept local so this
    module's tests don't cross-import a private fixture. The
    workspace-create handler that should seed this in production
    hasn't landed yet (§11 "Cap"); the test suite does it by hand.
    """
    from datetime import timedelta

    from app.domain.llm.budget import WINDOW_DAYS

    row = BudgetLedger(
        id=new_ulid(),
        workspace_id=workspace_id,
        period_start=_PINNED - timedelta(days=WINDOW_DAYS),
        period_end=_PINNED,
        spent_cents=spent_cents,
        cap_cents=cap_cents,
        updated_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def _model_pick(
    *,
    provider_model_id: str = "01HWA00000000000000000MDLA",
    api_model_id: str = "01HWA00000000000000000MDLA",
    assignment_id: str = "01HWA00000000000000000ASGN",
) -> ModelPick:
    """Build a :class:`ModelPick` with sensible defaults.

    Frozen + slotted — safe to share across call sites; the
    :class:`MappingProxyType` wrapping makes the ``extra_api_params``
    read-only so a test can't mutate it and poison a sibling.
    """
    return ModelPick(
        provider_model_id=provider_model_id,
        api_model_id=api_model_id,
        max_tokens=None,
        temperature=None,
        extra_api_params=MappingProxyType({}),
        required_capabilities=(),
        assignment_id=assignment_id,
    )


def _fetch_rows(session: Session, *, workspace_id: str) -> list[LlmUsageRow]:
    """Return every ``llm_usage`` row for ``workspace_id``, attempt-asc."""
    return list(
        session.execute(
            select(LlmUsageRow)
            .where(LlmUsageRow.workspace_id == workspace_id)
            .order_by(LlmUsageRow.attempt.asc())
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Happy-path delegation
# ---------------------------------------------------------------------------


class TestRecordHappyPath:
    """Successful call with every cd-wjpl telemetry column populated."""

    def test_first_rung_success_writes_row_and_bumps_ledger(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """status="ok", fallback_attempts=0 — row + ledger both land."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            correlation_id = new_ulid()
            result = record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=0,
                correlation_id=correlation_id,
                prompt_tokens=120,
                completion_tokens=60,
                cost_cents=17,
                latency_ms=412,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(
                    actor_user_id="01HWA00000000000000000USR1",
                    token_id="01HWA00000000000000000TOK1",
                    agent_label="manager-chat",
                    agent_conversation_ref="conv-xyz",
                ),
                clock=clock,
            )
            db_session.flush()

            assert isinstance(result, RecordedCall)
            assert result.usage.correlation_id == correlation_id
            assert result.usage.status == "ok"

            rows = _fetch_rows(db_session, workspace_id=ws.id)
            assert len(rows) == 1
            row = rows[0]
            # Core shape.
            assert row.capability == "chat.manager"
            assert row.cost_cents == 17
            assert row.tokens_in == 120
            assert row.tokens_out == 60
            assert row.latency_ms == 412
            assert row.status == "ok"
            assert row.correlation_id == correlation_id
            assert row.attempt == 0
            # cd-wjpl telemetry.
            assert row.assignment_id == "01HWA00000000000000000ASGN"
            assert row.fallback_attempts == 0
            assert row.finish_reason == "stop"
            assert row.actor_user_id == "01HWA00000000000000000USR1"
            assert row.token_id == "01HWA00000000000000000TOK1"
            assert row.agent_label == "manager-chat"

            # Ledger bumped.
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 17
        finally:
            reset_current(token)

    def test_fallback_success_round_trips(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """fallback_attempts=2 — the row reflects it, ledger bumps."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=2,  # Primary + secondary failed; tertiary succeeded.
                correlation_id=new_ulid(),
                prompt_tokens=80,
                completion_tokens=30,
                cost_cents=9,
                latency_ms=1150,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(
                    actor_user_id="01HWA00000000000000000USR2",
                    token_id=None,
                    agent_label=None,
                ),
                attempt=2,
                clock=clock,
            )
            db_session.flush()

            rows = _fetch_rows(db_session, workspace_id=ws.id)
            assert len(rows) == 1
            assert rows[0].fallback_attempts == 2
            assert rows[0].attempt == 2
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 9
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Terminal-error telemetry
# ---------------------------------------------------------------------------


class TestRecordTerminalError:
    """status="error" / "timeout" — row lands for /admin/usage visibility."""

    def test_error_writes_row_with_zero_cost(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Terminal error: row written with status="error", cost=0.

        The caller passes ``cost_cents=0`` on error by convention —
        §11 "Cost tracking" never bills a provider failure. The
        ledger therefore naturally stays flat even though
        ``record_usage`` unconditionally adds ``cost_cents`` to
        ``spent_cents`` on non-refused statuses.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=3,  # Chain exhausted.
                correlation_id=new_ulid(),
                prompt_tokens=0,
                completion_tokens=0,
                cost_cents=0,  # Caller zero-costs terminal errors.
                latency_ms=30_000,
                status="error",
                finish_reason=None,  # No body → no reason.
                attribution=AgentAttribution(
                    actor_user_id="01HWA00000000000000000USR3",
                    token_id=None,
                    agent_label=None,
                ),
                attempt=3,
                clock=clock,
            )
            db_session.flush()

            rows = _fetch_rows(db_session, workspace_id=ws.id)
            assert len(rows) == 1
            assert rows[0].status == "error"
            assert rows[0].cost_cents == 0
            assert rows[0].fallback_attempts == 3
            assert rows[0].finish_reason is None
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            # Zero-cost error → ledger doesn't move.
            assert ledger.spent_cents == 0
        finally:
            reset_current(token)

    def test_timeout_writes_row_with_null_finish_reason(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """status="timeout" — same shape as error, different status body."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=1,
                correlation_id=new_ulid(),
                prompt_tokens=50,
                completion_tokens=0,
                cost_cents=0,
                latency_ms=60_000,
                status="timeout",
                finish_reason=None,
                attribution=AgentAttribution(
                    actor_user_id=None,
                    token_id=None,
                    agent_label=None,
                ),
                clock=clock,
            )
            db_session.flush()

            rows = _fetch_rows(db_session, workspace_id=ws.id)
            assert len(rows) == 1
            assert rows[0].status == "timeout"
            assert rows[0].finish_reason is None
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Attribution modes — §11 "Agent audit trail"
# ---------------------------------------------------------------------------


class TestAttributionModes:
    """Three caller shapes: delegated token, passkey session, service."""

    def test_delegated_token_populates_all_three_fields(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Token-minted delegated agent — user + token + label all written."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=0,
                correlation_id=new_ulid(),
                prompt_tokens=10,
                completion_tokens=10,
                cost_cents=1,
                latency_ms=100,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(
                    actor_user_id="u1",
                    token_id="t1",
                    agent_label="manager-chat",
                ),
                clock=clock,
            )
            db_session.flush()

            row = _fetch_rows(db_session, workspace_id=ws.id)[0]
            assert row.actor_user_id == "u1"
            assert row.token_id == "t1"
            assert row.agent_label == "manager-chat"
        finally:
            reset_current(token)

    def test_passkey_session_only_carries_user_id(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Passkey session — no token, no agent label, only user id lands."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=0,
                correlation_id=new_ulid(),
                prompt_tokens=10,
                completion_tokens=10,
                cost_cents=1,
                latency_ms=100,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(
                    actor_user_id="u1",
                    token_id=None,
                    agent_label=None,
                ),
                clock=clock,
            )
            db_session.flush()

            row = _fetch_rows(db_session, workspace_id=ws.id)[0]
            assert row.actor_user_id == "u1"
            assert row.token_id is None
            assert row.agent_label is None
        finally:
            reset_current(token)

    def test_service_initiated_call_has_null_attribution(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Digest worker / health check — every §11 actor field NULL."""
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record(
                db_session,
                ctx,
                capability="daily_digest",
                model_pick=_model_pick(),
                fallback_attempts=0,
                correlation_id=new_ulid(),
                prompt_tokens=10,
                completion_tokens=10,
                cost_cents=1,
                latency_ms=100,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(
                    actor_user_id=None,
                    token_id=None,
                    agent_label=None,
                ),
                clock=clock,
            )
            db_session.flush()

            row = _fetch_rows(db_session, workspace_id=ws.id)[0]
            assert row.actor_user_id is None
            assert row.token_id is None
            assert row.agent_label is None
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Delegation invariants — idempotency + refusal-bypass safety
# ---------------------------------------------------------------------------


class TestDelegationInvariants:
    """``record_usage``'s contract must survive the extra orchestration layer."""

    def test_same_correlation_and_attempt_dedupe(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Two ``record()`` calls with same (correlation, attempt) → one row.

        Inherited from :func:`record_usage` — asserted here so a
        future refactor that accidentally bypasses ``record_usage``'s
        SAVEPOINT trips immediately.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            correlation_id = new_ulid()
            kwargs = dict(
                session=db_session,
                ctx=ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=0,
                correlation_id=correlation_id,
                prompt_tokens=10,
                completion_tokens=10,
                cost_cents=5,
                latency_ms=100,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(None, None, None),
                attempt=0,
                clock=clock,
            )
            record(**kwargs)  # type: ignore[arg-type]
            db_session.flush()
            # Second invocation with identical triple — dedup.
            record(**kwargs)  # type: ignore[arg-type]
            db_session.flush()

            rows = _fetch_rows(db_session, workspace_id=ws.id)
            assert len(rows) == 1
            ledger = db_session.execute(
                select(BudgetLedger).where(BudgetLedger.workspace_id == ws.id)
            ).scalar_one()
            assert ledger.spent_cents == 5
        finally:
            reset_current(token)

    def test_refused_status_is_defensive_noop(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """status="refused" shouldn't reach the recorder — but if it does, no crash.

        §11 "At-cap behaviour": refusals don't write ``llm_usage``.
        The caller that went through :func:`check_budget` never
        reaches :func:`record` for a refusal; a bypass path that
        nevertheless does MUST NOT destabilise the session. The
        defensive branch in :func:`record_usage` returns early; we
        assert the row count stays at zero + the session stays
        usable (a subsequent :func:`record` call on a fresh
        correlation id still lands).
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            # Refused path — no crash, no row.
            record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=0,
                correlation_id=new_ulid(),
                prompt_tokens=0,
                completion_tokens=0,
                cost_cents=0,
                latency_ms=0,
                status="refused",
                finish_reason=None,
                attribution=AgentAttribution(None, None, None),
                clock=clock,
            )
            db_session.flush()
            assert _fetch_rows(db_session, workspace_id=ws.id) == []

            # Session still healthy — a subsequent happy-path call
            # lands cleanly.
            record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=0,
                correlation_id=new_ulid(),
                prompt_tokens=10,
                completion_tokens=10,
                cost_cents=3,
                latency_ms=50,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(None, None, None),
                clock=clock,
            )
            db_session.flush()
            rows = _fetch_rows(db_session, workspace_id=ws.id)
            assert len(rows) == 1
            assert rows[0].status == "ok"
        finally:
            reset_current(token)


# ---------------------------------------------------------------------------
# Surface invariants — frozen dataclasses, correlation-id round-trip
# ---------------------------------------------------------------------------


class TestSurfaceInvariants:
    """Public types stay frozen + slotted; attribution coercion holds."""

    def test_agent_attribution_is_frozen(self) -> None:
        """``AgentAttribution`` is hashable, immutable, slotted.

        The API layer stashes one instance per request; mutating it
        after the fact would silently corrupt the audit trail.
        """
        attribution = AgentAttribution(actor_user_id="u", token_id="t", agent_label="l")
        with pytest.raises((AttributeError, Exception)):
            attribution.actor_user_id = "other"  # type: ignore[misc]

    def test_recorded_call_is_frozen(self) -> None:
        """``RecordedCall`` is frozen — defensive against caller mutation."""
        # Build a minimal ``RecordedCall`` without a DB round-trip.
        from app.domain.llm.budget import LlmUsage

        usage = LlmUsage(
            prompt_tokens=0,
            completion_tokens=0,
            cost_cents=0,
            provider_model_id="x",
            api_model_id="x",
            assignment_id="",
            capability="c",
            correlation_id="corr",
            attempt=0,
            status="ok",
        )
        call = RecordedCall(usage=usage)
        with pytest.raises((AttributeError, Exception)):
            call.usage = usage  # type: ignore[misc]

    def test_correlation_id_round_trips_through_recorded_call(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """``RecordedCall.usage.correlation_id`` echoes the input verbatim.

        The API layer echoes this back on ``X-Correlation-Id-Echo``
        (§11 "Client abstraction"); any mutation would break the
        audit-correlation join. Reached via ``result.usage`` since
        the redundant top-level field was removed under cd-z8h1 —
        ``usage`` is the single source of truth.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            correlation_id = "01HWA00000000000000000CRRR"
            result = record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=0,
                correlation_id=correlation_id,
                prompt_tokens=1,
                completion_tokens=1,
                cost_cents=0,
                latency_ms=10,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(None, None, None),
                clock=clock,
            )
            # Single source of truth: the correlation id lives on the
            # persisted usage record. ``RecordedCall`` exposes it via
            # ``result.usage.correlation_id`` only — the redundant
            # top-level field was removed under cd-z8h1.
            assert result.usage.correlation_id == correlation_id
        finally:
            reset_current(token)

    def test_agent_attribution_coerces_empty_string_to_none(self) -> None:
        """Empty-string fields on :class:`AgentAttribution` normalise to ``None``.

        The API layer typically reads these from headers / token
        metadata; a missing header lands as ``""`` in the usual
        FastAPI pattern. Coercing at construction keeps the
        ``NULL`` semantics consistent across the /admin/usage
        filter surface — an operator should never see a "call with
        empty label" that's just a caller-side typo.
        """
        attr = AgentAttribution(
            actor_user_id="",
            token_id="",
            agent_label="",
            agent_conversation_ref="",
        )
        assert attr.actor_user_id is None
        assert attr.token_id is None
        assert attr.agent_label is None
        assert attr.agent_conversation_ref is None

    def test_agent_attribution_preserves_non_empty_and_none(self) -> None:
        """Non-empty strings and explicit ``None`` pass through unchanged.

        Guards against an over-eager coercion that would swallow a
        legit single-space label or mis-normalise a ``None``.
        """
        attr = AgentAttribution(
            actor_user_id="u1",
            token_id=None,
            agent_label="manager-chat",
        )
        assert attr.actor_user_id == "u1"
        assert attr.token_id is None
        assert attr.agent_label == "manager-chat"
        assert attr.agent_conversation_ref is None

    def test_empty_agent_label_writes_null_column(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """``agent_label=""`` on the attribution lands as DB NULL.

        End-to-end pin: the attribution coerces at construction, the
        recorder threads the normalised value into :class:`~app.
        domain.llm.budget.LlmUsage`, and :func:`record_usage` writes
        the NULL. Mirrors the ``assignment_id`` empty → NULL contract
        so a /admin/usage filter ``WHERE agent_label IS NULL`` lines
        up with the spec's "absent" semantics.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(),
                fallback_attempts=0,
                correlation_id=new_ulid(),
                prompt_tokens=5,
                completion_tokens=5,
                cost_cents=1,
                latency_ms=10,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(
                    actor_user_id="u1",
                    token_id="",  # empty — should coerce.
                    agent_label="",  # empty — should coerce.
                ),
                clock=clock,
            )
            db_session.flush()
            row = _fetch_rows(db_session, workspace_id=ws.id)[0]
            assert row.token_id is None
            assert row.agent_label is None

        finally:
            reset_current(token)

    def test_empty_assignment_id_writes_null_column(
        self, db_session: Session, clock: FrozenClock
    ) -> None:
        """Resolver-bypass path: ``ModelPick(assignment_id="")`` → DB NULL.

        The domain dataclass uses empty-string as the "bypassed"
        sentinel; the DB column contract is NULL. ``record_usage``
        coerces the empty to ``None`` so the /admin/usage query
        (``WHERE assignment_id IS NULL``) lines up with the spec.
        """
        ws = seed_workspace(db_session)
        ctx = build_context(ws.id)
        _seed_ledger(db_session, workspace_id=ws.id, cap_cents=500)
        token = set_current(ctx)
        try:
            record(
                db_session,
                ctx,
                capability="chat.manager",
                model_pick=_model_pick(assignment_id=""),
                fallback_attempts=0,
                correlation_id=new_ulid(),
                prompt_tokens=10,
                completion_tokens=10,
                cost_cents=1,
                latency_ms=50,
                status="ok",
                finish_reason="stop",
                attribution=AgentAttribution(None, None, None),
                clock=clock,
            )
            db_session.flush()
            row = _fetch_rows(db_session, workspace_id=ws.id)[0]
            assert row.assignment_id is None
        finally:
            reset_current(token)
