"""instructions

Revision ID: 53e464485919
Revises: a2bb41eeb016
Create Date: 2026-04-20 08:55:09.935990

Creates the two instructions-context tables that back the scope-aware
SOP / KB library (see ``docs/specs/02-domain-model.md`` §"instruction",
§"instruction_version" and ``docs/specs/07-instructions-kb.md``):

* ``instruction`` — the long-lived anchor row. UNIQUE
  ``(workspace_id, slug)`` enforces cd-bce's acceptance criterion
  (a workspace cannot mint two instructions with the same slug).
  CHECK on ``scope_kind`` clamps the v1 taxonomy (``template |
  property | area | asset | stay | role | workspace``). The
  ``(workspace_id, scope_kind, scope_id)`` index powers the
  "instructions that apply to this <scope>" lookup the worker task
  screen runs on every open (§07 §"Rendered in context"). FK
  hygiene: ``workspace_id`` cascades — sweeping a workspace sweeps
  its instructions library (§15 export snapshots first).
  ``scope_id`` stays a plain :class:`str` soft-ref because the
  target entity is polymorphic (template, property, area, asset,
  stay, role) and SQLAlchemy does not express a polymorphic FK
  portably. ``current_version_id`` is *also* a soft-ref — a hard FK
  back to :class:`instruction_version` would close a circular
  dependency (the version FK-points at its instruction) and force a
  two-phase write on insert. The domain layer writes the pointer
  atomically on version bump — same pattern as
  ``task.current_evidence_id`` (§02). ``created_by`` is a soft-ref
  :class:`str` for the same reason as sibling audit-trail columns
  across the app.

* ``instruction_version`` — an immutable snapshot of the body. UNIQUE
  ``(instruction_id, version_num)`` enforces the monotonic version
  invariant; CHECK ``version_num >= 1`` clamps the off-by-one bug of
  minting v0. ``instruction_id`` FK cascades — deleting an
  instruction drops every version row with it. ``workspace_id`` is
  *denormalised* from the parent ``Instruction`` so the ORM tenant
  filter's injected predicate rides a local column (no join through
  ``instruction`` required) — the pattern matches
  ``permission_group_member`` in :mod:`app.adapters.db.authz`. It
  also cascades, so sweeping a workspace drops every version row in
  lock-step with its parent. ``author_id`` is a soft-ref
  :class:`str` because the author may be a system actor.

Both tables are workspace-scoped (registered via the package's
``__init__``). Tables are created in a stable deterministic order
matching the dependency chain (``instruction`` before
``instruction_version`` because the child has an FK into the parent);
``downgrade()`` drops in reverse.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "53e464485919"
down_revision: str | Sequence[str] | None = "a2bb41eeb016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "instruction",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=True),
        sa.Column("current_version_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_kind IN ('template', 'property', 'area', 'asset', "
            "'stay', 'role', 'workspace')",
            name=op.f("ck_instruction_scope_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_instruction_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruction")),
        sa.UniqueConstraint(
            "workspace_id",
            "slug",
            name="uq_instruction_workspace_slug",
        ),
    )
    with op.batch_alter_table("instruction", schema=None) as batch_op:
        batch_op.create_index(
            "ix_instruction_workspace_scope",
            ["workspace_id", "scope_kind", "scope_id"],
            unique=False,
        )

    op.create_table(
        "instruction_version",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("instruction_id", sa.String(), nullable=False),
        sa.Column("version_num", sa.Integer(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version_num >= 1",
            name=op.f("ck_instruction_version_version_num_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["instruction_id"],
            ["instruction.id"],
            name=op.f("fk_instruction_version_instruction_id_instruction"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_instruction_version_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruction_version")),
        sa.UniqueConstraint(
            "instruction_id",
            "version_num",
            name="uq_instruction_version_instruction_version_num",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("instruction_version")
    with op.batch_alter_table("instruction", schema=None) as batch_op:
        batch_op.drop_index("ix_instruction_workspace_scope")
    op.drop_table("instruction")
