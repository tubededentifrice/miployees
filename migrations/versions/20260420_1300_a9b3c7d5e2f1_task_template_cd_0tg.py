"""task_template_cd_0tg

Revision ID: a9b3c7d5e2f1
Revises: 8f3c2a4d1e90
Create Date: 2026-04-20 13:00:00.000000

Extends ``task_template`` with the richer §02 / §06 columns the
CRUD service (cd-0tg) needs. The v1 slice (cd-chd) landed only the
minimum columns the scheduler worker needs to materialise
occurrences; this migration adds the spec-level fields the manager
UI and `TaskTemplateCreate` / `TaskTemplateView` DTOs reference —
scope shape (property / area), priority, photo-evidence policy,
linked instructions, inventory consumption hints, LLM hints, and
the ``deleted_at`` soft-delete column.

Column rationale:

* ``name`` — human-visible display name. The v1 slice used ``title``;
  the spec settled on ``name`` to stay consistent with `schedule.name`
  and the mocks. Added as nullable + backfilled from ``title`` so the
  existing rows remain readable; new writes populate both until a
  follow-up drops ``title``.
* ``role_id`` — soft reference to the ``work_role`` table (landed by
  cd-5kv4, §05). Nullable because a template without a pinned role
  lets the schedule / ad-hoc creator pick per-occurrence. No FK yet;
  the column is a plain ``String`` until the ``work_role`` table
  lands, matching the ``scope_property_id`` convention on
  ``role_grant``.
* ``duration_minutes`` — aliases ``default_duration_min``. Renamed to
  match the spec + mocks; we keep both until a follow-up drops the
  old name.
* ``property_scope`` / ``listed_property_ids`` — scope shape for
  which properties the template targets. ``any`` means workspace-
  wide, ``one`` means a single specific property, ``listed`` means
  the enumerated list. CHECK-enforced; domain validates the shape
  consistency ("listed → non-empty list", "one → exactly one",
  "any → empty").
* ``area_scope`` / ``listed_area_ids`` — same shape rule applied to
  areas within properties. ``derived`` is allowed for stay-lifecycle
  bundles (§06 "Stay lifecycle rules") where the area comes from the
  stay context at generation time.
* ``photo_evidence`` — three-value enum replacing the v1 slice's
  ``required_evidence`` + ``photo_required`` pair. ``disabled`` means
  the camera picker is hidden; ``optional`` shows it but accepts a
  completion without a photo; ``required`` rejects completions that
  lack one (§06 "Evidence policy inheritance").
* ``linked_instruction_ids`` — JSON list of instruction ids (§07)
  surfaced on the task detail screen.
* ``priority`` — four-value enum (``low | normal | high | urgent``)
  used by the manager's sort + chip.
* ``inventory_consumption_json`` — flat SKU → qty payload for the
  consume-on-task worker (§08). JSON rather than a join table for
  the v1 slice; promoted to a proper row model with cd-jkwr.
* ``llm_hints_md`` — free-text hints the agent inbox (§06) passes to
  the LLM when explaining the task.
* ``deleted_at`` — soft-delete marker. Nullable; live rows carry
  ``NULL``. Every domain-level list honours it via the ``deleted``
  filter. The ``occurrence.template_id`` FK uses ``RESTRICT`` on the
  DB side (§06), so hard-deleting a template with history would
  fail; the soft-delete path is the one callers use.

**All new columns are nullable or carry server defaults** so
existing rows (in dev / test) survive the migration without
backfill. The CRUD service (app/domain/tasks/templates.py) fills
them on INSERT; reads that encounter a pre-migration row treat
``NULL`` on the enum columns as the default shown in the server
default (``any`` / ``disabled`` / ``normal``). Backfill statements
copy ``title → name`` and ``default_duration_min → duration_minutes``
so the new service can ignore the legacy columns entirely; the
legacy columns survive until a follow-up drop because existing
integration tests (``tests/integration/test_db_tasks.py``) still
reference them by name.

**Reversibility.** ``downgrade()`` drops every added column. Data in
the added columns is discarded — acceptable for a rollback of a
feature extension on a dev database.

See ``docs/specs/02-domain-model.md`` §"task_template",
``docs/specs/06-tasks-and-scheduling.md`` §"Task template",
§"Checklist template shape", §"Evidence policy inheritance".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9b3c7d5e2f1"
down_revision: str | Sequence[str] | None = "8f3c2a4d1e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("task_template", schema=None) as batch_op:
        # Display / identity columns. ``name`` is added nullable so
        # pre-existing rows survive; the backfill below copies
        # ``title`` into it, then we tighten with an UPDATE + (in a
        # follow-up migration) a NOT NULL. Doing the tighten in a
        # separate step keeps the migration safe against partial
        # apply.
        batch_op.add_column(sa.Column("name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("role_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("duration_minutes", sa.Integer(), nullable=True))

        # Scope shape columns. Server defaults make these safe to add
        # on existing rows; the CHECK constraints enforce the enum at
        # the DB layer as defence-in-depth against a buggy caller.
        batch_op.add_column(
            sa.Column(
                "property_scope",
                sa.String(),
                nullable=False,
                server_default="any",
            )
        )
        batch_op.add_column(
            sa.Column(
                "listed_property_ids",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.add_column(
            sa.Column(
                "area_scope",
                sa.String(),
                nullable=False,
                server_default="any",
            )
        )
        batch_op.add_column(
            sa.Column(
                "listed_area_ids",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )

        # Evidence policy, priority, and the rest of the spec payload.
        batch_op.add_column(
            sa.Column(
                "photo_evidence",
                sa.String(),
                nullable=False,
                server_default="disabled",
            )
        )
        batch_op.add_column(
            sa.Column(
                "linked_instruction_ids",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.add_column(
            sa.Column(
                "priority",
                sa.String(),
                nullable=False,
                server_default="normal",
            )
        )
        batch_op.add_column(
            sa.Column(
                "inventory_consumption_json",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(sa.Column("llm_hints_md", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )

        # CHECK constraints. Each named ``<column>`` so the ORM
        # naming convention renders a deterministic final name
        # (``ck_task_template_<column>``) — matches the v1-slice
        # pattern in ``200bedec0eed_tasks.py``.
        batch_op.create_check_constraint(
            "property_scope",
            "property_scope IN ('any', 'one', 'listed')",
        )
        batch_op.create_check_constraint(
            "area_scope",
            "area_scope IN ('any', 'one', 'listed', 'derived')",
        )
        batch_op.create_check_constraint(
            "photo_evidence",
            "photo_evidence IN ('disabled', 'optional', 'required')",
        )
        batch_op.create_check_constraint(
            "priority",
            "priority IN ('low', 'normal', 'high', 'urgent')",
        )

        # Index on ``deleted_at`` for the common "list live templates"
        # query: `WHERE workspace_id = ? AND deleted_at IS NULL`.
        batch_op.create_index(
            "ix_task_template_workspace_deleted",
            ["workspace_id", "deleted_at"],
            unique=False,
        )

    # Backfill the renamed columns so existing rows stay readable
    # through the new service surface. The legacy columns remain in
    # place; a follow-up migration drops them once no caller writes
    # to the old names.
    op.execute("UPDATE task_template SET name = title WHERE name IS NULL")
    op.execute(
        "UPDATE task_template SET duration_minutes = default_duration_min "
        "WHERE duration_minutes IS NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.drop_index("ix_task_template_workspace_deleted")
        # ``create_check_constraint`` above passes the raw constraint
        # body ("property_scope") which the naming convention renders
        # as ``ck_task_template_property_scope``. Alembic then double-
        # prefixes when we pass the already-rendered name here, so
        # we pass the raw body (matching the create-side convention)
        # and let the naming convention produce the final name.
        batch_op.drop_constraint("priority", type_="check")
        batch_op.drop_constraint("photo_evidence", type_="check")
        batch_op.drop_constraint("area_scope", type_="check")
        batch_op.drop_constraint("property_scope", type_="check")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("llm_hints_md")
        batch_op.drop_column("inventory_consumption_json")
        batch_op.drop_column("priority")
        batch_op.drop_column("linked_instruction_ids")
        batch_op.drop_column("photo_evidence")
        batch_op.drop_column("listed_area_ids")
        batch_op.drop_column("area_scope")
        batch_op.drop_column("listed_property_ids")
        batch_op.drop_column("property_scope")
        batch_op.drop_column("duration_minutes")
        batch_op.drop_column("role_id")
        batch_op.drop_column("name")
