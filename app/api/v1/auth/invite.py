"""Invite-accept HTTP router (bare host).

Bare-host routes, tenant-agnostic at entry — the invite token carries
the target workspace via its ``subject_id``. Two router factories live
here side-by-side:

* :func:`build_invites_router` (plural ``/invites/``, **the spec**) —
  spec §12 §"Auth" pins ``GET /api/v1/invites/{token}`` for
  pre-accept introspection plus ``POST /api/v1/invites/{token}/accept``
  with the token in the URL. Every new SPA flow targets this shape.
* :func:`build_invite_router` (singular ``/invite/``, **legacy**) —
  the original Phase-0 SPA shape: ``POST /invite/accept`` with the
  token in the body, plus ``/invite/{invite_id}/confirm`` and
  ``/invite/complete``. Kept alive verbatim during the cutover (see
  cd-z6vm) so the in-flight SPA build does not break; **deprecated**
  for new callers and slated for removal once the SPA cuts over.

The router branches on invite shape:

* ``POST /invites/{token}/accept`` (or legacy ``POST /invite/accept``
  ``{token}``) — redeems the magic link and returns either a
  ``NewUserAcceptance`` (brand-new invitee — the SPA forwards to the
  passkey ceremony, which on finish calls
  :func:`app.domain.identity.membership.complete_invite` via
  :mod:`app.api.v1.auth.passkey`) or an ``ExistingUserAcceptance``
  (known user with an active session — the SPA renders the
  acceptance card).
* ``GET /invites/{token}`` — read-only introspect; returns the same
  preview the SPA needs to render an Accept card before the user
  clicks Accept (inviter, workspace, grants, expiry, ``kind``).
  Does NOT burn the underlying magic-link nonce.
* ``POST /invite/{invite_id}/confirm`` — existing-user second leg
  (legacy router): confirms the pending invite activation.
* ``POST /invite/complete`` — new-user second leg (legacy router,
  post passkey-finish hook). Today
  :func:`app.auth.passkey.register_finish_signup` does not carry
  invite awareness; the SPA posts here with the ``invite_id`` it
  kept from the ``/invite/accept`` response once the passkey
  ceremony completes. A later consolidation (cd-kd26) folds this
  into the passkey-finish callback.

Existence-leak guard: on the plural surface, every token-validity
error (invalid signature, expired, already consumed) collapses onto
``404 invite_not_found`` so an attacker cannot use the introspect
endpoint as a token-validity oracle. The legacy singular surface
preserves its richer error vocabulary for back-compat.

See ``docs/specs/03-auth-and-tokens.md`` §"Additional users" and
``docs/specs/12-rest-api.md`` §"Auth".
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Final

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
    "InviteIntrospectionResponse",
    "build_invite_router",
    "build_invites_router",
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


class InviteIntrospectionResponse(BaseModel):
    """Response body for ``GET /api/v1/invites/{token}``.

    Read-only preview of an invite — the SPA renders this on the
    AcceptInvitePage before the user clicks Accept so they see what
    they are joining and who invited them. Mirrors
    :class:`~app.domain.identity.membership.InviteIntrospection`;
    the boundary type stays here so the OpenAPI schema is owned by
    the router.

    The ``kind`` field branches on passkey-presence the same way
    :class:`AcceptResponse` does — ``"new_user"`` for an invitee
    with no passkey on file (the subsequent accept will return a
    :class:`~app.domain.identity.membership.NewUserAcceptance`),
    ``"existing_user"`` otherwise.
    """

    kind: str = Field(
        ...,
        description=(
            "'new_user' → the subsequent accept will start a "
            "passkey ceremony; 'existing_user' → it will render "
            "the acceptance card."
        ),
    )
    invite_id: str
    workspace_id: str
    workspace_slug: str
    workspace_name: str
    inviter_display_name: str
    email_lower: str
    expires_at: datetime
    grants: list[dict[str, Any]]
    permission_group_memberships: list[dict[str, Any]]


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


# ---------------------------------------------------------------------------
# Plural router (spec-aligned, path-carried token)
# ---------------------------------------------------------------------------


# Existence-leak guard: every token-validity error on the plural surface
# collapses onto 404 ``invite_not_found``. Distinct from the singular
# ``_http_for_token`` (which exposes 400 invalid_token / 410 expired /
# 409 already_consumed for the existing SPA back-compat) — see module
# docstring.
_INVITES_NOT_FOUND_DETAIL: Final[dict[str, str]] = {"error": "invite_not_found"}


def _http_for_invites_token(exc: Exception) -> HTTPException:
    """Map magic-link errors onto 404 ``invite_not_found`` for the plural surface.

    On the plural ``/invites/{token}`` surface every token-validity
    error (invalid signature, expired, already consumed, purpose
    mismatch) is collapsed onto a single 404 so the introspect
    endpoint cannot be used as a token-validity oracle.

    :class:`~app.auth.magic_link.ConsumeLockout` and
    :class:`~app.auth.magic_link.RateLimited` keep their 429 mapping
    — those signal abuse-mitigation activity, not invite existence,
    and the SPA needs to render a "try later" hint.
    """
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
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=dict(_INVITES_NOT_FOUND_DETAIL),
    )


def _http_for_invites_invite(exc: Exception) -> HTTPException:
    """Map :mod:`membership` errors onto 404 for the plural surface.

    Same existence-leak guard as :func:`_http_for_invites_token`:
    every invite-row branch (not found, expired, already accepted,
    state invalid) flattens to 404 so the introspect endpoint does
    not leak whether the row exists.

    :class:`~app.domain.identity.membership.PasskeySessionRequired`
    is **not** raised by introspect (read-only). On the accept leg we
    keep the 401 mapping so the SPA's existing 401-redirect behaviour
    works against the plural endpoint too.
    """
    if isinstance(exc, membership.PasskeySessionRequired):
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "passkey_session_required"},
        )
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=dict(_INVITES_NOT_FOUND_DETAIL),
    )


def build_invites_router(
    *,
    throttle: Throttle,
    settings: Settings | None = None,
) -> APIRouter:
    """Return a fresh :class:`APIRouter` for the spec-aligned plural shape.

    Mounts ``GET /invites/{token}`` (introspect) +
    ``POST /invites/{token}/accept`` (accept with token in path) at
    ``/api/v1`` so the final paths are
    ``/api/v1/invites/{token}`` and
    ``/api/v1/invites/{token}/accept`` (spec §12 §"Auth").

    Shares the throttle instance with :func:`build_invite_router` —
    the factory passes the same :class:`Throttle` to both so brute-
    force protection counts peeks + accepts in one bucket.
    """
    router = APIRouter(
        prefix="/invites",
        tags=["identity", "auth", "invite"],
    )
    cfg = settings if settings is not None else get_settings()

    @router.get(
        "/{token}",
        response_model=InviteIntrospectionResponse,
        operation_id="auth.invites.introspect",
        summary="Introspect an invite (read-only preview)",
    )
    def get_introspect(
        token: str,
        request: Request,
        session: _Db,
        crewday_session: Annotated[
            str | None, Cookie(alias=auth_session.SESSION_COOKIE_NAME)
        ] = None,
    ) -> InviteIntrospectionResponse:
        """Return the Accept-card preview without burning the nonce."""
        active_user_id = _resolve_active_user_id(
            session,
            cookie_value=crewday_session,
            ua=request.headers.get("user-agent", ""),
            accept_language=request.headers.get("accept-language", ""),
        )
        try:
            preview = membership.introspect_invite(
                session,
                token=token,
                ip=_client_ip(request),
                throttle=throttle,
                settings=cfg,
                active_user_id=active_user_id,
            )
        except _TokenDomainError as exc:
            raise _http_for_invites_token(exc) from exc
        except _InviteDomainError as exc:
            raise _http_for_invites_invite(exc) from exc

        return InviteIntrospectionResponse(
            kind=preview.kind,
            invite_id=preview.invite_id,
            workspace_id=preview.workspace_id,
            workspace_slug=preview.workspace_slug,
            workspace_name=preview.workspace_name,
            inviter_display_name=preview.inviter_display_name,
            email_lower=preview.email_lower,
            expires_at=preview.expires_at,
            grants=preview.grants,
            permission_group_memberships=preview.permission_group_memberships,
        )

    @router.post(
        "/{token}/accept",
        response_model=AcceptResponse,
        operation_id="auth.invites.accept",
        summary="Redeem the invite magic link (token in path)",
    )
    def post_accept(
        token: str,
        request: Request,
        session: _Db,
        crewday_session: Annotated[
            str | None, Cookie(alias=auth_session.SESSION_COOKIE_NAME)
        ] = None,
    ) -> AcceptResponse:
        """Same as legacy ``POST /invite/accept`` but with token in path."""
        active_user_id = _resolve_active_user_id(
            session,
            cookie_value=crewday_session,
            ua=request.headers.get("user-agent", ""),
            accept_language=request.headers.get("accept-language", ""),
        )
        try:
            outcome = membership.consume_invite_token(
                session,
                token=token,
                ip=_client_ip(request),
                throttle=throttle,
                settings=cfg,
                active_user_id=active_user_id,
            )
        except membership.PasskeySessionRequired as exc:
            # Same shape as the legacy router — see post_accept on
            # build_invite_router for the rationale.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "passkey_session_required"},
            ) from exc
        except _TokenDomainError as exc:
            raise _http_for_invites_token(exc) from exc
        except _InviteDomainError as exc:
            raise _http_for_invites_invite(exc) from exc

        if isinstance(outcome, membership.NewUserAcceptance):
            return AcceptResponse(
                kind="new_user",
                invite_id=outcome.session.invite_id,
                user_id=outcome.session.user_id,
                email_lower=outcome.session.email_lower,
                display_name=outcome.session.display_name,
            )
        return AcceptResponse(
            kind="existing_user",
            invite_id=outcome.card.invite_id,
            workspace_id=outcome.card.workspace_id,
            workspace_slug=outcome.card.workspace_slug,
            workspace_name=outcome.card.workspace_name,
            grants=outcome.card.grants,
            permission_group_memberships=outcome.card.group_memberships,
        )

    return router
