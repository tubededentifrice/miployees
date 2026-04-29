"""nl task preview cd-6lwh

Revision ID: c3e5f7a9b1d2
Revises: b2d4f6a8c0e2
Create Date: 2026-04-29 17:00:00.000000

Persist short-lived natural-language task intake previews.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3e5f7a9b1d2"
down_revision: str | Sequence[str] | None = "b2d4f6a8c0e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "nl_task_preview",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("requested_by_user_id", sa.String(), nullable=True),
        sa.Column("original_text", sa.String(), nullable=False),
        sa.Column("resolved_json", sa.JSON(), nullable=False),
        sa.Column("assumptions_json", sa.JSON(), nullable=False),
        sa.Column("ambiguities_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["user.id"],
            name=op.f("fk_nl_task_preview_requested_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_nl_task_preview_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_nl_task_preview")),
    )
    op.create_index(
        "ix_nl_task_preview_workspace_expires",
        "nl_task_preview",
        ["workspace_id", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_nl_task_preview_workspace_expires", table_name="nl_task_preview")
    op.drop_table("nl_task_preview")
