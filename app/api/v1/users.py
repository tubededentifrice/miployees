"""Workspace-scoped user-management HTTP router (``/users/*``).

Mounted inside the ``/w/<slug>/api/v1`` tree by the app factory.
Every route requires an active :class:`~app.tenancy.WorkspaceContext`.
Today's v1 surface:

* ``POST /users/invite`` — spec §03 "Additional users (invite →
  click-to-accept)". Inserts a pending ``invite`` row, mails the
  ``grant_invite`` magic link. A pending ``work_engagement`` row is
  NOT created at invite time — the accept-side path
  (:func:`app.domain.identity.membership._activate_invite`) seeds
  it once the invitee completes their passkey challenge.
* ``PATCH /users/{user_id}`` — partial profile update. Self-edits
  pass through without a capability check; edits targeting someone
  else require ``users.edit_profile_other`` (default
  ``owners, managers``).
* ``POST /users/{user_id}/archive`` — soft-archive the user's
  :class:`WorkEngagement` in this workspace PLUS every active
  :class:`UserWorkRole` they hold here. Idempotent.
* ``POST /users/{user_id}/reinstate`` — reverse archive. Idempotent.
  v1 implementation is workspace-local; the cross-workspace
  reinstate path that clears ``users.archived_at`` is tracked as a
  follow-up.
* ``DELETE /users/{user_id}/grants`` — removes every role_grant +
  permission_group_member + user_workspace row for ``user_id`` in
  the caller's workspace, plus revokes all sessions scoped there.
  Honours the last-owner guard.

Handler shape mirrors :mod:`app.api.v1.auth.signup` — unpack body,
call the domain service, map typed errors to HTTP symbols. The
UoW (:func:`app.api.deps.db_session`) owns the transaction
boundary; domain code never commits itself.

See ``docs/specs/12-rest-api.md`` §"Users",
``docs/specs/03-auth-and-tokens.md`` §"Additional users", and
``docs/specs/05-employees-and-roles.md`` §"Archive / reinstate".
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import Workspace
from app.adapters.mail.ports import Mailer
from app.api.deps import current_workspace_context, db_session
from app.auth._throttle import Throttle
from app.authz import PermissionDenied
from app.config import Settings, get_settings
from app.domain.identity import membership
from app.domain.identity.permission_groups import (
    LastOwnerMember,
    write_member_remove_rejected_audit,
)
from app.services.employees import (
    EmployeeNotFound,
    EmployeeProfileUpdate,
    EmployeeView,
    ProfileFieldForbidden,
    archive_employee,
    get_employee,
    reinstate_employee,
    update_profile,
)
from app.tenancy import WorkspaceContext, tenant_agnostic

__all__ = [
    "EmployeeProfileResponse",
    "EmployeeUpdateRequest",
    "InviteRequest",
    "InviteResponse",
    "build_users_router",
]


_log = logging.getLogger(__name__)


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class GrantInput(BaseModel):
    """One entry in the ``grants`` list on ``POST /users/invite``.

    v1 only accepts ``scope_kind='workspace'`` (property / organization
    scopes land in a follow-up). ``scope_id`` must match the caller's
    workspace — the domain service rejects mismatches with
    :class:`~app.domain.identity.membership.InviteBodyInvalid`.
    """

    scope_kind: str = Field("workspace", description="Always 'workspace' in v1.")
    scope_id: str = Field(..., description="Target workspace id (ULID).")
    grant_role: str = Field(
        ..., description="One of 'manager' | 'worker' | 'client' | 'guest'."
    )
    # Reserved fields present for spec parity; ignored by the v1
    # domain service. Keeping them on the wire means a follow-up
    # (organization-scope) lands without a breaking body shape.
    binding_org_id: str | None = None
    scope_property_id: str | None = None


class GroupMembershipInput(BaseModel):
    """One entry in the ``permission_group_memberships`` list."""

    group_id: str


class InviteRequest(BaseModel):
    """Request body for ``POST /users/invite``.

    ``work_engagement`` + ``user_work_roles`` are intentionally absent:
    their backing tables don't exist in Phase 1. A future body-shape
    bump will add them without breaking callers that send the current
    payload. Tracked as cd-1hd0.
    """

    email: str = Field(..., min_length=3, max_length=320)
    display_name: str = Field(..., min_length=1, max_length=160)
    grants: list[GrantInput]
    permission_group_memberships: list[GroupMembershipInput] | None = None


class InviteResponse(BaseModel):
    """Response body for ``POST /users/invite``."""

    invite_id: str
    pending_email: str
    user_id: str | None
    user_created: bool


class RemoveMemberResponse(BaseModel):
    """Response body for ``DELETE /users/{user_id}/grants``.

    Empty — a 204 would be more idiomatic but carrying a body lets
    the SPA assert on the symbolic outcome without introspecting the
    status code. Follow-up refactor may switch to 204.
    """

    status: str = "removed"


class EmployeeUpdateRequest(BaseModel):
    """Request body for ``PATCH /users/{user_id}``.

    Mirrors :class:`~app.services.employees.EmployeeProfileUpdate` —
    every field is optional and Pydantic's ``model_fields_set``
    distinguishes "omitted" from "explicitly set to None".
    ``extra='forbid'`` so unknown fields fail loud at 422 rather
    than being silently ignored. ``display_name=None`` is rejected
    at the DTO boundary via :meth:`_reject_display_name_null` so
    the NOT NULL contract on :class:`User` surfaces as a 422
    validation error rather than a 500.
    """

    model_config = {"extra": "forbid"}

    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    locale: str | None = Field(default=None, max_length=35)
    timezone: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _reject_display_name_null(self) -> EmployeeUpdateRequest:
        """Reject an explicit ``display_name=None`` — the column is NOT NULL."""
        if "display_name" in self.model_fields_set and self.display_name is None:
            raise ValueError("display_name cannot be cleared; it is NOT NULL")
        return self


class EmployeeProfileResponse(BaseModel):
    """Response body for ``PATCH /users/{user_id}`` / ``GET /users/{user_id}``.

    Projection of :class:`~app.services.employees.EmployeeView` —
    carries the minimal identity-level fields the SPA needs plus the
    workspace-scoped engagement archival marker (derived from the
    active :class:`~app.adapters.db.workspace.models.WorkEngagement`
    row, if any).
    """

    id: str
    email: str
    display_name: str
    locale: str | None
    timezone: str | None
    avatar_blob_hash: str | None
    engagement_archived_on: str | None
    created_at: str


def _view_to_response(view: EmployeeView) -> EmployeeProfileResponse:
    """Project an :class:`EmployeeView` into the HTTP response shape."""
    return EmployeeProfileResponse(
        id=view.id,
        email=view.email,
        display_name=view.display_name,
        locale=view.locale,
        timezone=view.timezone,
        avatar_blob_hash=view.avatar_blob_hash,
        engagement_archived_on=(
            view.engagement_archived_on.isoformat()
            if view.engagement_archived_on is not None
            else None
        ),
        created_at=view.created_at.isoformat(),
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _http_for_invite(exc: Exception) -> HTTPException:
    """Map a :func:`membership.invite` domain error to an HTTP response."""
    if isinstance(exc, membership.InviteBodyInvalid):
        return HTTPException(
            status_code=422,
            detail={"error": "invalid_body", "message": str(exc)},
        )
    # Rate-limited from the magic-link throttle is unlikely on this
    # route (per-invite throttle is cheap) but we map it for safety.
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def _http_for_remove(exc: Exception) -> HTTPException:
    """Map a :func:`membership.remove_member` error to an HTTP response.

    :class:`LastOwnerMember` is **not** handled here: it is a
    :class:`~app.domain.errors.Validation` subclass, so the RFC 7807
    exception handler in :mod:`app.api.errors` translates it directly
    into a 422 ``would_orphan_owners_group`` problem+json envelope.
    The router still intercepts it to write the forensic rejection
    audit row on a fresh UoW, then re-raises the typed exception.
    """
    if isinstance(exc, membership.NotAMember):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_a_member"},
        )
    if isinstance(exc, membership.InviteStateInvalid):
        # ``InviteStateInvalid`` is re-raised when the workspace has
        # no owners group — a server-side invariant break.
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "workspace_invariant_broken"},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def _client_ip(request: Request) -> str:
    """Best-effort source IP for ``request``."""
    if request.client is None:
        return ""
    return request.client.host


def _resolve_inviter_display_name(session: Session, *, user_id: str) -> str:
    """Return the inviter's display name for the invite email copy."""
    from app.adapters.db.identity.models import User

    with tenant_agnostic():
        user = session.get(User, user_id)
    if user is None:
        return "A crew.day user"
    return user.display_name


def _resolve_workspace_name(session: Session, *, workspace_id: str) -> str:
    """Return the workspace display name for the invite email copy."""
    row = session.scalar(select(Workspace).where(Workspace.id == workspace_id))
    if row is None:
        return "your workspace"
    return row.name


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_users_router(
    *,
    mailer: Mailer,
    throttle: Throttle,
    base_url: str | None = None,
    settings: Settings | None = None,
) -> APIRouter:
    """Return a fresh :class:`APIRouter` wired to ``mailer`` + ``throttle``.

    Mounted by the v1 app factory at
    ``/w/<slug>/api/v1/users``. Tests instantiate it directly with
    a recording mailer + per-case throttle for isolation.
    """
    # Tags: ``identity`` (spec §01 context map) surfaces every
    # identity-adjacent operation under one OpenAPI section;
    # ``users`` is kept for back-compat with existing clients that
    # filter on the finer-grained tag.
    router = APIRouter(prefix="/users", tags=["identity", "users"])
    cfg = settings if settings is not None else get_settings()
    resolved_base_url = base_url if base_url is not None else cfg.public_url

    @router.post(
        "/invite",
        status_code=status.HTTP_201_CREATED,
        response_model=InviteResponse,
        summary="Invite a user to the caller's workspace",
    )
    def post_invite(
        body: InviteRequest,
        request: Request,
        ctx: _Ctx,
        session: _Db,
    ) -> InviteResponse:
        """Create or refresh a pending invite and mail the magic link."""
        del request  # ip currently unused; magic_link re-derives internally
        if resolved_base_url is None:
            raise RuntimeError(
                "base_url / settings.public_url is not set; "
                "cannot build magic-link URLs"
            )

        grants_payload: list[dict[str, Any]] = [g.model_dump() for g in body.grants]
        memberships_payload: list[dict[str, Any]] = [
            gm.model_dump() for gm in (body.permission_group_memberships or [])
        ]
        inviter_display_name = _resolve_inviter_display_name(
            session, user_id=ctx.actor_id
        )
        workspace_name = _resolve_workspace_name(session, workspace_id=ctx.workspace_id)

        try:
            outcome = membership.invite(
                session,
                ctx,
                email=body.email,
                display_name=body.display_name,
                grants=grants_payload,
                group_memberships=memberships_payload,
                mailer=mailer,
                throttle=throttle,
                base_url=resolved_base_url,
                settings=cfg,
                inviter_display_name=inviter_display_name,
                workspace_name=workspace_name,
            )
        except membership.InviteBodyInvalid as exc:
            raise _http_for_invite(exc) from exc

        return InviteResponse(
            invite_id=outcome.id,
            pending_email=outcome.pending_email,
            user_id=outcome.user_id,
            user_created=outcome.user_created,
        )

    @router.patch(
        "/{user_id}",
        response_model=EmployeeProfileResponse,
        summary="Update the profile of a user in the caller's workspace",
    )
    def patch_user(
        user_id: str,
        body: EmployeeUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> EmployeeProfileResponse:
        """Partial profile update.

        Routes-level contract:

        * 200 + refreshed view on success.
        * 404 ``employee_not_found`` when the target user is not a
          member of the caller's workspace.
        * 403 ``forbidden`` when the caller is neither the target nor
          holds ``users.edit_profile_other``.
        * 422 on DTO validation failure (pydantic).
        """
        # Re-emit the request body into the service DTO. The HTTP
        # shape and the service shape are intentionally identical so
        # this is a straight passthrough; keeping the two types
        # distinct lets us evolve either surface without touching
        # the other.
        sent_fields = body.model_fields_set
        service_body = EmployeeProfileUpdate.model_validate(
            {f: getattr(body, f) for f in sent_fields}
        )

        try:
            view = update_profile(
                session,
                ctx,
                user_id=user_id,
                body=service_body,
            )
        except EmployeeNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "employee_not_found"},
            ) from exc
        except ProfileFieldForbidden as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "forbidden"},
            ) from exc
        return _view_to_response(view)

    @router.post(
        "/{user_id}/archive",
        response_model=EmployeeProfileResponse,
        summary="Archive a user's engagement + work roles in this workspace",
    )
    def post_archive(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> EmployeeProfileResponse:
        """Soft-archive + idempotent. See §05 "Archive / reinstate"."""
        try:
            view = archive_employee(session, ctx, user_id=user_id)
        except EmployeeNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "employee_not_found"},
            ) from exc
        except PermissionDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "forbidden"},
            ) from exc
        return _view_to_response(view)

    @router.post(
        "/{user_id}/reinstate",
        response_model=EmployeeProfileResponse,
        summary="Reinstate a user's engagement + work roles in this workspace",
    )
    def post_reinstate(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> EmployeeProfileResponse:
        """Reverse archive. Idempotent. See §05 "Archive / reinstate"."""
        try:
            view = reinstate_employee(session, ctx, user_id=user_id)
        except EmployeeNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "employee_not_found"},
            ) from exc
        except PermissionDenied as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "forbidden"},
            ) from exc
        return _view_to_response(view)

    @router.get(
        "/{user_id}",
        response_model=EmployeeProfileResponse,
        summary="Read a user's workspace-scoped profile",
    )
    def get_user(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> EmployeeProfileResponse:
        """Read-only projection the SPA keys off for the profile page."""
        try:
            view = get_employee(session, ctx, user_id=user_id)
        except EmployeeNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "employee_not_found"},
            ) from exc
        return _view_to_response(view)

    @router.delete(
        "/{user_id}/grants",
        response_model=RemoveMemberResponse,
        summary="Remove a user's grants + sessions in the caller's workspace",
    )
    def delete_grants(
        user_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> RemoveMemberResponse:
        """Strip every grant + group-member + session tied to ``user_id``."""
        try:
            membership.remove_member(session, ctx, user_id=user_id)
        except LastOwnerMember:
            # Typed refusal — forensic row lands on a fresh UoW so
            # the primary UoW's rollback does not sweep it. Re-raise
            # the typed exception so the RFC 7807 seam translates it
            # into a 422 ``would_orphan_owners_group`` envelope.
            try:
                with make_uow() as fresh:
                    assert isinstance(fresh, Session)
                    write_member_remove_rejected_audit(
                        fresh,
                        ctx,
                        group_id=_resolve_owners_group_id(session, ctx.workspace_id)
                        or "",
                        user_id=user_id,
                        reason="would_orphan_owners_group",
                    )
            except Exception:
                _log.exception("remove_member refusal audit failed on fresh UoW")
            raise
        except (membership.NotAMember, membership.InviteStateInvalid) as exc:
            raise _http_for_remove(exc) from exc

        return RemoveMemberResponse()

    return router


def _resolve_owners_group_id(session: Session, workspace_id: str) -> str | None:
    """Return the ``owners`` group id for the workspace or ``None``."""
    from app.adapters.db.authz.models import PermissionGroup

    row = session.scalar(
        select(PermissionGroup).where(
            PermissionGroup.workspace_id == workspace_id,
            PermissionGroup.slug == "owners",
            PermissionGroup.system.is_(True),
        )
    )
    return row.id if row is not None else None
