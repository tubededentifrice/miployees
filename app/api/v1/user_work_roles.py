"""User-work-roles HTTP router — ``/user_work_roles`` + ``/users/{id}/user_work_roles``.

Spec §12 "Users / work roles / settings":

```
GET    /users/{id}/user_work_roles
POST   /user_work_roles
PATCH  /user_work_roles/{id}
DELETE /user_work_roles/{id}
```

Every route requires an active :class:`~app.tenancy.WorkspaceContext`
and tags ``identity`` + ``user_work_roles``. Mutations gate on
``work_roles.manage`` at workspace scope. The list endpoint is
user-scoped (reads the user's user_work_role rows) and uses
``scope.view`` so any grant role can see their own (workers typically
only fetch their own record).

See ``docs/specs/05-employees-and-roles.md`` §"User work role",
``docs/specs/12-rest-api.md`` §"Users / work roles / settings".
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
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
from app.domain.identity.user_work_roles import (
    UserWorkRoleCreate,
    UserWorkRoleInvariantViolated,
    UserWorkRoleNotFound,
    UserWorkRoleUpdate,
    UserWorkRoleView,
    create_user_work_role,
    delete_user_work_role,
    list_user_work_roles,
    update_user_work_role,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "UserWorkRoleCreateRequest",
    "UserWorkRoleListResponse",
    "UserWorkRoleResponse",
    "UserWorkRoleUpdateRequest",
    "build_user_work_roles_router",
    "build_users_user_work_roles_router",
    "router",
    "users_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class UserWorkRoleCreateRequest(BaseModel):
    """Request body for ``POST /user_work_roles``."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., min_length=1, max_length=64)
    work_role_id: str = Field(..., min_length=1, max_length=64)
    started_on: date
    ended_on: date | None = None
    pay_rule_id: str | None = Field(default=None, max_length=64)


class UserWorkRoleUpdateRequest(BaseModel):
    """Request body for ``PATCH /user_work_roles/{id}``.

    Only ``ended_on`` and ``pay_rule_id`` are mutable per §05 "User
    work role" — mutating identity columns (``user_id``,
    ``work_role_id``, ``started_on``) requires a new row. The
    frozen-column stance matches the service DTO.
    """

    model_config = ConfigDict(extra="forbid")

    ended_on: date | None = Field(default=None)
    pay_rule_id: str | None = Field(default=None, max_length=64)


class UserWorkRoleResponse(BaseModel):
    """Response shape for work-role link operations."""

    id: str
    user_id: str
    workspace_id: str
    work_role_id: str
    started_on: date
    ended_on: date | None
    pay_rule_id: str | None
    created_at: datetime
    deleted_at: datetime | None


class UserWorkRoleListResponse(BaseModel):
    """Collection envelope for ``GET /users/{id}/user_work_roles``."""

    data: list[UserWorkRoleResponse]
    next_cursor: str | None = None
    has_more: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _view_to_response(view: UserWorkRoleView) -> UserWorkRoleResponse:
    return UserWorkRoleResponse(
        id=view.id,
        user_id=view.user_id,
        workspace_id=view.workspace_id,
        work_role_id=view.work_role_id,
        started_on=view.started_on,
        ended_on=view.ended_on,
        pay_rule_id=view.pay_rule_id,
        created_at=view.created_at,
        deleted_at=view.deleted_at,
    )


def _http_for_invariant(exc: UserWorkRoleInvariantViolated) -> HTTPException:
    """Translate a §05 invariant violation into 422."""
    return HTTPException(
        status_code=422,
        detail={"error": "user_work_role_invariant", "message": str(exc)},
    )


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "user_work_role_not_found"},
    )


# ---------------------------------------------------------------------------
# Router factories
# ---------------------------------------------------------------------------


def build_user_work_roles_router() -> APIRouter:
    """Return the top-level ``/user_work_roles`` router (POST + PATCH + DELETE)."""
    api = APIRouter(prefix="/user_work_roles", tags=["identity", "user_work_roles"])

    manage_gate = Depends(Permission("work_roles.manage", scope_kind="workspace"))

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=UserWorkRoleResponse,
        operation_id="user_work_roles.create",
        summary="Link a user to a work role inside the caller's workspace",
        dependencies=[manage_gate],
    )
    def create(
        body: UserWorkRoleCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserWorkRoleResponse:
        """Insert a ``user_work_role`` link after the §05 invariants."""
        service_body = UserWorkRoleCreate.model_validate(body.model_dump())
        try:
            view = create_user_work_role(session, ctx, body=service_body)
        except UserWorkRoleInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.patch(
        "/{user_work_role_id}",
        response_model=UserWorkRoleResponse,
        operation_id="user_work_roles.update",
        summary="Partial update of a user_work_role link",
        dependencies=[manage_gate],
    )
    def update(
        user_work_role_id: str,
        body: UserWorkRoleUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> UserWorkRoleResponse:
        """Update ``ended_on`` / ``pay_rule_id``."""
        sent = body.model_fields_set
        service_body = UserWorkRoleUpdate.model_validate(
            {f: getattr(body, f) for f in sent}
        )
        try:
            view = update_user_work_role(
                session, ctx, user_work_role_id=user_work_role_id, body=service_body
            )
        except UserWorkRoleNotFound as exc:
            raise _http_for_not_found() from exc
        except UserWorkRoleInvariantViolated as exc:
            raise _http_for_invariant(exc) from exc
        return _view_to_response(view)

    @api.delete(
        "/{user_work_role_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="user_work_roles.delete",
        summary="Soft-delete a user_work_role link — idempotent",
        dependencies=[manage_gate],
    )
    def delete(
        user_work_role_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Stamp ``deleted_at``; no response body per §12 "Deletion"."""
        try:
            delete_user_work_role(session, ctx, user_work_role_id=user_work_role_id)
        except UserWorkRoleNotFound as exc:
            raise _http_for_not_found() from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


def build_users_user_work_roles_router() -> APIRouter:
    """Return the ``/users/{user_id}/user_work_roles`` list router.

    Separated from the top-level router so the list URL keeps its
    spec shape (``/users/{id}/user_work_roles``) without mounting
    every other ``/users`` endpoint under the same tree. The app
    factory mounts this alongside the users router, see
    :mod:`app.api.factory`.
    """
    api = APIRouter(prefix="/users", tags=["identity", "user_work_roles"])

    # Listing is user-scoped. A worker fetching their own record is a
    # common read-path hit, so ``scope.view`` is the right default-
    # allow (includes ``all_workers``). Cross-user reads by a worker
    # are not a security concern at workspace scope — the roster is
    # visible to every grant role.
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))

    @api.get(
        "/{user_id}/user_work_roles",
        response_model=UserWorkRoleListResponse,
        operation_id="user_work_roles.list_by_user",
        summary="List a user's work-role links in the caller's workspace",
        dependencies=[view_gate],
    )
    def list_(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
        cursor: PageCursorQuery = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> UserWorkRoleListResponse:
        """Cursor-paginated listing of live user_work_role rows."""
        after_id = decode_cursor(cursor)
        views = list_user_work_roles(
            session,
            ctx,
            user_id=user_id,
            limit=limit,
            after_id=after_id,
        )
        page = paginate(
            views,
            limit=limit,
            key_getter=lambda v: v.id,
        )
        return UserWorkRoleListResponse(
            data=[_view_to_response(v) for v in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    return api


router = build_user_work_roles_router()
users_router = build_users_user_work_roles_router()
