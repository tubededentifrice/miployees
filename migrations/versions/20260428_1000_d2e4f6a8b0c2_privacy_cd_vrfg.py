"""privacy_cd_vrfg

Revision ID: d2e4f6a8b0c2
Revises: c0d3e6f9a2b5
Create Date: 2026-04-28 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d2e4f6a8b0c2"
down_revision = "c0d3e6f9a2b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "privacy_export",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("blob_hash", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_privacy_export")),
    )
    with op.batch_alter_table("privacy_export", schema=None) as batch_op:
        batch_op.create_index(
            "ix_privacy_export_user_requested",
            ["user_id", "requested_at"],
            unique=False,
        )

    op.create_table(
        "payout_destination",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("display_stub", sa.String(), nullable=True),
        sa.Column("secret_ref_id", sa.String(), nullable=True),
        sa.Column("country", sa.String(), nullable=True),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "LENGTH(currency) = 3",
            name=op.f("ck_payout_destination_currency_length"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_payout_destination_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_payout_destination_user_id_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payout_destination")),
    )
    with op.batch_alter_table("payout_destination", schema=None) as batch_op:
        batch_op.create_index(
            "ix_payout_destination_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )

    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.add_column(sa.Column("payout_snapshot_json", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "payout_manifest_purged_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("payslip", schema=None) as batch_op:
        batch_op.drop_column("payout_manifest_purged_at")
        batch_op.drop_column("payout_snapshot_json")

    with op.batch_alter_table("payout_destination", schema=None) as batch_op:
        batch_op.drop_index("ix_payout_destination_workspace_user")
    op.drop_table("payout_destination")

    with op.batch_alter_table("privacy_export", schema=None) as batch_op:
        batch_op.drop_index("ix_privacy_export_user_requested")
    op.drop_table("privacy_export")
