"""Ops-context SQLAlchemy adapter: deployment-wide operations tables.

Holds cross-workspace operations plumbing — tables that apply to the
whole deployment and must be reachable without a live
:class:`~app.tenancy.WorkspaceContext`. Nothing here is workspace-
scoped: the tenancy registry (:mod:`app.tenancy.registry`) is
deliberately NOT called.

Importing this package re-exports the :class:`WorkerHeartbeat` mapped
class so alembic's ``env.py`` loader — which walks
``app.adapters.db.<context>.models`` and imports each submodule —
picks the table up via the per-context ``<context>.models`` convention.

See ``docs/specs/16-deployment-operations.md`` §"Healthchecks" and
``docs/specs/01-architecture.md`` §"Adapters".
"""

from __future__ import annotations

from app.adapters.db.ops.models import IdempotencyKey, WorkerHeartbeat

__all__ = ["IdempotencyKey", "WorkerHeartbeat"]
