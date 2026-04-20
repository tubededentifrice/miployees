"""occurrence_cd_22e

Revision ID: c4f6a9b8d2e1
Revises: b2d4c8f3a5e6
Create Date: 2026-04-20 15:00:00.000000

Extends ``occurrence`` with the two property-local timestamps the
cd-22e scheduler worker needs to materialise rows idempotently:

* ``scheduled_for_local`` — ISO-8601 property-local timestamp (e.g.
  ``2026-04-20T09:00``). Mirrors ``Schedule.dtstart_local`` in shape
  — stored as text so the tz-naive local-clock value cannot be
  silently coerced to UTC on the way through a typed column. The
  scheduler worker resolves to UTC via ``property.timezone`` at
  generation time; the text column stays the generator's idempotency
  key.
* ``originally_scheduled_for`` — historical anchor the generator
  stamps at creation time. Seeded equal to ``scheduled_for_local``;
  callers that later reschedule a task (pull-back, manager edit)
  leave this field alone so reports can distinguish SLA slips from
  deliberate moves (§06 "Task row").

Both columns are nullable on the migration side so existing cd-chd /
cd-k4l rows survive without a backfill. The scheduler worker
populates both on every INSERT.

**Idempotency guard.** Adds the partial unique index

    UNIQUE(schedule_id, scheduled_for_local) WHERE schedule_id IS NOT NULL

matching §06 "Generation" step 3 ("each candidate not already
present"): two runs of ``generate_task_occurrences`` over the same
horizon must not materialise the same ``(schedule_id,
scheduled_for_local)`` twice. Scoped to ``schedule_id IS NOT NULL``
so one-off tasks (no parent schedule) do not trip the unique on a
``NULL`` schedule_id. Both SQLite (3.24+) and PostgreSQL honour
partial unique indexes; the ``_where`` kwargs on the
:class:`~sqlalchemy.schema.Index` are dialect-specific, hence the
raw SQL body rendered by both backends below.

**Reversibility.** ``downgrade()`` drops the index and both columns.
Data in the added columns is discarded; occurrences in the new
columns revert to the cd-k4l shape (``starts_at`` only).

See ``docs/specs/02-domain-model.md`` §"occurrence" / §"task",
``docs/specs/06-tasks-and-scheduling.md`` §"Generation".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f6a9b8d2e1"
down_revision: str | Sequence[str] | None = "b2d4c8f3a5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("scheduled_for_local", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("originally_scheduled_for", sa.String(), nullable=True)
        )

    # Partial unique index. ``batch_alter_table`` does not pass the
    # dialect ``_where`` kwargs through SQLite's table-copy path, so
    # we emit the index at the top level. Both SQLite 3.24+ and
    # PostgreSQL accept the same ``WHERE schedule_id IS NOT NULL``
    # predicate; the ``sqlite_where`` / ``postgresql_where`` kwargs
    # tell SQLAlchemy which dialect to render against.
    op.create_index(
        "uq_occurrence_schedule_scheduled_for_local",
        "occurrence",
        ["schedule_id", "scheduled_for_local"],
        unique=True,
        sqlite_where=sa.text("schedule_id IS NOT NULL"),
        postgresql_where=sa.text("schedule_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_occurrence_schedule_scheduled_for_local", table_name="occurrence")
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.drop_column("originally_scheduled_for")
        batch_op.drop_column("scheduled_for_local")
