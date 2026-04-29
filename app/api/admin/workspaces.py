"""Deployment-admin workspace lifecycle routes.

Mounts under ``/admin/api/v1`` (§12 "Admin surface"):

* ``GET /workspaces`` — list every workspace (id, slug, plan,
  verification, size).
* ``GET /workspaces/{id}`` — summary card (counts, usage, recent
  activity).
* ``POST /workspaces/{id}/trust`` — promote
  ``verification_state`` to ``"trusted"``.
* ``POST /workspaces/{id}/archive`` — owners-only soft-delete.

Every route gates on
:func:`app.api.admin.deps.current_deployment_admin_principal`;
the canonical 404 envelope hides the surface from non-admin
callers per spec. ``archive`` additionally gates on
:func:`app.api.admin._owners.ensure_deployment_owner` — until
cd-zkr seeds the deployment owners group, every caller fails
that gate and the route 404s, which is the spec-mandated
fail-closed posture.

The ``verification_state`` and ``archived_at`` projections are
read off :mod:`app.api.admin._workspace_state` — an interim
adapter that stores the values inside
:attr:`Workspace.settings_json` until cd-s8kk lands the typed
columns + indexes. The seam keeps the admin surface usable
without forcing a schema migration into this task's atomic
landing.

See ``docs/specs/12-rest-api.md`` §"Admin surface" and
``docs/specs/02-domain-model.md`` §"workspaces".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.places.models import PropertyWorkspace
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.admin._audit import audit_admin
from app.api.admin._owners import ensure_deployment_owner
from app.api.admin._usage_helpers import (
    _deployment_default_cap,
    _list_workspace_aggregates,
    _resolved_cap,
    _window,
)
from app.api.admin._workspace_state import (
    format_archived_at,
    load_workspace,
    set_archived_at,
    set_verification_state,
    verification_state_of,
)
from app.api.admin.deps import current_deployment_admin_principal
from app.api.deps import db_session
from app.tenancy import DeploymentContext, tenant_agnostic

__all__ = [
    "WorkspaceArchiveResponse",
    "WorkspaceListResponse",
    "WorkspaceSummaryResponse",
    "WorkspaceTrustResponse",
    "build_admin_workspaces_router",
]


_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class WorkspaceListItem(BaseModel):
    """One row of ``GET /admin/api/v1/workspaces``.

    Mirrors the spec's "list (id, slug, plan, verification, size)"
    contract with a few inline aggregates the SPA's ``WorkspacesPage``
    renders without a follow-up call:

    * ``id`` / ``slug`` / ``name`` / ``plan`` — straight off
      :class:`Workspace`.
    * ``verification_state`` — read from the interim
      ``settings_json`` slot (cd-s8kk follow-up promotes the column).
    * ``properties_count`` — active :class:`PropertyWorkspace` rows.
    * ``members_count`` — count of :class:`UserWorkspace` rows.
    * ``spent_cents_30d`` / ``cap_cents_30d`` — rolling usage and
      resolved LLM budget cap, matching the admin usage table.
    * ``archived_at`` — ISO-8601 UTC string; ``None`` for live rows.
    * ``created_at`` — ISO-8601 UTC string for the row's creation.
    """

    id: str
    slug: str
    name: str
    plan: str
    verification_state: str
    properties_count: int
    members_count: int
    spent_cents_30d: int
    cap_cents_30d: int
    archived_at: str | None
    created_at: str


class WorkspaceListResponse(BaseModel):
    """Body of ``GET /admin/api/v1/workspaces``.

    Returned as ``{workspaces: [...]}`` rather than a bare array
    so the response is forward-compatible with the cursor envelope
    the wider §12 pagination contract uses (``data + next_cursor +
    has_more``). The cd-jlms slice ships every row in one page;
    the cursor envelope lands when the deployment grows past a
    practical fit-in-memory bound.
    """

    workspaces: list[WorkspaceListItem]


class WorkspaceSummaryResponse(BaseModel):
    """Body of ``GET /admin/api/v1/workspaces/{id}``.

    Carries the list-row fields plus the inline usage + admin
    counts the SPA's workspace-detail card needs in one round trip:

    * ``llm_calls_30d`` — count of :class:`LlmUsage` rows in the
      last 30 days.
    * ``llm_spend_cents_30d`` — sum of ``cost_cents`` over the
      same window.
    * ``admins_count`` — count of deployment-scoped grants whose
      :attr:`RoleGrant.workspace_id` points at this workspace
      (always zero today; wired here so cd-zkr's group rollout
      doesn't change the contract).
    """

    id: str
    slug: str
    name: str
    plan: str
    verification_state: str
    members_count: int
    llm_calls_30d: int
    llm_spend_cents_30d: int
    archived_at: str | None
    created_at: str


class WorkspaceTrustResponse(BaseModel):
    """Body of ``POST /admin/api/v1/workspaces/{id}/trust``.

    Echoes the post-mutation ``verification_state`` so the SPA's
    optimistic cache can swap the cell without a re-fetch.
    """

    id: str
    verification_state: str


class WorkspaceArchiveResponse(BaseModel):
    """Body of ``POST /admin/api/v1/workspaces/{id}/archive``.

    Returns the archive timestamp the route just stamped. The
    SPA renders the row as "archived" once this field is non-null;
    a re-archive of an already-archived row is a no-op (idempotent)
    and returns the original timestamp.
    """

    id: str
    archived_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_created_at(workspace: Workspace) -> str:
    """ISO-8601 UTC for ``workspace.created_at``.

    SQLite drops tzinfo on round-trips; force UTC so the wire
    value always carries the explicit ``+00:00`` offset (§02
    "Time is UTC at rest, local for display"). Mirrors the same
    fix-up in :mod:`app.api.admin.me`.
    """
    moment = workspace.created_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.isoformat()


def _members_count(session: Session, *, workspace_id: str) -> int:
    """Count :class:`UserWorkspace` rows for ``workspace_id``.

    Reads :class:`UserWorkspace` (the derived membership junction)
    rather than walking ``role_grant`` because the spec's
    "members_count" semantically tracks "users who can sign into
    this workspace" — exactly the join the refresh worker
    populates.
    """
    with tenant_agnostic():
        count = session.scalar(
            select(func.count())
            .select_from(UserWorkspace)
            .where(UserWorkspace.workspace_id == workspace_id)
        )
    return int(count or 0)


def _property_counts(session: Session) -> dict[str, int]:
    """Return active property-membership counts keyed by workspace id."""
    with tenant_agnostic():
        rows = session.execute(
            select(PropertyWorkspace.workspace_id, func.count())
            .where(PropertyWorkspace.status == "active")
            .group_by(PropertyWorkspace.workspace_id)
        ).all()
    return {workspace_id: int(count or 0) for workspace_id, count in rows}


def _llm_window_aggregates(
    session: Session,
    *,
    workspace_id: str,
    now: datetime,
) -> tuple[int, int]:
    """Return ``(call_count, spend_cents)`` for the rolling 30 d window.

    The cutoff is computed off ``now`` (the system clock) so tests
    can pin the window with a frozen clock. The query wraps in
    :func:`tenant_agnostic` because the admin tree runs on the
    bare host and the ORM tenant filter would otherwise either
    inject ``workspace_id = :ctx`` (we'd want it equal to the
    *target* workspace, not the active one) or fail closed for
    lack of context.
    """
    cutoff = now - _ROLLING_30D
    with tenant_agnostic():
        rows = session.execute(
            select(
                func.count(LlmUsage.id),
                func.coalesce(func.sum(LlmUsage.cost_cents), 0),
            )
            .where(LlmUsage.workspace_id == workspace_id)
            .where(LlmUsage.created_at >= cutoff)
        ).one()
    call_count, spend_cents = rows
    return int(call_count or 0), int(spend_cents or 0)


def _list_item(
    session: Session,
    *,
    workspace: Workspace,
    properties_count: int,
    spent_cents_30d: int,
    cap_cents_30d: int,
) -> WorkspaceListItem:
    """Project a :class:`Workspace` row into the list-row response."""
    return WorkspaceListItem(
        id=workspace.id,
        slug=workspace.slug,
        name=workspace.name,
        plan=workspace.plan,
        verification_state=verification_state_of(workspace),
        properties_count=properties_count,
        members_count=_members_count(session, workspace_id=workspace.id),
        spent_cents_30d=spent_cents_30d,
        cap_cents_30d=cap_cents_30d,
        archived_at=format_archived_at(workspace),
        created_at=_format_created_at(workspace),
    )


def _not_found() -> HTTPException:
    """Canonical 404 envelope — same shape as the admin auth dep emits."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found"},
    )


# Rolling-window cutoff for the workspace-summary aggregates.
# Module-level so tests can monkey-patch (or import) the same
# constant the production query uses.
_ROLLING_30D: Final[timedelta] = timedelta(days=30)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_admin_workspaces_router() -> APIRouter:
    """Return the router carrying the workspace-lifecycle admin routes.

    Mounted by :data:`app.api.admin.admin_router`; the router carries
    no prefix of its own, so its routes register at
    ``/workspaces`` / ``/workspaces/{id}`` / ``/workspaces/{id}/trust``
    / ``/workspaces/{id}/archive``.
    """
    router = APIRouter(tags=["admin"])

    @router.get(
        "/workspaces",
        response_model=WorkspaceListResponse,
        operation_id="admin.workspaces.list",
        summary="List every workspace on the deployment",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "workspaces-list",
                "summary": "List every workspace on the deployment",
                "mutates": False,
            },
        },
    )
    def list_workspaces(
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> WorkspaceListResponse:
        """Return every :class:`Workspace` row, ordered oldest-first.

        Matches the SPA's ``WorkspacesPage`` chronological roster.
        Archived workspaces stay in the list — the admin's
        "what does the deployment look like?" view must surface
        every tenant, not just the live ones; the
        :attr:`WorkspaceListItem.archived_at` cell tells the row
        apart visually.
        """
        with tenant_agnostic():
            rows = (
                session.execute(
                    select(Workspace).order_by(
                        Workspace.created_at.asc(), Workspace.id.asc()
                    )
                )
                .scalars()
                .all()
            )
        cutoff = _window(datetime.now(UTC))
        usage = _list_workspace_aggregates(session, cutoff=cutoff)
        property_counts = _property_counts(session)
        deployment_default = _deployment_default_cap(request)
        items = [
            _list_item(
                session,
                workspace=row,
                properties_count=property_counts.get(row.id, 0),
                spent_cents_30d=usage.get(row.id, (0, 0))[1],
                cap_cents_30d=_resolved_cap(row, deployment_default=deployment_default),
            )
            for row in rows
        ]
        return WorkspaceListResponse(workspaces=items)

    @router.get(
        "/workspaces/{id}",
        response_model=WorkspaceSummaryResponse,
        operation_id="admin.workspaces.get",
        summary="Return one workspace's detail card",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "workspaces-get",
                "summary": "Return one workspace's detail card",
                "mutates": False,
            },
        },
    )
    def get_workspace(
        id: str,
        _ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
    ) -> WorkspaceSummaryResponse:
        """Return the workspace's summary card or 404 when missing.

        ``id`` is the :class:`Workspace.id` ULID, matching the
        spec's "addressed by ``{id}``, not ``{slug}``" rule. A
        missing row 404s with the canonical envelope — same shape
        as a non-admin caller hitting any admin route, so the
        error surface stays uniform.
        """
        workspace = load_workspace(session, workspace_id=id)
        if workspace is None:
            raise _not_found()
        calls_30d, spend_30d = _llm_window_aggregates(
            session, workspace_id=workspace.id, now=datetime.now(UTC)
        )
        return WorkspaceSummaryResponse(
            id=workspace.id,
            slug=workspace.slug,
            name=workspace.name,
            plan=workspace.plan,
            verification_state=verification_state_of(workspace),
            members_count=_members_count(session, workspace_id=workspace.id),
            llm_calls_30d=calls_30d,
            llm_spend_cents_30d=spend_30d,
            archived_at=format_archived_at(workspace),
            created_at=_format_created_at(workspace),
        )

    @router.post(
        "/workspaces/{id}/trust",
        response_model=WorkspaceTrustResponse,
        operation_id="admin.workspaces.trust",
        summary='Promote a workspace to verification_state="trusted"',
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "workspaces-trust",
                "summary": "Promote a workspace to trusted",
                "mutates": True,
            },
        },
    )
    def trust_workspace(
        id: str,
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> WorkspaceTrustResponse:
        """Stamp ``verification_state='trusted'`` and audit.

        Idempotent: trusting an already-trusted workspace returns
        the same 200 envelope (no separate audit row). The SPA's
        optimistic cache treats the response as the new authoritative
        value either way.

        The mutation lifts the §15 "Tight initial caps" — at
        ``verification_state='human_verified'`` (and above), the
        free-tier quota blob expands to its full ceiling. The
        actual cap-blob refresh is owned by the workspace-side
        signup service (cd-3i5); this route only flips the state
        flag and writes the audit row. A future refactor wires
        the cap refresh into the same UoW.
        """
        workspace = load_workspace(session, workspace_id=id)
        if workspace is None:
            raise _not_found()
        previous = verification_state_of(workspace)
        if previous == "trusted":
            return WorkspaceTrustResponse(id=workspace.id, verification_state="trusted")
        with tenant_agnostic():
            set_verification_state(workspace, value="trusted")
            audit_admin(
                session,
                ctx=ctx,
                request=request,
                entity_kind="workspace",
                entity_id=workspace.id,
                action="workspace.trusted",
                diff={
                    "verification_state": {"before": previous, "after": "trusted"},
                },
            )
            session.flush()
        return WorkspaceTrustResponse(id=workspace.id, verification_state="trusted")

    @router.post(
        "/workspaces/{id}/archive",
        response_model=WorkspaceArchiveResponse,
        operation_id="admin.workspaces.archive",
        summary="Soft-archive a workspace (deployment owners only)",
        openapi_extra={
            "x-cli": {
                "group": "admin",
                "verb": "workspaces-archive",
                "summary": "Soft-archive a workspace",
                "mutates": True,
            },
        },
    )
    def archive_workspace(
        id: str,
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
        session: _Db,
        request: Request,
    ) -> WorkspaceArchiveResponse:
        """Stamp ``archived_at`` and audit.

        **Owners-only** per spec §12 "Admin surface". The owner
        check fails closed today (cd-zkr has not yet seeded the
        deployment owners group); every caller therefore 404s.
        Once cd-zkr lands, only deployment-owner admins pass —
        a non-owner admin still sees a 404 (surface invisibility).

        Idempotent: archiving an already-archived workspace
        returns the original timestamp (no fresh stamp, no
        duplicate audit row). The SPA renders the row as
        archived either way.
        """
        ensure_deployment_owner(session, ctx=ctx)
        workspace = load_workspace(session, workspace_id=id)
        if workspace is None:
            raise _not_found()
        existing = format_archived_at(workspace)
        if existing is not None:
            return WorkspaceArchiveResponse(id=workspace.id, archived_at=existing)
        moment = datetime.now(UTC)
        with tenant_agnostic():
            set_archived_at(workspace, when=moment)
            audit_admin(
                session,
                ctx=ctx,
                request=request,
                entity_kind="workspace",
                entity_id=workspace.id,
                action="workspace.archived",
                diff={"archived_at": moment.astimezone(UTC).isoformat()},
            )
            session.flush()
        formatted = format_archived_at(workspace)
        # ``set_archived_at`` always lands a parseable value, so
        # ``format_archived_at`` returns a non-null string here;
        # the defensive fallback below preserves the response
        # contract if a future refactor regresses that invariant.
        if formatted is None:  # pragma: no cover - defensive
            formatted = moment.astimezone(UTC).isoformat()
        return WorkspaceArchiveResponse(id=workspace.id, archived_at=formatted)

    return router
