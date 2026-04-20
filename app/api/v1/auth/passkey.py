"""Passkey registration + login HTTP routers.

Three routers are exposed:

* :data:`router` — mounted at ``/auth/passkey`` inside the
  workspace-scoped tree (``/w/<slug>/api/v1/auth/passkey``). Both
  endpoints require an authenticated session and an active
  :class:`~app.tenancy.WorkspaceContext` (the ctx's ``actor_id``
  identifies the user); they call
  :func:`app.auth.passkey.register_start` /
  :func:`app.auth.passkey.register_finish`.

* :data:`signup_router` — mounted at
  ``/api/v1/auth/passkey/signup`` at the **bare host**. No workspace
  exists yet; the caller supplies a ``signup_session_id`` issued by
  the magic-link verify step (cd-3i5) and, on finish, the freshly
  minted ``user_id``. These handlers call
  :func:`app.auth.passkey.register_start_signup` /
  :func:`app.auth.passkey.register_finish_signup`.

* :func:`build_login_router` — bare-host login flow
  (``/api/v1/auth/passkey/login``). No session exists yet; the
  browser does a conditional-UI passkey ceremony and on success the
  router stamps a ``Set-Cookie: __Host-crewday_session=...`` header
  via :func:`app.auth.session.build_session_cookie`. Constructed by
  the v1 app factory with the process-wide :class:`Throttle` so
  concurrent requests share the rolling lockout state.

Handlers are intentionally thin: unpack the body, call the domain
service under the request's Unit-of-Work, shape the response. The
UoW (see :func:`app.api.deps.db_session`) owns the transaction
boundary — domain code never calls ``session.commit()`` (§01
"Key runtime invariants" #3).

See ``docs/specs/03-auth-and-tokens.md`` §"WebAuthn specifics",
§"Login", §"Self-serve signup" step 3, §"Additional passkeys";
``docs/specs/15-security-privacy.md`` §"Passkey specifics".
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.abuse.throttle import ShieldStore
from app.abuse.throttle import throttle as throttle_decorator
from app.adapters.db.session import make_uow
from app.api.deps import current_workspace_context, db_session
from app.audit import write_audit
from app.auth._hashing import hash_with_pepper
from app.auth._throttle import PasskeyLoginLockout, Throttle
from app.auth.keys import derive_subkey
from app.auth.passkey import (
    AuthenticationOptions,
    ChallengeAlreadyConsumed,
    ChallengeExpired,
    ChallengeNotFound,
    ChallengeSubjectMismatch,
    CloneDetected,
    InvalidLoginAttempt,
    InvalidRegistration,
    LoginResult,
    PasskeyCredentialRef,
    RegistrationOptions,
    TooManyPasskeys,
    login_finish,
    login_start,
    register_finish,
    register_finish_signup,
    register_start,
    register_start_signup,
)
from app.auth.session import build_session_cookie
from app.auth.session import (
    invalidate_for_credential as session_invalidate_for_credential,
)
from app.auth.webauthn import base64url_to_bytes
from app.config import Settings, get_settings
from app.tenancy import WorkspaceContext
from app.util.clock import SystemClock

__all__ = ["build_login_router", "router", "signup_router"]


# FastAPI's convention puts ``Depends`` on the default; newer style is
# ``Annotated[T, Depends(...)]`` which keeps ruff's B008 happy and
# makes the parameter typing read naturally. We use the annotated
# form uniformly.
_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class RegisterStartResponse(BaseModel):
    """Response body for ``POST /auth/passkey/register/start``."""

    challenge_id: str
    options: dict[str, Any] = Field(
        ...,
        description=(
            "Parsed PublicKeyCredentialCreationOptions ready for "
            "navigator.credentials.create()."
        ),
    )


class RegisterFinishRequest(BaseModel):
    """Request body for ``POST /auth/passkey/register/finish``."""

    challenge_id: str
    credential: dict[str, Any] = Field(
        ...,
        description=(
            "Raw JSON attestation response from navigator.credentials.create()."
        ),
    )


class RegisterFinishResponse(BaseModel):
    """Response body for ``POST /auth/passkey/register/finish``."""

    credential_id: str
    transports: str | None
    backup_eligible: bool
    aaguid: str


class SignupRegisterStartRequest(BaseModel):
    """Request body for ``POST /auth/passkey/signup/register/start``.

    ``signup_session_id`` is the handle minted by the magic-link
    verify step; the bare-host flow is tenant-agnostic, so we read
    the display name + email from the pending signup row
    indirectly via the request body.
    """

    signup_session_id: str
    email: str
    display_name: str


class SignupRegisterFinishRequest(BaseModel):
    """Request body for ``POST /auth/passkey/signup/register/finish``.

    ``user_id`` is the freshly-minted ULID the signup service
    reserved for this account. The signup service's finish handler
    calls this endpoint inside its own UoW so the user + grant +
    credential land atomically.
    """

    signup_session_id: str
    user_id: str
    challenge_id: str
    credential: dict[str, Any]


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


# Tuple of domain error types the routers map to :class:`HTTPException`.
# Anything else propagates unchanged — a stray ``RuntimeError`` is a
# real 500 and the operator needs to see the traceback.
_DomainError = (
    TooManyPasskeys,
    InvalidRegistration,
    ChallengeNotFound,
    ChallengeAlreadyConsumed,
    ChallengeExpired,
    ChallengeSubjectMismatch,
    LookupError,
)


def _http_for(exc: Exception) -> HTTPException:
    """Return the :class:`HTTPException` mapping for a known domain error.

    ``exc`` is one of :data:`_DomainError`; the caller has already
    narrowed with ``except (...) as exc`` so the mapping is total.
    The envelope is a thin ``{"error": <symbol>}`` for v1; the full
    RFC 7807 problem+json shape lands with cd-waq3. Keeping the
    envelope private to this helper means swapping shapes later is a
    single diff.
    """
    if isinstance(exc, TooManyPasskeys):
        # Starlette renamed the constant from *_ENTITY to *_CONTENT in
        # a recent release; use the literal 422 so the router works
        # across minor versions without a conditional import.
        return HTTPException(
            status_code=422,
            detail={"error": "too_many_passkeys"},
        )
    if isinstance(exc, ChallengeNotFound | ChallengeAlreadyConsumed):
        # AC #5 — a replayed finish raises ChallengeAlreadyConsumed;
        # a genuinely unknown id is indistinguishable for privacy and
        # maps to the same 409. The HTTP body does NOT reveal which.
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "challenge_consumed_or_unknown"},
        )
    if isinstance(exc, ChallengeExpired):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "challenge_expired"},
        )
    if isinstance(exc, InvalidRegistration | ChallengeSubjectMismatch):
        # AC #2 — mismatched challenge / origin / rp_id → 400.
        # ChallengeSubjectMismatch collapses into the same shape so
        # the client can't fingerprint internal subject routing.
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_registration"},
        )
    # Fallback: a LookupError that isn't a ChallengeNotFound — user
    # load miss on the authenticated flow. Map to 401 so the router
    # doesn't reveal whether the ULID exists.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "not_authenticated"},
    )


# ---------------------------------------------------------------------------
# Workspace-scoped router — authenticated "add another passkey" flow
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/auth/passkey", tags=["auth"])


@router.post(
    "/register/start",
    response_model=RegisterStartResponse,
    summary="Begin passkey registration for the authenticated user",
)
def post_register_start(
    ctx: _Ctx,
    session: _Db,
) -> RegisterStartResponse:
    """Mint a fresh challenge for the caller's next passkey."""
    try:
        opts: RegistrationOptions = register_start(
            ctx,
            session,
            user_id=ctx.actor_id,
        )
    except _DomainError as exc:
        raise _http_for(exc) from exc
    return RegisterStartResponse(
        challenge_id=opts.challenge_id,
        options=opts.options,
    )


@router.post(
    "/register/finish",
    response_model=RegisterFinishResponse,
    summary="Verify + persist a passkey for the authenticated user",
)
def post_register_finish(
    body: RegisterFinishRequest,
    ctx: _Ctx,
    session: _Db,
) -> RegisterFinishResponse:
    """Verify the browser's attestation and insert the credential row."""
    try:
        ref: PasskeyCredentialRef = register_finish(
            ctx,
            session,
            user_id=ctx.actor_id,
            challenge_id=body.challenge_id,
            credential=body.credential,
        )
    except _DomainError as exc:
        raise _http_for(exc) from exc
    return RegisterFinishResponse(
        credential_id=ref.credential_id_b64url,
        transports=ref.transports,
        backup_eligible=ref.backup_eligible,
        aaguid=ref.aaguid,
    )


# ---------------------------------------------------------------------------
# Bare-host signup router — first passkey for a fresh account
# ---------------------------------------------------------------------------


signup_router = APIRouter(prefix="/auth/passkey/signup", tags=["auth", "signup"])


@signup_router.post(
    "/register/start",
    response_model=RegisterStartResponse,
    summary="Begin passkey registration during self-serve signup",
)
def post_signup_register_start(
    body: SignupRegisterStartRequest,
    session: _Db,
) -> RegisterStartResponse:
    """Mint a signup-scoped challenge (no workspace, no user row yet)."""
    try:
        opts = register_start_signup(
            session,
            signup_session_id=body.signup_session_id,
            email=body.email,
            display_name=body.display_name,
        )
    except _DomainError as exc:
        raise _http_for(exc) from exc
    return RegisterStartResponse(
        challenge_id=opts.challenge_id,
        options=opts.options,
    )


@signup_router.post(
    "/register/finish",
    response_model=RegisterFinishResponse,
    summary="Verify + persist the signup flow's first passkey",
)
def post_signup_register_finish(
    body: SignupRegisterFinishRequest,
    session: _Db,
) -> RegisterFinishResponse:
    """Verify the attestation, insert the credential row."""
    try:
        ref = register_finish_signup(
            session,
            signup_session_id=body.signup_session_id,
            user_id=body.user_id,
            challenge_id=body.challenge_id,
            credential=body.credential,
        )
    except _DomainError as exc:
        raise _http_for(exc) from exc
    return RegisterFinishResponse(
        credential_id=ref.credential_id_b64url,
        transports=ref.transports,
        backup_eligible=ref.backup_eligible,
        aaguid=ref.aaguid,
    )


# ---------------------------------------------------------------------------
# Bare-host login router — discoverable credential / conditional UI
# ---------------------------------------------------------------------------


class LoginStartResponse(BaseModel):
    """Response body for ``POST /auth/passkey/login/start``."""

    challenge_id: str
    options: dict[str, Any] = Field(
        ...,
        description=(
            "Parsed PublicKeyCredentialRequestOptions ready for "
            "navigator.credentials.get()."
        ),
    )


class LoginFinishRequest(BaseModel):
    """Request body for ``POST /auth/passkey/login/finish``."""

    challenge_id: str
    credential: dict[str, Any] = Field(
        ...,
        description=("Raw JSON assertion response from navigator.credentials.get()."),
    )


class LoginFinishResponse(BaseModel):
    """Response body for ``POST /auth/passkey/login/finish``.

    Only the ``user_id`` is surfaced — the session cookie is delivered
    as a ``Set-Cookie`` header (``__Host-crewday_session``), not in
    the body. JSON response (not a 302 redirect) keeps the SPA in
    control of navigation after sign-in.
    """

    user_id: str


# HKDF purpose for peppering the throttle's credential-id + IP hashes
# on the login surface. Distinct from ``session-hash`` / ``magic-link``
# so an oracle on one surface doesn't weaken the others.
_PASSKEY_LOGIN_HKDF_PURPOSE = "passkey-login-throttle"


def _client_ip(request: Request) -> str:
    """Best-effort source IP for ``request``.

    Mirrors the magic / signup routers. Returns the empty string when
    the framework can't resolve a client — keeps hashing total and
    means a test client that omits ``host`` still gets a deterministic
    (empty-string) bucket rather than a crash.
    """
    if request.client is None:
        return ""
    return request.client.host


def _login_begin_key(*args: object, **kwargs: object) -> str:
    """Return the per-IP bucket key for the passkey-login begin throttle.

    :func:`app.abuse.throttle.throttle` forwards the wrapped handler's
    positional + keyword arguments verbatim; FastAPI is free to bind
    ``request`` by position or by keyword depending on the version.
    We scan both: first ``kwargs["request"]``, then the positional
    tuple. A :class:`Request` match wins. If nothing in the argv is a
    :class:`Request` (shouldn't happen at runtime), we fall back to
    the empty string so the throttle degrades to "one shared bucket"
    rather than crashing — matches the same fail-safe shape
    :func:`_client_ip` uses on an unresolved client.
    """
    req = kwargs.get("request")
    if isinstance(req, Request):
        return _client_ip(req)
    for arg in args:
        if isinstance(arg, Request):
            return _client_ip(arg)
    return ""


_log = logging.getLogger(__name__)


def _login_audit_ctx() -> WorkspaceContext:
    """Return the tenant-agnostic :class:`WorkspaceContext` for login audit.

    Login runs before any workspace is picked (§03 "Sessions"); the
    audit row carries zero-ULID placeholders for workspace + actor
    and the real details live in the ``diff`` payload. Matches the
    shape used by :mod:`app.auth.session` + :mod:`app.auth.passkey`.
    """
    return WorkspaceContext(
        workspace_id="00000000000000000000000000",
        workspace_slug="",
        actor_id="00000000000000000000000000",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id="00000000000000000000000000",
    )


def _invalidate_for_credential_fresh_uow(
    *,
    credential_id_b64: str,
    cause: str,
) -> None:
    """Invalidate every session for the clone-detected credential's owner.

    Runs on a **fresh** UoW for the same reason the failure audits do:
    the primary UoW rolls back on :class:`CloneDetected`, and an
    invalidate inside it would disappear with the rollback. Opening a
    fresh UoW via :func:`make_uow` matches the audit-rescue pattern.

    Failures are logged and swallowed so an audit / DB hiccup doesn't
    shadow the intended 401 response. Catch is broad (``Exception``) —
    never ``BaseException``, so operator aborts still propagate.
    """
    try:
        credential_id = base64url_to_bytes(credential_id_b64)
    except (ValueError, TypeError):
        _log.exception(
            "clone invalidate: credential id %r not base64url", credential_id_b64
        )
        return
    try:
        with make_uow() as uow_session:
            assert isinstance(uow_session, Session)
            session_invalidate_for_credential(
                uow_session,
                credential_id=credential_id,
                cause=cause,
            )
    except Exception:
        _log.exception("clone-detected session invalidate failed on fresh UoW")


def _write_login_audit_fresh_uow(
    *,
    action: str,
    credential_id_b64: str | None,
    diff: dict[str, Any],
) -> None:
    """Emit a failure audit row on its own UoW.

    The primary UoW rolls back on the domain service's raise — any
    audit row written inside it is lost. Opening a fresh UoW via
    :func:`make_uow` (the same pattern magic-link + signup use for
    their refusal audits) means the rejection trail survives the
    rollback that returns 401 / 429 to the client.

    Failures of the audit UoW are logged and swallowed: this helper
    runs inside an ``except`` clause about to re-raise the mapped
    :class:`HTTPException`, and an audit failure here would shadow
    the client's intended status code with a 500. The catch is
    deliberately broad (``Exception``) so any transient DB / config
    hiccup still logs-and-drops; :class:`BaseException` propagates
    so operator aborts aren't swallowed.

    PII minimisation (§15): only hashes + the public credential id
    in base64url. Never the plaintext IP, UA, email, or user id.
    """
    try:
        with make_uow() as uow_session:
            assert isinstance(uow_session, Session)
            write_audit(
                uow_session,
                _login_audit_ctx(),
                entity_kind="passkey_credential",
                entity_id=credential_id_b64 or "",
                action=action,
                diff=diff,
            )
    except Exception:
        _log.exception("passkey login refusal audit write failed on fresh UoW")


def _extract_credential_id_b64(credential: dict[str, Any]) -> str | None:
    """Return the assertion's ``id`` field as base64url, or None.

    Used for audit payloads on failure — the caller has already been
    through :func:`app.auth.passkey._decode_credential_id` which
    would have raised :class:`InvalidLoginAttempt` on a malformed id,
    so a ``None`` here is a genuine "body didn't carry an id" (shape
    error that beat the domain service to the draw). Keeping the
    extraction defensive means the audit write never crashes on an
    edge-case payload.
    """
    raw_id = credential.get("id") if isinstance(credential, dict) else None
    if isinstance(raw_id, str) and raw_id:
        return raw_id
    return None


def build_login_router(
    *,
    throttle: Throttle,
    settings: Settings | None = None,
    begin_shield: ShieldStore | None = None,
) -> APIRouter:
    """Return a fresh :class:`APIRouter` for the passkey login flow.

    The v1 app factory constructs one instance per process with the
    shared :class:`Throttle` so every worker sees the same rolling
    lockout counters (single-process today, §01 "One worker pool per
    process"). Tests instantiate a fresh router per case with their
    own :class:`Throttle` so per-test state never bleeds across
    sibling cases.

    ``settings`` is read once at build time — the HKDF subkey for
    peppering the throttle's credential-id / IP hashes stays
    constant for the router's lifetime. The login domain service
    itself reads :func:`app.config.get_settings` lazily; the router
    only needs the root-key hash material.

    ``begin_shield`` is the :class:`~app.abuse.throttle.ShieldStore`
    backing the per-IP 10/min rate limit on the ``/login/start``
    endpoint (spec §15 "Rate limiting and abuse controls": *"10/min
    per IP for login begin"*). Default: a fresh store per router
    build, which matches the production singleton-per-process shape
    and gives each test-constructed router a clean rolling window.
    """
    cfg = settings if settings is not None else get_settings()
    # Derive the HKDF subkey once at router build — the root key is
    # stable for the process lifetime and the subkey is used on every
    # login request for hashing the credential id + IP into throttle
    # buckets.
    login_pepper = derive_subkey(cfg.root_key, purpose=_PASSKEY_LOGIN_HKDF_PURPOSE)

    # Per-router shield store so two test-built routers don't share the
    # sliding-window counter. Production constructs one router per
    # process (§01 "One worker pool per process"), so a per-router
    # default is exactly one process-wide store in practice.
    shield = begin_shield if begin_shield is not None else ShieldStore()

    router = APIRouter(prefix="/auth/passkey/login", tags=["auth", "login"])

    # §15 "Rate limiting and abuse controls": 10/min per IP for login
    # begin. Keyed on the raw client IP string (empty for unresolved
    # clients, per :func:`_client_ip`). We key on the plaintext IP
    # here rather than a peppered hash because this bucket never
    # leaves the process — the Throttle lockout buckets downstream
    # hash their IPs because they pin audit rows; this one only gates
    # a single call and the hit list never hits disk.
    #
    # ``key_fn`` walks both positional and keyword argument tuples so
    # it stays correct whether FastAPI binds ``request`` positionally
    # or by keyword (version-dependent).
    @router.post(
        "/start",
        response_model=LoginStartResponse,
        summary="Begin a passkey login; returns request options + a challenge id",
    )
    @throttle_decorator(
        scope="passkey.login.begin",
        key_fn=_login_begin_key,
        limit=10,
        window_s=60,
        store=shield,
    )
    def post_login_start(request: Request, session: _Db) -> LoginStartResponse:
        """Mint an assertion challenge for conditional UI.

        No body — the caller is anonymous (no session yet). The
        challenge row carries a login-sentinel subject so the
        finish handler can reject a signup or register challenge
        smuggled through the login path.

        The per-IP 10/min rate limit is applied by the
        :func:`app.abuse.throttle.throttle` decorator **before** the
        handler body runs — the 11th request inside a minute from
        the same IP returns ``429 rate_limited`` and never touches
        the DB. ``request`` sits in the signature first because the
        decorator's ``key_fn`` reads it positionally.
        """
        opts: AuthenticationOptions = login_start(session)
        return LoginStartResponse(
            challenge_id=opts.challenge_id,
            options=opts.options,
        )

    @router.post(
        "/finish",
        response_model=LoginFinishResponse,
        summary=(
            "Verify the passkey assertion, issue a session cookie, "
            "return the authenticating user id"
        ),
    )
    def post_login_finish(
        body: LoginFinishRequest,
        request: Request,
        response: Response,
        session: _Db,
    ) -> LoginFinishResponse:
        """Run :func:`login_finish`, stamp the session cookie on success.

        On failure we hash the observable identifiers for audit and
        throttle advancement. The HTTP envelope collapses
        :class:`InvalidLoginAttempt`, :class:`CloneDetected`,
        :class:`ChallengeSubjectMismatch`, :class:`ChallengeNotFound`,
        :class:`ChallengeAlreadyConsumed`, and :class:`ChallengeExpired`
        into the same ``401 invalid_credential`` shape so the client
        can't fingerprint which internal gate refused the request.
        :class:`PasskeyLoginLockout` maps to ``429 rate_limited``
        because the lockout is the one failure the SPA can act on
        (back off).
        """
        ip = _client_ip(request)
        ua = request.headers.get("user-agent", "")
        accept_language = request.headers.get("accept-language", "")
        credential_id_b64 = _extract_credential_id_b64(body.credential)
        ip_hash = hash_with_pepper(ip, login_pepper)

        try:
            result: LoginResult = login_finish(
                session,
                challenge_id=body.challenge_id,
                credential=body.credential,
                ip=ip,
                ua=ua,
                ip_hash_pepper=login_pepper,
                throttle=throttle,
                accept_language=accept_language,
            )
        except PasskeyLoginLockout as exc:
            # Throttle already raised — no throttle advancement here,
            # the lockout *is* the advancement. Still audit so
            # operators see the sustained pressure.
            _write_login_audit_fresh_uow(
                action="passkey.login_rejected",
                credential_id_b64=credential_id_b64,
                diff={
                    "reason": "rate_limited",
                    "cred_id_b64": credential_id_b64,
                    "ip_hash": ip_hash,
                    "scope": exc.scope,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": "rate_limited"},
            ) from exc
        except CloneDetected as exc:
            # Clone detection is the rare case that warrants two
            # audit rows plus a session invalidation: the operator-
            # facing ``cloned_detected`` (so §15's "auto-revoke on
            # rollback" has a record) and the uniform
            # ``login_rejected`` so the refusal trail matches every
            # other 401 shape. The invalidate runs on a fresh UoW
            # because the primary UoW rolls back on the raise —
            # leaving the suspected-stolen sessions live would be the
            # worst-of-both-worlds posture §15 forbids.
            if credential_id_b64 is not None:
                credential_id_hash = hash_with_pepper(credential_id_b64, login_pepper)
                throttle.record_passkey_login_failure(
                    credential_id_hash=credential_id_hash,
                    ip_hash=ip_hash,
                    now=SystemClock().now(),
                )
            _invalidate_for_credential_fresh_uow(
                credential_id_b64=exc.credential_id_b64,
                cause="clone_detected",
            )
            _write_login_audit_fresh_uow(
                action="passkey.cloned_detected",
                credential_id_b64=exc.credential_id_b64,
                diff={
                    "cred_id_b64": exc.credential_id_b64,
                    "ip_hash": ip_hash,
                    "old_sign_count": exc.old_sign_count,
                    "new_sign_count": exc.new_sign_count,
                },
            )
            _write_login_audit_fresh_uow(
                action="passkey.login_rejected",
                credential_id_b64=exc.credential_id_b64,
                diff={
                    "reason": "CloneDetected",
                    "cred_id_b64": exc.credential_id_b64,
                    "ip_hash": ip_hash,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_credential"},
            ) from exc
        except (
            InvalidLoginAttempt,
            ChallengeNotFound,
            ChallengeAlreadyConsumed,
            ChallengeExpired,
            ChallengeSubjectMismatch,
        ) as exc:
            # Any of these is a "the caller's attempt didn't redeem"
            # signal — advance the per-credential + per-IP failure
            # counters and audit with the fine-grained reason.
            reason = type(exc).__name__
            if credential_id_b64 is not None:
                credential_id_hash = hash_with_pepper(credential_id_b64, login_pepper)
                throttle.record_passkey_login_failure(
                    credential_id_hash=credential_id_hash,
                    ip_hash=ip_hash,
                    now=SystemClock().now(),
                )
            _write_login_audit_fresh_uow(
                action="passkey.login_rejected",
                credential_id_b64=credential_id_b64,
                diff={
                    "reason": reason,
                    "cred_id_b64": credential_id_b64,
                    "ip_hash": ip_hash,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_credential"},
            ) from exc

        # Success — reset the throttle counters so a previous bad
        # attempt doesn't count against the user's next login, then
        # stamp the session cookie on the response.
        credential_id_hash = hash_with_pepper(result.credential_id_b64url, login_pepper)
        throttle.record_passkey_login_success(
            credential_id_hash=credential_id_hash,
            ip_hash=ip_hash,
        )
        cookie_header = build_session_cookie(
            result.session_issue.cookie_value,
            result.session_issue.expires_at,
        )
        response.headers.append("set-cookie", cookie_header)
        return LoginFinishResponse(user_id=result.user_id)

    return router
