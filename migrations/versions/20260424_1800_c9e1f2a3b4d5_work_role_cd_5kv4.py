"""work_role_cd_5kv4

Revision ID: c9e1f2a3b4d5
Revises: b8d0e1f2a3b4
Create Date: 2026-04-24 18:00:00.000000

Lands the §05 ``work_role`` and ``user_work_role`` tables — the
foundation for cd-8luu's candidate-pool path, the schedules
backup-list validator (the stub on
:mod:`app.domain.tasks.schedules`), and cd-dv2 (employees service).

``work_role`` shape:

* ``id`` ULID PK (plain ``String``, matching the cd-yesa
  harmonisation).
* ``workspace_id`` FK ``workspace.id`` ON DELETE CASCADE NOT NULL —
  hard-deleting a workspace sweeps its work-role catalogue (the
  §15 export worker snapshots first).
* ``key`` text NOT NULL — stable slug (``maid``, ``cook``).
  Editable; §05 records that a rename audit-logs as
  ``work_role.rekey`` but is not blocked. UNIQUE
  ``(workspace_id, key)`` enforces "one slug per workspace"
  without pinning the slug to immutability.
* ``name`` text NOT NULL — display label.
* ``description_md`` text NOT NULL DEFAULT ``''`` — markdown body
  surfaced in the role-editor sidebar.
* ``default_settings_json`` JSON NOT NULL DEFAULT ``'{}'`` —
  per-role provisioning hints (§05 "Recommended role defaults").
  Bare-string default matches the sibling cd-cm5 / cd-pjm /
  cd-jdhm convention so SQLite + PG round-trip the same literal.
* ``icon_name`` text NOT NULL DEFAULT ``''`` — Lucide PascalCase
  name (§14 "Icons"). Empty-string default keeps the column
  NOT NULL while letting seeders / importers omit the icon up
  front (the UI resolves an empty value to its neutral fallback).
* ``created_at`` tstz NOT NULL.
* ``deleted_at`` tstz NULL — soft-delete marker.

``work_role`` indexes:

* UNIQUE ``uq_work_role_workspace_key`` on ``(workspace_id, key)``
  (declared above).
* ``ix_work_role_workspace_deleted`` on
  ``(workspace_id, deleted_at)`` — backs the "list live roles for
  this workspace" hot path (``WHERE workspace_id = ? AND deleted_at
  IS NULL``).

``user_work_role`` shape:

* ``id`` ULID PK.
* ``user_id`` text NOT NULL — soft reference to ``users.id``,
  matching the ``user_workspace.user_id`` rationale (no FK until
  the broader tenancy-join refactor lands).
* ``workspace_id`` FK ``workspace.id`` ON DELETE CASCADE NOT NULL
  — denormalised so the ORM tenant filter rides a local column
  without a join through ``work_role``.
* ``work_role_id`` FK ``work_role.id`` ON DELETE CASCADE NOT NULL
  — hard-deleting a role sweeps every link.
* ``started_on`` date NOT NULL.
* ``ended_on`` date NULL — soft end on a single (user, workspace,
  role) row. A rehire on a new date mints a fresh row, so the
  history is linear and the active row is the one whose
  ``ended_on`` is NULL or in the future.
* ``pay_rule_id`` text NULL — soft reference to the future
  ``pay_rule`` table (cd-ea7). Plain ``String``, matching the
  ``role_grant.scope_property_id`` convention; once cd-ea7 lands a
  follow-up migration may promote this into a real FK.
* ``created_at`` tstz NOT NULL.
* ``deleted_at`` tstz NULL — soft-delete marker.

``user_work_role`` indexes:

* UNIQUE ``uq_user_work_role_identity`` on
  ``(user_id, workspace_id, work_role_id, started_on)`` — §05's
  identity key.
* ``ix_user_work_role_workspace_user`` on
  ``(workspace_id, user_id)`` — "every role this user holds in
  this workspace" hot path.
* ``ix_user_work_role_workspace_role`` on
  ``(workspace_id, work_role_id)`` — "every user who holds this
  role" hot path; the candidate-pool branch in cd-8luu walks this
  index to assemble §06's step-2 pool.

**Domain-enforced invariants.** §05 records two cross-table
invariants that are not expressed in DDL:

1. Every active ``user_work_role`` must carry the same
   ``workspace_id`` as the row's ``work_role`` (a user cannot
   borrow a role definition across workspaces). Expressing the
   check in portable DDL would need a trigger or a per-backend
   assertion; cd-dv2 (employees service) owns the runtime check.
2. A user holding ``role_grant.grant_role = 'worker'`` on this
   workspace must have ≥ 1 active ``user_work_role`` row here
   (the inverse is not required). The membership service enforces
   this on the write path.

Both invariants are documented on the ORM docstrings so callers
do not need to re-derive them from the spec.

**Reversibility.** ``downgrade()`` drops the secondary indexes
first (so SQLite's batch rebuild doesn't fight a lingering index
on a renamed table), then the tables themselves in FK-safe order
(``user_work_role`` before ``work_role`` because the child FK-
points at the parent). The UNIQUE constraints disappear with
their tables. No data-loss concern beyond the obvious "rolling
back drops the work-role catalogue" — a real rollback should
dump both tables first.

See ``docs/specs/05-employees-and-roles.md`` §"Work role" /
§"User work role", ``docs/specs/02-domain-model.md`` §"People,
work roles, engagements".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e1f2a3b4d5"
down_revision: str | Sequence[str] | None = "b8d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``work_role`` — per-workspace job catalogue. Created first
    # because ``user_work_role.work_role_id`` FK-points at it.
    op.create_table(
        "work_role",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "description_md",
            sa.String(),
            nullable=False,
            server_default="",
        ),
        # Bare-string JSON default matches the cd-cm5 / cd-pjm /
        # cd-jdhm sibling pattern; round-trips the same literal on
        # SQLite and PG.
        sa.Column(
            "default_settings_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "icon_name",
            sa.String(),
            nullable=False,
            server_default="",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_work_role_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_role")),
        sa.UniqueConstraint(
            "workspace_id",
            "key",
            name="uq_work_role_workspace_key",
        ),
    )
    with op.batch_alter_table("work_role", schema=None) as batch_op:
        # "List live roles for this workspace" hot path — leading
        # ``workspace_id`` carries the tenant filter; trailing
        # ``deleted_at`` lets the planner skip tombstones.
        batch_op.create_index(
            "ix_work_role_workspace_deleted",
            ["workspace_id", "deleted_at"],
            unique=False,
        )

    # ``user_work_role`` — links a user to a work role within one
    # workspace, with per-assignment overrides. ``work_role_id`` FK
    # cascades on delete so a hard-deleted role sweeps every link.
    op.create_table(
        "user_work_role",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("work_role_id", sa.String(), nullable=False),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("ended_on", sa.Date(), nullable=True),
        # Soft reference to the future ``pay_rule`` table (cd-ea7).
        # Plain ``String`` until that table lands; matches the
        # ``role_grant.scope_property_id`` convention.
        sa.Column("pay_rule_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["work_role_id"],
            ["work_role.id"],
            name=op.f("fk_user_work_role_work_role_id_work_role"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_user_work_role_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_work_role")),
        sa.UniqueConstraint(
            "user_id",
            "workspace_id",
            "work_role_id",
            "started_on",
            name="uq_user_work_role_identity",
        ),
    )
    with op.batch_alter_table("user_work_role", schema=None) as batch_op:
        # "Every role this user holds in this workspace" — the
        # employees surface (cd-dv2) and the settings cascade
        # resolver both filter through this index.
        batch_op.create_index(
            "ix_user_work_role_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )
        # "Every user who holds this role in this workspace" — the
        # candidate-pool branch (cd-8luu) walks this index to build
        # §06's step-2 pool.
        batch_op.create_index(
            "ix_user_work_role_workspace_role",
            ["workspace_id", "work_role_id"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema.

    FK-safe order: drop the child table (``user_work_role``) before
    the parent (``work_role``). Secondary indexes go first so the
    SQLite batch rebuild doesn't fight a lingering index on a
    renamed table; the UNIQUE constraints disappear with the
    tables themselves.
    """
    with op.batch_alter_table("user_work_role", schema=None) as batch_op:
        batch_op.drop_index("ix_user_work_role_workspace_role")
        batch_op.drop_index("ix_user_work_role_workspace_user")
    op.drop_table("user_work_role")

    with op.batch_alter_table("work_role", schema=None) as batch_op:
        batch_op.drop_index("ix_work_role_workspace_deleted")
    op.drop_table("work_role")
