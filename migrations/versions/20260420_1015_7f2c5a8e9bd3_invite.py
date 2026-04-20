"""invite

Revision ID: 7f2c5a8e9bd3
Revises: 0aa2a5606810
Create Date: 2026-04-20 10:15:00.000000

Adds the ``invite`` table — primary entity of the click-to-accept
membership flow (§03 "Additional users (invite → click-to-accept)").
One row per pending "please join" payload a workspace owner / manager
emits; flipped to ``accepted`` when the invitee's two-stage ceremony
lands every downstream row (role_grant + permission_group_member + the
v1 derived user_workspace juncture) in one transaction.

**Tenant-scoped.** ``workspace_id`` is a hard FK and every query from
the membership service runs under a live
:class:`~app.tenancy.WorkspaceContext`. The accept flow at the bare
host wraps its lookup in :func:`app.tenancy.tenant_agnostic`.

**PII minimisation (§15).** ``pending_email`` / ``pending_email_lower``
carry the address in the clear (we need it to seed the ``user.email``
column on accept and render the acceptance-card UX); the plaintext
never lands in audit diffs — only ``email_hash`` (SHA-256 + HKDF
pepper, same shape as ``magic_link_nonce`` + ``signup_attempt``).

No hard FK on ``user_id``: the user row may not exist at invite time
(brand-new invitee) and is linked lazily on accept. A soft pointer
keeps the ``DELETE FROM users`` path clean without cascading invite
forensics.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f2c5a8e9bd3"
down_revision: str | Sequence[str] | None = "0aa2a5606810"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "invite",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("pending_email", sa.String(), nullable=False),
        sa.Column("pending_email_lower", sa.String(), nullable=False),
        sa.Column("email_hash", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("grants_json", sa.JSON(), nullable=False),
        sa.Column("group_memberships_json", sa.JSON(), nullable=False),
        sa.Column("invited_by_user_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_invite_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["user.id"],
            name=op.f("fk_invite_invited_by_user_id_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invite")),
        sa.CheckConstraint(
            "state IN ('pending', 'accepted', 'expired', 'revoked')",
            name="ck_invite_state",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "pending_email_lower",
            "state",
            name="uq_invite_workspace_email_state",
        ),
    )
    with op.batch_alter_table("invite", schema=None) as batch_op:
        batch_op.create_index("ix_invite_workspace", ["workspace_id"], unique=False)
        batch_op.create_index(
            "ix_invite_email_lower", ["pending_email_lower"], unique=False
        )
        batch_op.create_index("ix_invite_expires", ["expires_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("invite", schema=None) as batch_op:
        batch_op.drop_index("ix_invite_expires")
        batch_op.drop_index("ix_invite_email_lower")
        batch_op.drop_index("ix_invite_workspace")

    op.drop_table("invite")
