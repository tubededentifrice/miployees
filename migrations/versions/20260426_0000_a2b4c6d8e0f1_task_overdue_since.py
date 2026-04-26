"""task_overdue_since

Revision ID: a2b4c6d8e0f1
Revises: d1e3f5a7b9c2
Create Date: 2026-04-26 00:00:00.000000

Lands the spec-drift ``occurrence.overdue_since`` column the
:func:`detect_overdue` worker (cd-hurw) needs. §06 "State machine"
records ``overdue`` as a soft state set by the sweeper when
``due_by_utc`` is past; ``overdue_since`` carries the timestamp of
the flip and is cleared on any manual transition. The current
``occurrence.state`` CHECK does not yet permit ``'overdue'`` either
(cd-7am7 left the widening as follow-up); both come together so the
sweeper can write a legal row + remember when it slipped.

Shape:

* ``overdue_since TIMESTAMP WITH TIME ZONE NULL`` — null on every
  non-overdue row; set to the flip time when the sweeper transitions
  ``state='overdue'``. No server default — null IS the "not currently
  overdue" sentinel.
* B-tree composite index ``(workspace_id, state, overdue_since)`` so
  the sweeper's "find every task in scheduled / pending / in_progress
  whose ends_at + grace < now" predicate stays cheap on big tenants
  (the ``state IN (...)`` prefix is the selective leg, ``overdue_since``
  trails for the secondary "already overdue, skip" branch).
* Backfill: every existing row with ``state='overdue'`` gets
  ``overdue_since = COALESCE(ends_at + 15 minutes, NOW())``. The
  15-minute default matches the spec's
  ``tasks.overdue_grace_minutes`` default. Today the prior CHECK
  forbade the ``'overdue'`` state altogether so no row is touched in
  practice; the UPDATE is kept for forward-compat and so a manual
  pre-migration write of the new state would still pick up a coherent
  ``overdue_since``.
* Widens the ``occurrence.state`` CHECK to include ``'overdue'``
  alongside the cd-k4l set (``scheduled / pending / in_progress /
  done / skipped / approved / cancelled``). Without the widening the
  sweeper's UPDATE would trip the constraint.

**Reversibility.** ``downgrade()`` drops the index + column and
narrows the CHECK back to the cd-k4l set. Rows the sweeper had
flipped to ``'overdue'`` would fail the narrower CHECK on rewrite;
the downgrade is best-effort for a dev DB. An operator running a
real rollback should first walk every ``state='overdue'`` row back
to ``'pending'`` (or whatever the manual-transition target was).

See ``docs/specs/06-tasks-and-scheduling.md`` §"State machine" and
``docs/specs/02-domain-model.md`` §"task" / §"occurrence".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b4c6d8e0f1"
down_revision: str | Sequence[str] | None = "d1e3f5a7b9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Default grace window the spec pins for ``tasks.overdue_grace_minutes``.
# Repeated here so the backfill stays self-contained (no Python import
# from the worker module — the migration is the source of truth at
# upgrade time, and a live worker may not yet exist on the migrating
# host).
_DEFAULT_OVERDUE_GRACE_MINUTES: int = 15


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Add the column. Nullable; no server default — null is the
    #    "not currently overdue" sentinel and forcing every row to
    #    carry an explicit value would muddy the contract.
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("overdue_since", sa.DateTime(timezone=True), nullable=True)
        )

    # 2. Backfill any existing ``state='overdue'`` row. Today the
    #    CHECK constraint (cd-k4l) forbids the value so this UPDATE
    #    matches zero rows in practice; kept for forward-compat and
    #    in case an operator hand-wrote the state before this
    #    migration ran. ``COALESCE(ends_at + interval, NOW())``
    #    matches the spec default of 15 minutes after the SLA.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "UPDATE occurrence "
                "SET overdue_since = COALESCE("
                "    ends_at + (:grace_minutes * INTERVAL '1 minute'), NOW()"
                ") "
                "WHERE state = 'overdue' AND overdue_since IS NULL"
            ),
            {"grace_minutes": _DEFAULT_OVERDUE_GRACE_MINUTES},
        )
    else:
        # SQLite: ``datetime(ends_at, '+15 minutes')`` mirrors the
        # PostgreSQL ``INTERVAL`` math; ``CURRENT_TIMESTAMP`` is the
        # naive-UTC fallback when ``ends_at`` is null (which the v1
        # schema rules out via NOT NULL, but the COALESCE keeps the
        # statement defensive).
        bind.execute(
            sa.text(
                "UPDATE occurrence "
                "SET overdue_since = COALESCE("
                "    datetime(ends_at, :grace_modifier), CURRENT_TIMESTAMP"
                ") "
                "WHERE state = 'overdue' AND overdue_since IS NULL"
            ),
            {"grace_modifier": f"+{_DEFAULT_OVERDUE_GRACE_MINUTES} minutes"},
        )

    # 3. Widen the ``state`` CHECK to admit ``'overdue'``. Drop +
    #    re-create under ``batch_alter_table`` so SQLite's
    #    table-rebuild path stays clean.
    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.drop_constraint("state", type_="check")
        batch_op.create_check_constraint(
            "state",
            "state IN ('scheduled', 'pending', 'in_progress', 'done', "
            "'skipped', 'approved', 'cancelled', 'overdue')",
        )

    # 4. Composite index for the sweeper's hot path. Emit at top level
    #    rather than inside ``batch_alter_table`` so a future partial
    #    or dialect-specific kwarg can land here without rebuilding
    #    the whole table.
    op.create_index(
        "ix_occurrence_workspace_state_overdue_since",
        "occurrence",
        ["workspace_id", "state", "overdue_since"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the composite index and the column, then narrows the CHECK
    constraint back to the cd-k4l set. Rows the sweeper had flipped
    to ``'overdue'`` would fail the narrower CHECK on rewrite; the
    downgrade is best-effort for a dev DB. An operator planning a
    real rollback should first walk every ``state='overdue'`` row
    back to ``'pending'`` so the constraint reinstatement does not
    fail at table-rebuild time.
    """
    op.drop_index(
        "ix_occurrence_workspace_state_overdue_since", table_name="occurrence"
    )

    with op.batch_alter_table("occurrence", schema=None) as batch_op:
        batch_op.drop_constraint("state", type_="check")
        batch_op.create_check_constraint(
            "state",
            "state IN ('scheduled', 'pending', 'in_progress', 'done', "
            "'skipped', 'approved', 'cancelled')",
        )
        batch_op.drop_column("overdue_since")
