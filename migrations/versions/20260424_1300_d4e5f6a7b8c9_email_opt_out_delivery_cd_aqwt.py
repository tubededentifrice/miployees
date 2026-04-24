"""email_opt_out_delivery_cd_aqwt

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-24 13:00:00.000000

Extends the messaging substrate landed by cd-pjm with the two tables
§10 needs to drive opt-out-aware fanout and bounce-reply correlation:

* ``email_opt_out`` — per-user per-category unsubscribe ledger. The
  §10 worker consults this table before emitting any opt-outable
  email (it uses the unique ``(workspace_id, user_id, category)``
  triple as a pre-send probe). ``category`` aligns with
  ``email_delivery.template_key`` family; ``source`` is a CHECK-
  clamped enum pinning how the row was created (``unsubscribe_link``
  / ``profile`` / ``admin``). FK: both ``workspace_id`` and
  ``user_id`` CASCADE — an archived workspace or user sweeps its
  opt-outs.

* ``email_delivery`` — per-send delivery ledger. One row per queued
  email; ``delivery_state`` walks ``queued → sent → delivered`` on
  success and ``queued → sent → bounced`` / ``queued → failed`` on
  error paths. ``provider_message_id`` carries the ESP-issued id the
  bounce-reply correlator keys on; ``context_snapshot_json`` freezes
  the renderer context at queue time so replay / audit reads the
  same subject + body the recipient saw. ``first_error`` snapshots
  the first adapter failure and is never overwritten on subsequent
  retries. FK: ``workspace_id`` CASCADE; ``to_person_id`` is a
  **soft reference** (plain ``String``, no FK) because §10
  reminders can target a ``client_user`` that has not yet been
  materialised in the ``user`` table when the email is queued.

Indexes on ``email_delivery``:

* ``ix_email_delivery_workspace_person_sent`` —
  ``(workspace_id, to_person_id, sent_at)`` — audit hot path.
* ``ix_email_delivery_workspace_provider_msgid`` —
  ``(workspace_id, provider_message_id)`` — bounce-reply correlator.
* ``ix_email_delivery_workspace_state_sent`` —
  ``(workspace_id, delivery_state, sent_at)`` — retry scheduler.

Both tables are workspace-scoped (registered via
``app/adapters/db/messaging/__init__.py``). Portable across SQLite +
Postgres — CHECK bodies only for the enum-like columns, no server-
side enum types. ``context_snapshot_json`` uses ``sa.JSON`` with
``server_default='{}'`` to match the ``notification.payload_json`` /
``chat_message.attachments_json`` convention from cd-pjm.

**Reversibility.** ``downgrade()`` drops ``email_delivery`` then
``email_opt_out`` (no inter-table FK, order is arbitrary but
mirrors upgrade for symmetry). The ``upgrade → downgrade →
upgrade`` loop yields the same schema shape — the
``test_schema_fingerprint_matches_on_sqlite_and_pg`` gate keeps
this honest.

See ``docs/specs/10-messaging-notifications.md`` §"email_opt_out",
§"Delivery tracking"; ``docs/specs/02-domain-model.md`` §"Comms".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # ``email_opt_out`` — per-user per-category unsubscribe marker.
    op.create_table(
        "email_opt_out",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.CheckConstraint(
            "source IN ('unsubscribe_link', 'profile', 'admin')",
            name=op.f("ck_email_opt_out_source"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_email_opt_out_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_email_opt_out_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_opt_out")),
    )
    with op.batch_alter_table("email_opt_out", schema=None) as batch_op:
        # Unique: one row per ``(workspace_id, user_id, category)``
        # triple — the §10 worker's pre-send probe key.
        batch_op.create_index(
            "uq_email_opt_out_user_category",
            ["workspace_id", "user_id", "category"],
            unique=True,
        )
        # Non-unique: per-user lookup for ``/me`` + audit.
        batch_op.create_index(
            "ix_email_opt_out_workspace_user",
            ["workspace_id", "user_id"],
            unique=False,
        )

    # ``email_delivery`` — per-send delivery ledger.
    op.create_table(
        "email_delivery",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        # Soft reference — no FK on purpose: §10 reminders can
        # target a client_user that is not yet a row in ``user``.
        sa.Column("to_person_id", sa.String(), nullable=False),
        sa.Column("to_email_at_send", sa.String(), nullable=False),
        sa.Column("template_key", sa.String(), nullable=False),
        sa.Column(
            "context_snapshot_json",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("delivery_state", sa.String(), nullable=False),
        sa.Column("first_error", sa.Text(), nullable=True),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("inbound_linkage", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "delivery_state IN ('queued', 'sent', 'delivered', 'bounced', 'failed')",
            name=op.f("ck_email_delivery_delivery_state"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_email_delivery_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_delivery")),
    )
    with op.batch_alter_table("email_delivery", schema=None) as batch_op:
        batch_op.create_index(
            "ix_email_delivery_workspace_person_sent",
            ["workspace_id", "to_person_id", "sent_at"],
            unique=False,
        )
        batch_op.create_index(
            "ix_email_delivery_workspace_provider_msgid",
            ["workspace_id", "provider_message_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_email_delivery_workspace_state_sent",
            ["workspace_id", "delivery_state", "sent_at"],
            unique=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("email_delivery", schema=None) as batch_op:
        batch_op.drop_index("ix_email_delivery_workspace_state_sent")
        batch_op.drop_index("ix_email_delivery_workspace_provider_msgid")
        batch_op.drop_index("ix_email_delivery_workspace_person_sent")
    op.drop_table("email_delivery")

    with op.batch_alter_table("email_opt_out", schema=None) as batch_op:
        batch_op.drop_index("ix_email_opt_out_workspace_user")
        batch_op.drop_index("uq_email_opt_out_user_category")
    op.drop_table("email_opt_out")
