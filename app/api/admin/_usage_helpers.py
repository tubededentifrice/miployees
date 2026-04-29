"""Shared admin usage helpers.

Both the usage table and workspace directory need the same rolling
LLM spend and cap projection. Keep that logic outside either router so
route modules do not import each other's private helpers.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Final

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.workspace.models import Workspace
from app.capabilities import Capabilities, DeploymentSettings
from app.tenancy import tenant_agnostic

__all__ = [
    "_QUOTA_CAP_KEY",
    "_deployment_default_cap",
    "_list_workspace_aggregates",
    "_resolved_cap",
    "_window",
]


# Rolling-window cutoff shared by the admin usage surfaces.
_ROLLING_30D: Final[timedelta] = timedelta(days=30)


# ``llm_budget_cents_30d`` is the canonical cap key inside
# :attr:`Workspace.quota_json` — see :data:`FREE_TIER_DEFAULTS`
# in :mod:`app.domain.plans`.
_QUOTA_CAP_KEY: Final[str] = "llm_budget_cents_30d"


def _deployment_default_cap(request: Request) -> int:
    """Resolve the deployment's default LLM budget cap."""
    capabilities: Capabilities | None = getattr(request.app.state, "capabilities", None)
    if capabilities is not None:
        return int(capabilities.settings.llm_default_budget_cents_30d)
    return _DEFAULT_DEPLOYMENT_BUDGET_CENTS


# ``DeploymentSettings`` is a slotted dataclass; class-level
# field access returns a slot descriptor rather than the default,
# which trips mypy. Build the registry of cap defaults once at
# import time so the runtime fallback is deterministic without
# the fragile ``DeploymentSettings.field`` read.
def _resolve_default_budget_cents() -> int:
    for field in fields(DeploymentSettings):
        if field.name == "llm_default_budget_cents_30d":
            value = field.default
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    raise RuntimeError(  # pragma: no cover - dataclass invariant
        "DeploymentSettings.llm_default_budget_cents_30d default missing"
    )


_DEFAULT_DEPLOYMENT_BUDGET_CENTS: Final[int] = _resolve_default_budget_cents()


def _resolved_cap(workspace: Workspace, *, deployment_default: int) -> int:
    """Return the workspace's resolved cap in cents."""
    raw = (
        workspace.quota_json.get(_QUOTA_CAP_KEY)
        if isinstance(workspace.quota_json, dict)
        else None
    )
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return deployment_default


def _window(now: datetime) -> datetime:
    """Return the cutoff timestamp for the rolling 30-day window."""
    return now.astimezone(UTC) - _ROLLING_30D


def _list_workspace_aggregates(
    session: Session,
    *,
    cutoff: datetime,
) -> dict[str, tuple[int, int]]:
    """Return ``{workspace_id: (call_count, spend_cents)}`` for the window."""
    with tenant_agnostic():
        rows = session.execute(
            select(
                LlmUsage.workspace_id,
                func.count(LlmUsage.id),
                func.coalesce(func.sum(LlmUsage.cost_cents), 0),
            )
            .where(LlmUsage.created_at >= cutoff)
            .group_by(LlmUsage.workspace_id)
        ).all()
    return {
        workspace_id: (int(count or 0), int(spent or 0))
        for workspace_id, count, spent in rows
    }
