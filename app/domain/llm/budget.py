"""Rolling 30-day workspace LLM budget envelope (cd-irng).

Every LLM call is gated by a per-workspace rolling dollar envelope
that sits one layer above the per-capability daily caps (§11
"Workspace usage budget"). A noisy chat thread cannot burn another
capability's headroom because the envelope is workspace-wide; once
``cost_30d_usd + projected_call_cost > cap_usd_30d`` every
capability refuses until older calls age out of the 30-day window.

Public surface:

* :class:`LlmUsage` — frozen value carried by :func:`record_usage`.
  Distinct from :class:`app.adapters.db.llm.models.LlmUsage` (the
  ORM row) — callers pass the dataclass, the adapter writes the row.
* :class:`BudgetExceeded` — raised by :func:`check_budget` when the
  envelope would overshoot. Carries ``capability``, ``window``, and
  ``message`` shaped after the §11 JSON refusal envelope.
* :func:`estimate_cost_cents` — pricing helper. Unknown models and
  ``:free``-suffixed models both return 0 per §11 "Pricing source";
  unknown models also emit a ``WARNING`` with the workspace id so
  the admin surface can flag missing registry rows.
* :func:`check_budget` — pre-flight; raises :class:`BudgetExceeded`
  when the cached aggregate + projected cost would exceed the cap.
* :func:`record_usage` — post-flight; writes one :class:`LlmUsage`
  ORM row, bumps the ledger's ``spent_cents`` atomically, and is
  idempotent on ``(workspace_id, correlation_id, attempt)``.
* :func:`warm_start_aggregate` — called from worker bootstrap to
  materialise ``spent_cents`` from the last 30 days of ``llm_usage``.
* :func:`refresh_aggregate` — 60 s worker tick that re-sums the
  current window and writes the result back to ``budget_ledger``.

Semantic notes:

* The cap lives on :class:`~app.adapters.db.llm.models.BudgetLedger`
  (a.k.a. ``workspace_budget`` in the spec — see the docstring of
  :class:`~app.adapters.db.llm.models.BudgetLedger` and the follow-up
  note in the cd-irng test summary for the naming drift).
* Refused calls write NO ``llm_usage`` row — the meter counts only
  calls that left the client (§11 "At-cap behaviour"). Refusals log
  at ``INFO`` with ``event="llm.budget_exceeded"``; no audit row.
* ``record_usage`` on a ``status="refused"`` usage is a no-op at the
  DB level (same "refusals don't count" rule); callers of
  :func:`record_usage` that also called :func:`check_budget` should
  never reach this path, but the guard is defensive.
* Free-tier models (``:free`` suffix) still write a row for
  telemetry; ``cost_cents == 0`` so the aggregate contribution is
  zero.
* Period window: a rolling 30 days anchored on ``clock.now()`` at
  each call. The ledger row is recreated if the current window
  falls outside its ``[period_start, period_end)`` bounds, so the
  worker's 60 s refresh never trails the meter by more than that.

Concurrency:

* :func:`record_usage` wraps the ledger update in a row-level lock:
  ``SELECT ... FOR UPDATE`` on PostgreSQL, a no-op
  ``UPDATE budget_ledger SET updated_at = updated_at`` on SQLite
  (SQLite's ``BEGIN IMMEDIATE`` on the first write promotes the
  connection to RESERVED, serialising writers across the whole DB —
  the promotion is tripped by the UPDATE).
* The unique index on ``(workspace_id, correlation_id, attempt)``
  catches a concurrent retry: the second INSERT raises
  :class:`sqlalchemy.exc.IntegrityError`, which :func:`record_usage`
  swallows and returns without bumping the ledger.

Contract with the router:

* :class:`~app.domain.llm.router.ModelPick` carries
  ``api_model_id`` / ``extra_api_params`` — :func:`estimate_cost_cents`
  takes the wire name so the pricing table can key on it without
  joining back through the resolver. Callers that don't have a
  :class:`ModelPick` (e.g. a capability unassigned fallback) pass
  the raw model id they would have used.

See ``docs/specs/11-llm-and-agents.md`` §"Workspace usage budget"
§"Meter", §"Cap", §"At-cap behaviour", §"Pricing source";
``docs/specs/02-domain-model.md`` §"LLM" §"workspace_budget" /
§"workspace_usage".
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "WINDOW_DAYS",
    "WINDOW_LABEL",
    "BudgetExceeded",
    "LlmUsage",
    "PricingTable",
    "UsageStatus",
    "check_budget",
    "default_pricing_table",
    "estimate_cost_cents",
    "record_usage",
    "refresh_aggregate",
    "reset_unknown_model_dedup_for_tests",
    "warm_start_aggregate",
]


_log = logging.getLogger(__name__)


# Dedup guard for the "unknown model" WARNING. A busy workspace on a
# model we can't price would otherwise spam the log with one WARNING
# per call — cd-irng §11 "Pricing source" pins the fall-back to
# ``(0, 0)`` but the operator only needs to see the missing registry
# row once per (workspace, api_model_id) pair per process lifetime.
# The first hit logs at WARNING; subsequent hits log at DEBUG so the
# code path stays traceable without drowning the inbox. Scoped to the
# process (workers restart often enough that a stale registry fix
# propagates within one deploy window).
_UNKNOWN_MODEL_DEDUP: set[tuple[str | None, str]] = set()
_UNKNOWN_MODEL_DEDUP_LOCK = threading.Lock()


def reset_unknown_model_dedup_for_tests() -> None:
    """Clear the unknown-model dedup set (test-only seam).

    Fresh processes start with an empty set; the dedup logic below
    populates it on first-seen pairs. Tests that exercise the
    "unknown model" WARNING need a deterministic starting state so
    the first assertion actually sees the WARNING (rather than the
    deduped DEBUG from a sibling case).
    """
    with _UNKNOWN_MODEL_DEDUP_LOCK:
        _UNKNOWN_MODEL_DEDUP.clear()


# §11 pins the window to 30 days. Kept as a module-level constant so
# the test suite can reach the same value the implementation uses
# without re-deriving it from the spec.
WINDOW_DAYS: Final[int] = 30

# Label shipped on the :class:`BudgetExceeded` payload and logged
# alongside refusal events. Matches the §11 JSON refusal envelope's
# ``window`` field ("30d_rolling").
WINDOW_LABEL: Final[str] = "30d_rolling"


# Enum body mirrors :data:`app.adapters.db.llm.models._LLM_USAGE_STATUS_VALUES`.
# Kept narrow on the domain side so callers can't slip an unknown
# string past the type checker; the adapter CHECK body is the final
# enforcement.
UsageStatus = Literal["ok", "error", "refused", "timeout"]


# Pricing table: ``api_model_id`` → ``(input_cents_per_million,
# output_cents_per_million)``. The per-million denomination matches
# OpenRouter / OpenAI-compatible wire conventions; storing cents
# (not dollars) sidesteps decimal / rounding hazards across SQLite
# + PG (same rationale as the DB cap / spent columns).
#
# An unknown key returns ``(0, 0)`` and logs a WARNING at call time
# (§11 "Pricing source"). A ``:free``-suffixed model is short-
# circuited to ``(0, 0)`` without a warning — the meter records the
# row for telemetry but the cost contribution is zero.
PricingTable = dict[str, tuple[int, int]]


def default_pricing_table() -> PricingTable:
    """Return an empty pricing table.

    The deployment-scope ``llm_provider_model`` registry (§11
    "Provider / model / provider-model registry") has not yet
    landed. Until it does, callers inject their own table; the
    default empty map treats every model as unknown, which falls
    through to ``(0, 0)`` per §11 "Pricing source" and logs a
    WARNING so the operator surface can flag the missing rows.
    """
    return {}


class BudgetExceeded(Exception):
    """Raised by :func:`check_budget` when the envelope would overshoot.

    The shape mirrors the §11 JSON refusal envelope (``error``,
    ``capability``, ``window``, ``message``) so the API layer can
    serialise the exception verbatim without a projection step. The
    FastAPI exception handler (cd-6bcl / future ``app/api/errors.py``
    entry) surfaces this as HTTP 402 ``budget_exceeded`` by calling
    :meth:`to_dict`.

    Convention parallels :class:`~app.domain.llm.router.
    CapabilityUnassignedError` (bare :class:`Exception` subclass with
    structured attributes) rather than :class:`~app.domain.errors.
    DomainError`, because the 402 refusal doesn't share the RFC 7807
    problem+json envelope: §11 "At-cap behaviour" pins its own JSON
    shape. When the ``DomainError`` seam eventually absorbs LLM
    refusals, :meth:`to_dict` maps directly into the ``extra`` slot.
    """

    __slots__ = ("capability", "message_text", "window", "workspace_id")

    def __init__(
        self,
        *,
        capability: str,
        workspace_id: str,
        message: str = (
            "Workspace agent budget exceeded. Agents will resume as "
            "older calls age out."
        ),
    ) -> None:
        super().__init__(message)
        self.capability = capability
        self.workspace_id = workspace_id
        self.window = WINDOW_LABEL
        self.message_text = message

    def to_dict(self) -> dict[str, Any]:
        """Return the §11 JSON refusal envelope as a plain dict.

        Keys: ``error`` (constant ``"budget_exceeded"``),
        ``capability``, ``window``, ``message``. The API seam wraps
        this in a 402 ``PaymentRequired`` response body verbatim; the
        CLI prints ``message`` as the user-facing line.

        ``workspace_id`` is deliberately NOT included — the caller's
        session already knows which workspace it's talking to, and
        surfacing the ULID over the wire leaks a tenant identifier
        with no downstream value (§15 "Privacy").
        """
        return {
            "error": "budget_exceeded",
            "capability": self.capability,
            "window": self.window,
            "message": self.message_text,
        }


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """Post-flight usage record — the domain value :func:`record_usage` accepts.

    Distinct from :class:`app.adapters.db.llm.models.LlmUsage` — that
    class is the ORM row; this one is the seam callers construct. The
    adapter translates between the two.

    * ``prompt_tokens`` / ``completion_tokens`` — provider-reported
      actual counts (never the estimated pre-flight numbers).
    * ``cost_cents`` — re-computed from current pricing at write time
      via :func:`estimate_cost_cents` or equivalent. Storing cents
      (not dollars) sidesteps decimal / rounding hazards across
      SQLite + PG. **Must be ``0`` when ``status != "ok"``** —
      see "Cost / status invariant" below.
    * ``latency_ms`` — wall time between request-out and body-in, as
      measured by the adapter.
    * ``api_model_id`` / ``provider_model_id`` — the wire name / the
      deployment-scope ``llm_provider_model`` ULID the call resolved
      to. Today both carry the same ULID per
      :class:`~app.domain.llm.router.ModelPick` (the registry
      follow-up will split them).
    * ``assignment_id`` — the :class:`~app.adapters.db.llm.models.
      ModelAssignment` row this rung was resolved from. Empty string
      means "resolver bypassed" (e.g. admin smoke test) — the adapter
      writes it as ``NULL`` so the DB column contract (NULL = bypass)
      lines up with the spec; the ledger update still fires so the
      operator sees the spend.
    * ``capability`` — the §11 capability key
      (``chat.manager`` / ``receipt_ocr`` / …).
    * ``correlation_id`` — ties related calls across a logical
      operation. Paired with ``attempt`` on the idempotency unique.
    * ``attempt`` — 0 = first attempt, bumped on every fallback rung.
    * ``status`` — ``"ok" | "error" | "refused" | "timeout"``.
      ``"refused"`` writes nothing (refusals don't count per §11).

    cd-wjpl telemetry fields (all default-safe so callers that haven't
    been updated keep round-tripping):

    * ``fallback_attempts`` — how many prior rungs failed before this
      one succeeded. 0 = first-rung success. Matches §11 "LLMResult"
      ``fallback_attempts`` contract; the /admin/usage feed surfaces
      this verbatim for the operator's "was I on the primary?" view.
    * ``finish_reason`` — the provider's free-form reason string
      (``stop`` / ``length`` / ``content_filter`` / ``tool_calls`` /
      …). NULL when the call produced no body (timeout / transport
      error).
    * ``actor_user_id`` — the delegating user (§11 "Agent audit
      trail"). NULL for service-initiated calls (daily digest worker,
      health check).
    * ``token_id`` — the delegated API token used. NULL for
      passkey-session calls.
    * ``agent_label`` — short human label (``manager-chat``,
      ``expenses-autofill``, …). Denormalised off
      :attr:`~app.adapters.db.llm.models.AgentToken.label` so a
      /admin/usage readout doesn't need the join. NULL when the call
      carries no agent context.

    Cost / status invariant:

    * :func:`record_usage` bumps the ledger's ``spent_cents`` by
      ``cost_cents`` unconditionally on non-refused statuses. Before
      cd-z8h1 the "callers pass ``cost_cents=0`` on terminal errors"
      rule lived only in the docstring, so a future caller that
      forgot would silently inflate the 30-day meter. The invariant
      is now enforced at construction: if ``status`` is anything
      other than ``"ok"`` (``"error"`` / ``"timeout"`` / ``"refused"``)
      the dataclass raises :class:`ValueError` unless
      ``cost_cents == 0``. §11 "Cost tracking" never bills a provider
      failure; the /admin/usage row still lands for visibility, but
      the meter stays on the set of "successful calls" the spec
      describes.
    * Edge case: a provider that reports partial usage on a terminal
      timeout (socket died mid-stream) should be carried on the
      ``status="ok"`` row the adapter already wrote for the
      partial-body path, not on a second ``status="timeout"`` row.
      The /admin/usage feed's ``finish_reason != "stop"`` filter
      still catches the "this looked like an error" case without the
      meter double-counting.
    """

    prompt_tokens: int
    completion_tokens: int
    cost_cents: int
    provider_model_id: str
    api_model_id: str
    assignment_id: str
    capability: str
    correlation_id: str
    attempt: int
    status: UsageStatus
    latency_ms: int = 0
    # cd-wjpl agent-trail telemetry. Defaults keep pre-cd-wjpl
    # callers (e.g. the digest worker smoke path) compiling without
    # a change — the DB columns are nullable / server-defaulted.
    fallback_attempts: int = 0
    finish_reason: str | None = None
    actor_user_id: str | None = None
    token_id: str | None = None
    agent_label: str | None = None

    def __post_init__(self) -> None:
        """Enforce the cost / status invariant — see class docstring.

        ``record_usage`` bumps the ledger by ``cost_cents`` on every
        non-refused status; a caller that sets a non-zero cost on a
        terminal failure would silently inflate the 30-day meter. The
        §11 "Cost tracking" convention pins the caller to zero on
        ``error`` / ``timeout`` / ``refused`` — this check turns the
        docstring hope into a type-level invariant so the next caller
        who reads neither docstring still can't mis-bill a failure.

        Raised eagerly at construction (not later in
        :func:`record_usage`) so the offending call site shows up in
        the traceback rather than a line inside the shared helper.
        """
        if self.status != "ok" and self.cost_cents != 0:
            raise ValueError(
                f"LlmUsage.cost_cents must be 0 when status={self.status!r}; "
                f"got cost_cents={self.cost_cents}. §11 'Cost tracking' never "
                f"bills a non-successful call against the workspace's 30-day "
                f"meter. Pass cost_cents=0 on error / timeout / refused."
            )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def estimate_cost_cents(
    prompt_tokens: int,
    max_output_tokens: int,
    *,
    api_model_id: str,
    pricing: PricingTable,
    workspace_id: str | None = None,
) -> int:
    """Estimate the call's cost in cents.

    Cost = ``(prompt * input_per_million + output * output_per_million)
    / 1_000_000`` with the conventional "banker's round" the spec
    pins to cent resolution. Unknown models fall back to ``(0, 0)``
    per §11 "Pricing source" and log a WARNING so the admin surface
    can flag the missing registry entry — the WARNING carries the
    ``workspace_id`` (when known) as structured context for log
    aggregation.

    Free-tier models (``:free`` suffix on OpenRouter, §11 demo mode)
    short-circuit to zero without a warning; the demo deployment
    seeds these deliberately and a warning per call would drown the
    operator inbox.

    Returns an :class:`int` — the domain layer keeps cents throughout
    to match the DB representation (``cap_cents``, ``spent_cents``,
    :class:`LlmUsage.cost_cents`).
    """
    if api_model_id.endswith(":free"):
        # Free-tier short-circuit. Log at DEBUG so operators can
        # confirm the free-tier code path is firing (spec §11 "Demo
        # mode" seeds these deliberately, and an occasional DEBUG
        # line is enough for the "are my demo workspaces hitting
        # ``:free`` models?" question). A WARNING per call would
        # drown the operator inbox on a deliberately-free deployment.
        _log.debug(
            "llm.pricing.free_tier",
            extra={
                "api_model_id": api_model_id,
                "workspace_id": workspace_id,
            },
        )
        return 0
    prices = pricing.get(api_model_id)
    if prices is None:
        # Dedup the WARNING on ``(workspace_id, api_model_id)`` so a
        # workspace hammering an unregistered model logs once per
        # process lifetime instead of once per call. The first miss
        # stays at WARNING (operators want a loud signal for the
        # missing registry row); subsequent misses drop to DEBUG so
        # the code path is still traceable when diagnosing a
        # misdirected budget number.
        dedup_key = (workspace_id, api_model_id)
        with _UNKNOWN_MODEL_DEDUP_LOCK:
            seen = dedup_key in _UNKNOWN_MODEL_DEDUP
            if not seen:
                _UNKNOWN_MODEL_DEDUP.add(dedup_key)
        if seen:
            _log.debug(
                "llm.pricing.unknown_model",
                extra={
                    "api_model_id": api_model_id,
                    "workspace_id": workspace_id,
                    "deduped": True,
                },
            )
        else:
            _log.warning(
                "llm.pricing.unknown_model",
                extra={
                    "api_model_id": api_model_id,
                    "workspace_id": workspace_id,
                },
            )
        return 0
    input_per_million, output_per_million = prices
    # Integer maths: rely on Python's true-div + int() to floor; the
    # spec pins cent resolution so sub-cent remainders are discarded.
    # Multiplying first keeps precision high enough that the floor is
    # always within one cent of the real cost.
    total = prompt_tokens * input_per_million + max_output_tokens * output_per_million
    return total // 1_000_000


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


def _window_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Return the ``[period_start, period_end)`` bounds of the 30d window.

    Rolling window anchored on ``now``; ``period_end`` is ``now``
    itself so the ledger row covers the exact trailing 30 days.
    Kept behind a helper so callers don't re-derive the arithmetic
    in three places and let a latent off-by-one slip in.
    """
    period_end = now
    period_start = now - timedelta(days=WINDOW_DAYS)
    return period_start, period_end


def _sum_usage_cents(
    session: Session,
    *,
    workspace_id: str,
    window_start: datetime,
) -> int:
    """Sum ``llm_usage.cost_cents`` from ``window_start`` to now.

    Skips rows with ``status="refused"`` — refusals don't count per
    §11 "At-cap behaviour", and ``record_usage`` skips writing them,
    but the filter is belt-and-braces for historical data.
    """
    stmt = select(func.coalesce(func.sum(LlmUsageRow.cost_cents), 0)).where(
        LlmUsageRow.workspace_id == workspace_id,
        LlmUsageRow.created_at >= window_start,
        LlmUsageRow.status != "refused",
    )
    result = session.execute(stmt).scalar_one()
    # ``coalesce(sum, 0)`` returns Decimal on PG; cast to int so the
    # return type is stable across backends.
    return int(result)


def _load_ledger_row(
    session: Session,
    *,
    workspace_id: str,
) -> BudgetLedger | None:
    """Return the most-recent ledger row for ``workspace_id``.

    The unique index on ``(workspace_id, period_start, period_end)``
    permits more than one row — the worker rewrites the window on
    each tick — but only the most recent one tracks the current 30
    days. Sort by ``period_end`` descending so a caller that just
    wrote a new window picks up the fresh row immediately.
    """
    stmt = (
        select(BudgetLedger)
        .where(BudgetLedger.workspace_id == workspace_id)
        .order_by(BudgetLedger.period_end.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _lock_ledger_row(
    session: Session,
    *,
    ledger_id: str,
) -> None:
    """Acquire a write lock on the ledger row for ``ledger_id``.

    Cross-dialect primitive, same pattern as
    :func:`app.domain.identity._owner_guard.count_owner_members_locked`:

    * **PostgreSQL**: ``SELECT 1 FROM budget_ledger WHERE id = :id FOR
      UPDATE`` takes a row-level lock that survives until the caller
      commits or rolls back.
    * **SQLite**: a no-op ``UPDATE budget_ledger SET updated_at =
      updated_at WHERE id = :id`` promotes the connection from SHARED
      to RESERVED, serialising writers across the whole DB via the
      driver's default ``busy_timeout``.

    Helper never commits — the caller owns the transaction boundary;
    releasing the lock mid-transaction would re-open the TOCTOU
    window between the aggregate read and the ``spent_cents`` bump.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(
            select(BudgetLedger.id)
            .where(BudgetLedger.id == ledger_id)
            .with_for_update()
        )
        return
    # SQLite (and any other non-PG dialect the test shard might add).
    # The no-op self-assignment triggers the write-lock promotion
    # without touching the meaningful columns.
    session.execute(
        update(BudgetLedger)
        .where(BudgetLedger.id == ledger_id)
        .values(updated_at=BudgetLedger.updated_at)
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def check_budget(
    session: Session,
    ctx: WorkspaceContext,
    *,
    capability: str,
    projected_cost_cents: int,
    clock: Clock | None = None,
) -> None:
    """Pre-flight check: raise :class:`BudgetExceeded` when the cap is near.

    Reads the ledger's cached ``spent_cents`` (refreshed by the
    worker every 60 s) plus this call's ``projected_cost_cents``; if
    the sum strictly exceeds ``cap_cents``, refuse. ``==`` passes
    through so the cap is inclusive (a call that exactly hits the
    cap is allowed; the next one overshoots and fails).

    A workspace without a ledger row is treated as "cap = 0" + a
    WARNING — refusing every call is the fail-closed outcome, and
    logging loudly lets the operator notice the missing seed. The
    workspace-create handler is expected to insert the row in the
    same transaction as the workspace (§11 "Cap"); until that
    landing ships, callers must seed the row explicitly (tests) or
    accept that an un-seeded workspace refuses every LLM call.

    Refusals log at ``INFO`` with ``event="llm.budget_exceeded"``
    per §11 "At-cap behaviour"; no audit row — the refusal is
    operational telemetry, not a state change.
    """
    c = clock if clock is not None else SystemClock()
    ledger = _load_ledger_row(session, workspace_id=ctx.workspace_id)
    if ledger is None:
        # Fail closed — the missing ledger row is a seeding bug, not
        # a "no cap means unlimited" signal. Log LOUDLY at WARNING so
        # the operator surface can flag the workspace for a manual
        # seed step.
        _log.warning(
            "llm.budget.ledger_missing",
            extra={
                "workspace_id": ctx.workspace_id,
                "capability": capability,
            },
        )
        _log_refusal(
            workspace_id=ctx.workspace_id,
            capability=capability,
            spent_cents=0,
            cap_cents=0,
            projected_cost_cents=projected_cost_cents,
            clock=c,
        )
        raise BudgetExceeded(
            capability=capability,
            workspace_id=ctx.workspace_id,
        )

    spent = ledger.spent_cents
    cap = ledger.cap_cents
    # Strict `>` keeps the cap inclusive: a call that exactly hits
    # the cap is allowed. The next call overshoots and fails.
    if spent + projected_cost_cents > cap:
        _log_refusal(
            workspace_id=ctx.workspace_id,
            capability=capability,
            spent_cents=spent,
            cap_cents=cap,
            projected_cost_cents=projected_cost_cents,
            clock=c,
        )
        raise BudgetExceeded(
            capability=capability,
            workspace_id=ctx.workspace_id,
        )


def _log_refusal(
    *,
    workspace_id: str,
    capability: str,
    spent_cents: int,
    cap_cents: int,
    projected_cost_cents: int,
    clock: Clock,
) -> None:
    """INFO log for a budget-refused call per §11 "At-cap behaviour"."""
    overshoot = spent_cents + projected_cost_cents - cap_cents
    _log.info(
        "llm.budget_exceeded",
        extra={
            "event": "llm.budget_exceeded",
            "workspace_id": workspace_id,
            "capability": capability,
            "window": WINDOW_LABEL,
            "spent_cents": spent_cents,
            "cap_cents": cap_cents,
            "projected_cost_cents": projected_cost_cents,
            "overshoot_cents": max(0, overshoot),
            "occurred_at": clock.now().isoformat(),
        },
    )


def record_usage(
    session: Session,
    ctx: WorkspaceContext,
    usage: LlmUsage,
    *,
    clock: Clock | None = None,
) -> None:
    """Post-flight: write the usage row and bump the ledger aggregate.

    Idempotent on ``(workspace_id, correlation_id, attempt)``: a
    retry that carries the same triple is a no-op. The unique index
    ``uq_llm_usage_workspace_correlation_attempt`` catches the
    concurrent insert at the DB level — the second writer's
    ``IntegrityError`` is swallowed here and the ledger is NOT
    bumped, so the aggregate stays exact under retry storms.

    Refused calls (``status="refused"``) short-circuit without
    writing — refusals don't count per §11 "At-cap behaviour". The
    caller that already went through :func:`check_budget` never
    reaches this path for a refusal; the guard is defensive against
    a bypass path that minted a refused row.

    Concurrency: the ledger row is locked before the ``spent_cents``
    update so two concurrent writers can't read the same value and
    both write ``spent + cost``. On PostgreSQL this is a row-level
    ``SELECT ... FOR UPDATE``; on SQLite the write-lock promotion
    achieves the same serialisation.
    """
    if usage.status == "refused":
        # §11 "At-cap behaviour": refusals do not write llm_usage
        # because the call never left the client. The meter counts
        # only calls that left the client. A caller that nevertheless
        # reaches this branch has bypassed :func:`check_budget` — log
        # at DEBUG so the path is traceable without alerting.
        _log.debug(
            "llm.usage.refused_skipped",
            extra={
                "workspace_id": ctx.workspace_id,
                "capability": usage.capability,
                "correlation_id": usage.correlation_id,
            },
        )
        return

    c = clock if clock is not None else SystemClock()
    now = c.now()

    # 1) Attempt the INSERT inside a nested SAVEPOINT so a duplicate
    #    (unique violation on ``(workspace_id, correlation_id,
    #    attempt)``) can be rolled back WITHOUT losing any work the
    #    caller has already done on the session. Using the top-level
    #    ``session.rollback()`` here would unwind the caller's
    #    outer transaction — fatal under the SAVEPOINT-per-test
    #    pattern, and painful in production where the budget helper
    #    sits inside a larger domain transaction (write audit row +
    #    bump ledger + write usage).
    # ``assignment_id`` is a required string on the domain dataclass
    # (empty = "resolver bypassed") but the DB column is nullable —
    # coerce empty to NULL so the column contract ("NULL = bypass")
    # lines up with the spec and with the downstream /admin/usage
    # filter logic. cd-wjpl agent-trail fields are already typed
    # ``str | None`` on the domain side and pass straight through.
    row = LlmUsageRow(
        id=new_ulid(clock=c),
        workspace_id=ctx.workspace_id,
        capability=usage.capability,
        model_id=usage.api_model_id,
        tokens_in=usage.prompt_tokens,
        tokens_out=usage.completion_tokens,
        cost_cents=usage.cost_cents,
        latency_ms=usage.latency_ms,
        status=usage.status,
        correlation_id=usage.correlation_id,
        attempt=usage.attempt,
        assignment_id=usage.assignment_id or None,
        fallback_attempts=usage.fallback_attempts,
        finish_reason=usage.finish_reason,
        actor_user_id=usage.actor_user_id,
        token_id=usage.token_id,
        agent_label=usage.agent_label,
        created_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        # Duplicate ``(workspace_id, correlation_id, attempt)``: the
        # other writer landed first. The SAVEPOINT has already been
        # rolled back by the context manager; the session stays
        # usable. Return as a no-op — the first writer's row stands,
        # their ledger bump stands, and our caller should observe
        # "already recorded".
        _log.debug(
            "llm.usage.duplicate_ignored",
            extra={
                "workspace_id": ctx.workspace_id,
                "capability": usage.capability,
                "correlation_id": usage.correlation_id,
                "attempt": usage.attempt,
            },
        )
        return

    # 2) Bump the ledger aggregate under a row lock. The lock covers
    #    the read-modify-write so two concurrent writers don't both
    #    read ``spent`` and write ``spent + cost`` (the classic
    #    read-modify-write hazard).
    ledger = _load_ledger_row(session, workspace_id=ctx.workspace_id)
    if ledger is None:
        # No ledger row → the workspace was never seeded. The INSERT
        # above still stands (for telemetry), but we can't bump
        # anything. Log at WARNING so the operator spots the missing
        # seed; :func:`check_budget` already logs the same event on
        # the refusal path, so a non-zero usage row without a ledger
        # is the signal.
        _log.warning(
            "llm.budget.ledger_missing_on_record",
            extra={
                "workspace_id": ctx.workspace_id,
                "capability": usage.capability,
                "cost_cents": usage.cost_cents,
            },
        )
        return

    _lock_ledger_row(session, ledger_id=ledger.id)

    session.execute(
        update(BudgetLedger)
        .where(BudgetLedger.id == ledger.id)
        .values(
            spent_cents=BudgetLedger.spent_cents + usage.cost_cents,
            updated_at=now,
        )
    )


def warm_start_aggregate(
    session: Session,
    ctx: WorkspaceContext,
    *,
    clock: Clock | None = None,
) -> int:
    """Recompute the ledger's ``spent_cents`` from the last 30 days.

    Called once at worker bootstrap so a process restart doesn't
    leave the cached aggregate stale. Re-reads every non-refused
    :class:`~app.adapters.db.llm.models.LlmUsage` row in the 30-day
    window and writes the sum back to the most recent ledger row.

    Returns the new ``spent_cents`` so the caller can log it or
    cross-check against expectations.

    Creates a ledger row if none exists? **No.** The workspace-create
    handler is the sole seed path (§11 "Cap"); materialising one
    here would mask the seeding bug a warm-start on a fresh workspace
    should surface. The caller sees a zero return and logs a
    WARNING instead.
    """
    c = clock if clock is not None else SystemClock()
    now = c.now()
    window_start, _ = _window_bounds(now)
    total = _sum_usage_cents(
        session, workspace_id=ctx.workspace_id, window_start=window_start
    )
    ledger = _load_ledger_row(session, workspace_id=ctx.workspace_id)
    if ledger is None:
        _log.warning(
            "llm.budget.ledger_missing_on_warm_start",
            extra={
                "workspace_id": ctx.workspace_id,
                "computed_cents": total,
            },
        )
        return 0
    session.execute(
        update(BudgetLedger)
        .where(BudgetLedger.id == ledger.id)
        .values(spent_cents=total, updated_at=now)
    )
    return total


def refresh_aggregate(
    session: Session,
    ctx: WorkspaceContext,
    *,
    clock: Clock | None = None,
) -> int:
    """60 s worker tick: re-sum ``llm_usage`` and write back to the ledger.

    Structurally identical to :func:`warm_start_aggregate` (both do
    the same read + write) — kept as a separate name so the worker
    scheduler reads as two distinct tasks (bootstrap vs periodic)
    and so a future refactor can tune them independently (e.g. a
    delta-based refresh that skips the full sum when no rows have
    arrived since the last tick).

    Also rolls the ledger's ``[period_start, period_end)`` bounds
    forward: after the window has moved, the ledger row's recorded
    window should match ``now - 30d → now`` so a downstream reader
    (admin UI) sees a coherent window label.

    **Not yet wired into the scheduler** — tracked by cd-ca1k. Until
    that lands, the cached aggregate is only bumped by the
    :func:`record_usage` post-flight path. Callers that rely on
    the out-of-band refresh (to sweep crashes before the bump, or
    to age out rows as they fall off the window) should call this
    function directly.
    """
    c = clock if clock is not None else SystemClock()
    now = c.now()
    window_start, window_end = _window_bounds(now)
    total = _sum_usage_cents(
        session, workspace_id=ctx.workspace_id, window_start=window_start
    )
    ledger = _load_ledger_row(session, workspace_id=ctx.workspace_id)
    if ledger is None:
        _log.warning(
            "llm.budget.ledger_missing_on_refresh",
            extra={
                "workspace_id": ctx.workspace_id,
                "computed_cents": total,
            },
        )
        return 0
    session.execute(
        update(BudgetLedger)
        .where(BudgetLedger.id == ledger.id)
        .values(
            spent_cents=total,
            period_start=window_start,
            period_end=window_end,
            updated_at=now,
        )
    )
    return total
