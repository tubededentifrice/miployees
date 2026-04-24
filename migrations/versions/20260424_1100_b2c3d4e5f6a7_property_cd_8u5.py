"""property_cd_8u5

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-24 11:00:00.000000

Extends ``property`` with the richer §02 / §04 columns the property
domain service (cd-8u5) needs. The v1 slice (cd-i6u, migration
``89ebd89b9de4``) landed only the minimum shared by every downstream
context — ``address`` (text), ``timezone``, optional ``lat`` / ``lon``,
a ``tags_json`` payload and ``created_at``. This migration adds the
spec-level fields the manager UI and the ``PropertyCreate`` /
``PropertyView`` DTOs reference:

* ``name`` — human-visible display name ("Villa Sud", "Apt 3B"). v1
  rows had none; we backfill from ``address`` so the first line of
  the legacy blob shows in lists after the migration.
* ``kind`` — ``residence | vacation | str | mixed``. Drives default
  lifecycle rule + area seeding behaviour (§04 "`kind` semantics").
  Server default ``residence`` matches the most conservative seed
  (no auto-rules) so legacy rows do not spontaneously grow
  lifecycle rules.
* ``address_json`` — canonical structured address. Empty object for
  legacy rows; the domain service back-fills ``country`` in both
  directions on write (§04 "`address_json` canonical shape").
* ``country`` — ISO-3166-1 alpha-2 country code. Authoritative
  source: ``address_json.country`` when present, else workspace
  default. Backfilled with a safe placeholder (``XX``) for legacy
  rows so the service layer can repair them on the next update.
* ``locale`` — BCP-47 locale tag. Nullable: when null, derived from
  workspace language + ``country``.
* ``default_currency`` — ISO-4217. Nullable: inherits workspace
  currency when unset.
* ``client_org_id`` — soft reference to the future ``organization``
  table (cd-t8m / §22). Nullable + no FK; the column is a plain
  ``String`` until the ``organization`` table lands, matching the
  ``role_grant.scope_property_id`` convention.
* ``owner_user_id`` — display-only pointer to ``users.id`` (§04
  "owner of record"). Soft reference for the same reason as above —
  the FK promotion happens alongside a broader tenancy-join
  refactor.
* ``welcome_defaults_json`` — JSON blob used by the guest welcome
  page (§04 "Welcome defaults"). Empty object by default.
* ``property_notes_md`` — internal staff-visible notes. Empty string
  by default.
* ``updated_at`` — mutation timestamp. Nullable + backfilled from
  ``created_at`` so reads never see ``NULL``.
* ``deleted_at`` — soft-delete marker. Nullable; live rows carry
  ``NULL``. Every domain-level list honours it via the ``deleted``
  filter.

**All new columns are nullable or carry server defaults** so
existing rows survive the migration without a bespoke backfill
statement. The CRUD service (``app/domain/places/property_service.py``)
fills them on write.

**CHECK on ``kind``.** ``kind IN ('residence', 'vacation', 'str',
'mixed')`` — defence-in-depth against a buggy caller. The domain
layer narrows the string to a ``Literal`` on read.

**Reversibility.** ``downgrade()`` drops every added column. Data in
the added columns is discarded — acceptable for a rollback of a
feature extension on a dev database.

See ``docs/specs/02-domain-model.md`` §"property",
``docs/specs/04-properties-and-stays.md`` §"Property" /
§"`address_json` canonical shape" / §"`kind` semantics".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("property", schema=None) as batch_op:
        # Display / identity columns. ``name`` added nullable so pre-
        # existing rows survive; we backfill from ``address`` below.
        batch_op.add_column(sa.Column("name", sa.String(), nullable=True))

        # ``kind`` drives area + lifecycle-rule seeding. ``residence``
        # is the most conservative default (no auto-rules) so legacy
        # rows don't spontaneously grow scheduling behaviour.
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(),
                nullable=False,
                server_default="residence",
            )
        )

        # Structured address + country. ``country`` backfills with the
        # placeholder ``XX``: the domain service repairs it on the next
        # update via the address_json.country ↔ country back-fill.
        batch_op.add_column(
            sa.Column(
                "address_json",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "country",
                sa.String(),
                nullable=False,
                server_default="XX",
            )
        )

        # Locale + currency: nullable, inherit workspace on read.
        batch_op.add_column(sa.Column("locale", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("default_currency", sa.String(), nullable=True))

        # Billing / ownership pointers — soft references per the
        # module-level rationale.
        batch_op.add_column(sa.Column("client_org_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("owner_user_id", sa.String(), nullable=True))

        # Welcome payload + staff-visible notes.
        batch_op.add_column(
            sa.Column(
                "welcome_defaults_json",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "property_notes_md",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )

        # Timestamps. ``updated_at`` nullable so the migration is cheap
        # on a large table; the backfill below pins it to ``created_at``.
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )

        # CHECK-enforced enum. Matches §04 "`kind` semantics".
        batch_op.create_check_constraint(
            "kind",
            "kind IN ('residence', 'vacation', 'str', 'mixed')",
        )

        # Index on ``deleted_at`` for the common "live list" query:
        # ``WHERE deleted_at IS NULL``.
        batch_op.create_index(
            "ix_property_deleted",
            ["deleted_at"],
            unique=False,
        )

    # Backfill the renamed / derived columns so existing rows stay
    # readable through the new service surface. ``address`` survives
    # untouched as the single-line rendering; ``address_json`` starts
    # empty for legacy rows and is repaired on the next write.
    op.execute("UPDATE property SET name = address WHERE name IS NULL")
    op.execute("UPDATE property SET updated_at = created_at WHERE updated_at IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("property", schema=None) as batch_op:
        batch_op.drop_index("ix_property_deleted")
        # Raw body matches the create-side convention (see cd-0tg
        # migration) — Alembic's naming convention renders the final
        # constraint name from this body.
        batch_op.drop_constraint("kind", type_="check")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("property_notes_md")
        batch_op.drop_column("welcome_defaults_json")
        batch_op.drop_column("owner_user_id")
        batch_op.drop_column("client_org_id")
        batch_op.drop_column("default_currency")
        batch_op.drop_column("locale")
        batch_op.drop_column("country")
        batch_op.drop_column("address_json")
        batch_op.drop_column("kind")
        batch_op.drop_column("name")
