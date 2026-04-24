"""occurrence_oneoff_cd_0rf

Revision ID: d5e7b3a1c9f4
Revises: c4f6a9b8d2e1
Create Date: 2026-04-20 16:00:00.000000

Extends ``occurrence`` with the columns the cd-0rf one-off task
creation service (``app.domain.tasks.oneoff.create_oneoff``) needs
to materialise a task that is not tied to a schedule. Every §06
"Task row" column the scheduler worker did not already write through
in cd-22e lands here:

* ``title`` — copied from the template at generation time or posted
  explicitly on the ad-hoc path. Required for one-off tasks (no
  parent template to fall back on at render time); nullable on the
  migration side so existing cd-22e rows survive without a backfill.
* ``description_md`` — rendered body; nullable.
* ``priority`` — one of ``low | normal | high | urgent``; CHECK-gated
  the same way :class:`TaskTemplate.priority` is. Server default
  ``'normal'`` so existing rows satisfy the CHECK.
* ``photo_evidence`` — one of ``disabled | optional | required``;
  CHECK-gated matching :class:`TaskTemplate.photo_evidence`. Server
  default ``'disabled'``.
* ``duration_minutes`` — per-occurrence override (copied from the
  template on the ad-hoc path). Nullable; the render path falls
  back to ``ends_at - starts_at`` when unset.
* ``area_id`` / ``unit_id`` — soft pointers (no FK) matching the
  convention on :class:`Schedule.area_id` — losing an area / unit
  must not orphan the task row, and the domain layer validates
  existence at write time once cd-sn26 widens the area/unit CRUD.
* ``expected_role_id`` — soft pointer to the ``work_role`` table
  (§05). No FK at v1; cd-5kv4 landed the table, a follow-up may
  promote the column into a real FK.
* ``linked_instruction_ids`` — JSON list of instruction ids (§07).
* ``inventory_consumption_json`` — SKU → qty map (§08).
* ``is_personal`` — bool, default ``false``. Column default matches
  §06 "Self-created and personal tasks": only the quick-add UI
  flips it to ``true`` explicitly. Scheduled / imported / generator-
  created tasks leave it at ``false``.
* ``created_by_user_id`` — the user who originated the task.
  ``ON DELETE SET NULL`` so history survives the actor's deletion
  (matches the convention on :class:`Occurrence.assignee_user_id` /
  :class:`Occurrence.completed_by_user_id`). Nullable on the
  migration side so existing rows inserted by the cd-22e generator
  (which does not populate this column) stay legal; new writes
  through the ad-hoc path always populate it.

Every added column is nullable or has a server default so existing
rows survive without a backfill. The one-off service
(:func:`app.domain.tasks.oneoff.create_oneoff`) fills them on
INSERT. The scheduler worker (:mod:`app.worker.tasks.generator`)
stays unchanged in cd-0rf — a follow-up Beads task teaches the
generator to write these columns through from the template at
generation time so scheduled tasks carry the full §06 shape
(left for cd-0rf-scheduled-carry-through).

**Reversibility.** ``downgrade()`` drops every added column + both
CHECK constraints. Data in the added columns is discarded. The
``created_by_user_id`` FK is dropped before the column to avoid
orphaned constraint rows.

See ``docs/specs/02-domain-model.md`` §"occurrence" / §"task",
``docs/specs/06-tasks-and-scheduling.md`` §"Task row",
§"Self-created and personal tasks".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e7b3a1c9f4"
down_revision: str | Sequence[str] | None = "c4f6a9b8d2e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PRIORITY_VALUES: tuple[str, ...] = ("low", "normal", "high", "urgent")
_PHOTO_EVIDENCE_VALUES: tuple[str, ...] = ("disabled", "optional", "required")


def _in_clause(values: tuple[str, ...]) -> str:
    return "'" + "', '".join(values) + "'"


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        # Widen ``template_id`` + ``property_id`` to nullable so
        # one-off tasks without a parent template (ad-hoc "remind me
        # to call Maria") or without a property (a personal errand)
        # can land. The cd-chd / cd-22e invariant "every row has a
        # template" survives as a soft rule carried by the service
        # layer — the scheduler worker still writes a template every
        # time — so history / RESTRICT semantics are preserved in
        # practice.
        batch_op.alter_column("template_id", existing_type=sa.String(), nullable=True)
        batch_op.alter_column("property_id", existing_type=sa.String(), nullable=True)

        # Display / intent columns. ``title`` is nullable on the
        # migration side — existing cd-chd / cd-22e rows have none —
        # but every new write populates it.
        batch_op.add_column(sa.Column("title", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("description_md", sa.String(), nullable=True))
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
                "photo_evidence",
                sa.String(),
                nullable=False,
                server_default="disabled",
            )
        )
        batch_op.add_column(sa.Column("duration_minutes", sa.Integer(), nullable=True))
        # Soft pointers — no FK. Losing the area / unit / role must
        # not orphan the task row; the domain service validates
        # existence at write time.
        batch_op.add_column(sa.Column("area_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("unit_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("expected_role_id", sa.String(), nullable=True))
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
                "inventory_consumption_json",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "is_personal",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        # ``created_by_user_id`` carries the user who originated the
        # task. ``SET NULL`` on delete so history survives the actor's
        # removal. Nullable on the migration side because the cd-22e
        # generator does not populate it; the ad-hoc service always
        # does.
        batch_op.add_column(sa.Column("created_by_user_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_occurrence_created_by_user",
            referent_table="user",
            local_cols=["created_by_user_id"],
            remote_cols=["id"],
            ondelete="SET NULL",
        )

        # CHECK gates for the new enum columns. Mirrors the
        # convention on :class:`TaskTemplate.priority` /
        # :class:`TaskTemplate.photo_evidence`.
        batch_op.create_check_constraint(
            "priority",
            f"priority IN ({_in_clause(_PRIORITY_VALUES)})",
        )
        batch_op.create_check_constraint(
            "photo_evidence",
            f"photo_evidence IN ({_in_clause(_PHOTO_EVIDENCE_VALUES)})",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.drop_constraint("photo_evidence", type_="check")
        batch_op.drop_constraint("priority", type_="check")
        batch_op.drop_constraint("fk_occurrence_created_by_user", type_="foreignkey")
        batch_op.drop_column("created_by_user_id")
        batch_op.drop_column("is_personal")
        batch_op.drop_column("inventory_consumption_json")
        batch_op.drop_column("linked_instruction_ids")
        batch_op.drop_column("expected_role_id")
        batch_op.drop_column("unit_id")
        batch_op.drop_column("area_id")
        batch_op.drop_column("duration_minutes")
        batch_op.drop_column("photo_evidence")
        batch_op.drop_column("priority")
        batch_op.drop_column("description_md")
        batch_op.drop_column("title")
        # Narrow ``template_id`` + ``property_id`` back to NOT NULL.
        # Any cd-0rf-authored rows with a NULL value would violate
        # the narrower constraint on reapply — acceptable rollback
        # cost on a dev DB (matches the convention in the cd-k4l
        # enum-narrow downgrade).
        batch_op.alter_column("template_id", existing_type=sa.String(), nullable=False)
        batch_op.alter_column("property_id", existing_type=sa.String(), nullable=False)
