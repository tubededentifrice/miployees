"""schedule_cd_k4l

Revision ID: b2d4c8f3a5e6
Revises: a9b3c7d5e2f1
Create Date: 2026-04-20 14:00:00.000000

Extends ``schedule`` with the richer §06 columns the CRUD service
(cd-k4l) needs, widens the ``occurrence.state`` CHECK enum with
``scheduled`` + ``cancelled``, and adds
``occurrence.cancellation_reason`` so the
:func:`app.domain.tasks.schedules.delete` cascade can stamp
``'schedule deleted'`` on the tasks it sweeps.

Column rationale:

* ``schedule.name`` — human-visible display name. Nullable on the
  migration side so existing cd-chd rows survive; the CRUD service
  populates it on every INSERT / UPDATE.
* ``schedule.area_id`` — soft reference to the area the schedule
  targets. ``NULL`` means "applies across the property's areas".
  No FK — losing an area should not orphan a schedule row, and the
  domain layer validates the area exists at write time.
* ``schedule.dtstart_local`` — ISO-8601 property-local timestamp
  (``2026-04-20T09:00``). The scheduler worker resolves to UTC via
  ``property.timezone`` at occurrence-generation time. Kept as text
  (not ``DateTime``) because the value is intentionally timezone-
  naive in the property frame; moving it through a typed column
  would invite implicit UTC coercion.
* ``schedule.duration_minutes`` — per-schedule duration override.
  Nullable → fall back to the template's ``duration_minutes``.
* ``schedule.rdate_local`` / ``schedule.exdate_local`` — line-
  separated ISO-8601 local overrides. Stored verbatim; the
  generator re-parses on use. Server-default empty string so
  existing rows survive the migration.
* ``schedule.active_from`` / ``schedule.active_until`` — property-
  local date bounds (``YYYY-MM-DD``). ``active_from`` is the
  since-when for the schedule (`active_from` is on the manager UI
  as ``since <month>``); ``active_until`` is open-ended for
  weeklies. Both are nullable to survive the migration; new writes
  populate at least ``active_from``.
* ``schedule.paused_at`` — pause marker. ``NULL`` = live. Per §06
  "Pause vs active range" a non-null ``paused_at`` wins over the
  active-range predicate — a paused schedule never generates
  occurrences.
* ``schedule.backup_assignee_user_ids`` — JSON list of user ids
  walked in order by the assignment algorithm (§06). Server-
  default ``[]`` so existing rows stay legal.
* ``schedule.deleted_at`` — soft-delete marker. The template
  in-use check reads this column via :func:`_active_schedule_ids`
  in :mod:`app.domain.tasks.templates`, so references through a
  soft-deleted schedule no longer block a template's soft-delete.
* ``occurrence.cancellation_reason`` — free-form text paired with
  ``state='cancelled'``. The cascade in
  :func:`app.domain.tasks.schedules.delete` stamps ``'schedule
  deleted'``; future task-cancel flows populate their own reason.

**Occurrence state enum widening.** The cd-chd slice pinned the
enum at ``pending, in_progress, done, skipped, approved``. cd-k4l
widens it with ``scheduled`` (worker-staged pre-materialisation
state) and ``cancelled`` (set by the schedule-delete cascade). The
``completed`` / ``overdue`` values land with the scheduler worker
follow-up (cd-22e) and the overdue-sweeper job.

**All new columns are nullable or carry server defaults** so
existing rows survive without a backfill. The CRUD service fills
them on INSERT; reads that encounter a pre-migration row treat
``NULL`` on the enum / JSON columns as the default shown in the
server default.

**Reversibility.** ``downgrade()`` drops every added column and
narrows the occurrence-state enum back to the cd-chd set. Data in
the added columns is discarded; any occurrence in a state added by
this migration (``scheduled`` / ``cancelled``) would be invalidated
under the old CHECK — an acceptable rollback cost on a dev DB.

See ``docs/specs/02-domain-model.md`` §"schedule" / §"occurrence",
``docs/specs/06-tasks-and-scheduling.md`` §"Schedule", §"Pause /
resume", §"Deleting and editing".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2d4c8f3a5e6"
down_revision: str | Sequence[str] | None = "a9b3c7d5e2f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("schedule", schema=None) as batch_op:
        # Display + targeting columns. ``name`` and ``area_id`` are
        # both nullable — pre-migration rows stay legal without a
        # backfill; new writes populate them.
        batch_op.add_column(sa.Column("name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("area_id", sa.String(), nullable=True))

        # Property-local scheduling columns. Text rather than
        # ``DateTime`` because the value is intentionally timezone-
        # naive in the property frame; see the file docstring.
        batch_op.add_column(sa.Column("dtstart_local", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("duration_minutes", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "rdate_local",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(
            sa.Column(
                "exdate_local",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )
        batch_op.add_column(sa.Column("active_from", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("active_until", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )

        # Assignment columns. ``backup_assignee_user_ids`` defaults
        # to an empty JSON list so existing rows stay legal.
        batch_op.add_column(
            sa.Column(
                "backup_assignee_user_ids",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )

        # Composite index on ``(workspace_id, deleted_at)`` powers
        # the "list live schedules" hot path in the manager UI.
        batch_op.create_index(
            "ix_schedule_workspace_deleted",
            ["workspace_id", "deleted_at"],
            unique=False,
        )

    # Widen ``occurrence.state`` CHECK and add ``cancellation_reason``.
    # Dropping the CHECK by its SQLAlchemy naming-convention name and
    # re-creating it with the wider enum keeps the table shape under
    # batch_alter_table's control on SQLite.
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cancellation_reason", sa.String(), nullable=True)
        )
        batch_op.drop_constraint("state", type_="check")
        batch_op.create_check_constraint(
            "state",
            "state IN ('scheduled', 'pending', 'in_progress', 'done', "
            "'skipped', 'approved', 'cancelled')",
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.drop_constraint("state", type_="check")
        batch_op.create_check_constraint(
            "state",
            "state IN ('pending', 'in_progress', 'done', 'skipped', 'approved')",
        )
        batch_op.drop_column("cancellation_reason")

    with op.batch_alter_table("schedule", schema=None) as batch_op:
        batch_op.drop_index("ix_schedule_workspace_deleted")
        batch_op.drop_column("backup_assignee_user_ids")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("paused_at")
        batch_op.drop_column("active_until")
        batch_op.drop_column("active_from")
        batch_op.drop_column("exdate_local")
        batch_op.drop_column("rdate_local")
        batch_op.drop_column("duration_minutes")
        batch_op.drop_column("dtstart_local")
        batch_op.drop_column("area_id")
        batch_op.drop_column("name")
