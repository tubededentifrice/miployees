"""workspace_basics_cd_n6p

Revision ID: c4d6e8fab2c5
Revises: b3c5d7e9f1a2
Create Date: 2026-04-26 12:00:00.000000

Lands the four owner-mutable workspace base columns plus the
``updated_at`` cache-invalidation seam (cd-n6p):

* ``default_timezone TEXT NOT NULL DEFAULT 'UTC'`` — IANA tz database
  identifier. Used for property-level fallback display tz (§02
  "workspaces" base columns).
* ``default_locale TEXT NOT NULL DEFAULT 'en'`` — BCP-47 tag from
  the shipped locale list (§02 "workspaces"; §14 "Workspace
  settings"). NOT NULL with the conservative ``'en'`` server default
  so readers never coalesce a NULL to a fallback value. cd-n6p also
  narrows §02's row description to drop the legacy ``text?``
  nullability marker — the v0 plan to derive the locale from
  ``default_language`` + ``default_country`` is dropped for v1
  (neither column ships in this migration), so the locale is the
  canonical key.
* ``default_currency TEXT NOT NULL DEFAULT 'USD'`` — ISO-4217
  alpha-3 (§02 "workspaces"). The DB CHECK enforces ``LENGTH = 3``
  + uppercase ASCII; the domain layer narrows further to the
  shipped allow-list at :mod:`app.util.currency`.
* ``updated_at TIMESTAMP WITH TIME ZONE NOT NULL`` — bumped on every
  basics edit so SSE subscribers can refresh the workspace picker
  after an owner renames the workspace or changes the default
  formatting (§14). Backfilled to ``created_at`` for existing rows
  so the column is NOT NULL without a magic sentinel.

The §02 workspace row description also lists ``verification_state``,
``signup_ip``, ``default_language``, ``default_country``,
``created_via``, ``created_by_user_id`` as base columns. Those stay
deferred to cd-055 (signup quotas) so this migration's surface
matches exactly what the owner-settings service needs: a tight
diff that can roll back without touching unrelated columns.

**Reversibility.** ``downgrade()`` drops every added column. Owner
edits to ``name`` / timezone / locale / currency persist on the
``name`` column (already there) and are lost on the three new
columns; ``updated_at`` is purely advisory so its loss is harmless.
An operator running a real rollback should ``pg_dump`` the table
first.

**Pattern.** ``add_column`` lands inside ``batch_alter_table`` so
SQLite's table-rebuild path stays clean. ``updated_at`` is added
nullable, backfilled to ``created_at``, then promoted to
NOT NULL — the same shape :file:`20260424_1100_b2c3d4e5f6a7_property_cd_8u5.py`
uses for the same column on ``property``. The three text columns
ride a ``server_default`` so backfill is implicit (existing rows
materialise the constant on read).

See ``docs/specs/02-domain-model.md`` §"workspaces" and
§"Settings cascade", ``docs/specs/05-employees-and-roles.md``
§"Surface grants at a glance" (owners as governance anchor),
``docs/specs/14-web-frontend.md`` §"Workspace settings".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d6e8fab2c5"
down_revision: str | Sequence[str] | None = "b3c5d7e9f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        # 1. Three owner-mutable text columns. ``server_default``
        #    backfills existing rows to the conservative defaults
        #    (UTC / en / USD); the service writes explicit values on
        #    every PATCH so the server default is ultimately just a
        #    backfill seam.
        batch_op.add_column(
            sa.Column(
                "default_timezone",
                sa.String(),
                nullable=False,
                server_default="UTC",
            )
        )
        batch_op.add_column(
            sa.Column(
                "default_locale",
                sa.String(),
                nullable=False,
                server_default="en",
            )
        )
        batch_op.add_column(
            sa.Column(
                "default_currency",
                sa.String(),
                nullable=False,
                server_default="USD",
            )
        )

        # 2. ``updated_at`` lands nullable so the migration is cheap
        #    on a large table. Backfilled below to ``created_at`` so
        #    every existing row carries a coherent value, then
        #    promoted to NOT NULL. ``server_default = CURRENT_TIMESTAMP``
        #    so a fresh INSERT that does not name the column (the
        #    pattern in unit-test fixtures and any future bootstrap
        #    helper that predates the column) still lands a coherent
        #    value rather than failing the NOT NULL contract; the
        #    domain service always writes an explicit value, so the
        #    server default is purely a safety net.
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )

    # Backfill ``updated_at`` from ``created_at`` so the NOT NULL
    # promotion below has no orphan rows. Portable across dialects.
    op.execute("UPDATE workspace SET updated_at = created_at WHERE updated_at IS NULL")

    with op.batch_alter_table("workspace", schema=None) as batch_op:
        # Promote ``updated_at`` to NOT NULL once the backfill is
        # complete. Same shape as cd-8u5's ``property.updated_at``
        # rollout but tightened — ``workspace.updated_at`` is the
        # cache-invalidation seam SSE subscribers key off (§14), so
        # NULL would force every reader to coalesce defensively.
        batch_op.alter_column(
            "updated_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )

        # Defence-in-depth shape check on the currency column. The
        # full ISO-4217 narrowing lives in :mod:`app.util.currency`;
        # the CHECK only catches the obvious ``LENGTH = 3`` violation
        # so a corrupt write without service mediation cannot land an
        # ``EURO`` or empty string. Mirrors the shape of the property
        # table's ``country`` CHECK (cd-8u5).
        batch_op.create_check_constraint(
            "default_currency_shape",
            "LENGTH(default_currency) = 3",
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the four columns added in :func:`upgrade`. Owner edits to
    timezone / locale / currency are lost on rollback (acceptable on
    a dev DB; an operator running a real rollback should ``pg_dump``
    the table first since the content is operator configuration).
    """
    with op.batch_alter_table("workspace", schema=None) as batch_op:
        batch_op.drop_constraint("default_currency_shape", type_="check")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("default_currency")
        batch_op.drop_column("default_locale")
        batch_op.drop_column("default_timezone")
