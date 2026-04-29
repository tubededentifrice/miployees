"""chat gateway inbound cd-r09r

Revision ID: a1c3e5f7b9d1
Revises: f0a2c4e6b8d0
Create Date: 2026-04-29 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c3e5f7b9d1"
down_revision: str | Sequence[str] | None = "f0a2c4e6b8d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "chat_gateway_binding",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("external_contact", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("display_label", sa.String(), nullable=False),
        sa.Column(
            "provider_metadata_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["chat_channel.id"],
            name=op.f("fk_chat_gateway_binding_channel_id_chat_channel"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_chat_gateway_binding_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_gateway_binding")),
    )
    with op.batch_alter_table("chat_gateway_binding", schema=None) as batch_op:
        batch_op.create_index(
            "uq_chat_gateway_binding_provider_contact",
            ["provider", "external_contact"],
            unique=True,
        )
        batch_op.create_index(
            "ix_chat_gateway_binding_workspace",
            ["workspace_id"],
            unique=False,
        )

    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.String(),
                nullable=False,
                server_default="app",
            )
        )
        batch_op.add_column(
            sa.Column("provider_message_id", sa.String(), nullable=True)
        )
        batch_op.add_column(sa.Column("gateway_binding_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_chat_message_gateway_binding_id_chat_gateway_binding",
            "chat_gateway_binding",
            ["gateway_binding_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "uq_chat_message_source_provider_message_id",
            ["source", "provider_message_id"],
            unique=True,
        )
        batch_op.alter_column(
            "source",
            existing_type=sa.String(),
            server_default=None,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("chat_message", schema=None) as batch_op:
        batch_op.drop_index("uq_chat_message_source_provider_message_id")
        batch_op.drop_constraint(
            "fk_chat_message_gateway_binding_id_chat_gateway_binding",
            type_="foreignkey",
        )
        batch_op.drop_column("gateway_binding_id")
        batch_op.drop_column("provider_message_id")
        batch_op.drop_column("source")

    with op.batch_alter_table("chat_gateway_binding", schema=None) as batch_op:
        batch_op.drop_index("ix_chat_gateway_binding_workspace")
        batch_op.drop_index("uq_chat_gateway_binding_provider_contact")
    op.drop_table("chat_gateway_binding")
