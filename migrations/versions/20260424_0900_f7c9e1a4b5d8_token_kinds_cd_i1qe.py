"""token_kinds_cd_i1qe

Revision ID: f7c9e1a4b5d8
Revises: e6f8c0b4a2d7
Create Date: 2026-04-24 09:00:00.000000

Extends ``api_token`` with the three-kind discriminator
(``scoped | delegated | personal``) the §03 "API tokens" section has
described since cd-c91 landed but which no migration carried yet. Lands
alongside cd-i1qe so the next runtime surface (delegated + PAT) can
mint rows without stepping over the existing scoped-only invariant.

Columns added:

* ``kind`` — ``VARCHAR NOT NULL DEFAULT 'scoped'``. Stored as plain
  text (not an enum type) so the constraint set is identical on
  SQLite and Postgres; the CHECK constraint below pins the allowed
  values. The ``'scoped'`` default backfills every existing cd-c91
  row (all of which are scoped, workspace-pinned tokens) without
  needing a data migration.
* ``delegate_for_user_id`` — ``VARCHAR NULL`` with an
  ``ON DELETE SET NULL`` FK to ``user.id``. Populated only when
  ``kind = 'delegated'`` (the row carries no useful authority once
  the delegating user is gone — the service layer already returns
  401, and the null-on-delete keeps the audit trail joinable).
* ``subject_user_id`` — ``VARCHAR NULL`` with an ``ON DELETE SET NULL``
  FK to ``user.id``. Populated only when ``kind = 'personal'``
  (mutually exclusive with ``delegate_for_user_id`` per §03
  "Personal access tokens"). Same ``SET NULL`` rationale.

Column widening:

* ``workspace_id`` — ``NOT NULL`` → ``NULL``. Personal access tokens
  live at the identity scope, not a workspace, so PAT rows write
  ``workspace_id = NULL``. Existing cd-c91 rows (all ``scoped``)
  keep a populated ``workspace_id``; the domain service enforces
  the "scoped + delegated rows MUST carry a workspace_id, personal
  rows MUST NOT" invariant in ``mint()``, reinforced by the CHECK
  constraint added below.

Constraints added:

* ``ck_api_token_kind`` — ``kind IN ('scoped', 'delegated',
  'personal')``. Belts-and-braces for a service bug that tries to
  write an unknown kind; the domain vocabulary is the enforcement
  point but DB-level CHECKs stay cheap and catch the programmer
  error loudly.
* ``ck_api_token_kind_shape`` — the mutual-exclusion + workspace
  invariant, keyed off ``kind``:
    - ``scoped``: ``delegate_for_user_id IS NULL AND
      subject_user_id IS NULL AND workspace_id IS NOT NULL``
    - ``delegated``: ``delegate_for_user_id IS NOT NULL AND
      subject_user_id IS NULL AND workspace_id IS NOT NULL``
    - ``personal``: ``subject_user_id IS NOT NULL AND
      delegate_for_user_id IS NULL AND workspace_id IS NULL``
  A row that violates this shape is a service bug we want to fail
  loud at INSERT time; the clause is portable across SQLite and
  Postgres (no dialect-specific operators).

No new indexes: the existing ``ix_api_token_user`` +
``ix_api_token_workspace`` already cover the list / cap-count
queries. The revoke-cascade paths read on ``delegate_for_user_id``
/ ``subject_user_id`` by first resolving the ``user`` row and then
narrowing — the user PK already indexes that side of the FK, and a
standalone index on either nullable column would be mostly NULL
rows for the common scoped-only workspace. We can add one in a
follow-up if a production query profile justifies it.

**Reversibility.** ``downgrade()`` DELETEs every ``kind='personal'``
row first (they carry ``workspace_id = NULL`` which the narrower
``NOT NULL`` column cannot hold — SQLite's batch copy and Postgres'
``ALTER COLUMN`` would both fail without the purge), then drops
the CHECK constraints + FKs + three added columns, and narrows
``workspace_id`` back to ``NOT NULL``. ``scoped`` and ``delegated``
rows survive the rollback with their workspace pin intact. PAT row
loss is acceptable on a dev DB rollback and operators planning a
real rollback must accept the cascade: this is documented in the
``downgrade()`` docstring so the loss is never silent.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens" /
§"Delegated tokens" / §"Personal access tokens" and
``docs/specs/02-domain-model.md`` §"api_token".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7c9e1a4b5d8"
down_revision: str | Sequence[str] | None = "e6f8c0b4a2d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_KIND_VALUES: tuple[str, ...] = ("scoped", "delegated", "personal")


def _kind_in_clause() -> str:
    return "'" + "', '".join(_KIND_VALUES) + "'"


# Shape invariant — keyed off ``kind``. Kept as a module constant so
# upgrade and downgrade share one rule (the downgrade needs to drop
# this constraint under the same name the upgrade created).
_SHAPE_CK: str = (
    "("
    "(kind = 'scoped' AND delegate_for_user_id IS NULL "
    "AND subject_user_id IS NULL AND workspace_id IS NOT NULL)"
    " OR "
    "(kind = 'delegated' AND delegate_for_user_id IS NOT NULL "
    "AND subject_user_id IS NULL AND workspace_id IS NOT NULL)"
    " OR "
    "(kind = 'personal' AND subject_user_id IS NOT NULL "
    "AND delegate_for_user_id IS NULL AND workspace_id IS NULL)"
    ")"
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("api_token", schema=None) as batch_op:
        # ``workspace_id`` widens to nullable so personal access tokens
        # (no workspace) can land. Existing rows remain populated; the
        # CHECK below re-ties the invariant by kind.
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(),
            nullable=True,
        )

        # ``kind`` — the new discriminator. DEFAULT 'scoped' so existing
        # rows backfill without a data migration; every new scoped mint
        # carries the same value explicitly, so the server default is
        # ultimately only a backfill mechanism.
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(),
                nullable=False,
                server_default="scoped",
            )
        )

        # ``delegate_for_user_id`` — populated only for ``delegated``
        # rows. ``SET NULL`` on delete keeps the audit trail joinable
        # after a user hard-delete; the domain service already returns
        # 401 on a delegated token whose user is archived / gone.
        batch_op.add_column(
            sa.Column("delegate_for_user_id", sa.String(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_api_token_delegate_for_user",
            referent_table="user",
            local_cols=["delegate_for_user_id"],
            remote_cols=["id"],
            ondelete="SET NULL",
        )

        # ``subject_user_id`` — populated only for ``personal`` rows.
        # Mutually exclusive with ``delegate_for_user_id`` per §03.
        batch_op.add_column(sa.Column("subject_user_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_api_token_subject_user",
            referent_table="user",
            local_cols=["subject_user_id"],
            remote_cols=["id"],
            ondelete="SET NULL",
        )

        # CHECK gates. ``ck_api_token_kind`` is the value allowlist;
        # ``ck_api_token_kind_shape`` enforces the mutual-exclusion +
        # workspace invariant.
        batch_op.create_check_constraint(
            "ck_api_token_kind",
            f"kind IN ({_kind_in_clause()})",
        )
        batch_op.create_check_constraint("ck_api_token_kind_shape", _SHAPE_CK)


def downgrade() -> None:
    """Downgrade schema.

    Deletes every ``personal`` row FIRST — those rows carry
    ``workspace_id = NULL`` which violates the narrower ``NOT NULL``
    the column gets back below, and leaving them in place would make
    the ``ALTER COLUMN ... SET NOT NULL`` step fail on Postgres (and
    fail the batch table-copy on SQLite). PATs have no representation
    under the pre-cd-i1qe schema, so deletion is the only reversible
    choice — flagged as "acceptable rollback cost on a dev DB" in the
    module docstring. Operators planning a real rollback must accept
    the loss of every ``kind='personal'`` row.

    Then drops the CHECK constraints + FKs + three added columns in
    reverse order, and narrows ``workspace_id`` back to ``NOT NULL``.
    ``scoped`` and ``delegated`` rows keep their workspace pin and
    survive the rollback.
    """
    # Purge PAT rows before narrowing ``workspace_id`` — they're the
    # only rows that can carry NULL in that column. Use a raw
    # ``DELETE`` rather than an ORM fetch so the step works with the
    # pre-cd-i1qe model import (which would reject the new columns
    # via the CHECK constraint still in place at this point).
    op.execute("DELETE FROM api_token WHERE kind = 'personal'")

    with op.batch_alter_table("api_token", schema=None) as batch_op:
        batch_op.drop_constraint("ck_api_token_kind_shape", type_="check")
        batch_op.drop_constraint("ck_api_token_kind", type_="check")
        batch_op.drop_constraint(
            "fk_api_token_subject_user",
            type_="foreignkey",
        )
        batch_op.drop_column("subject_user_id")
        batch_op.drop_constraint(
            "fk_api_token_delegate_for_user",
            type_="foreignkey",
        )
        batch_op.drop_column("delegate_for_user_id")
        batch_op.drop_column("kind")
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(),
            nullable=False,
        )
