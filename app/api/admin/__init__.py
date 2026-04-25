"""Deployment-scoped admin API tree.

Exposes :data:`admin_router` — the :class:`APIRouter` the app
factory mounts under ``/admin/api/v1`` (spec §12 "Base URL",
"Admin surface"). Real admin routes (cd-jlms et al.) attach
their handlers to this router; the only handler currently wired
here is :func:`_ping`, a throwaway probe used by the cd-xgmu
integration tests to exercise the
:func:`current_deployment_admin_principal` dep end-to-end without
waiting for the production admin surface to land.

Authorisation lives on the per-route deps. Every route gates on
:func:`app.api.admin.deps.current_deployment_admin_principal`
(or its :func:`require_deployment_scope` companion) so a caller
without an active ``scope_kind='deployment'`` ``role_grant`` row
or a deployment-scoped API token receives ``404`` —
**not** ``403`` — per spec §12: "the surface does not advertise
its own existence to tenants".

See ``docs/specs/12-rest-api.md`` §"Admin surface".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.admin.deps import current_deployment_admin_principal
from app.tenancy import DeploymentContext

__all__ = ["admin_router"]


admin_router = APIRouter(tags=["admin"])


@admin_router.get(
    "/_ping",
    include_in_schema=False,
    summary="Smoke-test ping for the deployment admin auth dep",
)
def _ping(
    ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
) -> dict[str, object]:
    """Return a minimal envelope identifying the resolved principal.

    Used by the cd-xgmu integration tests to verify the dep wires
    end-to-end through :func:`app.api.factory.create_app`. Hidden
    from OpenAPI (``include_in_schema=False``) so the committed
    schema does not advertise the probe to tenants. Will be
    superseded by ``GET /admin/api/v1/me`` (cd-yj4k) — at that
    point this route can be removed.

    The body carries the :attr:`DeploymentContext.actor_kind` and a
    sorted list of granted scopes so tests can assert the dep
    populated the expected shape (full catalogue for session /
    delegated principals; row-pinned subset for scoped tokens).
    """
    return {
        "ok": True,
        "actor_kind": ctx.actor_kind,
        "user_id": ctx.user_id,
        "principal": ctx.principal,
        "scopes": sorted(ctx.deployment_scopes),
    }
