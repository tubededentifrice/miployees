"""webauthn_challenge

Revision ID: dbf10d5d2f11
Revises: 92f86ca1f70b
Create Date: 2026-04-20 03:40:44.177754

Adds the ``webauthn_challenge`` table — the short-lived durable
store for the challenge bytes minted by ``POST /auth/passkey/
register/start`` and consumed by ``.../register/finish`` (see
``docs/specs/03-auth-and-tokens.md`` §"WebAuthn specifics" and
§"Self-serve signup" step 3, cd-8m4).

The row carries:

* ``id`` — opaque handle sent to the browser as ``challenge_id``.
* ``challenge`` — raw random bytes py_webauthn verifies the
  authenticator echoed. ``LargeBinary`` preserves the exact bytes.
* ``user_id`` (nullable) — set when an authenticated user is adding
  a passkey from their profile.
* ``signup_session_id`` (nullable) — set when the bare-host signup
  flow is enrolling the first passkey for a brand-new account. The
  ``signup_session`` table doesn't exist yet (cd-3i5), so this is a
  soft-typed string column — the FK lands alongside the signup
  migration.
* ``exclude_credentials`` — snapshot of the base64url credential-id
  list that the browser's authenticator must refuse to re-register
  against (§03 "Additional passkeys": up to 5 per user).
* ``created_at`` / ``expires_at`` — 10-minute TTL honoured by the
  finish handler; a sweeper prunes stale rows.

The CHECK constraint enforces the "exactly one subject" invariant:
the row must carry either ``user_id`` xor ``signup_session_id`` —
never both, never neither. No tenancy scoping: registration runs at
the bare host during signup and at the identity scope for "add
another passkey"; neither path has a live
``WorkspaceContext`` when the challenge is minted.

FK cascades on ``user`` so archiving / purging a user sweeps any
in-flight challenges in a single pass — the same pattern as
``passkey_credential``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "dbf10d5d2f11"
down_revision: str | Sequence[str] | None = "92f86ca1f70b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "webauthn_challenge",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("signup_session_id", sa.String(), nullable=True),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("exclude_credentials", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(user_id IS NOT NULL AND signup_session_id IS NULL) OR "
            "(user_id IS NULL AND signup_session_id IS NOT NULL)",
            name=op.f("ck_webauthn_challenge_ck_webauthn_challenge_subject"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_webauthn_challenge_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webauthn_challenge")),
    )
    with op.batch_alter_table("webauthn_challenge", schema=None) as batch_op:
        batch_op.create_index(
            "ix_webauthn_challenge_expires", ["expires_at"], unique=False
        )
        batch_op.create_index(
            "ix_webauthn_challenge_signup", ["signup_session_id"], unique=False
        )
        batch_op.create_index("ix_webauthn_challenge_user", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("webauthn_challenge", schema=None) as batch_op:
        batch_op.drop_index("ix_webauthn_challenge_user")
        batch_op.drop_index("ix_webauthn_challenge_signup")
        batch_op.drop_index("ix_webauthn_challenge_expires")

    op.drop_table("webauthn_challenge")
