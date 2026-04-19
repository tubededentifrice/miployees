"""DeploymentSetting — deployment-wide operator-mutable settings.

See ``docs/specs/01-architecture.md`` §"Capability registry" and
``docs/specs/02-domain-model.md`` §"Conventions".

This table is **not** workspace-scoped: settings here govern the whole
deployment (signup open/closed, default LLM budget, etc.) and must be
reachable during signup itself — before any workspace exists. The
registry in :mod:`app.tenancy.registry` is therefore intentionally NOT
called for this table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["DeploymentSetting"]


class DeploymentSetting(Base):
    """Key/value row for a single deployment-wide setting.

    ``value`` is JSON (bool, int, string, list, or dict) so every
    setting landing here can share one table without adding a column
    per field. The capability registry
    (:mod:`app.capabilities`) is the single consumer — it reads the
    rows at boot and on each admin-settings mutation via
    :meth:`~app.capabilities.Capabilities.refresh_settings`.
    """

    __tablename__ = "deployment_setting"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
