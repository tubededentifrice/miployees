"""Passkey registration HTTP routers.

Two routers are exposed:

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

Handlers are intentionally thin: unpack the body, call the domain
service under the request's Unit-of-Work, shape the response. The
UoW (see :func:`app.api.deps.db_session`) owns the transaction
boundary — domain code never calls ``session.commit()`` (§01
"Key runtime invariants" #3).

See ``docs/specs/03-auth-and-tokens.md`` §"WebAuthn specifics",
§"Self-serve signup" step 3, §"Additional passkeys".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.auth.passkey import (
    ChallengeAlreadyConsumed,
    ChallengeExpired,
    ChallengeNotFound,
    ChallengeSubjectMismatch,
    InvalidRegistration,
    PasskeyCredentialRef,
    RegistrationOptions,
    TooManyPasskeys,
    register_finish,
    register_finish_signup,
    register_start,
    register_start_signup,
)
from app.tenancy import WorkspaceContext

__all__ = ["router", "signup_router"]


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
