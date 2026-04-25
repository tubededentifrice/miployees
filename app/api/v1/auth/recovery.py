"""Self-service lost-device recovery HTTP router (``/recover/passkey/*``).

Bare-host routes, tenant-agnostic. The surface complements the
signup router: three endpoints walk the user from "lost every
device" to "new passkey in hand", spending at most 15 minutes in
the recovery window (spec §03 "Self-service lost-device recovery").

Routes (all ``/api/v1/recover/*`` — the v1 app factory mounts the
router at ``/api/v1``; the router itself carries the ``/recover``
prefix):

* ``POST /recover/passkey/request`` ``{email}`` — 202 on success
  (both on hit and miss — enumeration guard), 429 on rate-limit
  trip.
* ``GET /recover/passkey/verify?token=…`` — consume the magic link;
  return the ``recovery_session_id`` the SPA threads into the
  passkey-finish call. 400 / 410 / 429 on token errors.
* ``POST /recover/passkey/finish`` — register the new passkey,
  revoke old credentials + sessions, complete the recovery.

Error mapping mirrors signup / magic-link: the spec's vocabulary
lives here so RFC 7807 migration is a single-diff swap.

**Rejected-audit trail.** ``POST /recover/passkey/request`` landing
on a rate-limit writes ``audit.recovery.rejected`` on a fresh UoW
(independent of whatever rollback happens on the primary UoW) so
rate-limited requests leave a forensic trail even when no nonce
row ever lands. Mirrors the signup router's abuse-refusal
pattern.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service lost-device
recovery", §"Recovery paths" and ``docs/specs/15-security-privacy.md``
§"Self-service lost-device & email-change abuse mitigations".
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import canonicalise_email
from app.adapters.db.session import make_uow
from app.adapters.mail.ports import Mailer
from app.api.deps import db_session
from app.audit import write_audit
from app.auth import passkey, recovery
from app.auth._hashing import hash_with_pepper
from app.auth._throttle import RecoveryRateLimited, Throttle
from app.auth.keys import derive_subkey
from app.auth.magic_link import (
    AlreadyConsumed,
    ConsumeLockout,
    InvalidToken,
    PendingDispatch,
    PurposeMismatch,
    RateLimited,
    TokenExpired,
    _agnostic_audit_ctx,
)
from app.config import Settings, get_settings
from app.util.clock import SystemClock

__all__ = [
    "RecoveryFinishBody",
    "RecoveryFinishResponse",
    "RecoveryPasskeyStartBody",
    "RecoveryPasskeyStartResponse",
    "RecoveryRequestBody",
    "RecoveryRequestResponse",
    "RecoveryVerifyResponse",
    "build_recovery_router",
]


_Db = Annotated[Session, Depends(db_session)]

_log = logging.getLogger(__name__)


# HKDF purpose for the abuse-audit pepper. Mirrors :mod:`app.auth.signup`
# + :mod:`app.auth.recovery` so the refusal audit hashes the same email
# with the same subkey as the domain service — abuse correlation joins
# cleanly across the two trails.
_ABUSE_HKDF_PURPOSE = "magic-link"


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class RecoveryRequestBody(BaseModel):
    """Request body for ``POST /recover/passkey/request``.

    ``email`` is typed as plain ``str`` — the domain layer canonicalises
    before use, and the recovery service deliberately avoids pulling in
    :class:`pydantic.EmailStr` for the same reason the magic-link router
    doesn't (see :mod:`app.api.v1.auth.magic`).
    """

    email: str = Field(..., min_length=3, max_length=320)


class RecoveryRequestResponse(BaseModel):
    """202 body — status-only, per the enumeration guard.

    The caller cannot distinguish hit from miss on response shape,
    status, or body; only the audit log carries that discriminator.
    """

    status: str = "accepted"


class RecoveryVerifyResponse(BaseModel):
    """Response body carrying the recovery session handle."""

    recovery_session_id: str


class RecoveryPasskeyStartBody(BaseModel):
    """Request body for ``POST /recover/passkey/start``.

    The ``recovery_session_id`` pins the ceremony to the verified
    recovery session so only its owner can request a challenge
    against it.
    """

    recovery_session_id: str


class RecoveryPasskeyStartResponse(BaseModel):
    """Parsed ``PublicKeyCredentialCreationOptions`` + challenge handle."""

    challenge_id: str
    options: dict[str, Any]


class RecoveryFinishBody(BaseModel):
    """Request body for ``POST /recover/passkey/finish``."""

    recovery_session_id: str
    challenge_id: str
    credential: dict[str, Any]


class RecoveryFinishResponse(BaseModel):
    """Response body for ``POST /recover/passkey/finish``.

    The SPA shows a "we revoked N passkeys + signed out M sessions"
    confirmation so the user sees the destructive blast radius of
    the action they just took.
    """

    user_id: str
    credential_id: str
    revoked_credential_count: int
    revoked_session_count: int


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


_VerifyDomainError = (
    RecoveryRateLimited,
    RateLimited,
    ConsumeLockout,
    InvalidToken,
    PurposeMismatch,
    TokenExpired,
    AlreadyConsumed,
    recovery.RecoverySessionNotFound,
    recovery.RecoverySessionExpired,
)


_FinishDomainError = (
    recovery.RecoverySessionNotFound,
    recovery.RecoverySessionExpired,
    passkey.ChallengeNotFound,
    passkey.ChallengeAlreadyConsumed,
    passkey.ChallengeExpired,
    passkey.ChallengeSubjectMismatch,
    passkey.InvalidRegistration,
    passkey.TooManyPasskeys,
)


def _http_for_verify(exc: Exception) -> HTTPException:
    """Map a verify-path domain error to an :class:`HTTPException`."""
    if isinstance(exc, RecoveryRateLimited):
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limited",
                "retry_after_seconds": exc.retry_after_seconds,
            },
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
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
    if isinstance(
        exc,
        recovery.RecoverySessionNotFound | recovery.RecoverySessionExpired,
    ):
        # Collapse the two into one 404 for privacy; the spec §15
        # discusses not leaking whether a recovery session ever
        # existed vs expired.
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "recovery_session_not_found"},
        )
    # InvalidToken — fallback for the verify family.
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_token"},
    )


def _http_for_finish(exc: Exception) -> HTTPException:
    """Map a finish-path domain error to an :class:`HTTPException`."""
    if isinstance(
        exc,
        recovery.RecoverySessionNotFound | recovery.RecoverySessionExpired,
    ):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "recovery_session_not_found"},
        )
    if isinstance(exc, passkey.ChallengeNotFound | passkey.ChallengeAlreadyConsumed):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "challenge_consumed_or_unknown"},
        )
    if isinstance(exc, passkey.ChallengeExpired):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "challenge_expired"},
        )
    if isinstance(exc, passkey.InvalidRegistration | passkey.ChallengeSubjectMismatch):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_registration"},
        )
    # TooManyPasskeys — impossible after a full revoke, but map it
    # for completeness so a future change that tweaks the revoke
    # step still produces a typed 422 rather than a 500.
    return HTTPException(
        status_code=422,
        detail={"error": "too_many_passkeys"},
    )


# ---------------------------------------------------------------------------
# Refusal audit on rate-limit trip
# ---------------------------------------------------------------------------


def _audit_recovery_refusal(
    *,
    email_hash: str,
    ip_hash: str,
    scope: str,
    retry_after_seconds: int,
) -> None:
    """Write one ``audit.recovery.rejected`` row on a fresh UoW.

    The request-path rate-limit raises before the primary UoW ever
    writes a row, so we open a fresh :func:`make_uow` so the
    refusal trail lands regardless of what rollback (if any) happens
    on the primary path. Mirrors the signup router's
    :func:`_audit_signup_refusal` + the magic-link router's
    rejected-audit seam.

    Failures of the audit UoW are logged and swallowed: this helper
    runs inside an ``except RecoveryRateLimited`` clause about to
    re-raise the mapped :class:`HTTPException`, and a raised audit
    failure here would shadow the 429 the client expects with a
    500. The catch is deliberately broad (``Exception``) so any
    transient DB / config hiccup still logs-and-drops.
    :class:`BaseException` propagates so operator aborts aren't
    swallowed.
    """
    diff = {
        "email_hash": email_hash,
        "ip_hash": ip_hash,
        "reason": f"rate_limited:{scope}",
        "retry_after_seconds": str(retry_after_seconds),
    }
    try:
        with make_uow() as uow_session:
            assert isinstance(uow_session, Session)
            write_audit(
                uow_session,
                _agnostic_audit_ctx(),
                entity_kind="user",
                entity_id="00000000000000000000000000",
                action="recovery.rejected",
                diff=diff,
            )
    except Exception:
        _log.exception("recovery refusal audit write failed on fresh UoW")


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Best-effort source IP for ``request`` — mirrors the magic router."""
    if request.client is None:
        return ""
    return request.client.host


def build_recovery_router(
    *,
    mailer: Mailer,
    throttle: Throttle,
    base_url: str | None = None,
    settings: Settings | None = None,
) -> APIRouter:
    """Return a fresh :class:`APIRouter` wired to ``mailer`` + ``throttle``.

    Mounted by the v1 app factory alongside the signup + magic routers.
    Tests instantiate it directly with a recording mailer + per-case
    throttle for isolation.
    """
    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``auth`` + ``recovery`` stay for fine-grained client filtering.
    router = APIRouter(
        prefix="/recover",
        tags=["identity", "auth", "recovery"],
    )
    cfg = settings if settings is not None else get_settings()
    resolved_base_url = base_url if base_url is not None else cfg.public_url

    @router.post(
        "/passkey/request",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RecoveryRequestResponse,
        summary="Request a recovery magic link (202 on accept; 429 on rate limit)",
    )
    def post_request(
        body: RecoveryRequestBody,
        request: Request,
    ) -> RecoveryRequestResponse:
        """Kick off a recovery request.

        Returns ``202`` on success for both hit and miss branches
        (enumeration guard). Returns ``429 rate_limited`` with a
        ``Retry-After`` header when the per-IP / per-email / global
        cap trips; the audit trail distinguishes the two branches
        via ``audit.recovery.requested`` (``hit=True/False``).

        **Outbox ordering (cd-9slq).** Owns its own
        :class:`UnitOfWork` instead of going through ``db_session``
        so the SMTP send fires only after the recovery audit + nonce
        rows are durable. A commit failure short-circuits the
        :meth:`PendingDispatch.deliver` call below so no recovery
        magic-link or no-account notice leaves the host with
        rolled-back state.
        """
        if resolved_base_url is None:
            raise RuntimeError(
                "base_url / settings.public_url is not set; cannot build recovery URLs"
            )
        ip = _client_ip(request)
        dispatch: PendingDispatch | None = None
        try:
            with make_uow() as uow_session:
                assert isinstance(uow_session, Session)
                dispatch = recovery.request_recovery(
                    uow_session,
                    email=body.email,
                    ip=ip,
                    mailer=mailer,
                    base_url=resolved_base_url,
                    throttle=throttle,
                    settings=cfg,
                )
        except RecoveryRateLimited as exc:
            # Derive hashes here so the refusal audit carries the same
            # forensic fields as a successful-request audit row —
            # operators can correlate without parsing exception
            # messages.
            pepper = derive_subkey(cfg.root_key, purpose=_ABUSE_HKDF_PURPOSE)
            email_hash = hash_with_pepper(canonicalise_email(body.email), pepper)
            ip_hash = hash_with_pepper(ip, pepper)
            _log.info(
                "recovery abuse refusal",
                extra={
                    "action": "audit.recovery.rate_limited",
                    "scope": exc.scope,
                    "ip_hash": ip_hash,
                    "email_hash": email_hash,
                },
            )
            _audit_recovery_refusal(
                email_hash=email_hash,
                ip_hash=ip_hash,
                scope=exc.scope,
                retry_after_seconds=exc.retry_after_seconds,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limited",
                    "retry_after_seconds": exc.retry_after_seconds,
                },
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except RateLimited as exc:
            # Magic-link's own throttle trip; no dedicated refusal audit
            # here because the magic-link request_link path already
            # carries its own shape (the rate-limit fires after the
            # throttle check there; the recover-start limit has
            # already passed or we wouldn't reach this call).
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": "rate_limited"},
            ) from exc
        # ``with`` exited cleanly → UoW committed → recovery audit +
        # magic-link nonce are durable on disk. Only now do we fire
        # the SMTP sends queued on the dispatch (cd-9slq).
        if dispatch is not None:
            dispatch.deliver()
        return RecoveryRequestResponse()

    @router.get(
        "/passkey/verify",
        response_model=RecoveryVerifyResponse,
        summary="Consume the recovery magic link; return the recovery session",
    )
    def get_verify(
        request: Request,
        session: _Db,
        token: str,
    ) -> RecoveryVerifyResponse:
        """Verify the recovery token; mint a transient recovery session.

        The session id returned here is accepted ONLY by the
        :func:`post_finish` endpoint below. It is not a web session
        and cannot be used to authenticate any other API route —
        the recovery store is a dedicated in-memory dict, distinct
        from :class:`app.adapters.db.identity.models.Session`.

        ``GET`` rather than ``POST`` because the SPA lands on this
        route by following the magic-link URL — the first request
        is a hard navigation, not a form submission. The token is
        consumed single-use by the magic-link service inside the
        service call, so the semantic doesn't conflict with HTTP
        GET's nominal idempotency (the second call 409s, matching
        the spec's single-use contract).
        """
        ip = _client_ip(request)
        try:
            ssn = recovery.verify_recovery(
                session,
                token=token,
                ip=ip,
                throttle=throttle,
                settings=cfg,
            )
        except _VerifyDomainError as exc:
            raise _http_for_verify(exc) from exc
        return RecoveryVerifyResponse(recovery_session_id=ssn.recovery_session_id)

    @router.post(
        "/passkey/start",
        response_model=RecoveryPasskeyStartResponse,
        summary="Mint the WebAuthn challenge for the recovery ceremony",
    )
    def post_passkey_start(
        body: RecoveryPasskeyStartBody,
        session: _Db,
    ) -> RecoveryPasskeyStartResponse:
        """Hand the SPA the ``PublicKeyCredentialCreationOptions`` for
        :func:`navigator.credentials.create`.

        Requires a live recovery session — an expired or unknown
        id 404s with the same symbol the finish path uses so the
        two routes share a vocabulary. The challenge is minted via
        :func:`passkey.register_start_recovery`, which skips the
        per-user passkey cap (the prior credentials are about to
        be revoked by :func:`post_passkey_finish`).
        """
        try:
            row = recovery._load_recovery_session(
                body.recovery_session_id,
                now=SystemClock().now(),
            )
        except (
            recovery.RecoverySessionNotFound,
            recovery.RecoverySessionExpired,
        ) as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "recovery_session_not_found"},
            ) from exc

        try:
            opts = passkey.register_start_recovery(
                session,
                user_id=row.user_id,
            )
        except passkey.TooManyPasskeys as exc:
            # Unreachable in practice (register_start_recovery skips the
            # cap), but kept for symmetry with the signup router's
            # mapping table.
            raise HTTPException(
                status_code=422,
                detail={"error": "too_many_passkeys"},
            ) from exc
        return RecoveryPasskeyStartResponse(
            challenge_id=opts.challenge_id,
            options=opts.options,
        )

    @router.post(
        "/passkey/finish",
        response_model=RecoveryFinishResponse,
        summary="Complete recovery — register the new passkey, revoke the old",
    )
    def post_finish(
        body: RecoveryFinishBody,
        request: Request,
        session: _Db,
    ) -> RecoveryFinishResponse:
        """Register the new passkey + revoke all prior credentials + sessions.

        One transaction: either every write lands or none do. A
        partial outcome (old credentials revoked but new passkey
        never registered, say) would leave the user permanently
        locked out — the caller's UoW rolls back on any exception
        inside :func:`recovery.complete_recovery`.
        """
        try:
            result = recovery.complete_recovery(
                session,
                recovery_session_id=body.recovery_session_id,
                challenge_id=body.challenge_id,
                credential=body.credential,
                ip=_client_ip(request),
                settings=cfg,
            )
        except _FinishDomainError as exc:
            raise _http_for_finish(exc) from exc
        return RecoveryFinishResponse(
            user_id=result.user_id,
            credential_id=result.new_credential_id,
            revoked_credential_count=result.revoked_credential_count,
            revoked_session_count=result.revoked_session_count,
        )

    return router
