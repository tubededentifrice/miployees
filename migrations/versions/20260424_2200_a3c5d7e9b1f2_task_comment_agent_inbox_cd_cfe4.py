"""task_comment_agent_inbox_cd_cfe4

Revision ID: a3c5d7e9b1f2
Revises: f2b4c5d6e7a8
Create Date: 2026-04-24 22:00:00.000000

Extends the v1 ``comment`` table to the §06 "Task notes are the agent
inbox" shape (cd-cfe4). The cd-chd slice (migration ``200bedec0eed``)
landed only the minimum pair every task thread needs
(``body_md`` / ``attachments_json``). cd-cfe4 adds:

* ``kind`` — NOT NULL TEXT ``user | agent | system`` with a
  CHECK constraint mirroring ``evidence.kind``. Server default
  ``'user'`` so pre-cd-cfe4 rows survive the migration; the domain
  service writes through on every new insert. Separates human
  authors from the embedded workspace agent and from internal
  state-change markers emitted by the completion / assignment
  services.
* ``mentioned_user_ids`` — NOT NULL JSON list (default ``[]``).
  Resolved ``@mention`` user ids the §10 messaging fanout reads at
  delivery time. Empty on ``agent`` / ``system`` rows.
* ``edited_at`` — nullable TIMESTAMPTZ. Edit marker. The domain
  service only allows edits within a 5-minute grace window on
  ``kind='user'`` rows; agent / system messages never flip this
  column.
* ``deleted_at`` — nullable TIMESTAMPTZ. Soft-delete marker. The
  domain service's ``list_comments`` hides non-null rows for every
  reader except workspace owners, so moderation history survives
  without bleeding into the worker / manager thread view.
* ``llm_call_id`` — nullable TEXT. Soft pointer to the
  :class:`llm_call` row that produced an agent message. No FK — the
  ``llm_call`` lifecycle is independent of comment retention.

Reversibility:

``downgrade()`` drops the five added columns and the CHECK
constraint in the same order. Any row carrying ``kind != 'user'``
on rollback loses its finer taxonomy; the rollback is data-
lossless for the cd-chd-era rows that only carried
``kind='user'`` (the default). Flagged here so the loss is never
silent.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Task notes are the
agent inbox", ``docs/specs/02-domain-model.md`` §"comment".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3c5d7e9b1f2"
down_revision: str | Sequence[str] | None = "f2b4c5d6e7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The three legal ``comment.kind`` values (§06 "Task notes are the
# agent inbox"). Kept beside the migration entry points so the up /
# down paths and the CHECK body stay in lockstep.
_COMMENT_KIND_VALUES: tuple[str, ...] = ("user", "agent", "system")


def _in_clause(values: tuple[str, ...]) -> str:
    return "'" + "', '".join(values) + "'"


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("comment", schema=None) as batch_op:
        # ``kind`` — NOT NULL with server default ``'user'`` so
        # pre-cd-cfe4 rows backfill without a bespoke UPDATE. The
        # CHECK constraint matches the ``_COMMENT_KIND_VALUES``
        # tuple above; the shared naming convention renders the
        # final constraint name as ``ck_comment_kind``.
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(),
                nullable=False,
                server_default="user",
            )
        )
        # ``mentioned_user_ids`` — NOT NULL JSON list. SQLAlchemy's
        # ``JSON`` type maps to JSON1 on SQLite and JSONB on
        # Postgres; the empty-list server default survives both. We
        # render the default as the JSON literal ``'[]'`` because
        # Alembic's ``server_default`` expects a string, and both
        # backends parse it correctly.
        batch_op.add_column(
            sa.Column(
                "mentioned_user_ids",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.add_column(
            sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("llm_call_id", sa.String(), nullable=True))

        batch_op.create_check_constraint(
            "kind",
            f"kind IN ({_in_clause(_COMMENT_KIND_VALUES)})",
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the five added columns and the CHECK constraint. Any row
    carrying ``kind != 'user'`` on rollback loses its finer
    taxonomy; the rollback is data-lossless for the cd-chd-era
    rows that only carried ``kind='user'`` (the default).
    """
    with op.batch_alter_table("comment", schema=None) as batch_op:
        batch_op.drop_constraint("kind", type_="check")
        batch_op.drop_column("llm_call_id")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("edited_at")
        batch_op.drop_column("mentioned_user_ids")
        batch_op.drop_column("kind")
