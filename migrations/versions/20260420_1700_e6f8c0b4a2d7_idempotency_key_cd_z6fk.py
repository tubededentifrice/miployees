"""idempotency_key_cd_z6fk

Revision ID: e6f8c0b4a2d7
Revises: d5e7b3a1c9f4
Create Date: 2026-04-20 17:00:00.000000

Adds the ``idempotency_key`` table — one row per
``(token_id, idempotency_key)`` retry window. The idempotency
middleware (:mod:`app.api.middleware.idempotency`) persists the
server's response for 24 h so a client that retries the same
``POST`` with the same ``Idempotency-Key`` header gets the stored
response back instead of re-executing the handler.

Schema:

* ``id`` — ULID primary key. Row identity kept separate from the
  ``(token_id, key)`` uniqueness pair so the row can be referenced
  by audit / debugging tooling without leaking the key itself.
* ``token_id`` — the API token that issued the original request.
  Plain ``String`` (no FK) matching the convention on
  :class:`~app.adapters.db.audit.models.AuditLog`: the idempotency
  cache must survive a token revoke / rotate (the in-flight retry
  is still valid) and the cache read path must not block on an FK
  resolve.
* ``key`` — the client-supplied ``Idempotency-Key`` header value.
  Opaque string, bounded at 255 chars on write (enforced by the
  middleware; the column is plain ``String`` so a future longer
  format does not need a migration).
* ``status`` — the HTTP status the handler returned (int).
* ``body_hash`` — sha256 of the canonical JSON serialisation of the
  inbound request body. Used to detect "same key, different body"
  ``409 idempotency_conflict`` per spec §12 "Idempotency".
* ``body`` — the **response** body bytes, stored as bytes so a
  replay returns the exact same octets (Content-Length + ETag
  preserved). ``LargeBinary`` maps to ``BLOB`` on SQLite and
  ``BYTEA`` on Postgres.
* ``headers`` — JSON map of the response headers we replay
  verbatim (``Content-Type``, ``ETag``, ``Location``, …). Stored
  as ``JSON`` so the shape is portable across dialects; the
  middleware filters the set it persists.
* ``created_at`` — insert timestamp. The TTL sweep deletes any
  row older than 24 h; a secondary index on this column lets the
  sweeper range-scan efficiently.

Indexes:

* ``uq_idempotency_key_token_id_key`` — UNIQUE ``(token_id, key)``.
  The middleware relies on DB-level uniqueness to win races
  between two concurrent retries of the same request; the losing
  side catches :class:`~sqlalchemy.exc.IntegrityError` and re-reads
  the winning row's cached response.
* ``ix_idempotency_key_created_at`` — secondary index supporting
  the TTL sweeper's ``DELETE FROM idempotency_key WHERE created_at
  < now() - interval '24 hours'``. A plain b-tree is fine on both
  SQLite and Postgres; a partial index would save a bit on hot
  rows but would need dialect-specific syntax and this table stays
  modest even at scale (one row per retry * 24 h TTL).

**Reversibility.** ``downgrade()`` drops the table + both indexes.
Data in the idempotency cache is discarded; that is acceptable
because every row is at most 24 h old by contract and a downgrade
on a live deployment is an operator incident anyway.

See ``docs/specs/12-rest-api.md`` §"Idempotency" and
``docs/specs/02-domain-model.md`` §"Conventions".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f8c0b4a2d7"
down_revision: str | Sequence[str] | None = "d5e7b3a1c9f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "idempotency_key",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("token_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("body_hash", sa.String(), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_key")),
        sa.UniqueConstraint("token_id", "key", name="uq_idempotency_key_token_id_key"),
    )
    op.create_index(
        "ix_idempotency_key_created_at",
        "idempotency_key",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_idempotency_key_created_at", table_name="idempotency_key")
    op.drop_table("idempotency_key")
