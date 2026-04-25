"""llm — workspace-scoped agent layer + deployment-scope registry.

This package ships two tiers of tables:

**Workspace-scoped** (``workspace_id`` column, registered in
:mod:`app.tenancy.registry`): :class:`ModelAssignment`,
:class:`AgentToken`, :class:`ApprovalRequest`, :class:`LlmUsage`,
:class:`BudgetLedger`, :class:`LlmCapabilityInheritance`. The ORM
tenant filter auto-injects a ``workspace_id`` predicate on every
SELECT / UPDATE / DELETE. A bare read without a
:class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

**Deployment-scope** (no ``workspace_id``, NOT registered):
:class:`LlmProvider`, :class:`LlmModel`, :class:`LlmProviderModel`.
Every workspace shares the same registry rows — they are edited
from the ``/admin/llm`` graph (§11 "LLM graph admin") and read by
the §11 resolver. Registering them in the workspace-scoped registry
would inject a ``workspace_id =`` predicate the column doesn't have
and break every read.

Together this is the §11 agent layer: per-workspace capability →
provider_model bindings backed by a deployment-shared provider /
model / provider_model registry, plus the HITL approval queue,
delegated tokens, usage ledger, and rolling budget envelope. The
``llm_call`` / ``llm_usage_daily`` / full ``agent_action`` state
machine land in follow-up migrations without breaking this slice's
write contract.

FK hygiene mirrors the rest of the app:

* ``workspace_id`` → ``workspace.id`` with ``ondelete='CASCADE'`` on
  every workspace-scoped row — sweeping a workspace sweeps its
  agent configuration (§15 export worker snapshots first).
* ``AgentToken.delegating_user_id`` →
  ``ApprovalRequest.requester_actor_id`` →
  ``ApprovalRequest.decided_by`` → ``user.id`` with
  ``ondelete='SET NULL'`` — a user hard-delete must not nuke the
  audit trail; rows survive with a NULL identity pointer and the
  domain layer reads the denormalised label fields downstream.
* :attr:`ModelAssignment.model_id` → :attr:`LlmProviderModel.id`
  ``ondelete='RESTRICT'`` (cd-4btd). Deleting a registry row that
  an active assignment still points at would silently strand the
  workspace without a chain — operators migrate the assignment
  first.
* :attr:`LlmUsage.model_id` stays a free-form **string** carrying
  the wire name that flowed across the network — historical rows
  must survive a registry row's retirement, so promoting it to an
  FK would break the /admin/usage feed. The §02 spec column
  ``llm_call.provider_model_id`` is the future shape; the rename is
  tracked as a follow-up.
* :class:`LlmProviderModel.provider_id` /
  :class:`LlmProviderModel.model_id` →
  :class:`LlmProvider.id` / :class:`LlmModel.id`
  ``ondelete='RESTRICT'`` so an operator can't sweep half the
  registry out from under live assignments.

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
    LlmModel,
    LlmProvider,
    LlmProviderModel,
    LlmUsage,
    ModelAssignment,
)
from app.tenancy.registry import register

# Workspace-scoped tables only. The cd-4btd registry trio
# (``llm_provider`` / ``llm_model`` / ``llm_provider_model``) is
# deployment-scope and must NOT be registered here — the ORM tenant
# filter would inject a ``workspace_id =`` predicate against a column
# that does not exist and break every read.
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
    "LlmModel",
    "LlmProvider",
    "LlmProviderModel",
    "LlmUsage",
    "ModelAssignment",
]
