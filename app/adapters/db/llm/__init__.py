"""llm — model_assignment / agent_token / approval_request / llm_usage / budget_ledger.

All five tables in this package are workspace-scoped: each row
carries a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A bare
read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

This cd-cm5 slice lands the minimum-viable DB shape the §11 agent /
LLM layer needs at the workspace edge: the capability → model
binding, delegated agent tokens, the HITL approval queue, per-call
usage ledger, and the rolling-period budget ledger. The spec's
richer normalised model — deployment-scope ``llm_provider`` /
``llm_model`` / ``llm_provider_model`` registry, per-call
``llm_call`` table, aggregated ``llm_usage_daily`` rollups, full
``agent_action`` state machine — lands in follow-up migrations
without breaking this slice's write contract.

FK hygiene mirrors the rest of the app:

* ``workspace_id`` → ``workspace.id`` with ``ondelete='CASCADE'`` on
  every row — sweeping a workspace sweeps its agent configuration
  (§15 export worker snapshots first).
* ``AgentToken.delegating_user_id`` →
  ``ApprovalRequest.requester_actor_id`` →
  ``ApprovalRequest.decided_by`` → ``user.id`` with
  ``ondelete='SET NULL'`` — a user hard-delete must not nuke the
  audit trail; rows survive with a NULL identity pointer and the
  domain layer reads the denormalised label fields downstream.
* ``ModelAssignment.model_id`` / ``LlmUsage.model_id`` are **soft
  references** — plain :class:`String` (no FK) because the
  deployment-scope ``llm_model`` registry has not yet landed. The
  service layer resolves the ULID at call time; declaring a FK now
  would force a later migration to re-wire it.

Agent tokens carry the sha256 digest of the plaintext token in
``AgentToken.hash`` (hex-encoded, 64 chars; unique across the
table to match ``api_token.hash`` and disambiguate auth-layer
lookups) and the first 6-8 chars of the plaintext in
``AgentToken.prefix`` for listing disambiguation. Service-layer
code performs the hashing; this package is DB-only. See
:class:`~app.adapters.db.llm.models.AgentToken` for the full
contract.

See ``docs/specs/02-domain-model.md`` §"LLM" and
``docs/specs/11-llm-and-agents.md`` §"Workspace usage budget",
§"Agent action approval", §"Agent audit trail".
"""

from __future__ import annotations

from app.adapters.db.llm.models import (
    AgentToken,
    ApprovalRequest,
    BudgetLedger,
    LlmCapabilityInheritance,
    LlmUsage,
    ModelAssignment,
)
from app.tenancy.registry import register

for _table in (
    "model_assignment",
    "agent_token",
    "approval_request",
    "llm_usage",
    "budget_ledger",
    "llm_capability_inheritance",
):
    register(_table)

__all__ = [
    "AgentToken",
    "ApprovalRequest",
    "BudgetLedger",
    "LlmCapabilityInheritance",
    "LlmUsage",
    "ModelAssignment",
]
