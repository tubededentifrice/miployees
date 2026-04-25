"""Self-service email-change HTTP router (cd-601a).

Three bare-host routes wire the spec's §03 "Self-service email
change" flow:

* ``POST /api/v1/me/email/change_request`` — passkey session only.
  Mints a magic link to the new address, an informational notice
  to the old address, and persists a pending row.

* ``POST /api/v1/auth/email/verify`` — passkey session for the same
  user. Consumes the new-address magic link, swaps ``users.email``
  atomically, mints a 72-hour revert link to the old address.

* ``POST /api/v1/auth/email/revert`` — token only (no session). The
  72-hour revert link from the old-address notice; redemption
  restores ``users.email``.

**Auth posture (§03 "Self-service email change").** The
``change_request`` route accepts the session cookie only. A caller
that ships an ``Authorization: Bearer …`` header (PAT, delegated, or
agent token) is rejected with ``403 forbidden`` even if they also
ship a session cookie — the email field is self-service via the
session cookie only, not via any token surface. The verify route
shares the same posture (the swap is destructive enough that we
keep the seam tight).

**Error vocabulary (§12 "Errors").** Every failure path emits a
``{"error": "<symbol>"}`` body matching the spec's taxonomy. The
wide RFC 7807 envelope is the §12 "Errors" wrapper applied
downstream by :func:`app.api.errors.add_exception_handlers`.

Mounted by the v1 app factory alongside the magic-link / signup /
recovery routers; bare-host because email is the identity anchor
and the swap is workspace-agnostic.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change",
``docs/specs/12-rest-api.md`` §"Auth", and
``docs/specs/15-security-privacy.md`` §"Self-service lost-device &
email-change abuse mitigations".
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.session import make_uow
from app.adapters.mail.ports import Mailer
from app.api.deps import db_session
from app.auth import session as auth_session
from app.auth._throttle import Throttle
from app.auth.magic_link import (
    AlreadyConsumed,
    ConsumeLockout,
    InvalidToken,
    PendingDispatch,
    PurposeMismatch,
    RateLimited,
    TokenExpired,
)
from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME
from app.config import Settings, get_settings
from app.domain.identity.email_change import (
    EmailChangeOutcome,
    EmailInUse,
    EmailRevertOutcome,
    EmailVerifyOutcome,
    InvalidEmail,
    PendingNotFound,
    RecentReenrollment,
    SessionUserMismatch,
    request_change,
    revert_change,
    verify_change,
)
from app.tenancy import tenant_agnostic

__all__ = [
    "EmailChangeRequestBody",
    "EmailChangeRequestResponse",
    "EmailRevertBody",
    "EmailRevertResponse",
    "EmailVerifyBody",
    "EmailVerifyResponse",
    "build_email_change_router",
]


_log = logging.getLogger(__name__)

_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class EmailChangeRequestBody(BaseModel):
    """Request body for ``POST /api/v1/me/email/change_request``.

    ``new_email`` is plain ``str``; the domain layer canonicalises +
    validates. Pydantic's ``min_length`` / ``max_length`` mirror the
    320-char §02 ceiling — anything fancier (RFC 5321 / 5322) is the
    domain layer's job.
    """

    new_email: str = Field(..., min_length=3, max_length=320)


class EmailChangeRequestResponse(BaseModel):
    """Response body — opaque accept envelope.

    The pending row's id is surfaced so the SPA can render
    "Pending: jane.new@example.com — check your inbox" without a
    second round-trip; nothing in the body identifies the new
    address (the SPA already knows what the user typed).
    """

    status: str = Field(default="accepted")
    pending_id: str


class EmailVerifyBody(BaseModel):
    """Request body for ``POST /api/v1/auth/email/verify``."""

    token: str = Field(..., min_length=1, max_length=4096)


class EmailVerifyResponse(BaseModel):
    """Response body for ``POST /api/v1/auth/email/verify`` on success."""

    status: str = Field(default="verified")
    user_id: str
    pending_id: str


class EmailRevertBody(BaseModel):
    """Request body for ``POST /api/v1/auth/email/revert``."""

    token: str = Field(..., min_length=1, max_length=4096)


class EmailRevertResponse(BaseModel):
    """Response body for ``POST /api/v1/auth/email/revert`` on success."""

    status: str = Field(default="reverted")
    user_id: str
    pending_id: str


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


_TokenError = (
    RateLimited,
    ConsumeLockout,
    InvalidToken,
    PurposeMismatch,
    TokenExpired,
    AlreadyConsumed,
)


def _http_for_token(exc: Exception) -> HTTPException:
    """Map a magic-link domain error onto an :class:`HTTPException`.

    Mirrors :func:`app.api.v1.auth.magic._http_for` so the email-
    change routes share the same error vocabulary as the generic
    consume endpoint. The wrapper prefers `expired` for both
    payload-exp and persisted-TTL lapses, matching the spec.
    """
    if isinstance(exc, RateLimited):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "rate_limited"},
        )
    if isinstance(exc, ConsumeLockout):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "consume_locked_out"},
        )
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
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_token"},
    )


def _client_ip(request: Request) -> str:
    """Best-effort source IP for ``request``.

    Same heuristic as the magic-link router — the deployment
    middleware (cd-ika7) will handle ``X-Forwarded-For`` trust later;
    until then the socket peer IP is enough for localhost / tailscale0
    traffic.
    """
    if request.client is None:
        return ""
    return request.client.host


def _refuse_bearer_token(request: Request) -> None:
    """Raise 403 if the caller carried an ``Authorization: Bearer …`` header.

    Spec §03 "Self-service email change" / "Request": "from a
    passkey session only (no PAT, no delegated token)". We refuse
    at the auth dep edge so a hostile token cannot probe the email
    field even if the caller also presented a valid session cookie
    — the spec is explicit that this surface is session-only.

    The check is a flat header sniff, not a full
    :func:`app.tenancy.middleware.resolve_actor` resolution, because
    we want the answer before the body is parsed and we don't need
    to know which token kind it is — any ``Bearer`` value is rejected.
    """
    auth_header = request.headers.get("Authorization")
    if auth_header is not None and auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "forbidden"},
        )


def _resolve_session_user(
    session: Session,
    *,
    request: Request,
    cookie_primary: str | None,
    cookie_dev: str | None,
) -> str:
    """Return the session user's id or raise HTTP 401 / 403.

    Forwards UA + Accept-Language so the §15 fingerprint gate fires.
    Bearer-header callers were already refused upstream by
    :func:`_refuse_bearer_token` — by the time we reach here, the
    caller is a session-cookie holder or anonymous.
    """
    cookie_value = cookie_primary or cookie_dev
    if not cookie_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_required"},
        )
    ua = request.headers.get("user-agent", "")
    accept_language = request.headers.get("accept-language", "")
    try:
        return auth_session.validate(
            session,
            cookie_value=cookie_value,
            ua=ua,
            accept_language=accept_language,
        )
    except (auth_session.SessionInvalid, auth_session.SessionExpired) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_invalid"},
        ) from exc


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_email_change_router(
    *,
    mailer: Mailer,
    throttle: Throttle,
    base_url: str | None = None,
    settings: Settings | None = None,
) -> APIRouter:
    """Return a fresh :class:`APIRouter` for the email-change surface.

    Factoring the wiring through a builder lets tests inject their
    own mailer + throttle without relying on a module-level
    singleton. The v1 app factory mounts this once at startup.
    ``base_url`` defaults to :attr:`Settings.public_url`; an unset
    value raises :class:`RuntimeError` on first request, matching
    the magic-link router's posture.
    """
    cfg = settings if settings is not None else get_settings()
    resolved_base_url = base_url if base_url is not None else cfg.public_url

    # The router carries no shared prefix because the three routes
    # live on two different prefixes (``/me`` and ``/auth``) per
    # §12. We mount them individually below.
    router = APIRouter(tags=["identity", "auth"])

    # -----------------------------------------------------------------
    # POST /me/email/change_request
    # -----------------------------------------------------------------

    @router.post(
        "/me/email/change_request",
        response_model=EmailChangeRequestResponse,
        operation_id="auth.me.email.change_request",
        summary="Request to change the caller's email (passkey session only)",
        openapi_extra={
            "x-cli": {
                "group": "auth-email",
                "verb": "change-request",
                "summary": "Request changing your account email",
                "mutates": True,
            },
        },
    )
    def post_change_request(
        body: EmailChangeRequestBody,
        request: Request,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> EmailChangeRequestResponse:
        """Mint the new-address magic link + send the old-address notice.

        **Outbox ordering (cd-9slq).** Owns its own
        :class:`UnitOfWork` instead of going through ``db_session``
        so both SMTP sends (the new-address magic link + the
        old-address notice) fire only after the
        ``email_change_pending`` row + magic-link nonce + audit
        rows commit. A commit failure short-circuits
        ``dispatch.deliver()`` so no working email-change token
        reaches the new mailbox without a matching pending row.
        """
        if resolved_base_url is None:
            raise RuntimeError(
                "base_url / settings.public_url is not set; "
                "cannot build email-change URLs"
            )
        _refuse_bearer_token(request)

        dispatch = PendingDispatch()
        outcome: EmailChangeOutcome | None = None
        try:
            with make_uow() as session:
                assert isinstance(session, Session)
                user_id = _resolve_session_user(
                    session,
                    request=request,
                    cookie_primary=session_cookie_primary,
                    cookie_dev=session_cookie_dev,
                )

                with tenant_agnostic():
                    user = session.get(User, user_id)
                if user is None:
                    # Session row points at a hard-deleted user — treat
                    # as unauth, mirroring :mod:`app.api.v1.auth.me`.
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail={"error": "session_invalid"},
                    )

                outcome = request_change(
                    session,
                    user=user,
                    new_email=body.new_email,
                    ip=_client_ip(request),
                    mailer=mailer,
                    base_url=resolved_base_url,
                    throttle=throttle,
                    settings=cfg,
                    dispatch=dispatch,
                )
        except InvalidEmail as exc:
            # 422 — Pydantic / Starlette rename ``UNPROCESSABLE_ENTITY``
            # to ``UNPROCESSABLE_CONTENT`` mid-rollout; we use the
            # numeric literal so neither alias goes stale.
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_email"},
            ) from exc
        except EmailInUse as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "email_in_use"},
            ) from exc
        except RecentReenrollment as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "recent_reenrollment"},
            ) from exc
        except _TokenError as exc:
            # Magic-link rate-limit / lockout — propagated from
            # :func:`request_link`. Map through the same vocabulary
            # the magic-link router uses.
            raise _http_for_token(exc) from exc
        # ``with`` exited cleanly → UoW committed → pending row +
        # nonce + audit are durable on disk. Fire the queued sends
        # (cd-9slq).
        dispatch.deliver()
        assert outcome is not None
        return EmailChangeRequestResponse(pending_id=outcome.pending_id)

    # -----------------------------------------------------------------
    # POST /auth/email/verify
    # -----------------------------------------------------------------

    @router.post(
        "/auth/email/verify",
        response_model=EmailVerifyResponse,
        operation_id="auth.email.verify",
        summary="Consume the new-address magic link and swap users.email",
        openapi_extra={
            "x-cli": {
                "group": "auth-email",
                "verb": "verify",
                "summary": "Confirm a pending email change",
                "mutates": True,
            },
        },
    )
    def post_verify(
        body: EmailVerifyBody,
        request: Request,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> EmailVerifyResponse:
        """Verify the magic link, swap email atomically, mail the revert link.

        **Outbox ordering (cd-9slq).** Owns its own
        :class:`UnitOfWork` instead of going through ``db_session``
        so the post-swap confirmation + revert-link sends fire only
        after the email swap + revert nonce + audit rows commit.
        A commit failure short-circuits ``dispatch.deliver()`` so
        no working revert token reaches the old mailbox without a
        matching revert nonce on disk.
        """
        if resolved_base_url is None:
            raise RuntimeError(
                "base_url / settings.public_url is not set; "
                "cannot build email-change URLs"
            )
        _refuse_bearer_token(request)

        dispatch = PendingDispatch()
        outcome: EmailVerifyOutcome | None = None
        try:
            with make_uow() as session:
                assert isinstance(session, Session)
                user_id = _resolve_session_user(
                    session,
                    request=request,
                    cookie_primary=session_cookie_primary,
                    cookie_dev=session_cookie_dev,
                )

                outcome = verify_change(
                    session,
                    token=body.token,
                    session_user_id=user_id,
                    ip=_client_ip(request),
                    mailer=mailer,
                    base_url=resolved_base_url,
                    throttle=throttle,
                    settings=cfg,
                    dispatch=dispatch,
                )
        except SessionUserMismatch as exc:
            # The session user is not the user the token was issued
            # for. Spec §03 step 2: "Requires an active passkey
            # session for the same ``user_id``". 403 keeps the
            # symbol distinct from the 401 (no session at all).
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": "session_user_mismatch"},
            ) from exc
        except EmailInUse as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "email_in_use"},
            ) from exc
        except PendingNotFound as exc:
            # The nonce was consumed but no live pending row exists —
            # collapse to ``410 expired`` so the caller cannot tell
            # "your token was tampered with" from "your row was
            # swept" apart.
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"error": "expired"},
            ) from exc
        except _TokenError as exc:
            raise _http_for_token(exc) from exc

        # ``with`` exited cleanly → UoW committed → swap +
        # revert nonce + audit are durable. Fire the queued sends
        # (confirmation to new + revert link to old) post-commit.
        dispatch.deliver()
        assert outcome is not None
        return EmailVerifyResponse(
            user_id=outcome.user_id,
            pending_id=outcome.pending_id,
        )

    # -----------------------------------------------------------------
    # POST /auth/email/revert
    # -----------------------------------------------------------------

    @router.post(
        "/auth/email/revert",
        response_model=EmailRevertResponse,
        operation_id="auth.email.revert",
        summary="Consume the revert link and restore the previous email",
        openapi_extra={
            "x-cli": {
                "group": "auth-email",
                "verb": "revert",
                "summary": "Revert a recent email change",
                "mutates": True,
            },
        },
    )
    def post_revert(
        body: EmailRevertBody,
        request: Request,
        session: _Db,
    ) -> EmailRevertResponse:
        """Verify the revert magic link and restore ``users.email``.

        No session is required — the spec pins this as a non-auth
        primitive consumed against the **old** address by virtue of
        the token's mailbox-controlled delivery. Bearer tokens are
        also refused: the spec treats this as session-or-token-free
        ("the revert link is the only flow that consumes a magic
        link against the old address after the swap"), so a token
        on the request is at best a bug.
        """
        _refuse_bearer_token(request)
        try:
            outcome: EmailRevertOutcome = revert_change(
                session,
                token=body.token,
                ip=_client_ip(request),
                throttle=throttle,
                settings=cfg,
            )
        except PendingNotFound as exc:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={"error": "expired"},
            ) from exc
        except _TokenError as exc:
            raise _http_for_token(exc) from exc

        return EmailRevertResponse(
            user_id=outcome.user_id,
            pending_id=outcome.pending_id,
        )

    return router
