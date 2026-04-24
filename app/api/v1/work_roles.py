"""Work-roles HTTP router — ``/work_roles`` (spec §12).

Mounted inside the ``/w/<slug>/api/v1`` tree by the app factory. Every
route requires an active :class:`~app.tenancy.WorkspaceContext`. The
v1 surface:

* ``GET /work_roles`` — cursor-paginated list of live work roles.
* ``POST /work_roles`` — create a new work role.
* ``PATCH /work_roles/{id}`` — partial update.

The spec §12 "Users / work roles / settings" does not expose a
``DELETE`` endpoint for work roles today (soft-delete is reserved
for a later lifecycle story that needs a resolution path for the
user_work_role rows still pointing at the role). The DTO + service
pair is ready for it whenever that lands.

Every route tagged ``identity`` (§01 context map) + ``work_roles``
(finer-grained filter). Mutations gate on ``work_roles.manage`` at
workspace scope (§05 action catalog, default-allow ``owners,
managers``); reads gate on ``scope.view`` so every grant role can
read the catalogue.

See ``docs/specs/05-employees-and-roles.md`` §"Work role",
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.api.pagination import (
    DEFAULT_LIMIT,
    LimitQuery,
    PageCursorQuery,
    decode_cursor,
    paginate,
)
from app.authz import Permission
from app.domain.identity.work_roles import (
    WorkRoleCreate,
    WorkRoleKeyConflict,
    WorkRoleNotFound,
    WorkRoleUpdate,
    WorkRoleView,
    create_work_role,
    list_work_roles,
    update_work_role,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "WorkRoleCreateRequest",
    "WorkRoleListResponse",
    "WorkRoleResponse",
    "WorkRoleUpdateRequest",
    "build_work_roles_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class WorkRoleCreateRequest(BaseModel):
    """Request body for ``POST /work_roles``.

    Mirrors :class:`~app.domain.identity.work_roles.WorkRoleCreate` —
    the HTTP shape and the service shape are identical, so this is a
    straight passthrough. Keeping the two types distinct means a
    later evolution of either surface does not force a breaking
    change on the other.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=160)
    description_md: str = Field(default="", max_length=20_000)
    default_settings_json: dict[str, Any] = Field(default_factory=dict)
    icon_name: str = Field(default="", max_length=64)


class WorkRoleUpdateRequest(BaseModel):
    """Request body for ``PATCH /work_roles/{id}``."""

    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description_md: str | None = Field(default=None, max_length=20_000)
    default_settings_json: dict[str, Any] | None = Field(default=None)
    icon_name: str | None = Field(default=None, max_length=64)


class WorkRoleResponse(BaseModel):
    """Response element for ``GET``/``POST``/``PATCH`` on work roles."""

    id: str
    workspace_id: str
    key: str
    name: str
    description_md: str
    default_settings_json: dict[str, Any]
    icon_name: str
    created_at: datetime
    deleted_at: datetime | None


class WorkRoleListResponse(BaseModel):
    """Collection envelope for ``GET /work_roles``.

    Shape matches §12 "Pagination" verbatim — ``{data, next_cursor,
    has_more}``.
    """

    data: list[WorkRoleResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _http_for_key_conflict(exc: WorkRoleKeyConflict) -> HTTPException:
    """Translate a duplicate-key into a 422 ``work_role_key_conflict``."""
    return HTTPException(
        status_code=422,
        detail={"error": "work_role_key_conflict", "message": str(exc)},
    )


def _view_to_response(view: WorkRoleView) -> WorkRoleResponse:
    return WorkRoleResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        key=view.key,
        name=view.name,
        description_md=view.description_md,
        default_settings_json=dict(view.default_settings_json),
        icon_name=view.icon_name,
        created_at=view.created_at,
        deleted_at=view.deleted_at,
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_work_roles_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired to the work-roles surface."""
    api = APIRouter(prefix="/work_roles", tags=["identity", "work_roles"])

    manage_gate = Depends(Permission("work_roles.manage", scope_kind="workspace"))
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))

    @api.get(
        "",
        response_model=WorkRoleListResponse,
        operation_id="work_roles.list",
        summary="List work roles in the caller's workspace",
        dependencies=[view_gate],
    )
    def list_(
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> WorkRoleListResponse:
        """Return a cursor-paginated page of live work-role rows."""
        after_id = decode_cursor(cursor)
        # Service returns up to ``limit + 1`` rows so :func:`paginate`
        # can compute ``has_more`` without a second query.
        views = list_work_roles(
            session,
            ctx,
            limit=limit,
            after_id=after_id,
        )
        page = paginate(
            views,
            limit=limit,
            key_getter=lambda v: v.id,
        )
        return WorkRoleListResponse(
            data=[_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=WorkRoleResponse,
        operation_id="work_roles.create",
        summary="Create a new work role",
        dependencies=[manage_gate],
    )
    def create(
        body: WorkRoleCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkRoleResponse:
        """Insert a new work role — unique per ``(workspace, key)``."""
        service_body = WorkRoleCreate.model_validate(body.model_dump())
        try:
            view = create_work_role(session, ctx, body=service_body)
        except WorkRoleKeyConflict as exc:
            raise _http_for_key_conflict(exc) from exc
        return _view_to_response(view)

    @api.patch(
        "/{work_role_id}",
        response_model=WorkRoleResponse,
        operation_id="work_roles.update",
        summary="Partial update of a work role",
        dependencies=[manage_gate],
    )
    def update(
        work_role_id: str,
        body: WorkRoleUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> WorkRoleResponse:
        """Update only the fields present in the body.

        Omitted fields stay put. A zero-delta patch is a no-op that
        still returns 200 with the current view — matches the
        users.patch convention.
        """
        # Passthrough to the service DTO. ``model_fields_set`` is
        # preserved through ``model_validate`` when we serialise only
        # the sent fields.
        sent = body.model_fields_set
        service_body = WorkRoleUpdate.model_validate(
            {f: getattr(body, f) for f in sent}
        )
        try:
            view = update_work_role(
                session, ctx, work_role_id=work_role_id, body=service_body
            )
        except WorkRoleNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "work_role_not_found"},
            ) from exc
        except WorkRoleKeyConflict as exc:
            raise _http_for_key_conflict(exc) from exc
        return _view_to_response(view)

    return api


router = build_work_roles_router()
