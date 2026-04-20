"""Deployment-scoped admin API tree.

Exposes :data:`admin_router` — a bare :class:`APIRouter` the app
factory mounts under ``/admin/api/v1`` (spec §12 "Base URL",
"Admin surface"). The router is empty in this task (cd-ika7);
real routes land with the admin Beads tasks (cd-jlms et al.).

A caller with no deployment grant will receive ``404`` on every
path under this prefix — that enforcement lives in the admin
authz middleware (cd-jlms), not here. This module only carries
the URL-routing seat so downstream work has a predictable place
to wire routes without reshaping the factory.

See ``docs/specs/12-rest-api.md`` §"Admin surface".
"""

from __future__ import annotations

from fastapi import APIRouter

admin_router = APIRouter(tags=["admin"])

__all__ = ["admin_router"]
