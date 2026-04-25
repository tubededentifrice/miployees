"""llm_registry_cd_4btd

Revision ID: c0d2e4f6a8b1
Revises: b9c1e3f5a7b9
Create Date: 2026-04-25 13:00:00.000000

Lands the §11 deployment-scope LLM registry trio and tightens
``model_assignment.model_id`` from a soft reference into a real FK
on the new ``llm_provider_model.id``.

New tables (all deployment-scope — no ``workspace_id``; not
registered in :mod:`app.tenancy.registry`):

* ``llm_provider`` — the deployment's known LLM-serving endpoints
  (``openrouter`` / ``openai_compatible`` / ``fake``). ``name`` is
  unique. ``provider_type`` carries a CHECK clamp (widening is
  additive). ``api_key_envelope_ref`` is a pointer into
  ``secret_envelope`` (§15) — never the ciphertext.
  ``updated_by_user_id`` ON DELETE SET NULL so a user hard-delete
  doesn't sweep the registry.
* ``llm_model`` — provider-agnostic model metadata
  (``canonical_name`` unique; capability tags JSON list;
  context-window / max-output caps; ``price_source`` CHECK clamp
  ``"" | "openrouter" | "manual"``).
* ``llm_provider_model`` — the (provider, model) join row that
  decouples canonical name from the provider's wire form. Carries
  per-combo pricing (``input_cost_per_million`` /
  ``output_cost_per_million`` / ``fixed_cost_per_call_usd``),
  per-call tuning overrides, the ``supports_temperature`` /
  ``supports_system_prompt`` flag pair, ``reasoning_effort``, and a
  ``extra_api_params`` JSON catch-all. Unique
  ``(provider_id, model_id)``. FKs ``provider_id`` /
  ``model_id`` ON DELETE RESTRICT — half-deleting the registry
  under live assignments is a hard error, not a silent loss.

``model_assignment.model_id`` ALTER:

* Pre-cd-4btd: ``String(26) NOT NULL`` with no FK (the soft
  reference cd-cm5 / cd-u84y carried until the registry landed).
* Post-cd-4btd: ``String(26) NOT NULL`` with a real FK to
  ``llm_provider_model.id`` ON DELETE RESTRICT. The resolver
  (:func:`app.domain.llm.router._to_pick`) joins through the
  registry so :class:`~app.domain.llm.router.ModelPick.api_model_id`
  carries the provider's wire form
  (``LlmProviderModel.api_model_id``) and
  :class:`~app.domain.llm.router.ModelPick.provider_model_id`
  carries the registry id.

**Data hazard.** Pre-cd-4btd ``model_assignment`` rows hold ULIDs
that point at a not-yet-existing registry. Adding the FK against
unmatched values would either fail the DDL (PG) or land a dangling
reference (SQLite, where FK enforcement is opt-in). The
greenfield-rollout policy (cd-4btd is the registry's first slice;
no production deployment yet) is the same idiom cd-i1qe used for
the ``api_token.kind`` widening: sweep the existing rows so the
restored FK lands cleanly. Operators with an off-spec dev DB that
*does* carry assignments must dump them to JSON before running
the upgrade — the rows are recoverable only against the
pre-migration schema.

The ALTER on ``model_assignment.model_id`` runs through
``batch_alter_table`` so SQLite materialises the FK via a
table-copy rather than the (non-portable) ``ALTER TABLE … ADD
FOREIGN KEY`` form. PG executes the underlying ``ALTER TABLE``
sequence directly; both backends round-trip the
``schema-fingerprint`` parity gate.

**Reversibility.** ``downgrade()`` reverses everything: the FK on
``model_assignment.model_id`` is dropped via ``batch_alter_table``,
restoring the soft-reference shape; existing assignment rows pass
through unchanged because the column type / NULL-ability is
identical pre- and post-migration. The three new tables drop in
reverse FK order (``llm_provider_model`` → ``llm_model`` →
``llm_provider``) so the FK on ``llm_provider_model.provider_id``
disappears before the ``llm_provider`` table it references. The
SQLite + PG round-trip is exercised by
:class:`tests.integration.test_db_llm.TestCd4btdMigrationRoundTrip`.

See ``docs/specs/11-llm-and-agents.md`` §"Provider / model /
provider-model registry" (lines 547-690),
``docs/specs/02-domain-model.md`` §"LLM".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0d2e4f6a8b1"
down_revision: str | Sequence[str] | None = "b9c1e3f5a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``llm_provider`` — deployment-scope provider endpoints.
    op.create_table(
        "llm_provider",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("provider_type", sa.String(), nullable=False),
        sa.Column("api_endpoint", sa.String(), nullable=True),
        sa.Column("api_key_envelope_ref", sa.String(), nullable=True),
        # Soft reference into ``llm_provider_model`` — see model
        # docstring for why this can't be a hard FK (the two tables
        # would form a circular dependency).
        sa.Column("default_model", sa.String(length=26), nullable=True),
        sa.Column(
            "timeout_s",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column(
            "requests_per_minute",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("60"),
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "provider_type IN ('openrouter', 'openai_compatible', 'fake')",
            name=op.f("ck_llm_provider_provider_type"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user.id"],
            name=op.f("fk_llm_provider_updated_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_provider")),
        sa.UniqueConstraint("name", name="uq_llm_provider_name"),
    )

    # ``llm_model`` — deployment-scope provider-agnostic model
    # metadata.
    op.create_table(
        "llm_model",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("canonical_name", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("vendor", sa.String(), nullable=False),
        sa.Column(
            "capabilities",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "price_source",
            sa.String(),
            nullable=False,
            server_default="",
        ),
        sa.Column("price_source_model_id", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by_user_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "price_source IN ('', 'openrouter', 'manual')",
            name=op.f("ck_llm_model_price_source"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["user.id"],
            name=op.f("fk_llm_model_updated_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_model")),
        sa.UniqueConstraint("canonical_name", name="uq_llm_model_canonical_name"),
    )

    # ``llm_provider_model`` — the (provider, model) join row.
    op.create_table(
        "llm_provider_model",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("provider_id", sa.String(), nullable=False),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("api_model_id", sa.String(), nullable=False),
        sa.Column(
            "input_cost_per_million",
            sa.Numeric(precision=10, scale=4),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "output_cost_per_million",
            sa.Numeric(precision=10, scale=4),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "fixed_cost_per_call_usd",
            sa.Numeric(precision=10, scale=4),
            nullable=True,
        ),
        sa.Column("max_tokens_override", sa.Integer(), nullable=True),
        sa.Column("temperature_override", sa.Float(), nullable=True),
        sa.Column(
            "supports_system_prompt",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "supports_temperature",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("reasoning_effort", sa.String(), nullable=True),
        sa.Column(
            "extra_api_params",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("price_source_override", sa.String(), nullable=True),
        sa.Column("price_source_model_id_override", sa.String(), nullable=True),
        sa.Column("price_last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["llm_provider.id"],
            name=op.f("fk_llm_provider_model_provider_id_llm_provider"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["llm_model.id"],
            name=op.f("fk_llm_provider_model_model_id_llm_model"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_provider_model")),
        sa.UniqueConstraint(
            "provider_id",
            "model_id",
            name="uq_llm_provider_model_provider_model",
        ),
    )

    # Sweep pre-cd-4btd ``model_assignment`` rows so the new FK on
    # ``model_id`` lands cleanly. Pre-cd-4btd those values were soft
    # references; they don't match any ``llm_provider_model.id`` (the
    # table didn't exist). See module docstring for the rationale and
    # the recovery path operators must take if they want to preserve
    # off-spec rows.
    op.execute("DELETE FROM model_assignment")

    # Promote ``model_id`` from soft reference into a real FK. The
    # batch_alter wrapper makes SQLite materialise the FK via the
    # table-copy idiom; PG executes the ALTER directly.
    with op.batch_alter_table("model_assignment", schema=None) as batch_op:
        batch_op.create_foreign_key(
            op.f("fk_model_assignment_model_id_llm_provider_model"),
            "llm_provider_model",
            ["model_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    """Downgrade schema.

    FK-safe order:

    1. Drop the cd-4btd FK on ``model_assignment.model_id`` so the
       column reverts to a soft reference. The column type / NULL-
       ability is unchanged, so existing rows survive — but those
       rows are unlikely to exist on a real downgrade because
       ``upgrade()`` swept ``model_assignment`` clean.
    2. Drop ``llm_provider_model`` (its FKs on ``provider_id`` /
       ``model_id`` disappear with the table).
    3. Drop ``llm_model``.
    4. Drop ``llm_provider``.

    Operators planning a *real* rollback should dump
    ``model_assignment`` + the registry trio first; the assignment
    rows are recoverable only against the pre-cd-4btd shape and the
    registry rows are irrecoverable under the post-downgrade schema
    by definition.
    """
    with op.batch_alter_table("model_assignment", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("fk_model_assignment_model_id_llm_provider_model"),
            type_="foreignkey",
        )

    op.drop_table("llm_provider_model")
    op.drop_table("llm_model")
    op.drop_table("llm_provider")
