"""Personal-access-token HTTP router — mint / list / revoke.

Mounted at bare host ``/api/v1/me/tokens`` (tenant-agnostic — PATs
live outside any workspace, §03 "Personal access tokens"). Every
route requires an authenticated **passkey session**; a PAT cannot
create another PAT (§03 guardrails "no transitive creation from
another token") and a delegated token cannot either — the router
relies on the session cookie dep chain that :mod:`me` already uses.

Routes:

* ``POST /me/tokens`` ``{label, scopes, expires_at_days?}`` →
  ``201 {token, key_id, prefix, expires_at, kind='personal'}``.
  The plaintext ``token`` is returned **only on this response**.
  The router applies the spec's 90-day PAT default TTL when
  ``expires_at_days`` is omitted. Scopes MUST all start with
  ``me.``; an empty scope set returns 422 ``scopes_required``
  and a workspace scope mixed in returns 422
  ``me_scope_conflict`` (§03 "Personal access tokens").
* ``GET /me/tokens`` → list of :class:`TokenSummaryResponse`
  projections. Returns every PAT owned by the session user
  (active + revoked), matching the workspace /tokens page's
  revocation history convention.
* ``DELETE /me/tokens/{token_id}`` → 204. Revokes the row iff it
  belongs to the session user AND is a PAT; unknown / foreign /
  workspace-token ids all collapse to 404 ``token_not_found`` per
  §03 ("we don't leak whose tokens exist").

Error shapes:

* 401 ``session_required`` — no session cookie.
* 401 ``session_invalid`` — cookie is unknown, expired, or fingerprint
  gate fired.
* 404 ``token_not_found`` — revoke targets an id that isn't a live
  PAT for the caller.
* 422 ``too_many_personal_tokens`` — 6th PAT attempted for the user.
* 422 ``scopes_required`` — body carried no scopes.
* 422 ``me_scope_conflict`` — body carried a scope outside ``me:*``.

See ``docs/specs/03-auth-and-tokens.md`` §"Personal access tokens"
and ``docs/specs/14-web-frontend.md`` §"Personal access tokens"
(``/me`` panel).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.auth import session as auth_session
from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME
from app.auth.tokens import (
    PERSONAL_DEFAULT_TTL_DAYS,
    PERSONAL_SCOPE_PREFIX,
    InvalidToken,
    MintedToken,
    TokenKind,
    TokenShapeError,
    TokenSummary,
    TooManyPersonalTokens,
    list_personal_tokens,
    mint,
    revoke_personal,
)
from app.util.clock import SystemClock

__all__ = [
    "MintPersonalTokenBody",
    "MintPersonalTokenResponse",
    "TokenSummaryResponse",
    "build_me_tokens_router",
]


_Db = Annotated[Session, Depends(db_session)]

# Spec §03 "Guardrails": same 10-year safety bound as the workspace
# tokens router — defensive against a typo producing a far-future
# datetime the DB / client can't render.
_MAX_TTL_DAYS: int = 365 * 10


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class MintPersonalTokenBody(BaseModel):
    """Request body for ``POST /api/v1/me/tokens``.

    ``scopes`` is a flat ``{"me.action_key": true}`` mapping — every
    key MUST start with ``me.`` per §03 "Personal access tokens".
    ``expires_at_days`` overrides the 90-day default.
    """

    label: str = Field(..., min_length=1, max_length=160)
    scopes: dict[str, Any] = Field(default_factory=dict)
    expires_at_days: int | None = Field(default=None, ge=1, le=_MAX_TTL_DAYS)


class MintPersonalTokenResponse(BaseModel):
    """Response body for ``POST /api/v1/me/tokens`` — plaintext shown once.

    :attr:`kind` is always ``'personal'`` but we carry it explicitly
    so the ``MintTokenResponse`` shapes line up across the two token
    routers and a smart client can consume either with one decoder.
    """

    token: str
    key_id: str
    prefix: str
    expires_at: datetime | None
    kind: TokenKind


class TokenSummaryResponse(BaseModel):
    """Response element for ``GET /api/v1/me/tokens``.

    Mirrors :class:`app.auth.tokens.TokenSummary` but omits the
    workspace-side discriminator fields (``delegate_for_user_id``)
    because this surface only serves PATs. ``subject_user_id`` is
    also omitted — it's always the session user; surfacing it would
    be redundant noise.
    """

    key_id: str
    label: str
    prefix: str
    scopes: dict[str, Any]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    kind: TokenKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_session_user(
    session: Session,
    *,
    cookie_primary: str | None,
    cookie_dev: str | None,
) -> str:
    """Return the authenticated user's id or raise HTTP 401.

    Both the prod ``__Host-crewday_session`` and the dev fallback
    ``crewday_session`` are accepted — matches the pattern used by
    :mod:`app.api.v1.auth.me`. No fingerprint / UA hints are plumbed
    through on v1 because the PAT router is itself only reachable
    from an authenticated SPA session that already passed the
    fingerprint gate on its last validate; a stricter gate here is
    tracked as cd-i1qe-me-tokens-fingerprint.
    """
    cookie_value = cookie_primary or cookie_dev
    if not cookie_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_required"},
        )
    try:
        return auth_session.validate(session, cookie_value=cookie_value)
    except (auth_session.SessionInvalid, auth_session.SessionExpired) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "session_invalid"},
        ) from exc


def _resolve_expires_at(body: MintPersonalTokenBody, now: datetime) -> datetime:
    """Return the concrete ``expires_at`` for a PAT mint request."""
    days = (
        body.expires_at_days
        if body.expires_at_days is not None
        else PERSONAL_DEFAULT_TTL_DAYS
    )
    return now + timedelta(days=days)


def _summary_to_response(summary: TokenSummary) -> TokenSummaryResponse:
    """Translate the domain projection to the wire shape."""
    return TokenSummaryResponse(
        key_id=summary.key_id,
        label=summary.label,
        prefix=summary.prefix,
        scopes=dict(summary.scopes),
        expires_at=summary.expires_at,
        last_used_at=summary.last_used_at,
        revoked_at=summary.revoked_at,
        created_at=summary.created_at,
        kind=summary.kind,
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_me_tokens_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` for the identity-scoped PAT surface.

    Factory shape matches every other auth router in the package so
    the app factory's wiring seam stays uniform and tests can mount
    the endpoint against an isolated FastAPI instance.
    """
    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``auth`` + ``tokens`` stay for fine-grained client filtering.
    router = APIRouter(
        prefix="/me/tokens",
        tags=["identity", "auth", "tokens"],
    )

    @router.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=MintPersonalTokenResponse,
        operation_id="auth.me.tokens.mint",
        summary="Mint a personal access token — plaintext returned once",
        openapi_extra={
            # Bare-host personal tokens live under a DISTINCT CLI
            # group from the workspace-scoped ``tokens`` surface
            # (``app/api/v1/auth/tokens.py``) — both would otherwise
            # collide on ``(group=tokens, verb=create)``. The runtime
            # (cd-lato) registers at most one Click command per
            # ``(group, verb)`` pair, so the heuristic's natural
            # collision has to be broken explicitly here.
            "x-cli": {
                "group": "me-tokens",
                "verb": "create",
                "summary": "Mint a personal access token (me:* scopes)",
                "mutates": True,
            },
        },
    )
    def post_me_token(
        body: MintPersonalTokenBody,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> MintPersonalTokenResponse:
        """Create a PAT for the session user, limited to the ``me:*`` scopes."""
        user_id = _resolve_session_user(
            session,
            cookie_primary=session_cookie_primary,
            cookie_dev=session_cookie_dev,
        )

        if not body.scopes:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "scopes_required",
                    "message": "personal access tokens require at least one me:* scope",
                },
            )
        # Router-level scope validation ahead of the service layer so
        # the error code matches §03's taxonomy exactly. A scope key
        # that does not start with ``me.`` is a 422 me_scope_conflict —
        # mixing workspace + PAT scopes is the bug we want the UI to
        # surface with its own copy.
        for key in body.scopes:
            if not key.startswith(PERSONAL_SCOPE_PREFIX):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "me_scope_conflict",
                        "message": (
                            f"personal access tokens accept only me:* scopes "
                            f"— got {key!r}"
                        ),
                    },
                )

        now = SystemClock().now()
        expires_at = _resolve_expires_at(body, now)

        try:
            result: MintedToken = mint(
                session,
                None,
                user_id=user_id,
                label=body.label,
                scopes=body.scopes,
                expires_at=expires_at,
                kind="personal",
                subject_user_id=user_id,
                now=now,
            )
        except TooManyPersonalTokens as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "too_many_personal_tokens",
                    "message": str(exc),
                },
            ) from exc
        except TokenShapeError as exc:
            # Belt-and-braces: the router's own scope-family gate
            # above already caught the common cases, but the domain
            # layer re-checks and a mismatch here means the router's
            # pre-check missed an invariant. Collapse to a generic
            # 422 so the caller still gets a typed error.
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_token_shape",
                    "message": str(exc),
                },
            ) from exc

        return MintPersonalTokenResponse(
            token=result.token,
            key_id=result.key_id,
            prefix=result.prefix,
            expires_at=result.expires_at,
            kind=result.kind,
        )

    @router.get(
        "",
        response_model=list[TokenSummaryResponse],
        operation_id="auth.me.tokens.list",
        summary="List every personal access token the caller owns",
        openapi_extra={
            "x-cli": {
                "group": "me-tokens",
                "verb": "list",
                "summary": "List your personal access tokens",
                "mutates": False,
            },
        },
    )
    def get_me_tokens(
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> list[TokenSummaryResponse]:
        """Return every PAT (active + revoked) for the session user."""
        user_id = _resolve_session_user(
            session,
            cookie_primary=session_cookie_primary,
            cookie_dev=session_cookie_dev,
        )
        summaries = list_personal_tokens(session, subject_user_id=user_id)
        return [_summary_to_response(s) for s in summaries]

    @router.delete(
        "/{token_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="auth.me.tokens.revoke",
        summary="Revoke one of the caller's personal access tokens",
        openapi_extra={
            "x-cli": {
                "group": "me-tokens",
                "verb": "revoke",
                "summary": "Revoke one of your personal access tokens",
                "mutates": True,
            },
        },
    )
    def delete_me_token(
        token_id: str,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias=DEV_SESSION_COOKIE_NAME),
        ] = None,
    ) -> Response:
        """Revoke a PAT owned by the session user. Idempotent."""
        user_id = _resolve_session_user(
            session,
            cookie_primary=session_cookie_primary,
            cookie_dev=session_cookie_dev,
        )
        try:
            revoke_personal(session, token_id=token_id, subject_user_id=user_id)
        except InvalidToken as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "token_not_found"},
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
