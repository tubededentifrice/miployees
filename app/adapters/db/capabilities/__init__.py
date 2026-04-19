"""Capabilities adapter — operator-mutable deployment settings table.

Re-exports the :class:`DeploymentSetting` mapped class so the
Alembic ``env.py`` model loader (see
``app/adapters/db/__init__.py`` + ``migrations/env.py``) picks the
table up via the per-context ``<context>.models`` convention.

See ``docs/specs/01-architecture.md`` §"Capability registry".
"""

from __future__ import annotations

from app.adapters.db.capabilities.models import DeploymentSetting

__all__ = ["DeploymentSetting"]
