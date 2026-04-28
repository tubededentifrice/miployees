"""payslip status for pay-period paid guard

Revision ID: e3f5a7b9c1d3
Revises: d2e4f6a8b0c2
Create Date: 2026-04-28 13:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e3f5a7b9c1d3"
down_revision = "d2e4f6a8b0c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(), nullable=False, server_default="draft")
        )
        batch_op.add_column(
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_check_constraint(
            op.f("ck_payslip_status"),
            "status IN ('draft', 'issued', 'paid', 'voided')",
        )

    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.alter_column("status", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_payslip_status"), type_="check")
        batch_op.drop_column("paid_at")
        batch_op.drop_column("issued_at")
        batch_op.drop_column("status")
