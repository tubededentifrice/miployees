"""deployment_setting

Revision ID: 6cb7f0a6f8a3
Revises:
Create Date: 2026-04-19 21:24:18.595378

Creates the deployment-wide operator-mutable settings table consumed
by :mod:`app.capabilities` (see ``docs/specs/01-architecture.md``
§"Capability registry"). The table is **not** workspace-scoped: rows
govern the whole deployment (signup open/closed, default LLM budget,
etc.) and must be reachable during signup itself, before any
workspace exists.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6cb7f0a6f8a3"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_setting",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_deployment_setting")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("deployment_setting")
