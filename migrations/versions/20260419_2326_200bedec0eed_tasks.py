"""tasks

Revision ID: 200bedec0eed
Revises: 89ebd89b9de4
Create Date: 2026-04-19 23:26:19.752453

Creates the seven tasks tables that back the template → schedule →
occurrence → {checklist / evidence / comment} chain (see
``docs/specs/02-domain-model.md`` §"task_template", §"schedule",
§"occurrence", §"checklist_item", §"evidence", §"comment" and
``docs/specs/06-tasks-and-scheduling.md``):

* ``task_template`` — reusable blueprint for an occurrence. Carries
  the fields every spawned occurrence copies down at generation time.
  ``required_evidence`` enum (``none | photo | note | voice | gps``)
  and optional ``default_assignee_role`` (``manager | worker |
  client | guest``) are enforced by CHECK constraints. The v1 slice
  lands only the columns the tasks MVP (cd-0tg template CRUD,
  cd-4qr turnover auto-generation) needs; richer §02 columns
  (``paused_at``, ``active_from``, soft-delete) land with those
  follow-ups.

* ``checklist_template_item`` — per-template checklist rows. Cascades
  on ``task_template`` so deleting a template sweeps its blueprint.
  The ``(template_id, position)`` uniqueness keeps the ordered list
  rigorous; ``workspace_id`` is denormalised so the ORM tenant
  filter can enforce boundaries on reads that only touch this table.

* ``schedule`` — recurrence rule that materialises occurrences via
  the scheduler worker. ``property_id`` is nullable (``SET NULL``
  on property delete) so a schedule can be workspace-wide and drops
  back to that state when its property goes away rather than being
  swept. ``(workspace_id, next_generation_at)`` is the worker's
  hot-path index. CHECK ``until > dtstart`` guards against
  backwards windows; ``assignee_role`` CHECK mirrors the template
  enum (NULL legal).

* ``occurrence`` — materialised unit of work. ``schedule_id`` is
  ``SET NULL`` (history survives schedule deletion; one-off tasks
  already carry NULL). ``template_id`` is ``RESTRICT`` — a template
  that has produced occurrences cannot be hard-deleted; callers
  soft-delete it (column not in this slice; arrives with cd-0tg).
  ``property_id`` is ``CASCADE`` — hard-deleting a property sweeps
  its history. All user pointers (``assignee``, ``completed_by``,
  ``reviewer``) are ``SET NULL`` so the row outlives its actors.
  CHECK ``state IN (pending, in_progress, done, skipped, approved)``
  and ``ends_at > starts_at``. Two per-acceptance-criterion
  indexes: ``(workspace_id, assignee_user_id, starts_at)`` for "my
  tasks" views and ``(workspace_id, state, starts_at)`` for manager
  queues.

* ``checklist_item`` — per-occurrence checklist row, copied from
  ``checklist_template_item`` at generation time and authoritative
  thereafter. Cascades on the parent occurrence. Unique
  ``(occurrence_id, position)`` keeps ordering rigorous.

* ``evidence`` — artefact attached to an occurrence. Cascades on
  the parent; ``created_by_user_id`` is ``SET NULL`` so history
  survives the actor's deletion. ``kind`` CHECK (``photo | note |
  voice | gps``) matches §02.

* ``comment`` — threaded markdown comment. Author pointer is
  ``SET NULL``; ``(workspace_id, occurrence_id, created_at)``
  index powers the per-thread read path.

Tables are created in FK dependency order (``task_template →
checklist_template_item``, ``task_template → schedule → occurrence
→ {checklist_item, evidence, comment}``). ``downgrade()`` drops in
reverse.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "200bedec0eed"
down_revision: str | Sequence[str] | None = "89ebd89b9de4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "task_template",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description_md", sa.String(), nullable=False),
        sa.Column("default_duration_min", sa.Integer(), nullable=False),
        sa.Column("required_evidence", sa.String(), nullable=False),
        sa.Column("photo_required", sa.Boolean(), nullable=False),
        sa.Column("default_assignee_role", sa.String(), nullable=True),
        sa.Column("checklist_template_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "default_assignee_role IS NULL OR default_assignee_role IN "
            "('manager', 'worker', 'client', 'guest')",
            name=op.f("ck_task_template_default_assignee_role"),
        ),
        sa.CheckConstraint(
            "required_evidence IN ('none', 'photo', 'note', 'voice', 'gps')",
            name=op.f("ck_task_template_required_evidence"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_task_template_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_template")),
    )
    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.create_index(
            "ix_task_template_workspace", ["workspace_id"], unique=False
        )

    op.create_table(
        "checklist_template_item",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("requires_photo", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["task_template.id"],
            name=op.f("fk_checklist_template_item_template_id_task_template"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_checklist_template_item_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklist_template_item")),
        sa.UniqueConstraint(
            "template_id",
            "position",
            name="uq_checklist_template_item_template_position",
        ),
    )
    with op.batch_alter_table("checklist_template_item", schema=None) as batch_op:
        batch_op.create_index(
            "ix_checklist_template_item_template", ["template_id"], unique=False
        )

    op.create_table(
        "schedule",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=True),
        sa.Column("rrule_text", sa.String(), nullable=False),
        sa.Column("dtstart", sa.DateTime(timezone=True), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assignee_user_id", sa.String(), nullable=True),
        sa.Column("assignee_role", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_generation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "assignee_role IS NULL OR assignee_role IN "
            "('manager', 'worker', 'client', 'guest')",
            name=op.f("ck_schedule_assignee_role"),
        ),
        sa.CheckConstraint(
            "until IS NULL OR until > dtstart",
            name=op.f("ck_schedule_until_after_dtstart"),
        ),
        sa.ForeignKeyConstraint(
            ["assignee_user_id"],
            ["user.id"],
            name=op.f("fk_schedule_assignee_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_schedule_property_id_property"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["task_template.id"],
            name=op.f("fk_schedule_template_id_task_template"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_schedule_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule")),
    )
    with op.batch_alter_table("schedule", schema=None) as batch_op:
        batch_op.create_index("ix_schedule_template", ["template_id"], unique=False)
        batch_op.create_index(
            "ix_schedule_workspace_next_gen",
            ["workspace_id", "next_generation_at"],
            unique=False,
        )

    op.create_table(
        "occurrence",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("schedule_id", sa.String(), nullable=True),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("assignee_user_id", sa.String(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by_user_id", sa.String(), nullable=True),
        sa.Column("reviewer_user_id", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'in_progress', 'done', 'skipped', 'approved')",
            name=op.f("ck_occurrence_state"),
        ),
        sa.CheckConstraint(
            "ends_at > starts_at", name=op.f("ck_occurrence_ends_after_starts")
        ),
        sa.ForeignKeyConstraint(
            ["assignee_user_id"],
            ["user.id"],
            name=op.f("fk_occurrence_assignee_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["completed_by_user_id"],
            ["user.id"],
            name=op.f("fk_occurrence_completed_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_occurrence_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["user.id"],
            name=op.f("fk_occurrence_reviewer_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedule.id"],
            name=op.f("fk_occurrence_schedule_id_schedule"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["task_template.id"],
            name=op.f("fk_occurrence_template_id_task_template"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_occurrence_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_occurrence")),
    )
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.create_index(
            "ix_occurrence_workspace_assignee_starts",
            ["workspace_id", "assignee_user_id", "starts_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_occurrence_workspace_state_starts",
            ["workspace_id", "state", "starts_at"],
            unique=False,
        )

    op.create_table(
        "checklist_item",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("requires_photo", sa.Boolean(), nullable=False),
        sa.Column("checked", sa.Boolean(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_blob_hash", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["occurrence.id"],
            name=op.f("fk_checklist_item_occurrence_id_occurrence"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_checklist_item_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checklist_item")),
        sa.UniqueConstraint(
            "occurrence_id",
            "position",
            name="uq_checklist_item_occurrence_position",
        ),
    )
    with op.batch_alter_table("checklist_item", schema=None) as batch_op:
        batch_op.create_index(
            "ix_checklist_item_occurrence", ["occurrence_id"], unique=False
        )

    op.create_table(
        "evidence",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("blob_hash", sa.String(), nullable=True),
        sa.Column("note_md", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('photo', 'note', 'voice', 'gps')",
            name=op.f("ck_evidence_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name=op.f("fk_evidence_created_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["occurrence.id"],
            name=op.f("fk_evidence_occurrence_id_occurrence"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_evidence_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
    )
    with op.batch_alter_table("evidence", schema=None) as batch_op:
        batch_op.create_index(
            "ix_evidence_workspace_occurrence",
            ["workspace_id", "occurrence_id"],
            unique=False,
        )

    op.create_table(
        "comment",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("occurrence_id", sa.String(), nullable=False),
        sa.Column("author_user_id", sa.String(), nullable=True),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attachments_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["user.id"],
            name=op.f("fk_comment_author_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["occurrence_id"],
            ["occurrence.id"],
            name=op.f("fk_comment_occurrence_id_occurrence"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_comment_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comment")),
    )
    with op.batch_alter_table("comment", schema=None) as batch_op:
        batch_op.create_index(
            "ix_comment_workspace_occurrence_created",
            ["workspace_id", "occurrence_id", "created_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("comment", schema=None) as batch_op:
        batch_op.drop_index("ix_comment_workspace_occurrence_created")
    op.drop_table("comment")

    with op.batch_alter_table("evidence", schema=None) as batch_op:
        batch_op.drop_index("ix_evidence_workspace_occurrence")
    op.drop_table("evidence")

    with op.batch_alter_table("checklist_item", schema=None) as batch_op:
        batch_op.drop_index("ix_checklist_item_occurrence")
    op.drop_table("checklist_item")

    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.drop_index("ix_occurrence_workspace_state_starts")
        batch_op.drop_index("ix_occurrence_workspace_assignee_starts")
    op.drop_table("occurrence")

    with op.batch_alter_table("schedule", schema=None) as batch_op:
        batch_op.drop_index("ix_schedule_workspace_next_gen")
        batch_op.drop_index("ix_schedule_template")
    op.drop_table("schedule")

    with op.batch_alter_table("checklist_template_item", schema=None) as batch_op:
        batch_op.drop_index("ix_checklist_template_item_template")
    op.drop_table("checklist_template_item")

    with op.batch_alter_table("task_template", schema=None) as batch_op:
        batch_op.drop_index("ix_task_template_workspace")
    op.drop_table("task_template")
