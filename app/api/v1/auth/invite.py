"""Invite-accept HTTP router (bare host).

Bare-host routes, tenant-agnostic at entry — the invite token carries
the target workspace via its ``subject_id``. The router branches on
invite shape:

* ``POST /invite/accept`` ``{token}`` — redeems the magic link and
  returns either a ``NewUserAcceptance`` (brand-new invitee — the
  SPA forwards to the passkey ceremony, which on finish calls
  :func:`app.domain.identity.membership.complete_invite` via
  :mod:`app.api.v1.auth.passkey`) or an ``ExistingUserAcceptance``
  (known user with an active session — the SPA renders the
  acceptance card).
* ``POST /invite/{invite_id}/confirm`` — existing-user second leg:
  confirms the pending invite activation.
* ``POST /invite/complete`` — new-user second leg (post passkey-
  finish hook). Today :func:`app.auth.passkey.register_finish_signup`
  does not carry invite awareness; the SPA posts here with the
  ``invite_id`` it kept from the ``/invite/accept`` response once
  the passkey ceremony completes. A later consolidation (cd-kd26)
  folds this into the passkey-finish callback.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users".
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.auth import session as auth_session
from app.auth._throttle import ConsumeLockout, Throttle
from app.auth.magic_link import (
    AlreadyConsumed,
    InvalidToken,
    PurposeMismatch,
    RateLimited,
    TokenExpired,
)
from app.config import Settings, get_settings
from app.domain.identity import membership
from app.tenancy import WorkspaceContext

__all__ = [
    "AcceptRequest",
    "AcceptResponse",
    "CompleteRequest",
    "ConfirmResponse",
    "build_invite_router",
]


_log = logging.getLogger(__name__)


_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AcceptRequest(BaseModel):
    """Request body for ``POST /invite/accept``."""

    token: str = Field(..., min_length=1)


class AcceptResponse(BaseModel):
    """Union response — the SPA branches on ``kind``."""

    kind: str = Field(
        ...,
        description=(
            "'new_user' → kick off the passkey ceremony; "
            "'existing_user' → render the acceptance card; "
            "'needs_sign_in' → prompt a passkey sign-in first."
        ),
    )
    invite_id: str
    # Populated on the ``new_user`` branch.
    user_id: str | None = None
    email_lower: str | None = None
    display_name: str | None = None
    # Populated on the ``existing_user`` branch.
    workspace_id: str | None = None
    workspace_slug: str | None = None
    workspace_name: str | None = None
    grants: list[dict[str, Any]] | None = None
    permission_group_memberships: list[dict[str, Any]] | None = None


class ConfirmResponse(BaseModel):
    """Response body for ``POST /invite/{invite_id}/confirm``."""

    workspace_id: str
    redirect: str


class CompleteRequest(BaseModel):
    """Request body for ``POST /invite/complete``.

    Called by the SPA after the passkey ceremony completes for a
    brand-new invitee. Follow-up cd-kd26 folds this into the
    passkey-finish callback; until then the SPA owns the handoff.
    """

    invite_id: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


_TokenDomainError = (
    InvalidToken,
    PurposeMismatch,
    TokenExpired,
    AlreadyConsumed,
    ConsumeLockout,
    RateLimited,
)


_InviteDomainError = (
    membership.InviteNotFound,
    membership.InviteStateInvalid,
    membership.InviteExpired,
    membership.InviteAlreadyAccepted,
    membership.PasskeySessionRequired,
)


def _http_for_token(exc: Exception) -> HTTPException:
    """Map a magic-link domain error onto an HTTP response."""
    if isinstance(exc, TokenExpired):
        return HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"error": "expired"},
        )
    if isinstance(exc, AlreadyConsumed):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "already_consumed"},
        )
    if isinstance(exc, PurposeMismatch):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "purpose_mismatch"},
        )
    if isinstance(exc, ConsumeLockout):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "consume_locked_out"},
        )
    if isinstance(exc, RateLimited):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited"},
        )
    # InvalidToken — default fallback.
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_token"},
    )


def _http_for_invite(exc: Exception) -> HTTPException:
    """Map a :mod:`membership` domain error onto an HTTP response."""
    if isinstance(exc, membership.InviteNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "invite_not_found"},
        )
    if isinstance(exc, membership.InviteExpired):
        return HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"error": "expired"},
        )
    if isinstance(exc, membership.InviteAlreadyAccepted):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "already_accepted"},
        )
    if isinstance(exc, membership.InviteStateInvalid):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "invalid_state"},
        )
    # PasskeySessionRequired is handled inline on /accept so the SPA
    # can render the ``needs_sign_in`` hint; mapping it here is the
    # fallback for the /confirm route.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "passkey_session_required"},
    )


def _client_ip(request: Request) -> str:
    """Best-effort source IP for ``request``."""
    if request.client is None:
        return ""
    return request.client.host


def _resolve_active_user_id(
    session: Session,
    *,
    cookie_value: str | None,
    ua: str = "",
    accept_language: str = "",
) -> str | None:
    """Validate the session cookie and return the user id.

    Returns ``None`` if the cookie is absent, invalid, or expired —
    the accept handler treats that as "no active session" and
    renders the ``needs_sign_in`` hint on the existing-user branch.

    ``ua`` / ``accept_language`` are forwarded to
    :func:`app.auth.session.validate` so the §15 fingerprint gate
    fires; defaulting to ``""`` keeps older call sites and tests
    that don't have a :class:`Request` handy working (the gate
    self-skips when the caller supplies neither header, matching the
    pre-hardening rollout-safety shape).
    """
    if not cookie_value:
        return None
    try:
        return auth_session.validate(
            session,
            cookie_value=cookie_value,
            ua=ua,
            accept_language=accept_language,
        )
    except (auth_session.SessionInvalid, auth_session.SessionExpired):
        return None


def build_invite_router(
    *,
    throttle: Throttle,
    settings: Settings | None = None,
) -> APIRouter:
    """Return a fresh :class:`APIRouter` wired to ``throttle``.

    Mounted by the v1 app factory at ``/api/v1/invite``. Tests
    instantiate it directly with a per-case throttle for isolation.
    """
    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``auth`` + ``invite`` stay for fine-grained client filtering.
    router = APIRouter(
        prefix="/invite",
        tags=["identity", "auth", "invite"],
    )
    cfg = settings if settings is not None else get_settings()

    @router.post(
        "/accept",
        response_model=AcceptResponse,
        operation_id="auth.invite.accept",
        summary="Redeem the invite magic link",
    )
    def post_accept(
        body: AcceptRequest,
        request: Request,
        session: _Db,
        crewday_session: Annotated[
            str | None, Cookie(alias=auth_session.SESSION_COOKIE_NAME)
        ] = None,
    ) -> AcceptResponse:
        """First leg of accept; branches on new-user vs existing-user."""
        active_user_id = _resolve_active_user_id(
            session,
            cookie_value=crewday_session,
            ua=request.headers.get("user-agent", ""),
            accept_language=request.headers.get("accept-language", ""),
        )
        try:
            outcome = membership.consume_invite_token(
                session,
                token=body.token,
                ip=_client_ip(request),
                throttle=throttle,
                settings=cfg,
                active_user_id=active_user_id,
            )
        except membership.PasskeySessionRequired as exc:
            # Spec §03: the existing-user branch needs an active
            # passkey session before the acceptance card renders.
            # We raise 401 so the SPA redirects to /login; once the
            # user signs in, the SPA calls ``/invite/{id}/confirm``
            # directly — the magic-link token is already spent at
            # this point (the ``magic_link`` service burnt the
            # nonce on its consume step), so a second ``/accept``
            # with the same token would 409.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "passkey_session_required"},
            ) from exc
        except _TokenDomainError as exc:
            raise _http_for_token(exc) from exc
        except _InviteDomainError as exc:
            raise _http_for_invite(exc) from exc

        if isinstance(outcome, membership.NewUserAcceptance):
            return AcceptResponse(
                kind="new_user",
                invite_id=outcome.session.invite_id,
                user_id=outcome.session.user_id,
                email_lower=outcome.session.email_lower,
                display_name=outcome.session.display_name,
            )
        # ExistingUserAcceptance
        return AcceptResponse(
            kind="existing_user",
            invite_id=outcome.card.invite_id,
            workspace_id=outcome.card.workspace_id,
            workspace_slug=outcome.card.workspace_slug,
            workspace_name=outcome.card.workspace_name,
            grants=outcome.card.grants,
            permission_group_memberships=outcome.card.group_memberships,
        )

    @router.post(
        "/{invite_id}/confirm",
        response_model=ConfirmResponse,
        operation_id="auth.invite.confirm",
        summary="Confirm pending invite acceptance (existing-user branch)",
    )
    def post_confirm(
        invite_id: str,
        request: Request,
        session: _Db,
        crewday_session: Annotated[
            str | None, Cookie(alias=auth_session.SESSION_COOKIE_NAME)
        ] = None,
    ) -> ConfirmResponse:
        """Activate the pending invite for an existing user."""
        active_user_id = _resolve_active_user_id(
            session,
            cookie_value=crewday_session,
            ua=request.headers.get("user-agent", ""),
            accept_language=request.headers.get("accept-language", ""),
        )
        if active_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "passkey_session_required"},
            )
        # Build a ctx for the audit row — the acting user is the
        # invitee who just signed in. The workspace id is pinned by
        # the invite row; we fill ``workspace_slug`` after the
        # membership service returns it.
        from app.adapters.db.identity.models import Invite
        from app.tenancy import tenant_agnostic
        from app.util.ulid import new_ulid

        with tenant_agnostic():
            invite_row = session.get(Invite, invite_id)
        if invite_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "invite_not_found"},
            )
        ctx = WorkspaceContext(
            workspace_id=invite_row.workspace_id,
            workspace_slug="",
            actor_id=active_user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id=new_ulid(),
        )
        try:
            workspace_id = membership.confirm_invite(session, ctx, invite_id=invite_id)
        except _InviteDomainError as exc:
            raise _http_for_invite(exc) from exc

        # Resolve slug for the redirect target.
        from sqlalchemy import select

        from app.adapters.db.workspace.models import Workspace

        with tenant_agnostic():
            ws = session.scalar(select(Workspace).where(Workspace.id == workspace_id))
        redirect = f"/w/{ws.slug}/today" if ws is not None else "/"
        return ConfirmResponse(workspace_id=workspace_id, redirect=redirect)

    @router.post(
        "/complete",
        response_model=ConfirmResponse,
        operation_id="auth.invite.complete",
        summary="Complete invite after new-user passkey ceremony",
    )
    def post_complete(
        body: CompleteRequest,
        session: _Db,
    ) -> ConfirmResponse:
        """Second leg for a brand-new invitee."""
        try:
            workspace_id = membership.complete_invite(
                session,
                invite_id=body.invite_id,
                settings=cfg,
            )
        except _InviteDomainError as exc:
            raise _http_for_invite(exc) from exc

        from sqlalchemy import select

        from app.adapters.db.workspace.models import Workspace
        from app.tenancy import tenant_agnostic

        with tenant_agnostic():
            ws = session.scalar(select(Workspace).where(Workspace.id == workspace_id))
        redirect = f"/w/{ws.slug}/today" if ws is not None else "/"
        return ConfirmResponse(workspace_id=workspace_id, redirect=redirect)

    return router
