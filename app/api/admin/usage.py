"""Deployment-admin usage aggregates routes.

Mounts under ``/admin/api/v1`` (§12 "Admin surface"):

* ``GET /usage/summary`` — rolling 30-day deployment-wide spend
  + per-capability breakdown.
* ``GET /usage/workspaces`` — per-workspace cap / spent / paused
  / percent table.
* ``PUT /usage/workspaces/{id}/cap`` — raise or lower a
  workspace's LLM budget cap.

Aggregates read directly from :class:`LlmUsage` (cd-cm5's
per-call ledger) — there is no denormalised cache yet, so the
sums run on the hot path. The ``ix_llm_usage_workspace_created``
index makes the per-workspace filter cheap; the deployment-wide
summary scans the full window. Once volume warrants, the
``llm_usage_daily`` rollup the spec calls for (§02 "LLM") lands
behind the same response shape.

The cap mutation writes :attr:`Workspace.quota_json[\"llm_budget_cents_30d\"]`
— the same blob :func:`app.domain.plans.seed_free_tier_quota`
seeds. The runtime budget gate (§11 ``BudgetLedger`` /
:func:`app.domain.llm.budget`) reads the cap off the same
mapping, so a cap change here flows through to the next call's
pre-flight check.

See ``docs/specs/12-rest-api.md`` §"Admin surface" §"Usage
aggregates", ``docs/specs/11-llm-and-agents.md`` §"Workspace
usage budget".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.workspace.models import Workspace
from app.api.admin._audit import audit_admin
from app.api.admin._usage_helpers import (
    _QUOTA_CAP_KEY,
    _deployment_default_cap,
    _list_workspace_aggregates,
    _resolved_cap,
    _window,
)
from app.api.admin._workspace_state import load_workspace
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.tenancy import DeploymentContext, tenant_agnostic

__all__ = [
    "UsageCapPayload",
    "UsageCapResponse",
    "UsageSummaryResponse",
    "UsageWorkspaceRow",
    "UsageWorkspacesResponse",
    "build_admin_usage_router",
]


_Db = Annotated[Session, Depends(db_session)]


class UsageSummaryEntry(BaseModel):
    """Per-capability row inside :class:`UsageSummaryResponse`."""

    capability: str
    spend_cents_30d: int
    calls_30d: int


class UsageSummaryResponse(BaseModel):
    """Body of ``GET /admin/api/v1/usage/summary``.

    Mirrors the SPA's :interface:`AdminUsageSummary`
    (``mocks/web/src/types/api.ts``) with a few field renames
    so the wire shape carries cents, not dollars (the SPA
    converts on render — keeping cents on the wire dodges the
    rounding ambiguity of partial-cent payloads).
    """

    window_label: str
    deployment_spend_cents_30d: int
    deployment_calls_30d: int
    workspace_count: int
    paused_workspace_count: int
    per_capability: list[UsageSummaryEntry]


class UsageWorkspaceRow(BaseModel):
    """One row of ``GET /admin/api/v1/usage/workspaces``.

    The ``cap_cents_30d`` field carries the workspace's resolved
    cap — overridden in :attr:`Workspace.quota_json` if present,
    otherwise the deployment default
    (:attr:`DeploymentSettings.llm_default_budget_cents_30d`).
    ``percent`` is the integer percentage of the cap consumed in
    the rolling window; capped at 100 for display so the UI
    progress bar doesn't render nonsense for an over-cap workspace.
    ``paused`` is true when the cap is exceeded and the runtime
    gate has paused new calls.
    """

    workspace_id: str
    slug: str
    name: str
    cap_cents_30d: int
    spent_cents_30d: int
    percent: int
    paused: bool


class UsageWorkspacesResponse(BaseModel):
    """Body of ``GET /admin/api/v1/usage/workspaces``."""

    workspaces: list[UsageWorkspaceRow]


class UsageCapPayload(BaseModel):
    """Request body for ``PUT /admin/api/v1/usage/workspaces/{id}/cap``.

    Single-field body so the URL pins the workspace and the
    JSON pins the cap. Validation: must be a non-negative int
    (cents). ``0`` is allowed — it hard-disables LLM calls for
    the workspace, mirroring :func:`app.domain.plans.tight_cap_cents`.
    """

    cap_cents_30d: int


class UsageCapResponse(BaseModel):
    """Body of ``PUT /admin/api/v1/usage/workspaces/{id}/cap``.

    Echoes the workspace identifier + the post-mutation cap so
    the SPA's optimistic cache can splice the new value without
    a re-fetch.
    """

    workspace_id: str
    cap_cents_30d: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percent(spent: int, cap: int) -> int:
    """Return the integer percentage of ``cap`` consumed by ``spent``.

    ``cap == 0`` collapses to 100 (the workspace is hard-disabled;
    every call would over-cap). Integer-cast so the SPA renders
    a clean integer.
    """
    if cap <= 0:
        return 100
    pct = (spent * 100) // cap
    return min(int(pct), 100)


def _list_capability_aggregates(
    session: Session,
    *,
    cutoff: datetime,
) -> list[UsageSummaryEntry]:
    """Return per-capability aggregates ordered by spend, descending."""
    with tenant_agnostic():
        rows = session.execute(
            select(
                LlmUsage.capability,
                func.count(LlmUsage.id),
                func.coalesce(func.sum(LlmUsage.cost_cents), 0),
            )
            .where(LlmUsage.created_at >= cutoff)
            .group_by(LlmUsage.capability)
            .order_by(func.coalesce(func.sum(LlmUsage.cost_cents), 0).desc())
        ).all()
    return [
        UsageSummaryEntry(
            capability=str(capability),
            spend_cents_30d=int(spend or 0),
            calls_30d=int(count or 0),
        )
        for capability, count, spend in rows
    ]


def _list_workspaces(session: Session) -> list[Workspace]:
    with tenant_agnostic():
        return list(
            session.scalars(
                select(Workspace).order_by(
                    Workspace.created_at.asc(), Workspace.id.asc()
                )
            ).all()
        )


def _not_found() -> HTTPException:
    """Canonical 404 envelope — same shape as the admin auth dep emits."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found"},
    )


def build_admin_usage_router() -> APIRouter:
    """Return the router carrying the usage-aggregates admin routes."""
    router = APIRouter(tags=["admin"])

    @router.get(
        "/usage/summary",
        response_model=UsageSummaryResponse,
        operation_id="admin.usage.summary",
        summary="Deployment-wide rolling 30-day usage summary",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "usage-summary",
                "summary": "Deployment-wide rolling 30-day usage summary",
                "mutates": False,
            },
        },
    )
    def usage_summary(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> UsageSummaryResponse:
        """Return the deployment's rolling 30-day usage envelope.

        Aggregates :class:`LlmUsage` over the rolling window and
        joins the workspace table for the cap-paused boolean
        count — a workspace is "paused" when its 30-day spend
        meets or exceeds its cap. The result feeds the SPA's
        :file:`UsagePage` summary tiles.
        """
        now = datetime.now(UTC)
        cutoff = _window(now)
        workspaces = _list_workspaces(session)
        per_workspace = _list_workspace_aggregates(session, cutoff=cutoff)
        deployment_default = _deployment_default_cap(request)
        deployment_calls = 0
        deployment_spend = 0
        paused = 0
        for workspace in workspaces:
            calls, spend = per_workspace.get(workspace.id, (0, 0))
            deployment_calls += calls
            deployment_spend += spend
            cap = _resolved_cap(workspace, deployment_default=deployment_default)
            if cap == 0 or spend >= cap:
                paused += 1
        return UsageSummaryResponse(
            window_label="rolling 30 days",
            deployment_spend_cents_30d=deployment_spend,
            deployment_calls_30d=deployment_calls,
            workspace_count=len(workspaces),
            paused_workspace_count=paused,
            per_capability=_list_capability_aggregates(session, cutoff=cutoff),
        )

    @router.get(
        "/usage/workspaces",
        response_model=UsageWorkspacesResponse,
        operation_id="admin.usage.workspaces",
        summary="Per-workspace cap / spent / paused table",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "usage-workspaces",
                "summary": "Per-workspace cap / spent / paused table",
                "mutates": False,
            },
        },
    )
    def usage_workspaces(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> UsageWorkspacesResponse:
        """Return the per-workspace usage table.

        Walks every workspace once, joins the rolling-window
        aggregate, and resolves each workspace's cap against the
        deployment default. The SPA's :file:`UsagePage` paginates
        client-side; the cd-jlms slice ships every row in one
        page (the table is small and bounded for v1 deployments).
        """
        now = datetime.now(UTC)
        cutoff = _window(now)
        workspaces = _list_workspaces(session)
        per_workspace = _list_workspace_aggregates(session, cutoff=cutoff)
        deployment_default = _deployment_default_cap(request)
        rows: list[UsageWorkspaceRow] = []
        for workspace in workspaces:
            spent = per_workspace.get(workspace.id, (0, 0))[1]
            cap = _resolved_cap(workspace, deployment_default=deployment_default)
            rows.append(
                UsageWorkspaceRow(
                    workspace_id=workspace.id,
                    slug=workspace.slug,
                    name=workspace.name,
                    cap_cents_30d=cap,
                    spent_cents_30d=spent,
                    percent=_percent(spent, cap),
                    paused=cap == 0 or spent >= cap,
                )
            )
        return UsageWorkspacesResponse(workspaces=rows)

    @router.put(
        "/usage/workspaces/{id}/cap",
        response_model=UsageCapResponse,
        operation_id="admin.usage.workspaces.cap",
        summary="Raise or lower a workspace's LLM budget cap",
        status_code=status.HTTP_200_OK,
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "usage-cap",
                "summary": "Raise or lower a workspace's LLM budget cap",
                "mutates": True,
            },
        },
    )
    def update_cap(
        id: str,
        payload: UsageCapPayload,
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> UsageCapResponse:
        """Stamp a new cap into ``workspace.quota_json``.

        Idempotent: writing the same cap returns the same
        envelope. Negative values 422 ``invalid_cap`` (caught by
        the pydantic validator on :class:`UsageCapPayload`).

        The route does **not** clear the
        :class:`BudgetLedger` row — the next post-flight write
        (:func:`app.domain.llm.budget.record_usage`) re-reads the
        cap from the quota blob, so the new value takes effect
        on the next call without a manual ledger reset.
        """
        if payload.cap_cents_30d < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "invalid_cap",
                    "message": "cap_cents_30d must be a non-negative integer",
                },
            )
        workspace = load_workspace(session, workspace_id=id)
        if workspace is None:
            raise _not_found()
        previous_quota = (
            workspace.quota_json if isinstance(workspace.quota_json, dict) else {}
        )
        previous_cap = previous_quota.get(_QUOTA_CAP_KEY)
        if previous_cap == payload.cap_cents_30d:
            return UsageCapResponse(
                workspace_id=workspace.id, cap_cents_30d=payload.cap_cents_30d
            )
        with tenant_agnostic():
            updated_quota = dict(previous_quota)
            updated_quota[_QUOTA_CAP_KEY] = payload.cap_cents_30d
            workspace.quota_json = updated_quota
            audit_admin(
                session,
                ctx=ctx,
                request=request,
                entity_kind="workspace",
                entity_id=workspace.id,
                action="usage.cap_updated",
                diff={
                    "cap_cents_30d": {
                        "before": previous_cap,
                        "after": payload.cap_cents_30d,
                    }
                },
            )
            session.flush()
        return UsageCapResponse(
            workspace_id=workspace.id, cap_cents_30d=payload.cap_cents_30d
        )

    return router
