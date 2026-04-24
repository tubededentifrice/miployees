"""llm_usage_agent_trail_cd_wjpl

Revision ID: b8d0e1f2a3b4
Revises: a7b8c9d0e1f2
Create Date: 2026-04-24 17:00:00.000000

Adds the §11 agent-trail telemetry columns to ``llm_usage`` so the
cd-wjpl recorder can persist the full post-flight context a
delegated-token LLM call ships: the resolved assignment rung, how
many prior rungs failed before the one that landed, the provider's
``finish_reason`` string, and the §11 "Agent audit trail" triple
(delegating user id, token id, agent label) that the
/admin/usage feed joins against when filtering by actor.

Shape additions on ``llm_usage`` (all nullable — backfill-free):

* ``assignment_id VARCHAR(26) NULL`` — the ``ModelAssignment.id`` rung
  the §11 resolver produced for this call. Nullable because the
  admin smoke path + future deployment-scope callers can bypass the
  resolver (§11 "Deployment-scope capabilities"). Soft reference —
  no FK; :class:`~app.adapters.db.llm.models.ModelAssignment` rows
  can be deleted without invalidating the usage row's audit trail
  (same ``model_id`` soft-reference rationale in the cd-cm5 module
  docstring).
* ``fallback_attempts INT NOT NULL DEFAULT 0`` — how many prior rungs
  failed before this one succeeded. 0 = first-rung success, matches
  §11 "LLMResult" ``fallback_attempts`` contract and the spec's
  §"Failure modes" ``X-LLM-Fallback-Attempts`` response header.
* ``finish_reason VARCHAR NULL`` — the provider's free-form finish
  reason (``stop``, ``length``, ``content_filter``, ``tool_calls``,
  …). Plain string (not an enum) because providers ship different
  vocabularies — §11 ``llm_call`` carries the same shape. NULL when
  the call never produced a body (timeout, transport error).
* ``actor_user_id VARCHAR(26) NULL`` — the delegating user. Matches
  §11 "Agent audit trail" semantics (``actor_id`` = the human on
  whose behalf the call fired). NULL for service-initiated calls
  (daily digest worker, health check, deployment-scope feedback
  capabilities). Soft reference — no FK so a user hard-delete does
  not break historical rows.
* ``token_id VARCHAR(26) NULL`` — the delegated API token the call
  used (§11 "Agent audit trail" ``token_id``). NULL for
  passkey-session calls. Soft reference — no FK so a revoked token
  sweep does not break the audit trail (same rationale as
  ``model_id``).
* ``agent_label VARCHAR NULL`` — short human label (e.g.
  ``"manager-chat"``, ``"expenses-autofill"``) denormalised off
  :class:`~app.adapters.db.llm.models.AgentToken.label` so a /admin/
  usage readout doesn't need to join through ``agent_token``. NULL
  when the call carries no agent context (passkey session, service
  worker).

Index additions on ``llm_usage``:

* ``ix_llm_usage_workspace_actor_created`` on ``(workspace_id,
  actor_user_id, created_at)`` — backs the /admin/usage feed's
  "filter this workspace's usage by the delegating user, newest
  first" hot path. Leading ``workspace_id`` carries the tenant
  filter; the trailing ``created_at`` keeps paginated scrolls
  cheap. Non-unique — many rows share a single ``(workspace,
  actor)`` tuple by design.

**`model_id` column name drift vs spec.** The cd-cm5 slice named the
column ``model_id`` but the spec (§02 "LLM" ``llm_call``) names the
equivalent column ``provider_model_id`` — the ``llm_model`` registry
is a different referent from the resolved provider-model wire name
that actually lands in the row. Renaming the column would churn
every existing query (``/admin/usage`` feed, router observability
seam, cd-irng's budget sum). cd-wjpl leaves the column name as-is
and documents the drift on the ORM; a follow-up Beads task owns the
rename once the deployment-scope ``llm_provider_model`` registry
lands so the downstream readers can flip together.

**Reversibility.** ``downgrade()`` drops the composite index first
and then each added column in reverse order — SQLite's ``batch_
alter_table`` rebuilds the table once across the whole block so the
schema-fingerprint gate stays honest on the upgrade → downgrade →
upgrade cycle. No data-loss concern: every new column is either
nullable or server-defaulted to 0, so rolling back just discards
the telemetry columns without touching the existing rows.

See ``docs/specs/02-domain-model.md`` §"LLM" §"llm_usage" /
§"audit_log", ``docs/specs/11-llm-and-agents.md`` §"Cost tracking"
§"Agent audit trail" §"Failure modes".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add the six telemetry columns + /admin/usage actor index in a
    # single ``batch_alter_table`` so SQLite materialises everything
    # through one table-copy rather than one per op. On PG this
    # renders as plain ``ALTER TABLE`` statements in sequence.
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        # Soft reference to ``ModelAssignment.id`` — no FK so an admin
        # deleting an assignment does not break the historical trail
        # (same pattern as ``model_id``).
        batch_op.add_column(
            sa.Column(
                "assignment_id",
                sa.String(length=26),
                nullable=True,
            )
        )
        # How many prior rungs failed before this one succeeded. 0 = the
        # primary worked. Non-null + server default so existing rows
        # (cd-cm5 / cd-irng era) stay self-consistent without a backfill.
        batch_op.add_column(
            sa.Column(
                "fallback_attempts",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        # Provider's free-form finish reason. Plain string — providers
        # ship different vocabularies (``stop`` / ``length`` /
        # ``content_filter`` / ``tool_calls`` / ``function_call`` / …).
        # NULL when the call never produced a body.
        batch_op.add_column(
            sa.Column(
                "finish_reason",
                sa.String(),
                nullable=True,
            )
        )
        # §11 "Agent audit trail" — the delegating user. NULL for
        # service-initiated calls (digest worker, health check).
        batch_op.add_column(
            sa.Column(
                "actor_user_id",
                sa.String(length=26),
                nullable=True,
            )
        )
        # The delegated API token used. NULL for passkey-session calls.
        batch_op.add_column(
            sa.Column(
                "token_id",
                sa.String(length=26),
                nullable=True,
            )
        )
        # Denormalised :class:`AgentToken.label` so /admin/usage can
        # render the row without a join. NULL for non-agent callers.
        batch_op.add_column(
            sa.Column(
                "agent_label",
                sa.String(),
                nullable=True,
            )
        )

        # "Usage for this workspace filtered by delegating user,
        # newest first" — the /admin/usage hot path. The leading
        # ``workspace_id`` carries the tenant filter; trailing
        # ``created_at`` keeps paginated scrolls cheap.
        batch_op.create_index(
            "ix_llm_usage_workspace_actor_created",
            ["workspace_id", "actor_user_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    FK-safe order: drop the secondary index first, then each column
    in reverse of the upgrade so the SQLite batch rebuild reverses
    cleanly. No data-loss concern — every new column is nullable or
    server-defaulted, so rolling back discards telemetry without
    touching the rest of the row.
    """
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_usage_workspace_actor_created")
        batch_op.drop_column("agent_label")
        batch_op.drop_column("token_id")
        batch_op.drop_column("actor_user_id")
        batch_op.drop_column("finish_reason")
        batch_op.drop_column("fallback_attempts")
        batch_op.drop_column("assignment_id")
