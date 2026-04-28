"""task_evidence_cd_wajj

Revision ID: b6d8f0a2c4e6
Revises: a5c7e9b1d3f5
Create Date: 2026-04-28 17:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d8f0a2c4e6"
down_revision: str | Sequence[str] | None = "a5c7e9b1d3f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("evidence", schema=None) as batch_op:
        batch_op.drop_constraint("kind", type_="check")
        batch_op.add_column(sa.Column("gps_lat", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("gps_lon", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column("checklist_snapshot_json", sa.JSON(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_check_constraint(
            "kind",
            "kind IN ('photo', 'note', 'voice', 'gps', 'checklist_snapshot')",
        )
        batch_op.create_index(
            "ix_evidence_workspace_deleted",
            ["workspace_id", "deleted_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM evidence WHERE kind = 'checklist_snapshot'")
    with op.batch_alter_table("evidence", schema=None) as batch_op:
        batch_op.drop_index("ix_evidence_workspace_deleted")
        batch_op.drop_constraint("kind", type_="check")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("checklist_snapshot_json")
        batch_op.drop_column("gps_lon")
        batch_op.drop_column("gps_lat")
        batch_op.create_check_constraint(
            "kind",
            "kind IN ('photo', 'note', 'voice', 'gps')",
        )
