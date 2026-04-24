"""expense_reimbursement cd-9guk

Revision ID: b4d6e8fac3d4
Revises: a3c5d7e9b1f2
Create Date: 2026-04-24 23:00:00.000000

Extends ``expense_claim`` with the reimbursement snapshot columns
the cd-9guk manager-approval service writes at the
``approved → reimbursed`` transition (see
``docs/specs/09-time-payroll-expenses.md`` §"Reimbursement" /
§"Approval (owner or manager)"):

* ``reimbursed_at`` — UTC wall-clock the manager (or operator)
  marked the claim settled. Set at the same time as the state flip;
  immutable thereafter. Nullable until then.
* ``reimbursed_via`` — payment channel actually used to settle the
  claim. Enum (``cash | bank | card | other``) with a CHECK
  constraint that mirrors the ``state`` / ``category`` shape; the
  domain layer narrows the literal at write time. Nullable until
  the transition. The §09 spec routes the canonical reimbursement
  through the payslip's payout-period rollup; ``reimbursed_via``
  records the explicit "I paid this *now*, how" signal so a one-off
  cash hand-off (or an early bank transfer outside the period
  close) lands in the audit narrative without standing up the
  ``payout_destination`` table (still deferred — see the
  ``expense_claim`` model docstring's "deviation from cd-lbn's
  prose" note).
* ``reimbursed_by`` — soft-ref :class:`String` to the
  :class:`~app.adapters.db.identity.models.User` who actioned the
  settlement. Distinct from ``decided_by`` (the approver): the
  approver may be one manager and the settler another (a manager
  approving on Friday + an operator pushing funds on Monday is the
  common shape). Nullable until the transition.

Backfill: every existing row gets ``NULL`` in all three columns —
the migration runs against draft / submitted / approved / rejected
rows that have not yet observed a reimbursement transition. The
``reimbursed_via`` CHECK is a nullable guard, so existing rows pass
without rewriting.

The block uses ``batch_alter_table`` so SQLite (which lacks
``ALTER TABLE ADD CHECK`` support) round-trips the new column +
CHECK via the standard table-rebuild pattern. Both upgrade and
downgrade are reversible against either backend; the downgrade
drops the three columns + CHECK in one batch.

See ``docs/specs/02-domain-model.md`` §"expense_claim",
``docs/specs/09-time-payroll-expenses.md`` §"Reimbursement".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d6e8fac3d4"
down_revision: str | Sequence[str] | None = "a3c5d7e9b1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema.

    Add the three reimbursement-snapshot columns and the
    ``reimbursed_via`` CHECK constraint via ``batch_alter_table`` so
    SQLite's table-rebuild path handles the CHECK addition the same
    way it would on Postgres' direct ``ALTER TABLE``.
    """
    with op.batch_alter_table("expense_claim", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "reimbursed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "reimbursed_via",
                sa.String(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "reimbursed_by",
                sa.String(),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "reimbursed_via",
            "reimbursed_via IS NULL "
            "OR reimbursed_via IN ('cash', 'bank', 'card', 'other')",
        )


def downgrade() -> None:
    """Downgrade schema.

    Drop the CHECK first (so SQLite's rebuild path doesn't fight a
    constraint referencing a column we're about to remove), then the
    three columns. Reversible round-trip on both SQLite and Postgres.
    """
    with op.batch_alter_table("expense_claim", schema=None) as batch_op:
        # The naming convention prepends ``ck_<table>_`` automatically;
        # pass the unprefixed name (``reimbursed_via``) the same way the
        # sibling ``ical_feed`` and ``schedule`` migrations do.
        batch_op.drop_constraint("reimbursed_via", type_="check")
        batch_op.drop_column("reimbursed_by")
        batch_op.drop_column("reimbursed_via")
        batch_op.drop_column("reimbursed_at")
