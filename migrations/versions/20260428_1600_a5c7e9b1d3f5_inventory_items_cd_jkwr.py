"""inventory_items_cd_jkwr

Revision ID: a5c7e9b1d3f5
Revises: f4a6b8c0d2e4
Create Date: 2026-04-28 16:00:00.000000

No-op compatibility revision.

The unshipped base inventory migration now emits the final
property-scoped item schema directly for cd-n6ga. This revision remains
in the chain so existing Alembic heads stay stable.
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a5c7e9b1d3f5"
down_revision: str | Sequence[str] | None = "f4a6b8c0d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""


def downgrade() -> None:
    """Downgrade schema."""
