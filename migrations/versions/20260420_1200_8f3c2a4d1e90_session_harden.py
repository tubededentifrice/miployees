"""session_harden

Revision ID: 8f3c2a4d1e90
Revises: c9a4b3d2e8f1
Create Date: 2026-04-20 12:00:00.000000

Adds the hardening columns pinned by cd-geqp / spec §15 "Cookies" /
§"Passkey specifics" on ``session``:

* ``absolute_expires_at`` — the 90-day hard cutoff, separate from the
  sliding ``expires_at``. A stolen cookie that keeps refreshing past
  half-life still hits the wall here.
* ``fingerprint_hash`` — SHA-256 of ``User-Agent + "\n" +
  Accept-Language`` under an HKDF-peppered key. Mismatch on
  :func:`app.auth.session.validate` forces re-auth.
* ``invalidated_at`` / ``invalidation_cause`` — non-destructive
  invalidation (row preserved for forensics) for events like passkey
  registration, recovery consumption, and sign-count rollback.

**All four columns are nullable** — the two expiry / fingerprint
columns because pre-hardening rows don't have the raw inputs we'd need
to backfill them (we never stored plaintext UA / Accept-Language; §15
PII minimisation), and the invalidation pair because a live session
carries ``NULL`` there by definition.

**Reversibility.** ``downgrade()`` drops every added column and is a
no-op for data (dropping columns discards the values — acceptable for
a rollback of a hardening feature; an operator who needs to keep the
forensic trail should dump the table first).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f3c2a4d1e90"
down_revision: str | Sequence[str] | None = "c9a4b3d2e8f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "absolute_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("fingerprint_hash", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "invalidated_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("invalidation_cause", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.drop_column("invalidation_cause")
        batch_op.drop_column("invalidated_at")
        batch_op.drop_column("fingerprint_hash")
        batch_op.drop_column("absolute_expires_at")
