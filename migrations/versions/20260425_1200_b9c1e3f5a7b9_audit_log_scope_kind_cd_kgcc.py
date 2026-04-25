"""audit_log_scope_kind_cd_kgcc

Revision ID: b9c1e3f5a7b9
Revises: a8b0d2e4f6a8
Create Date: 2026-04-25 12:00:00.000000

Adds the **deployment scope** to ``audit_log`` so admin mutations
(§12 "Admin surface" → ``GET /admin/api/v1/audit``) can write a
deployment-scoped audit row that lives outside any workspace. Today
the table is workspace-scoped (``workspace_id NOT NULL``) and cannot
represent an admin action whose subject is the deployment itself
(token mint/revoke against an operator's identity, deployment-setting
edit, signup-policy change, …).

Shape changes mirror the cd-wchi widening on ``role_grant``:

* ``scope_kind`` text NOT NULL DEFAULT ``'workspace'`` —
  ``CHECK (scope_kind IN ('workspace', 'deployment'))``. Backfilled
  to ``'workspace'`` for every existing row, then the server-default
  is dropped so future writes must declare the value explicitly.
* ``workspace_id`` widened to **NULLABLE**. The new pairing CHECK
  ``(scope_kind='deployment' AND workspace_id IS NULL) OR
  (scope_kind='workspace' AND workspace_id IS NOT NULL)`` enforces
  the biconditional invariant at the DB level — defence-in-depth for
  the application's writer (:mod:`app.audit`).
* New index ``ix_audit_log_scope_kind_created`` on
  ``(scope_kind, created_at)`` — backs the
  ``GET /admin/api/v1/audit`` feed (§12), which lists deployment rows
  newest-first. The existing per-workspace timeline index
  (``ix_audit_log_workspace_created``) does **not** cover the
  deployment-scope partition because every deployment row carries
  ``workspace_id IS NULL`` and the index is keyed off ``workspace_id``
  first.

**Backfill safety.** The new column lands with a server-default of
``'workspace'`` so the ``ALTER TABLE … ADD COLUMN`` step does not
leave existing rows with NULL. We follow up with an explicit UPDATE
to make the backfill defensive against future server-default
removals, then drop the server-default so the application is forced
to declare ``scope_kind`` on every insert.

**Reversibility.** ``downgrade()`` reverses every step. Any rows
that were inserted with ``scope_kind='deployment'`` are deleted on
downgrade — they cannot survive a ``workspace_id NOT NULL`` schema.
A real production rollback should dump those rows first; on a dev
DB they were never expected to outlive the migration.

See ``docs/specs/02-domain-model.md`` §"audit_log",
``docs/specs/12-rest-api.md`` §"Admin surface" → "Deployment audit",
and ``docs/specs/15-security-privacy.md`` §"Audit log".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c1e3f5a7b9"
down_revision: str | Sequence[str] | None = "a8b0d2e4f6a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Add ``scope_kind`` with a server-default so the ``ADD COLUMN``
    #    step does not stamp existing rows with NULL. We drop the
    #    server-default below — production callers must declare the
    #    column on every write.
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "scope_kind",
                sa.String(),
                nullable=False,
                server_default="workspace",
            )
        )

    # 2. Defensive backfill — every legacy row is workspace-scoped.
    #    The server-default already covers the ADD COLUMN path; this
    #    guards against future rewrites that drop the default first.
    op.execute("UPDATE audit_log SET scope_kind = 'workspace'")

    # 3. Relax ``workspace_id`` to NULLABLE, drop the server-default on
    #    ``scope_kind`` (every future insert must declare it), and
    #    install both CHECK constraints. Doing this in a single
    #    ``batch_alter_table`` keeps SQLite's table-rebuild atomic.
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(),
            nullable=True,
        )
        batch_op.alter_column(
            "scope_kind",
            existing_type=sa.String(),
            existing_nullable=False,
            server_default=None,
        )
        # Enum CHECK on ``scope_kind``. Naming via the shared
        # convention so the constraint name renders deterministically
        # as ``ck_audit_log_scope_kind``.
        batch_op.create_check_constraint(
            "scope_kind",
            "scope_kind IN ('workspace', 'deployment')",
        )
        # Biconditional CHECK pinning workspace_id to scope_kind.
        # Both directions enforced: a deployment row cannot carry a
        # workspace_id, a workspace row cannot omit one. The DB-level
        # CHECK is defence-in-depth; :mod:`app.audit` is the first
        # line of defence.
        batch_op.create_check_constraint(
            "scope_kind_workspace_pairing",
            "(scope_kind = 'deployment' AND workspace_id IS NULL) "
            "OR (scope_kind = 'workspace' AND workspace_id IS NOT NULL)",
        )

    # 4. Composite index backing the ``/admin/api/v1/audit`` feed.
    #    Emitted at the top level (not inside the batch context) so the
    #    index lands deterministically against the rebuilt SQLite table
    #    rather than racing the batch's own internal copy step.
    op.create_index(
        "ix_audit_log_scope_kind_created",
        "audit_log",
        ["scope_kind", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema.

    Reverses every step in :func:`upgrade`:

    1. Delete every deployment-scoped row — they cannot survive a
       ``workspace_id NOT NULL`` schema. A real production rollback
       should dump these rows to a side-table first; on a dev DB
       they were never expected to outlive the migration.
    2. Drop the new ``(scope_kind, created_at)`` index.
    3. Drop the two CHECK constraints, narrow ``workspace_id`` back
       to NOT NULL, and drop the ``scope_kind`` column itself.
    """
    # 1. Hard-delete any deployment-scoped rows so the narrower
    #    NOT NULL alteration below does not fail on legacy NULLs.
    op.execute("DELETE FROM audit_log WHERE scope_kind = 'deployment'")

    # 2. Drop the new feed-backing index before the column it
    #    references is removed.
    op.drop_index(
        "ix_audit_log_scope_kind_created",
        table_name="audit_log",
    )

    # 3. Drop the CHECKs, narrow workspace_id back, drop scope_kind.
    with op.batch_alter_table("audit_log", schema=None) as batch_op:
        batch_op.drop_constraint("scope_kind_workspace_pairing", type_="check")
        batch_op.drop_constraint("scope_kind", type_="check")
        batch_op.alter_column(
            "workspace_id",
            existing_type=sa.String(),
            nullable=False,
        )
        batch_op.drop_column("scope_kind")
