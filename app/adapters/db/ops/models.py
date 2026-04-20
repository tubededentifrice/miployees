"""Deployment-wide ops tables: ``worker_heartbeat`` and ``idempotency_key``.

Two concerns share this module because both sit outside the per-
workspace schema and both support the §16 ops surface:

* :class:`WorkerHeartbeat` — one row per named background worker,
  bumped every 30 s, read by ``/readyz``.
* :class:`IdempotencyKey` — the persisted replay cache for the
  ``Idempotency-Key`` middleware (spec §12 "Idempotency"). Keyed by
  ``(token_id, key)``; a TTL sweep deletes rows older than 24 h.

**Not workspace-scoped.** Both tables are deployment-wide: workers
run once per process regardless of tenant count, and an idempotency
row is keyed by the API token (which is itself workspace-scoped via
its own row, but the replay lookup must succeed before any
:class:`~app.tenancy.WorkspaceContext` is resolvable). Writers wrap
their reads/writes in :func:`app.tenancy.tenant_agnostic` with an
explicit justification.

See ``docs/specs/16-deployment-operations.md`` §"Healthchecks",
``docs/specs/12-rest-api.md`` §"Idempotency", and
``docs/specs/01-architecture.md`` §"Key runtime invariants".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["IdempotencyKey", "WorkerHeartbeat"]


class WorkerHeartbeat(Base):
    """One liveness row per named background worker.

    ``worker_name`` is a stable short identifier (``"scheduler"``,
    ``"llm_usage"``, ``"email_outbox"``, ...) — kept as free-form
    ``String`` rather than an enum so adding a new worker is a code
    diff, not a migration. ``heartbeat_at`` is aware UTC (§01 "Time is
    UTC at rest").

    The row is upserted — one row per worker for the lifetime of the
    deployment, not one row per tick; the table stays constant-sized
    and cleanup is unnecessary.
    """

    __tablename__ = "worker_heartbeat"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    worker_name: Mapped[str] = mapped_column(String, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("worker_name", name="uq_worker_heartbeat_worker_name"),
    )


class IdempotencyKey(Base):
    """Persisted replay cache entry for a ``POST`` + ``Idempotency-Key`` pair.

    One row per ``(token_id, key)`` tuple. The middleware writes the
    row inside the same transaction as the handler response: on
    commit, the cached response is durable; on rollback, the row is
    gone and the retry re-executes as if the first attempt never
    landed. The ``body_hash`` column holds the sha256 of the canonical
    JSON serialisation of the *inbound request body*; a retry with a
    different body hash raises :class:`~app.domain.errors.IdempotencyConflict`.

    ``body`` is :class:`LargeBinary` so the replay returns the exact
    bytes the handler emitted (important for content negotiation +
    ETag preservation); ``headers`` is a JSON map of the subset of
    response headers the middleware replays verbatim.

    No FK on ``token_id`` by design: the idempotency cache must
    survive a token revoke / rotate (the legitimate in-flight retry
    is still valid for 24 h), matching the convention on
    :class:`~app.adapters.db.audit.models.AuditLog`.
    """

    __tablename__ = "idempotency_key"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    token_id: Mapped[str] = mapped_column(String, nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    body_hash: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # ``Any`` is the SQLAlchemy-typed ``JSON`` column type; readers
    # narrow to ``dict[str, str]`` at the middleware boundary.
    headers: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("token_id", "key", name="uq_idempotency_key_token_id_key"),
        # Composite-style single-column index supporting the TTL
        # sweep's ``WHERE created_at < ...`` range scan. Explicit
        # name so alembic autogenerate sees a stable identifier
        # (the default ``ix_idempotency_key_created_at`` from the
        # naming convention collides with the uppercase variant some
        # older migrations emitted).
        Index("ix_idempotency_key_created_at", "created_at"),
    )
