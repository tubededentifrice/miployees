"""workspace_settings_json_cd_jdhm

Revision ID: a1b2c3d4e5f6
Revises: f7c9e1a4b5d8
Create Date: 2026-04-24 10:00:00.000000

Adds ``workspace.settings_json`` — the canonical home for workspace-
scoped operator-mutable settings (§02 "workspaces" + §"Settings
cascade"). The v1 slice of the ``workspace`` table (see
``20260419_2227_f247f50e8bee_workspace.py``) deferred this column to a
follow-up; cd-jdhm lands the minimal sliver the self-service recovery
kill-switch needs and keeps the shape compatible with the richer set
of settings cd-n6p (owner-only settings) will populate.

Shape:

* ``settings_json JSON NOT NULL DEFAULT '{}'`` — flat map of
  ``dotted.key → value`` holding concrete workspace defaults for every
  registered setting (§02 "Schema" under "Settings cascade"). Defaulted
  so existing rows backfill to an empty map without a data migration;
  ``NOT NULL`` so the resolver never needs to distinguish
  "absent row" from "absent key".

Not added here: the richer §02 surface (``verification_state``,
``signup_ip``, ``default_language`` / ``_currency`` / ``_country`` /
``_locale``, ``created_via``, ``created_by_user_id``) stays deferred
to cd-n6p / cd-055. The narrow column set keeps this migration
reversible without dragging in decisions those tasks own.

**Reversibility.** ``downgrade()`` drops the column outright;
settings written by the kill-switch are lost on rollback
(acceptable on a dev DB; an operator running a real rollback should
dump the table first — the column carries operator configuration,
not user data).

See ``docs/specs/02-domain-model.md`` §"workspaces" + §"Settings
cascade" and ``docs/specs/03-auth-and-tokens.md`` §"Workspace
kill-switch".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f7c9e1a4b5d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        # ``server_default`` backfills existing rows to ``'{}'``; the
        # ORM model defaults callers writing fresh rows to an empty
        # ``dict`` so the server default is ultimately only a backfill
        # seam. Kept ``NOT NULL`` so the resolver never branches on
        # "column absent" — the cascade layer reads the JSON payload
        # directly and a ``NULL`` would force a per-call coalesce.
        batch_op.add_column(
            sa.Column(
                "settings_json",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the ``settings_json`` column outright. Operator settings
    written into this column (including the recovery kill-switch) are
    lost on rollback — acceptable on a dev DB; an operator planning a
    real rollback should dump the table first since the content is
    operator configuration, not user data.
    """
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.drop_column("settings_json")
