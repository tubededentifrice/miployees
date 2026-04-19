"""identity

Revision ID: 1dc6908659d3
Revises: f247f50e8bee
Create Date: 2026-04-19 22:40:55.863448

Creates the four identity tables consumed by the auth stack (see
``docs/specs/02-domain-model.md`` §"users", §"passkey_credential",
§"session", §"api_token" and
``docs/specs/03-auth-and-tokens.md`` §"Data model"):

* ``user`` — one row per human, globally unique by
  ``email_lower``. ``email`` is the display-case value the user
  typed at enrolment; ``email_lower`` is the canonical lookup form
  and carries the unique constraint. Not registered as
  workspace-scoped — identity lives above the tenancy seam (§03
  "Sign-in runs before a workspace is picked").

* ``passkey_credential`` — WebAuthn credential bytes bound to a
  user. Credential id + public key stored as ``LargeBinary`` so the
  WebAuthn verifier sees the exact byte form the authenticator
  returned; base64url is a display concern and lives in the API
  layer.

* ``session`` — one row per logged-in browser. ``workspace_id`` is
  nullable because the sign-in ceremony mints a session before the
  user picks a workspace (the workspace-picker request swaps it
  in). ``ua_hash`` / ``ip_hash`` are hashed device fingerprints —
  §15 PII minimisation forbids storing the raw values.

* ``api_token`` — long-lived programmatic credential. The opaque
  token value is never stored; ``hash`` (sha256) carries the
  verification material and ``prefix`` (first 8 chars) anchors the
  listings view.

All four tables CASCADE-delete on ``user`` so archiving / purging a
user sweeps their auth trail atomically. ``session`` and
``api_token`` additionally CASCADE on ``workspace``. None of the
three user-scoped tables is registered with
:mod:`app.tenancy.registry` — the domain layer owns their tenancy
because the primary access pattern is ``user_id``, not
``workspace_id`` (auth must work before any WorkspaceContext
exists).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1dc6908659d3"
down_revision: str | Sequence[str] | None = "f247f50e8bee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("email_lower", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("locale", sa.String(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=True),
        sa.Column("avatar_blob_hash", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user")),
        sa.UniqueConstraint("email_lower", name=op.f("uq_user_email_lower")),
    )
    op.create_table(
        "api_token",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_api_token_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_api_token_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_token")),
        sa.UniqueConstraint("hash", name=op.f("uq_api_token_hash")),
    )
    with op.batch_alter_table("api_token", schema=None) as batch_op:
        batch_op.create_index("ix_api_token_user", ["user_id"], unique=False)
        batch_op.create_index("ix_api_token_workspace", ["workspace_id"], unique=False)

    op.create_table(
        "passkey_credential",
        sa.Column("id", sa.LargeBinary(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("transports", sa.String(), nullable=True),
        sa.Column("backup_eligible", sa.Boolean(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_passkey_credential_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_passkey_credential")),
    )
    with op.batch_alter_table("passkey_credential", schema=None) as batch_op:
        batch_op.create_index("ix_passkey_credential_user", ["user_id"], unique=False)

    op.create_table(
        "session",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ua_hash", sa.String(), nullable=True),
        sa.Column("ip_hash", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_session_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_session_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session")),
    )
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.create_index(
            "ix_session_user_expires", ["user_id", "expires_at"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.drop_index("ix_session_user_expires")
    op.drop_table("session")

    with op.batch_alter_table("passkey_credential", schema=None) as batch_op:
        batch_op.drop_index("ix_passkey_credential_user")
    op.drop_table("passkey_credential")

    with op.batch_alter_table("api_token", schema=None) as batch_op:
        batch_op.drop_index("ix_api_token_workspace")
        batch_op.drop_index("ix_api_token_user")
    op.drop_table("api_token")

    op.drop_table("user")
