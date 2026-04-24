"""Passkey registration + login HTTP routers.

Two routers are exposed:

* :data:`router` — mounted at ``/auth/passkey`` inside the
  workspace-scoped tree (``/w/<slug>/api/v1/auth/passkey``). Both
  endpoints require an authenticated session and an active
  :class:`~app.tenancy.WorkspaceContext` (the ctx's ``actor_id``
  identifies the user); they call
  :func:`app.auth.passkey.register_start` /
  :func:`app.auth.passkey.register_finish`.

* :func:`build_login_router` — bare-host login flow
  (``/api/v1/auth/passkey/login``). No session exists yet; the
  browser does a conditional-UI passkey ceremony and on success the
  router stamps a ``Set-Cookie: __Host-crewday_session=...`` header
  via :func:`app.auth.session.build_session_cookie`. Constructed by
  the v1 app factory with the process-wide :class:`Throttle` so
  concurrent requests share the rolling lockout state.

The signup flow's WebAuthn ceremony lives in
:mod:`app.api.v1.auth.signup` (``/api/v1/signup/passkey/{start,finish}``)
— its finish handler delegates to :func:`app.auth.signup.complete_signup`
which atomically creates the user, workspace, owners permission
group, and first passkey credential in one transaction. A parallel
bare-host ``/api/v1/auth/passkey/signup/register/{start,finish}``
router used to live in this module (still calling the same
:func:`app.auth.passkey.register_start_signup` /
:func:`app.auth.passkey.register_finish_signup` domain helpers the
canonical flow uses today); cd-ju0q retired the router itself in
favour of the single canonical signup flow above. The domain helpers
remain in active use by :func:`app.auth.signup.complete_signup`.

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
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.abuse.throttle import ShieldStore
from app.abuse.throttle import throttle as throttle_decorator
from app.adapters.db.identity.models import PasskeyCredential, WebAuthnChallenge
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
    LastPasskeyCredential,
    LoginResult,
    PasskeyCredentialRef,
    PasskeyNotFound,
    RegistrationOptions,
    TooManyPasskeys,
    login_finish,
    login_start,
    register_finish,
    register_start,
    revoke_passkey,
)
from app.auth.session import build_session_cookie
from app.auth.session import (
    invalidate_for_credential as session_invalidate_for_credential,
)
from app.auth.webauthn import base64url_to_bytes
from app.config import Settings, get_settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock

__all__ = ["build_login_router", "router"]


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


# Tags: ``identity`` surfaces every identity-adjacent operation
# under one OpenAPI section (spec §01 context map + §12 Auth);
# ``auth`` stays for fine-grained client filtering.
router = APIRouter(prefix="/auth/passkey", tags=["identity", "auth"])


@router.post(
    "/register/start",
    response_model=RegisterStartResponse,
    operation_id="auth.passkey.register_start",
    summary="Begin passkey registration for the authenticated user",
    openapi_extra={
        # Browser-only WebAuthn ceremony — the caller has to complete
        # it through ``navigator.credentials.create()``, so there is
        # no meaningful CLI surface. ``hidden: true`` keeps the CLI
        # generator (§13) from emitting a ``crewday auth passkey
        # register-start`` verb that would error out immediately.
        # ``x-interactive-only`` satisfies the §12 "mutating route"
        # rule: the flow already requires an authenticated passkey
        # session to reach it (tokens are rejected upstream).
        "x-cli": {
            "group": "auth",
            "verb": "passkey-register-start",
            "summary": "Begin passkey registration for the authenticated user",
            "mutates": True,
            "hidden": True,
        },
        "x-interactive-only": True,
    },
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
    operation_id="auth.passkey.register_finish",
    summary="Verify + persist a passkey for the authenticated user",
    openapi_extra={
        # Sibling of ``register_start`` — same browser-ceremony
        # rationale for ``hidden``; same passkey-session requirement
        # for ``x-interactive-only``.
        "x-cli": {
            "group": "auth",
            "verb": "passkey-register-finish",
            "summary": "Verify and persist a passkey for the authenticated user",
            "mutates": True,
            "hidden": True,
        },
        "x-interactive-only": True,
    },
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
        # cd-qx1f: challenge rows are single-use even on verification
        # failure. The primary UoW rolls back on the raise, so we
        # land the delete on a fresh UoW before mapping to HTTP.
        _delete_challenge_fresh_uow(body.challenge_id)
        raise _http_for(exc) from exc
    return RegisterFinishResponse(
        credential_id=ref.credential_id_b64url,
        transports=ref.transports,
        backup_eligible=ref.backup_eligible,
        aaguid=ref.aaguid,
    )


@router.delete(
    "/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="auth.passkey.revoke",
    summary="Revoke one of the authenticated user's passkeys",
    openapi_extra={
        # Unlike the register ceremonies, the CLI surface for
        # ``passkey-revoke`` is meaningful — an operator listing
        # their passkeys should be able to drop a lost device. Not
        # ``hidden``. §03 "Additional passkeys" pins the flow to a
        # live passkey session, so PATs + delegated tokens reject
        # with 403 ``session_only_endpoint`` (see §12 "Interactive-
        # only extension").
        "x-cli": {
            "group": "auth",
            "verb": "passkey-revoke",
            "summary": "Revoke one of your passkeys",
            "mutates": True,
        },
        "x-interactive-only": True,
    },
)
def delete_passkey(
    credential_id: str,
    ctx: _Ctx,
    session: _Db,
) -> Response:
    """Revoke ``credential_id`` for the authenticated user.

    * The credential must belong to the calling user — an id owned by
      someone else is collapsed with "unknown id" into a single 404 so
      the credential-id space is not an enumeration oracle.
    * Revoking the user's **last** passkey is refused with 422
      ``last_credential`` — leaving the user credential-less would
      force them through the recovery flow. The SPA should guide the
      user to enrol another passkey first or use ``/recover``
      intentionally.
    * Every session for the user is invalidated with cause
      ``"passkey_revoked"`` (§15 "Shared-origin XSS containment" /
      cd-geqp) in the same UoW as the delete. Audit rows land in
      cause-then-effect order: ``passkey.revoked`` first, then
      ``session.invalidated``.

    204 on success with an empty body — standard REST shape for a
    successful DELETE.
    """
    try:
        credential_id_bytes = base64url_to_bytes(credential_id)
    except (ValueError, TypeError) as exc:
        # Malformed base64url → 404 rather than 400: the bytes wouldn't
        # match any credential anyway, and surfacing "invalid
        # base64url" would let an attacker distinguish "well-formed id
        # for another user" from "syntactically invalid id".
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "passkey_not_found"},
        ) from exc

    try:
        revoke_passkey(
            ctx,
            session,
            user_id=ctx.actor_id,
            credential_id=credential_id_bytes,
        )
    except PasskeyNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "passkey_not_found"},
        ) from exc
    except LastPasskeyCredential as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "last_credential"},
        ) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


def _auto_revoke_credential_fresh_uow(
    *,
    credential_id_b64: str,
    reason: str = "clone_detected",
) -> None:
    """Hard-delete the clone-detected credential on a fresh UoW + audit.

    cd-cx19 / §15 "Passkey specifics": a sign-count rollback event
    auto-revokes the credential alongside the user-facing 401. The
    primary UoW rolls back on :class:`CloneDetected`, so this helper
    opens a fresh :func:`make_uow` to land the delete + audit row
    even when the caller's UoW is about to disappear.

    Hard-delete (not soft-delete): ``passkey_credential`` has no
    ``deleted_at`` column, matching the user-initiated
    :func:`app.auth.passkey.revoke_passkey` path. Forensic trail lives
    in the audit rows (``passkey.cloned_detected`` +
    ``passkey.auto_revoked``) — the credential row itself is gone.

    Responsibilities are sharp: this helper **only** revokes the
    credential + writes the auto-revoke audit. Session invalidation
    (with cause ``"clone_detected"``) is a separate concern and runs
    from :func:`_invalidate_for_credential_fresh_uow` earlier in the
    ``except CloneDetected`` branch — splitting the two means the
    §15 "Session-invalidation causes" table sees ``clone_detected``
    exactly once per event and the audit trail reads cleanly:

    1. ``session.invalidated`` (cause ``clone_detected``) —
       pre-existing sibling helper
    2. ``passkey.cloned_detected`` — domain detection audit
    3. ``passkey.auto_revoked`` — THIS helper's audit
    4. (credential row gone)

    Idempotency: a concurrent auto-revoke (or a subsequent replay)
    finds the row already gone and the helper returns cleanly — the
    audit row is NOT emitted for a no-op because the absence of the
    row means we're not the party revoking.

    Failures are logged and swallowed so a DB / audit hiccup never
    shadows the 401 response the router is about to return. The
    catch is deliberately broad (``Exception``) — :class:`BaseException`
    still propagates so operator aborts aren't swallowed.
    """
    try:
        credential_id = base64url_to_bytes(credential_id_b64)
    except (ValueError, TypeError):
        _log.exception(
            "clone auto-revoke: credential id %r not base64url",
            credential_id_b64,
        )
        return
    try:
        with make_uow() as uow_session:
            assert isinstance(uow_session, Session)
            # justification: passkey_credential is identity-scoped;
            # mirrors the tenant_agnostic gate on every other
            # credential-table read in this module (and in
            # app.auth.passkey.revoke_passkey).
            with tenant_agnostic():
                row = uow_session.get(PasskeyCredential, credential_id)
            if row is None:
                # Already revoked by a concurrent caller, or never
                # existed — either way the row is gone and emitting
                # an auto-revoke audit would falsely claim this call
                # did the work.
                _log.info(
                    "clone auto-revoke: credential %r already absent; no-op",
                    credential_id_b64,
                )
                return
            # Audit BEFORE the destructive write so a flush failure on
            # the audit insert doesn't leave an auto-revoked credential
            # without a paper trail. Both land under the same fresh
            # UoW — rollback takes them together. Mirrors the
            # cause-before-effect ordering of revoke_passkey.
            write_audit(
                uow_session,
                _login_audit_ctx(),
                entity_kind="passkey_credential",
                entity_id=credential_id_b64,
                action="passkey.auto_revoked",
                diff={
                    "reason": reason,
                    "cred_id_b64": credential_id_b64,
                    "user_id": row.user_id,
                },
            )
            # justification: passkey_credential is identity-scoped.
            with tenant_agnostic():
                uow_session.delete(row)
                uow_session.flush()
    except Exception:
        _log.exception(
            "clone-detected auto-revoke failed on fresh UoW for id=%r",
            credential_id_b64,
        )


def _delete_challenge_fresh_uow(challenge_id: str) -> None:
    """Idempotently delete ``challenge_id`` on its own Unit-of-Work.

    Spec §03 "WebAuthn specifics" / cd-qx1f: challenge rows are
    single-use **even on verification failure**. The primary UoW
    rolls back on any domain raise, so a challenge delete inside it
    would disappear with the rollback and leave the row redeemable
    until its 10-minute TTL — handing an attacker with a leaked
    ``challenge_id`` a replay window that the spec wants to be zero.
    Opening a fresh UoW via :func:`make_uow` here lands the delete
    even when the caller's UoW is about to roll back, mirroring the
    sibling :func:`_write_login_audit_fresh_uow` pattern.

    Idempotency: uses a ``DELETE ... WHERE id = ?`` statement, which
    tolerates zero rows affected. Two concurrent finish calls (one
    succeeds and deletes the row, the other fails post-success and
    tries to delete it again) both exit cleanly. No ``tenant_agnostic``
    gate is needed — ``webauthn_challenge`` is an identity-scoped
    table, not registered in :mod:`app.tenancy.registry`, so the
    ORM tenant filter ignores it.

    Failures are logged and swallowed so a DB hiccup on the delete
    never shadows the 4xx response the router is about to return.
    Broad ``Exception`` catch — ``BaseException`` still propagates so
    operator aborts aren't swallowed.
    """
    try:
        with make_uow() as uow_session:
            assert isinstance(uow_session, Session)
            uow_session.execute(
                delete(WebAuthnChallenge).where(WebAuthnChallenge.id == challenge_id)
            )
    except Exception:
        _log.exception("fresh-UoW challenge delete failed for id=%r", challenge_id)


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

    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``auth`` + ``login`` stay for fine-grained client filtering.
    router = APIRouter(
        prefix="/auth/passkey/login",
        tags=["identity", "auth", "login"],
    )

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
        operation_id="auth.passkey.login_start",
        summary="Begin a passkey login; returns request options + a challenge id",
        openapi_extra={
            # Bare-host login is pre-session — the caller has no
            # token, no passkey session, just an anonymous browser
            # about to run ``navigator.credentials.get()``. The
            # §12 "mutating route" rule still requires one of the
            # three agent-boundary gates; ``x-interactive-only`` is
            # the safe fit (the authentication exchange CANNOT be
            # driven by any token, by definition) and ``hidden``
            # keeps the CLI generator from emitting a verb that
            # could never complete the ceremony.
            "x-cli": {
                "group": "auth",
                "verb": "passkey-login-start",
                "summary": "Begin a passkey login",
                "mutates": True,
                "hidden": True,
            },
            "x-interactive-only": True,
        },
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
        operation_id="auth.passkey.login_finish",
        summary=(
            "Verify the passkey assertion, issue a session cookie, "
            "return the authenticating user id"
        ),
        openapi_extra={
            "x-cli": {
                "group": "auth",
                "verb": "passkey-login-finish",
                "summary": ("Verify a passkey assertion and issue a session cookie"),
                "mutates": True,
                "hidden": True,
            },
            "x-interactive-only": True,
        },
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
            #
            # cd-qx1f: the lockout short-circuits **before** any DB
            # read, so the challenge row is untouched and we do NOT
            # burn it here. A legitimate user rate-limited for 10
            # minutes can still finish with the same challenge once
            # the bucket drains (within its 10-min TTL). Every
            # branch below DOES burn the challenge because the
            # domain verified-and-failed — single-use even on
            # failure per §03 "WebAuthn specifics".
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
            # Clone detection is the rare case that warrants a session
            # invalidation, two audit rows (cloned_detected +
            # login_rejected), a challenge burn, AND a hard-delete of
            # the credential itself (cd-cx19). Every step runs on its
            # own fresh UoW because the primary UoW rolls back on the
            # raise; an in-primary-UoW write would disappear with it
            # and leave the suspected-stolen credential live — the
            # worst-of-both-worlds posture §15 forbids.
            #
            # Ordering (all fresh-UoW, each independent):
            #   1. session invalidation (cause clone_detected)
            #   2. passkey.cloned_detected audit (domain detection)
            #   3. passkey.login_rejected audit (uniform 401 trail)
            #   4. challenge row burn (cd-qx1f; single-use on failure)
            #   5. credential hard-delete + passkey.auto_revoked audit
            #      (cd-cx19; the "auto-revoke on rollback" half of §15)
            # Failure of any one is log-and-continue — the 401 must
            # land regardless.
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
            # cd-qx1f: single-use even on failure. Idempotent —
            # zero-rows-affected is fine if another caller beat us.
            _delete_challenge_fresh_uow(body.challenge_id)
            # cd-cx19: hard-delete the credential + emit
            # passkey.auto_revoked. Responsibilities are split from
            # the invalidate above — this helper does NOT touch
            # sessions. A subsequent login ceremony against the same
            # credential id will miss the row and hit the
            # InvalidLoginAttempt branch (unknown credential shape)
            # rather than CloneDetected again.
            _auto_revoke_credential_fresh_uow(
                credential_id_b64=exc.credential_id_b64,
                reason="clone_detected",
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
            # cd-qx1f: single-use even on failure. ``ChallengeNotFound``
            # naturally lands in a no-op delete (row already gone);
            # the other four burn the row so a leaked id can't be
            # replayed until TTL. Idempotent.
            _delete_challenge_fresh_uow(body.challenge_id)
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
