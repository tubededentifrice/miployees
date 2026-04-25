"""SQLAlchemy models for the LLM / agent layer (cd-cm5).

Defines the five workspace-scoped tables that back §11's agent and
LLM plumbing from the DB side:

* :class:`ModelAssignment` — capability → model map, one row per
  ``(workspace_id, capability)`` pair.
* :class:`AgentToken` — delegated agent tokens (one row per minted
  token). ``hash`` stores the **sha256** digest of the plaintext
  token; ``prefix`` carries the first 6-8 chars of the plaintext so
  the ``/me/tokens`` listing can disambiguate without revealing the
  secret. The service layer does the hashing — this module is DB
  only.
* :class:`ApprovalRequest` — human-in-the-loop agent-action
  approval queue. ``action_json`` is a JSON blob (pydantic-validated
  at the service layer).
* :class:`LlmUsage` — per-call usage ledger (tokens, cost, latency,
  status, correlation id).
* :class:`BudgetLedger` — rolling-period spend ledger (one row per
  ``(workspace_id, period_start, period_end)``).

The spec (§02 "LLM" and §11) lands a richer normalised model in a
later slice (``llm_provider`` / ``llm_model`` / ``llm_provider_model``
/ ``llm_assignment`` / ``llm_call`` / ``llm_usage_daily`` / etc.) —
this cd-cm5 v1 slice is the minimum-viable workspace-scoped shape
that covers the five columns the Beads task pins. The richer surface
lands via follow-up migrations without breaking this slice's write
contract.

As of cd-4btd this module also ships the deployment-scope
:class:`LlmProvider` / :class:`LlmModel` / :class:`LlmProviderModel`
registry. :attr:`ModelAssignment.model_id` is now a real FK to
:attr:`LlmProviderModel.id` (``ondelete="RESTRICT"``) so the §11
resolver can surface the provider's wire name
(:attr:`LlmProviderModel.api_model_id`) on
:class:`~app.domain.llm.router.ModelPick` without a second lookup.
:attr:`LlmUsage.model_id`, by contrast, stays a free-form wire-name
string so historical rows survive a registry row's retirement —
see the class docstring's spec-drift note.

FK hygiene (see the package ``__init__`` docstring for the full
rationale):

* ``workspace_id`` CASCADE on every row — sweeping a workspace
  sweeps its agent configuration.
* ``delegating_user_id`` / ``decided_by`` / ``requester_actor_id``
  SET NULL — history survives a user hard-delete; the audit trail
  ships with the denormalised identity columns downstream code
  depends on.

See ``docs/specs/02-domain-model.md`` §"LLM",
``docs/specs/11-llm-and-agents.md`` §"Workspace usage budget",
§"Agent action approval".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``user.id`` / ``workspace.id``
# FKs below resolve against ``Base.metadata`` only if the target
# packages have been imported, so we register them here as a side
# effect.
from app.adapters.db.identity import models as _identity_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = [
    "AgentToken",
    "ApprovalRequest",
    "BudgetLedger",
    "LlmCapabilityInheritance",
    "LlmModel",
    "LlmProvider",
    "LlmProviderModel",
    "LlmUsage",
    "ModelAssignment",
]


# Allowed ``approval_request.status`` values. Matches the §11 HITL
# flow (``pending → approved | rejected | timed_out``). The spec's
# richer ``agent_action.state`` machine (``pending | approved |
# rejected | expired | executed``) lands with a follow-up; the v1
# slice collapses ``expired → timed_out`` (name pinned by the Beads
# task) and does not yet materialise the post-approval
# ``executed`` terminal state. Widening is additive.
_APPROVAL_REQUEST_STATUS_VALUES: tuple[str, ...] = (
    "pending",
    "approved",
    "rejected",
    "timed_out",
)

# Allowed ``llm_usage.status`` values.
#
# The spec (``docs/specs/02-domain-model.md`` §"LLM",
# ``docs/specs/11-llm-and-agents.md``) carries a richer normalised
# ``llm_call`` table with a ``success: bool`` column and a free-form
# ``finish_reason`` (``stop | length | safety | tool_call | error``);
# it does not name a closed ``status`` enum on a workspace-scoped
# ``llm_usage`` table. The Beads task (cd-cm5) therefore authorises
# the DB-only adapter to pick an enum body explicitly and document
# the choice: the four values below partition the observable
# outcomes of an attempted call in a way the §11 budget envelope and
# /admin/usage surfaces can pivot on:
#
# * ``ok`` — the call left the client and the provider returned a
#   usable body (``finish_reason in {stop, length, tool_call}``).
# * ``error`` — the call left the client but the provider returned
#   an error body, an adapter-level HTTP failure, or a transport
#   failure that the chain did not classify as a timeout.
# * ``refused`` — the provider emitted an explicit content-refusal
#   (``finish_reason = safety`` / equivalent) **or** our own budget
#   envelope refused the call pre-flight. The §11 budget-refused
#   path still writes a ledger row (the spec lets refusals skip
#   ``llm_call`` — this slice keeps them for audit telemetry).
# * ``timeout`` — transport / provider deadline hit without a body.
#
# New status values land with an additive migration (CHECK body
# rewrite via ``batch_alter_table`` on SQLite, direct ALTER on PG).
_LLM_USAGE_STATUS_VALUES: tuple[str, ...] = (
    "ok",
    "error",
    "refused",
    "timeout",
)


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Matches the convention used by every sibling module (``tasks``,
    ``instructions``, ``places``, ``payroll``, ``messaging``, …).
    """
    return "'" + "', '".join(values) + "'"


class ModelAssignment(Base):
    """Capability → model binding for a workspace.

    A capability may carry **many** assignments, forming a priority-
    ordered fallback chain — the §11 resolver walks the chain on
    retryable failures (provider 5xx, 429, timeout, provider content
    refusal, transport error). Lower ``priority`` is tried first; 0 is
    the primary. The cd-cm5 v1 slice pinned one row per
    ``(workspace_id, capability)``; cd-u84y replaces that with the
    composite ``(workspace_id, capability, priority)`` shape the
    §11-pinned resolver and the v1 `LLMAssignment` API surface both
    depend on.

    Reassigning the primary is an UPDATE on the ``priority=0`` row (or
    an insert-then-reorder through the bulk reorder API); deletion
    reverts the capability to the deployment-level default pulled from
    the §11 assignment chain and, failing that, the inheritance parent
    in :class:`LlmCapabilityInheritance`.

    ``capability`` is a plain :class:`~sqlalchemy.String` because
    the §11 capability catalogue (``receipt_ocr``, ``nl_task_intake``,
    ``daily_digest``, ``staff_chat``, …) is a closed enum in code but
    grows over time; widening it as a CHECK body would force a
    migration on every capability addition. The service layer
    narrows the string to a :class:`Literal` on read.

    ``provider`` carries the provider name that serves this
    assignment (``openrouter``, ``openai_compatible``, ``fake``, …) —
    denormalised off the spec's ``llm_provider`` registry so a
    readout of the workspace's assignments does not need to join
    back through the deployment-scope provider table (which lands in
    a later slice).

    ``model_id`` is a real FK to :class:`LlmProviderModel.id` — the
    deployment-scope registry that decouples the canonical model
    name from the provider's wire name (§11 "``llm_provider_model``").
    ``ondelete="RESTRICT"`` matches the spec's protect semantics: an
    operator must explicitly migrate the workspace's assignments
    before the registry row can disappear, otherwise the resolver
    would silently lose its capability chain. The cd-4btd migration
    converted the prior soft reference (a plain ``String(26)`` with
    no FK) into this real edge.

    Per-call tuning columns (``max_tokens`` / ``temperature`` /
    ``extra_api_params``) match the spec's ``llm_assignment`` shape
    (§11 "Model assignment"). ``max_tokens`` / ``temperature`` are
    nullable so a NULL means "inherit the model default";
    ``extra_api_params`` is a JSON blob the adapter merges last over
    the provider-model defaults.

    ``required_capabilities`` is a JSON list copied from the §11
    capability catalogue entry on save — the admin UI warns when an
    operator binds a model that lacks a required sub-capability
    (``vision``, ``json_mode``, …). Empty list = no constraints.

    ``enabled`` lets an operator hold an assignment in the chain
    without activating it; the §11 resolver skips disabled rows. When
    every assignment for a capability is disabled, the resolver falls
    through to :class:`LlmCapabilityInheritance` and then raises
    ``CapabilityUnassignedError`` (§11 "Capability inheritance").

    FK hygiene: ``workspace_id`` CASCADE — sweeping a workspace
    sweeps its assignments.
    """

    __tablename__ = "model_assignment"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Capability key from the §11 catalogue. See class docstring for
    # why this is a plain string rather than a CHECK-clamped enum.
    capability: Mapped[str] = mapped_column(String, nullable=False)
    # Real FK to ``llm_provider_model.id`` (cd-4btd). The resolver
    # joins through this row to surface :attr:`LlmProviderModel.
    # api_model_id` (the provider's wire name) on
    # :class:`~app.domain.llm.router.ModelPick`. RESTRICT on the
    # deletion path: deleting a registry row that an active
    # assignment still points at would silently strand the workspace
    # without a chain — operators migrate the assignment first.
    model_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("llm_provider_model.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Provider name (denormalised). Short string; the enum is open
    # in practice because new providers land as pure data rows.
    provider: Mapped[str] = mapped_column(String, nullable=False)
    # Lower = tried first; 0 = primary. CHECK ``>= 0`` is sanity — a
    # negative priority would silently sort ahead of the primary and
    # break every downstream reorder invariant.
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # ``False`` hides the row from the §11 resolver without removing
    # it from the chain — the admin UI renders a disabled row as
    # "paused". Every assignment disabled → resolver raises
    # ``CapabilityUnassignedError`` (§11 "Failure modes"). The ORM
    # ``default=True`` covers the Python-side insert path; the
    # ``server_default=true()`` mirrors the migration's ``sa.true()``
    # so ``Base.metadata.create_all()`` (dev scratch paths) and the
    # alembic autogenerate loop agree on the DDL — and so raw SQL
    # inserts that bypass the ORM still land ``TRUE`` on PG / ``1`` on
    # SQLite without a dialect-specific literal.
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    # Per-call caps. Both nullable = inherit the provider-model /
    # model default.
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Extra provider-layer params (``top_p``, ``frequency_penalty``,
    # tool / function-call hints, …). Merged last over the provider-
    # model defaults at call time. Empty mapping default matches the
    # ``agent_token.scope_json`` / ``approval_request.action_json``
    # pattern — the bare-string ``server_default`` round-trips on
    # SQLite + PG without a dialect-specific literal.
    extra_api_params: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    # Required sub-capability tags the model must expose
    # (``vision``, ``json_mode``, …). Copied from the §11 capability
    # catalogue on save. Empty list = no constraints.
    required_capabilities: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # Defensive CHECK: ``priority`` is a sort key; a negative
        # would silently sort ahead of the primary and break every
        # reorder invariant. The reorder API keeps priorities dense
        # (0, 1, 2, …); this guard survives a buggy direct-insert
        # path the API doesn't own.
        CheckConstraint("priority >= 0", name="priority_non_negative"),
        # Sorted scan: ``(workspace_id, capability, priority)`` backs
        # the §11 resolver's "enabled assignments for this capability,
        # in priority order" query. Non-unique — multiple assignments
        # per ``(workspace, capability)`` is the whole point of this
        # slice. The composite's leading ``workspace_id`` carries the
        # tenant filter; per-capability lookup still rides the same
        # index's ``(workspace_id, capability)`` prefix.
        Index(
            "ix_model_assignment_workspace_capability_priority",
            "workspace_id",
            "capability",
            "priority",
        ),
    )


class AgentToken(Base):
    """Delegated agent token — one row per mint.

    A user (the *delegating* user) mints a short-lived token for one
    of their embedded chat agents to call on their behalf (§11,
    §03). The token string itself never lands in the DB: we store
    the **sha256** digest of the plaintext in :attr:`hash`, and the
    first 6-8 chars of the plaintext (opaque, unbruteforceable on
    its own) in :attr:`prefix` so ``GET /me/tokens`` can disambiguate
    rows for the user without revealing the secret. The hashing
    contract lives at the service layer — this model is DB-only.

    ``scope_json`` carries the token's scope set (matches §03's
    ``api_token.scopes`` shape); empty on a freshly-minted admin
    override, populated on the common scoped-agent case.

    ``expires_at`` is non-null — delegated tokens always carry a
    TTL per §03; the worker sweeps expired rows (``revoked_at`` is
    distinct — it marks an explicit user-initiated revocation that
    predates the TTL).

    FK hygiene:

    * ``workspace_id`` CASCADE — sweeping a workspace sweeps its
      delegated tokens.
    * ``delegating_user_id`` SET NULL — a user hard-delete must not
      nuke the token history (audit trail survives). The domain
      layer never reads an orphan delegating_user_id; the row is
      retained for audit only.
    """

    __tablename__ = "agent_token"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    delegating_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Human label ("manager-chat-agent", "worker-chat-agent", …).
    # Denormalised onto ``audit_log.agent_label`` per §11.
    label: Mapped[str] = mapped_column(String, nullable=False)
    # First 6-8 chars of the plaintext token. Stored so the
    # ``/me/tokens`` listing can disambiguate rows; opaque on its own.
    prefix: Mapped[str] = mapped_column(String, nullable=False)
    # sha256 digest of the plaintext token — hex-encoded, exactly 64
    # chars. The service layer performs the hashing; DB layer just
    # stores. ``unique=True`` mirrors the sibling :class:`ApiToken.
    # hash` pattern (§03 "Principles") — a collision would mean the
    # auth layer's hash-keyed lookup cannot disambiguate two rows;
    # the DB enforces the invariant regardless of which codepath
    # minted the row. sha256's collision space makes an accidental
    # duplicate essentially impossible, so the cost of the unique is
    # a one-time B-tree entry at mint time.
    hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # Scope set — matches §03's ``api_token.scopes`` shape. The
    # outer ``Any`` is scoped to SQLAlchemy's JSON column type —
    # callers writing a typed payload should use a TypedDict locally
    # and coerce into this column.
    scope_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Set on explicit user-initiated revocation (before ``expires_at``).
    # NULL while the token is live. The listing query can filter on
    # ``revoked_at IS NULL`` cheaply because the composite index below
    # has ``revoked_at`` trailing.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bumped on every successful agent call. Drives the §11 Agent
    # Activity view's "last seen" column and dead-token sweep.
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # "Look up a token by prefix within a workspace" — the
        # listing + revocation hot path. A partial ``WHERE
        # revoked_at IS NULL`` would be cheaper but is not portably
        # expressible across SQLite + PG at the Alembic layer
        # without per-dialect ``_where`` kwargs; the unrestricted
        # composite is sufficient for v1 volumes. The index also
        # carries the tenant filter on its leading ``workspace_id``.
        Index(
            "ix_agent_token_workspace_prefix",
            "workspace_id",
            "prefix",
        ),
    )


class ApprovalRequest(Base):
    """Human-in-the-loop agent-action approval row.

    Rows land in ``pending`` state when the agent hits a gated
    action (workspace-policy always-gated, workspace-policy
    configurable, per-user approval mode, §11 "Agent action
    approval"). A human reviewer decides via a passkey session (or
    a PAT with ``approvals:act``, §11) and the row transitions to
    ``approved`` / ``rejected``; the worker transitions
    ``pending → timed_out`` when the TTL passes without a decision.

    ``action_json`` is a free-form JSON blob — the pydantic schema
    at the service layer validates the shape (resolved URL, method,
    body, idempotency key) per §11's ``agent_action.resolved_
    payload_json``. Storing it as JSON here lets the spec evolve
    without a migration per field addition.

    ``rationale_md`` is the reviewer's optional free-form note
    attached to a decision; mirrors §11's ``decision_note_md``.

    FK hygiene:

    * ``workspace_id`` CASCADE — sweeping a workspace sweeps its
      approval queue.
    * ``requester_actor_id`` / ``decided_by`` SET NULL — a user
      hard-delete must not nuke approval history. The denormalised
      fields on the audit_log row downstream of the decision
      preserve the identity for display.
    """

    __tablename__ = "approval_request"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    requester_actor_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Free-form action payload — resolved URL, method, body,
    # idempotency key, action verb. Pydantic-validated at the
    # service layer. The outer ``Any`` is scoped to SQLAlchemy's
    # JSON column type — callers writing a typed payload should use
    # a TypedDict locally and coerce into this column.
    action_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # ``pending | approved | rejected | timed_out``. See
    # ``_APPROVAL_REQUEST_STATUS_VALUES``.
    status: Mapped[str] = mapped_column(String, nullable=False)
    decided_by: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Optional reviewer note — markdown per the sibling convention.
    rationale_md: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_in_clause(_APPROVAL_REQUEST_STATUS_VALUES)})",
            name="status",
        ),
        # "Pending-queue pagination" — the /approvals desk's hot
        # path. Tenant filter rides the leading ``workspace_id``;
        # ``status`` carries the equality filter (``= 'pending'``);
        # ``created_at`` carries the ordering (oldest first).
        Index(
            "ix_approval_request_workspace_status_created",
            "workspace_id",
            "status",
            "created_at",
        ),
    )


class LlmUsage(Base):
    """Per-call usage ledger — tokens, cost, latency, outcome.

    One row per attempted LLM call (including chain retries). The
    ``(workspace_id, created_at)`` and
    ``(workspace_id, capability, created_at)`` composite indexes
    power the /admin/usage feed + per-capability breakdowns; the
    cd-wjpl ``(workspace_id, actor_user_id, created_at)`` index
    serves the delegating-user filter.

    ``correlation_id`` ties related calls together across a logical
    operation (a single digest run may issue three calls — ledger
    rows share one correlation id). Matches §11's ``llm_call.
    correlation_id`` semantics without duplicating the spec's
    richer normalised shape; the full ``llm_call`` table lands in
    a later slice.

    ``tokens_in`` / ``tokens_out`` are the provider's reported token
    counts; ``cost_cents`` is the crew.day-computed dollar estimate
    snapped to the nearest cent (storing cents avoids decimal /
    rounding hazards across SQLite + PG). ``latency_ms`` is the
    adapter-measured wall time between request-out and body-in.

    **Spec-drift note on ``model_id``.** The column is named
    ``model_id`` here but the §02 ``llm_call`` spec names the
    equivalent column ``provider_model_id`` — and unlike
    :attr:`ModelAssignment.model_id` (a real FK as of cd-4btd) it
    still holds the provider's free-form **wire name string**, not
    the ``llm_provider_model.id``. The cd-wjpl / cd-irng post-flight
    writers populate it with
    :attr:`~app.domain.llm.budget.LlmUsage.api_model_id` — the
    string that flowed across the network — so historical rows
    survive a registry row's deletion. Promoting this column to an
    FK + rename is tracked as a follow-up Beads task; the surface is
    intentionally append-only because the /admin/usage feed must
    still render past calls even after their provider_model row has
    been retired.

    cd-wjpl telemetry columns (all nullable — §11 "Agent audit
    trail"):

    * ``assignment_id`` — the :class:`ModelAssignment.id` rung the
      §11 resolver picked for this call. NULL means the resolver
      was bypassed (admin smoke path, deployment-scope callers).
      Soft reference — no FK so deleting an assignment doesn't
      break the historical row.
    * ``fallback_attempts`` — how many prior rungs failed before
      this one succeeded. 0 = first-rung success. Matches §11
      "LLMResult" ``fallback_attempts`` contract.
    * ``finish_reason`` — the provider's free-form finish reason
      (``stop`` / ``length`` / ``content_filter`` / ``tool_calls`` /
      …). NULL for timeout / transport-error rows that never
      produced a body. Plain string — providers ship different
      vocabularies.
    * ``actor_user_id`` — the delegating user (§11 "Agent audit
      trail"). NULL for service-initiated calls (digest worker,
      health check). Soft reference — no FK so a user hard-delete
      preserves the trail.
    * ``token_id`` — the delegated API token id. NULL for
      passkey-session calls. Soft reference.
    * ``agent_label`` — short human label (``manager-chat``,
      ``expenses-autofill``, …) denormalised off
      :attr:`AgentToken.label`. NULL when the call carries no
      agent context.

    FK hygiene: ``workspace_id`` CASCADE. No user / token FK — the
    history survives a user hard-delete or a token sweep; the
    denormalised ``agent_label`` carries the display string without
    the join.
    """

    __tablename__ = "llm_usage"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Capability key from the §11 catalogue. Plain string — see
    # :class:`ModelAssignment.capability` for the rationale.
    capability: Mapped[str] = mapped_column(String, nullable=False)
    # Resolved provider-model reference. Column name is ``model_id``
    # for cd-cm5-era compatibility; the §02 spec names the equivalent
    # ``llm_call`` column ``provider_model_id`` — see the class
    # docstring's "Spec-drift note" for why the rename is deferred.
    model_id: Mapped[str] = mapped_column(String(26), nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # ``ok | error | refused | timeout`` — see
    # ``_LLM_USAGE_STATUS_VALUES`` for the enum body + why this
    # adapter pins a closed four-value set.
    status: Mapped[str] = mapped_column(String, nullable=False)
    # Ties related calls across a logical operation. Denormalised
    # onto ``audit_log.correlation_id`` per §11. Plain string —
    # callers mint a ULID; no FK to keep this module decoupled from
    # a hypothetical future ``llm_operation`` aggregate table.
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)
    # Retry index within one logical ``(workspace_id, correlation_id)``
    # operation — 0 is the first attempt; the fallback-chain walker
    # (§11 "Failure modes") bumps this on every rung. Paired with the
    # unique on ``(workspace_id, correlation_id, attempt)`` below,
    # this turns a retried ``record_usage`` post-flight write into a
    # single row instead of a double-count on the §11 budget envelope
    # (§11 "Workspace usage budget" §"At-cap behaviour"). Default 0
    # keeps cd-cm5-era rows (pre-cd-irng) backwards-compatible.
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # cd-wjpl telemetry: the ``ModelAssignment.id`` rung the §11
    # resolver picked. NULL when the resolver was bypassed (admin
    # smoke path, deployment-scope callers). Soft reference.
    assignment_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    # cd-wjpl telemetry: 0 = first-rung success. Matches §11
    # "LLMResult" ``fallback_attempts`` contract. Server default 0
    # keeps pre-cd-wjpl rows consistent without a backfill.
    fallback_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # cd-wjpl telemetry: provider's free-form finish reason. NULL
    # when the call produced no body. Plain string — providers ship
    # different vocabularies.
    finish_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # cd-wjpl telemetry: the delegating user (§11 "Agent audit
    # trail"). NULL for service-initiated calls. Soft reference —
    # no FK so a user hard-delete preserves the trail.
    actor_user_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    # cd-wjpl telemetry: the delegated API token. NULL for
    # passkey-session calls. Soft reference.
    token_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
    # cd-wjpl telemetry: denormalised :attr:`AgentToken.label` for
    # display. NULL when the call carries no agent context.
    agent_label: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_in_clause(_LLM_USAGE_STATUS_VALUES)})",
            name="status",
        ),
        # Feed hot path: "every usage row in this workspace, newest
        # first". Tenant filter rides the leading ``workspace_id``;
        # ``created_at`` carries the ordering.
        Index(
            "ix_llm_usage_workspace_created",
            "workspace_id",
            "created_at",
        ),
        # Per-capability breakdown: "usage for ``chat.manager`` in
        # this workspace over the last 30 days". Leading
        # ``workspace_id`` carries the tenant filter, ``capability``
        # the equality filter, ``created_at`` the window predicate.
        Index(
            "ix_llm_usage_workspace_capability_created",
            "workspace_id",
            "capability",
            "created_at",
        ),
        # Idempotency guard for the cd-irng ``record_usage`` path
        # (§11 "Workspace usage budget"): a retried post-flight write
        # that carries the same ``(workspace_id, correlation_id,
        # attempt)`` tuple as an already-landed row is silently
        # deduplicated at the service layer via the unique-violation
        # catch. Workspace leads so the tenant filter rides the same
        # index's prefix.
        Index(
            "uq_llm_usage_workspace_correlation_attempt",
            "workspace_id",
            "correlation_id",
            "attempt",
            unique=True,
        ),
        # cd-wjpl: "usage for this workspace filtered by delegating
        # user, newest first" — the /admin/usage hot path. Leading
        # ``workspace_id`` rides the tenant filter; trailing
        # ``created_at`` keeps paginated scrolls cheap.
        Index(
            "ix_llm_usage_workspace_actor_created",
            "workspace_id",
            "actor_user_id",
            "created_at",
        ),
    )


class BudgetLedger(Base):
    """Rolling-period spend ledger — one row per period window.

    Matches §11's "Workspace usage budget" envelope: a single row
    per ``(workspace_id, period_start, period_end)`` tuple carrying
    the period's accumulated ``spent_cents`` against its configured
    ``cap_cents``. The worker refreshes ``spent_cents`` every 60 s
    from the aggregated :class:`LlmUsage` rows in the window; the
    pre-flight check reads the cached aggregate before deciding
    whether the next call fits under ``cap_cents``.

    Storing cents (not dollars) sidesteps decimal / rounding hazards
    across SQLite + PG — :class:`Integer` is portable and exact;
    the spec's ``numeric(8,4)`` cap maps onto cents without loss
    (5.0000 USD ↔ 500 cents).

    ``period_start`` / ``period_end`` bound the rolling window. A
    rolling-30d implementation pins ``period_end - period_start = 30
    days``; a calendar-month implementation pins month boundaries.
    The unique index on the triple prevents two ledger rows from
    overlapping the same period.

    FK hygiene: ``workspace_id`` CASCADE — sweeping a workspace
    sweeps its ledger.
    """

    __tablename__ = "budget_ledger"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    spent_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cap_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "period_end > period_start",
            name="period_end_after_start",
        ),
        # Unique: one ledger row per ``(workspace_id, period_start,
        # period_end)``. A duplicate would be a worker bug — the
        # unique prevents two parallel refreshers from silently
        # inserting divergent aggregates.
        Index(
            "uq_budget_ledger_workspace_period",
            "workspace_id",
            "period_start",
            "period_end",
            unique=True,
        ),
    )


class LlmCapabilityInheritance(Base):
    """Parent-child fallback edge between two capabilities.

    When the §11 resolver finds no enabled :class:`ModelAssignment`
    for a child capability in the active workspace, it walks one hop
    up this edge to the parent and replays the resolver against the
    parent's chain. v1 seeds one edge per deployment
    (``chat.admin → chat.manager``); operators introduce surgical
    ties as sub-capabilities appear.

    Modelled on fj2's ``LLMUseCaseInheritance``. Scoped per-workspace
    so a deployment operator's default edges do not leak into an
    operator's per-workspace overrides — the service layer composes
    the workspace edge over the deployment seed at read time.

    Constraints:

    * CHECK ``capability <> inherits_from`` — a self-loop is an
      obvious data bug. Multi-hop cycle detection is a write-path
      concern: the admin / API layer that writes this table rejects
      ``422 capability_inheritance_cycle`` before the insert reaches
      the DB.
    * Unique ``(workspace_id, capability)`` — one edge per child per
      workspace. The child has either a single parent or none.

    FK hygiene: ``workspace_id`` CASCADE — sweeping a workspace
    sweeps its override edges.
    """

    __tablename__ = "llm_capability_inheritance"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Child capability — the one that falls through when its own
    # chain is exhausted. Same open-enum rationale as
    # :attr:`ModelAssignment.capability`.
    capability: Mapped[str] = mapped_column(String, nullable=False)
    # Parent capability — replayed against this row's chain. Must also
    # be a key in the §11 capability catalogue; enforcement lives at
    # the service layer (a CHECK body would force a migration on every
    # capability addition).
    inherits_from: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # A self-loop is an obvious data bug — the child would inherit
        # from itself and the resolver would spin. Multi-hop cycle
        # detection lives at the write-path (API / admin layer).
        CheckConstraint(
            "capability <> inherits_from",
            name="no_self_loop",
        ),
        # Unique: one inheritance edge per child per workspace. A
        # second edge for the same child would force the resolver to
        # pick a parent at random.
        Index(
            "uq_llm_capability_inheritance_workspace_capability",
            "workspace_id",
            "capability",
            unique=True,
        ),
    )


# ---------------------------------------------------------------------------
# Deployment-scope registry (cd-4btd).
#
# These three tables sit outside any workspace — every workspace in the
# deployment shares the same ``llm_provider`` / ``llm_model`` /
# ``llm_provider_model`` graph, edited from ``/admin/llm`` (§11). They
# are NOT registered in :mod:`app.tenancy.registry`: the ORM tenant
# filter must NOT inject a ``workspace_id`` predicate on these reads
# because there is no such column to pin against.
# ---------------------------------------------------------------------------


# Allowed ``llm_provider.provider_type`` values per §11 v1. ``fake``
# is dev/test only — production deployments forbid it via a
# §"LLM graph admin" runtime check, but the DB CHECK keeps the column
# closed against typo-driven drift. New types land additively (CHECK
# rewrite via ``batch_alter_table`` on SQLite, direct ALTER on PG).
_LLM_PROVIDER_TYPE_VALUES: tuple[str, ...] = (
    "openrouter",
    "openai_compatible",
    "fake",
)


# Allowed ``llm_model.price_source`` values per §11 "Price sync".
# Empty string = no sync configured for this model. The override
# column ``llm_provider_model.price_source_override`` carries a
# **different** enum body per spec (``'' | 'none' | 'openrouter'``)
# because ``'none'`` is the override-pin sentinel that tells the
# weekly sync to skip the row; the model-level ``price_source`` has
# no need for a pin sentinel and instead allows ``'manual'`` to mark
# operator-curated rows. Neither column is CHECK-clamped today
# beyond the model-level body below — the override stays free-form
# until the §11 "/admin/llm" surface lands and pins the wider grid.
_LLM_PRICE_SOURCE_VALUES: tuple[str, ...] = (
    "",
    "openrouter",
    "manual",
)


class LlmProvider(Base):
    """Deployment-scope provider registry row (§11 ``llm_provider``).

    A provider is a single LLM-serving endpoint the deployment knows
    how to talk to (OpenRouter, an OpenAI-compatible self-host, or
    the in-process ``fake`` adapter for tests). Every workspace in
    the deployment shares the same provider rows — they are edited
    from the ``/admin/llm`` graph (§11 "LLM graph admin") or its
    CLI equivalents (§13).

    ``api_key_envelope_ref`` is an opaque pointer into
    ``secret_envelope`` (§15) — never the ciphertext, never the
    plaintext key. The resolver dereferences it at call time. The
    column is nullable for the ``fake`` provider (no upstream key)
    and for self-hosted gateways that don't require auth.

    ``default_model`` is a soft reference to a
    :class:`LlmProviderModel.id` — used as a fallback when an
    assignment lists none. NULL means "no default; assignments must
    name a row".

    NOT workspace-scoped. The :mod:`app.tenancy.registry` registration
    list in :mod:`app.adapters.db.llm.__init__` deliberately excludes
    this table.
    """

    __tablename__ = "llm_provider"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Display name; uniqueness is enforced at the DB level so the
    # admin UI never shows two providers with the same label.
    name: Mapped[str] = mapped_column(String, nullable=False)
    # ``openrouter | openai_compatible | fake``. CHECK clamps the
    # set; widening is additive.
    provider_type: Mapped[str] = mapped_column(String, nullable=False)
    # Optional override for the provider type's default URL. Required
    # for ``openai_compatible``; ignored for ``fake``; nullable for
    # ``openrouter`` (the default URL is good).
    api_endpoint: Mapped[str | None] = mapped_column(String, nullable=True)
    # Opaque envelope pointer — never the ciphertext.
    api_key_envelope_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft FK to ``llm_provider_model.id``. NOT a hard FK — we'd
    # otherwise have a circular dependency (``llm_provider`` ↔
    # ``llm_provider_model``) that would force tricky deferred FK
    # creation across SQLite + PG. Service layer validates on write.
    default_model: Mapped[str | None] = mapped_column(String(26), nullable=True)
    timeout_s: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    requests_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    # Lower = tried first when a provider pool is probed. The
    # resolver's chain is per-assignment; ``priority`` here ranks
    # providers when an assignment doesn't pin one.
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Last operator to mutate this row. SET NULL on user delete so a
    # user hard-delete doesn't sweep the deployment-scope registry.
    updated_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"provider_type IN ({_in_clause(_LLM_PROVIDER_TYPE_VALUES)})",
            name="provider_type",
        ),
        UniqueConstraint("name", name="uq_llm_provider_name"),
    )


class LlmModel(Base):
    """Deployment-scope provider-agnostic model metadata (§11 ``llm_model``).

    A model row carries the provider-agnostic facts about a model:
    the canonical name, capability tags, context window, output
    cap. The same canonical model (``google/gemma-3-27b-it``,
    ``anthropic/claude-3-5-sonnet``, …) may be served by multiple
    providers — the per-provider tweaks live on
    :class:`LlmProviderModel`.

    ``capabilities`` is a JSON list of capability tags (``chat``,
    ``vision``, ``json_mode``, …) the model exposes. The §11
    capability catalogue carries a ``required_capabilities`` list per
    crew.day capability; saving an assignment whose ``provider_model``
    resolves to a model missing a required tag returns
    ``422 assignment_missing_capability`` (§11 "Model capability
    tags").

    ``price_source`` controls how the price-sync job populates
    :attr:`LlmProviderModel.input_cost_per_million` /
    ``output_cost_per_million``: ``""`` = no sync, ``"openrouter"``
    = pull from OpenRouter's catalogue, ``"manual"`` = operator
    fills it in. ``price_source_model_id`` overrides the lookup id
    when the canonical name doesn't match the upstream catalogue.

    NOT workspace-scoped.
    """

    __tablename__ = "llm_model"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Globally unique canonical model name (e.g. ``google/gemma-3-27b-
    # it``). Provider-agnostic — the provider-specific wire form
    # lives on :attr:`LlmProviderModel.api_model_id`.
    canonical_name: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    # Plain string — the §11 vendor list grows over time; CHECK-
    # clamping it would force a migration on every new vendor.
    vendor: Mapped[str] = mapped_column(String, nullable=False)
    # JSON list of capability tags. Empty list = no tags claimed —
    # the model is unusable for any crew.day capability that requires
    # a sub-capability.
    capabilities: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    # ``"" | "openrouter" | "manual"`` — see class docstring.
    price_source: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    # Optional override for the price-sync lookup id. NULL = use
    # ``canonical_name``.
    price_source_model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"price_source IN ({_in_clause(_LLM_PRICE_SOURCE_VALUES)})",
            name="price_source",
        ),
        UniqueConstraint("canonical_name", name="uq_llm_model_canonical_name"),
    )


class LlmProviderModel(Base):
    """Deployment-scope (provider, model) join row (§11 ``llm_provider_model``).

    The wire-name + per-combo tuning + pricing for a (provider,
    model) pair. The same canonical model can be priced and tuned
    differently across providers — OpenRouter's ``anthropic/
    claude-3-5-sonnet`` and a native Anthropic SDK adapter's
    ``claude-3-5-sonnet-20241022`` both refer to the same
    :class:`LlmModel` but carry distinct ``api_model_id`` strings on
    the wire.

    ``api_model_id`` is what the adapter sends on the wire; the
    resolver surfaces it on :class:`~app.domain.llm.router.ModelPick.
    api_model_id` so adapters never need to second-guess the
    canonical-vs-wire distinction.

    Pricing columns are :class:`Numeric(10, 4)` so the ledger can
    carry values like ``0.0003`` (sub-cent rates per million tokens)
    without floating-point hazard. Kept in dollars-per-million for
    parity with the OpenRouter catalogue and §11; the per-call
    :attr:`~app.adapters.db.llm.models.LlmUsage.cost_cents` is
    computed at write time and stored in cents.

    The flag pair ``supports_system_prompt`` /
    ``supports_temperature`` exists because o-series and
    reasoning-first models reject those features in practice; the
    adapter consults the flags before dispatch and elides the
    rejected param rather than forcing the operator to override it
    via :attr:`extra_api_params`.

    Unique ``(provider_id, model_id)`` — at most one row per pair.
    Two rows for the same pair would force the resolver to pick at
    random.

    NOT workspace-scoped.
    """

    __tablename__ = "llm_provider_model"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("llm_provider.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("llm_model.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # The wire-form name the provider expects. Independent of
    # :attr:`LlmModel.canonical_name` — same canonical may map to
    # different ``api_model_id`` strings under different providers.
    api_model_id: Mapped[str] = mapped_column(String, nullable=False)
    input_cost_per_million: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("0"), server_default="0"
    )
    output_cost_per_million: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("0"), server_default="0"
    )
    # Reserved for future per-call billed providers. NULL = unknown /
    # not applicable.
    fixed_cost_per_call_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), nullable=True
    )
    max_tokens_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temperature_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ``False`` = adapter folds the system prompt into the first user
    # turn. Default ``True`` keeps the existing call shape for the
    # 95 % of models that do support a system prompt.
    supports_system_prompt: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    # ``False`` = adapter strips the temperature param before
    # dispatch (o-series models reject it).
    supports_temperature: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    # ``"" | "low" | "medium" | "high"`` — open enum because the
    # vocabulary varies per reasoning provider.
    reasoning_effort: Mapped[str | None] = mapped_column(String, nullable=True)
    # Catch-all for rare / new fields the adapter forwards to the
    # provider unchanged. Merged last over the model defaults.
    extra_api_params: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    # Optional per-row override of :attr:`LlmModel.price_source`. Per
    # §11 "Price sync" the allowed values are ``'' | 'none' |
    # 'openrouter'``: ``''`` falls through to the model default,
    # ``'none'`` pins the row so the weekly sync skips it, and
    # ``'openrouter'`` pulls per-row pricing even when the parent
    # model is set to manual. Stored free-form (no CHECK clamp)
    # because the /admin/llm graph is the write authority and the
    # spec's enum body widens additively; service-layer validation
    # narrows on save.
    price_source_override: Mapped[str | None] = mapped_column(String, nullable=True)
    price_source_model_id_override: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    price_last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=true(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "model_id",
            name="uq_llm_provider_model_provider_model",
        ),
    )
