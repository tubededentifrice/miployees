"""Magic-link HTTP router (``/auth/magic/*``).

Two bare-host routes, tenant-agnostic:

* ``POST /auth/magic/request`` — 202 on enrolment (enumeration guard:
  status is identical whether or not the email exists); 429 on
  rate-limit trip.
* ``POST /auth/magic/consume`` — returns :class:`MagicLinkOutcome`
  fields on success; 400 / 409 / 410 / 429 on errors.

Mounted by the v1 app factory (cd-ika7) alongside the passkey login
router and the canonical signup flow at
:func:`app.api.v1.auth.signup.build_signup_router`. A fresh
:class:`Throttle` is built at module load; the throttle is
process-scoped (see :mod:`app.auth._throttle`) and shared across
every request this worker serves.

The router never talks to py_webauthn or sees plaintext tokens outside
the domain service — its job is to unpack the request body, call the
service, and map typed domain errors onto HTTP codes. The spec
(§03 "Magic link format") pins the error vocabulary the body returns.

**Rejected-audit trail.** A consume that raises a typed domain error
writes an ``audit.magic_link.rejected`` row inside a **fresh** UoW
(independent of the caller's rolled-back primary UoW) so pre-signup
abuse attempts leave a trail even when there is no nonce row to
correlate against. See :func:`app.auth.magic_link.write_rejected_audit`.

See ``docs/specs/03-auth-and-tokens.md`` §"Magic link format",
§"Recovery paths" and ``docs/specs/15-security-privacy.md``
§"Rate limiting and abuse controls".
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.adapters.db.session import make_uow
from app.adapters.mail.ports import Mailer
from app.api.deps import db_session
from app.auth.magic_link import (
    AlreadyConsumed,
    ConsumeLockout,
    InvalidToken,
    MagicLinkOutcome,
    MagicLinkPurpose,
    PurposeMismatch,
    RateLimited,
    Throttle,
    TokenExpired,
    consume_link,
    reason_for_exception,
    request_link,
    write_rejected_audit,
)
from app.config import Settings, get_settings

__all__ = [
    "MagicConsumeBody",
    "MagicConsumeResponse",
    "MagicRequestAcceptedResponse",
    "MagicRequestBody",
    "build_magic_router",
]

_log = logging.getLogger(__name__)

_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class MagicRequestBody(BaseModel):
    """Request body for ``POST /auth/magic/request``.

    ``email`` is typed as plain ``str`` rather than
    :class:`pydantic.EmailStr` because the domain layer already
    canonicalises (and production signup gates the format upstream).
    Pulling in ``email-validator`` just to re-validate here would
    burn a dependency for a format check we don't act on.
    """

    email: str = Field(..., min_length=3, max_length=320)
    purpose: MagicLinkPurpose = Field(
        ...,
        description=(
            "One of 'signup_verify', 'recover_passkey', "
            "'email_change_confirm', 'grant_invite'."
        ),
    )


class MagicConsumeBody(BaseModel):
    """Request body for ``POST /auth/magic/consume``."""

    token: str
    purpose: MagicLinkPurpose


class MagicConsumeResponse(BaseModel):
    """Response body for ``POST /auth/magic/consume`` on success."""

    purpose: str
    subject_id: str
    email_hash: str
    ip_hash: str


class MagicRequestAcceptedResponse(BaseModel):
    """Response body for ``POST /auth/magic/request`` on 202 accept.

    Body is deliberately opaque: the status is identical whether or
    not the email matched a user, which is the enumeration guard
    (§03 "Self-serve signup", §15 "Rate limiting and abuse controls").
    A single-field ``{"status": "accepted"}`` gives the SPA something
    to assert on without leaking the existence signal.
    """

    status: str = Field(
        default="accepted",
        description="Always the literal 'accepted'.",
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


_DomainError = (
    RateLimited,
    ConsumeLockout,
    InvalidToken,
    PurposeMismatch,
    TokenExpired,
    AlreadyConsumed,
)


def _http_for(exc: Exception) -> HTTPException:
    """Map a typed magic-link domain error to an :class:`HTTPException`.

    The envelope is ``{"error": <symbol>}`` to match the spec's
    error vocabulary (§03). The caller has already narrowed via
    ``except _DomainError`` so the mapping is total; a stray
    :class:`Exception` propagates as a real 500.
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
    # InvalidToken — fallback.
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_token"},
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    """Return a best-effort source IP for ``request``.

    FastAPI's :attr:`Request.client` is populated by Starlette from the
    accepted socket. The deployment middleware (cd-ika7) will handle
    ``X-Forwarded-For`` trust; until then this is fine for localhost /
    tailscale0 traffic where the client IP really is the socket peer.
    """
    if request.client is None:
        return ""
    return request.client.host


def build_magic_router(
    *,
    mailer: Mailer,
    throttle: Throttle,
    base_url: str | None = None,
    settings: Settings | None = None,
) -> APIRouter:
    """Return a fresh :class:`APIRouter` wired to ``mailer`` + ``throttle``.

    Factoring the wiring through a builder lets tests inject their
    own mailer double and a fresh throttle per case without relying
    on a module-level singleton. The v1 app factory (cd-ika7) will
    call this once at startup and mount the returned router on the
    bare-host tree.

    ``base_url`` defaults to ``settings.public_url`` — when both are
    ``None`` the service raises :class:`RuntimeError` on the first
    request, which is the right failure mode for a misconfigured
    deployment (better than silently emitting localhost links).
    """
    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``auth`` stays for fine-grained client filtering.
    router = APIRouter(prefix="/auth/magic", tags=["identity", "auth"])
    cfg = settings if settings is not None else get_settings()
    resolved_base_url = base_url if base_url is not None else cfg.public_url

    @router.post(
        "/request",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=MagicRequestAcceptedResponse,
        summary="Send a magic-link email (202 on enrolment; 429 on rate limit)",
    )
    def post_request(
        body: MagicRequestBody,
        request: Request,
        session: _Db,
    ) -> MagicRequestAcceptedResponse:
        """Mint + send one magic-link email.

        Returns ``202`` with an opaque ``{"status": "accepted"}`` body
        whenever the request is accepted for processing — the status
        is identical whether or not the email matched a user, which
        is the enumeration guard (§03 "Self-serve signup", §15 "Rate
        limiting and abuse controls").

        Returns ``429 rate_limited`` when the per-IP or per-email
        budget is exhausted; the throttle fires **before** the DB or
        mailer are touched, so a 429 is cheap and leaks no
        information about whether the email exists.
        """
        if resolved_base_url is None:
            raise RuntimeError(
                "base_url / settings.public_url is not set; "
                "cannot build magic-link URLs"
            )
        try:
            request_link(
                session,
                email=body.email,
                purpose=body.purpose,
                ip=_client_ip(request),
                mailer=mailer,
                base_url=resolved_base_url,
                throttle=throttle,
                settings=cfg,
            )
        except _DomainError as exc:
            raise _http_for(exc) from exc
        # 202 body is informational — the spec doesn't mandate a shape
        # beyond "accepted"; a status-only reply tells the SPA nothing
        # about whether the email existed, which is the whole point.
        return MagicRequestAcceptedResponse()

    @router.post(
        "/consume",
        response_model=MagicConsumeResponse,
        summary="Redeem a magic-link token (single-use)",
    )
    def post_consume(
        body: MagicConsumeBody,
        request: Request,
        session: _Db,
    ) -> MagicConsumeResponse:
        """Unseal + flip the nonce + return the outcome.

        Failure paths also write an ``audit.magic_link.rejected`` row
        inside a **fresh** UoW (see module docstring). The router
        re-raises the original exception after the rejected audit has
        committed so the HTTP mapping is unchanged.
        """
        ip = _client_ip(request)
        try:
            outcome: MagicLinkOutcome = consume_link(
                session,
                token=body.token,
                expected_purpose=body.purpose,
                ip=ip,
                throttle=throttle,
                settings=cfg,
            )
        except ConsumeLockout as exc:
            # Pre-flight lockout — the nonce row was never touched and
            # no failure counter needs advancing. The rejected audit
            # still lands (on a fresh UoW) so sustained abuse from a
            # locked-out IP is visible in the trail.
            _write_rejected_on_fresh_uow(
                token=body.token,
                expected_purpose=body.purpose,
                ip=ip,
                reason=reason_for_exception(exc),
                cfg=cfg,
            )
            raise _http_for(exc) from exc
        except _DomainError as exc:
            # Every observable failure advances the per-IP fail counter —
            # the spec flips the 10-min lockout on the 3rd fail.
            throttle.record_consume_failure(ip=ip, now=_now())
            # Rejected audit lives on a fresh UoW so it survives the
            # caller's rollback (§15 "Audit always written… including
            # misses"). The session injected here is about to roll
            # back on exception exit; we can't trust any row we add
            # to it.
            _write_rejected_on_fresh_uow(
                token=body.token,
                expected_purpose=body.purpose,
                ip=ip,
                reason=reason_for_exception(exc),
                cfg=cfg,
            )
            raise _http_for(exc) from exc
        throttle.record_consume_success(ip=ip)
        return MagicConsumeResponse(
            purpose=outcome.purpose,
            subject_id=outcome.subject_id,
            email_hash=outcome.email_hash,
            ip_hash=outcome.ip_hash,
        )

    return router


def _now() -> datetime:
    """Return the current UTC instant for throttle bookkeeping.

    The throttle's public methods all accept ``now`` explicitly, so
    this is the single place the router decides whose wall-clock it
    trusts. A future test-friendly router will thread a ``Clock``
    through the builder; today the router uses the system clock.
    """
    from app.util.clock import SystemClock

    return SystemClock().now()


def _write_rejected_on_fresh_uow(
    *,
    token: str,
    expected_purpose: str,
    ip: str,
    reason: str,
    cfg: Settings,
) -> None:
    """Commit an ``audit.magic_link.rejected`` row on a fresh UoW.

    The consume handler's primary UoW rolls back on exception exit,
    which would take any audit row written through its session with
    it. A fresh :func:`app.adapters.db.session.make_uow` gives us a
    committed trail independent of the primary transaction.

    Failures of the audit UoW are caught and logged rather than
    propagated: this helper runs inside an ``except _DomainError``
    clause about to re-raise the domain error, and a raised audit
    failure here would shadow the typed exception the HTTP mapper
    relies on — the end user would see a 500 instead of the expected
    400/409/410/429. The catch is deliberately broad (``Exception``,
    not just :class:`sqlalchemy.exc.SQLAlchemyError`) so a non-DB
    failure mode — e.g. :class:`app.auth.keys.KeyDerivationError` on
    an unset root key, a template / serializer programming error, or
    a network hiccup surfacing as ``OSError`` — still logs-and-drops
    instead of surfacing as a 500 that shadows the typed domain
    error. :class:`BaseException` (``KeyboardInterrupt``,
    ``SystemExit``) still propagates so operator-initiated aborts
    aren't swallowed.
    """
    try:
        with make_uow() as audit_session:
            assert isinstance(audit_session, Session)
            write_rejected_audit(
                audit_session,
                token=token,
                expected_purpose=expected_purpose,
                ip=ip,
                reason=reason,
                settings=cfg,
            )
    except Exception:
        _log.exception("magic_link.rejected audit write failed on fresh UoW")
