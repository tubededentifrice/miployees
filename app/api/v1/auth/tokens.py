"""API-token HTTP router — mint / list / revoke.

Mounted at ``/w/<slug>/api/v1/auth/tokens`` inside the workspace-scoped
tree (the v1 app factory, cd-ika7, wires the prefix). Every route
requires an authenticated session plus the ``api_tokens.manage``
action permission on the workspace scope (§05 action catalog —
default-allow: owners + managers, root-protected-deny).

This router handles the **workspace-pinned** token kinds: the
original ``scoped`` tokens (cd-c91) and the ``delegated`` tokens
(cd-i1qe). Personal access tokens are identity-scoped and live at
the bare host on ``/api/v1/me/tokens`` — see
:mod:`app.api.v1.auth.me_tokens`.

Routes:

* ``POST /auth/tokens`` → ``201 {token, key_id, prefix, expires_at,
  kind}``. Two shapes:

  * **Scoped** (default): ``{label, scopes, expires_at_days?}``.
    ``scopes`` is the flat ``{"action_key": true}`` shape §03 pins;
    default TTL 90 days (§03 "Guardrails").
  * **Delegated**: ``{label, delegate: true, expires_at_days?,
    scopes: {}}``. The session user's id populates
    ``delegate_for_user_id``; scopes MUST be empty (§03 "Delegated
    tokens"); default TTL 30 days.

  The plaintext ``token`` is returned **only on this response**;
  never again.

* ``GET /auth/tokens`` → list of :class:`TokenSummary` projections.
  Returns both active and revoked scoped / delegated rows — the
  ``/tokens`` UI shows both sections. Personal tokens are excluded
  per §03.
* ``DELETE /auth/tokens/{token_id}`` → 204. Flips ``revoked_at``;
  idempotent for already-revoked rows. An unknown / foreign /
  personal ``token_id`` returns 404 (same shape — we don't leak
  whose tokens exist).

Error shapes:

* 401 ``not_authenticated`` — no session (via the dep chain).
* 403 ``permission_denied`` — action gate fired.
* 404 ``token_not_found`` — revoke against an unknown, foreign, or
  personal ``token_id``.
* 422 ``too_many_tokens`` — 6th scoped/delegated mint for the user
  on this workspace.
* 422 ``delegated_requires_empty_scopes`` — delegated mint with a
  non-empty ``scopes`` body.
* 422 ``me_scope_conflict`` — scoped mint with a ``me:*`` key in
  ``scopes``.
* 422 ``invalid_kind`` — body carried an unknown ``kind`` literal.

Handlers are intentionally thin: validate the body, call the domain
service inside the request's UoW, map typed errors onto HTTP
symbols. The spec's error vocabulary stays in one place so swapping
to RFC 7807 later (cd-waq3) is a single diff.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens",
``docs/specs/12-rest-api.md`` §"Auth / tokens", and
``docs/specs/15-security-privacy.md`` §"Token hashing".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.auth.tokens import (
    DELEGATED_DEFAULT_TTL_DAYS,
    SCOPED_DEFAULT_TTL_DAYS,
    InvalidToken,
    MintedToken,
    TokenKind,
    TokenShapeError,
    TokenSummary,
    TooManyTokens,
    list_tokens,
    mint,
    revoke,
)
from app.authz import Permission
from app.tenancy import WorkspaceContext
from app.util.clock import SystemClock

__all__ = [
    "MintTokenBody",
    "MintTokenResponse",
    "TokenSummaryResponse",
    "build_tokens_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


# Spec §03 "Guardrails": "A workspace-level setting can raise any of
# them to 'never' but emits a noisy warning in the UI." v1 doesn't
# ship the setting yet; we cap at a generous upper bound so a typo
# like ``expires_at_days: 99999999`` can't produce a datetime that
# overflows the DB column or the client's display. 10 years is
# comfortably above the "longest realistic agent token" and well
# under ``datetime``'s own bounds.
_MAX_TTL_DAYS = 365 * 10


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class MintTokenBody(BaseModel):
    """Request body for ``POST /auth/tokens``.

    ``scopes`` is a flat ``{"action_key": true}`` mapping for v1 —
    matches the :attr:`ApiToken.scope_json` column shape so the
    router doesn't have to translate between "list of strings"
    (§03 body example) and "dict" (schema). A later cd-c91 follow-up
    may accept the list shape for symmetry with the spec's JSON
    example; for now the dict form is the internal canonical.

    ``delegate`` (§03 "Delegated tokens") — when ``true``, the row
    is minted as a delegated token acting for the session user
    (``delegate_for_user_id``); ``scopes`` MUST be empty because
    authority resolves against the delegating user's grants. When
    ``false`` (default), the row is a classic scoped token.

    ``expires_at_days`` overrides the per-kind default (90 days for
    scoped, 30 days for delegated); ``None`` means "use the default".
    """

    label: str = Field(..., min_length=1, max_length=160)
    scopes: dict[str, Any] = Field(default_factory=dict)
    expires_at_days: int | None = Field(default=None, ge=1, le=_MAX_TTL_DAYS)
    delegate: bool = Field(
        default=False,
        description=(
            "When true, mint a delegated token whose authority inherits "
            "the session user's role_grants (§03). Scopes must be empty."
        ),
    )


class MintTokenResponse(BaseModel):
    """Response body for ``POST /auth/tokens`` — plaintext shown once.

    The plaintext ``token`` is NEVER returned again; the UI must
    surface the "shown only once — copy it now" warning alongside
    this response. :attr:`key_id` and :attr:`prefix` are stable
    identifiers the UI can show on subsequent list / audit views.
    ``kind`` echoes the domain discriminator so the ``/tokens`` UI
    can render the right "Copy this once" chrome without a follow-up
    fetch.
    """

    token: str
    key_id: str
    prefix: str
    expires_at: datetime | None
    kind: TokenKind


class TokenSummaryResponse(BaseModel):
    """Response element for ``GET /auth/tokens``.

    Mirrors :class:`app.auth.tokens.TokenSummary` on the wire. The
    ``hash`` column is **not** surfaced — the domain projection
    already omits it (see :class:`app.auth.tokens.TokenSummary`
    docstring). ``kind`` and ``delegate_for_user_id`` surface the
    cd-i1qe discriminator so the UI can flag delegated rows.
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
    delegate_for_user_id: str | None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _resolve_expires_at(body: MintTokenBody, now: datetime) -> datetime:
    """Return the concrete ``expires_at`` for a mint request.

    Applies the spec's per-kind default when the client omits
    ``expires_at_days`` (30 days for delegated, 90 days for scoped);
    otherwise clamps against :data:`_MAX_TTL_DAYS` (the Pydantic
    validator already rejects out-of-range values, so the clamp is
    defensive against a future schema change).
    """
    if body.expires_at_days is not None:
        days = body.expires_at_days
    elif body.delegate:
        days = DELEGATED_DEFAULT_TTL_DAYS
    else:
        days = SCOPED_DEFAULT_TTL_DAYS
    return now + timedelta(days=days)


def _summary_to_response(summary: TokenSummary) -> TokenSummaryResponse:
    """Translate the domain projection to the wire shape.

    Thin enough to inline, but extracted so the ``GET /tokens``
    handler stays a flat list-comprehension and a future schema
    evolution (e.g. adding ``last_used_ip_hash``) has one edit site.
    """
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
        delegate_for_user_id=summary.delegate_for_user_id,
    )


def build_tokens_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for workspace-scoped token ops.

    Factory shape so the v1 app factory (cd-ika7) can mount the
    router with shared :class:`Permission` dependencies once the
    rule repository lands. For v1 we use the module-level
    :func:`Permission` factory directly — ``rule_repo=None`` resolves
    to :class:`EmptyPermissionRuleRepository`, which is correct
    until the ``permission_rule`` table ships.

    Tests instantiate this directly with
    :class:`fastapi.testclient.TestClient`; the module-level
    :data:`router` is a thin wrapper for the app factory's eager
    import.
    """
    api = APIRouter(prefix="/auth/tokens", tags=["auth", "tokens"])

    permission_gate = Depends(Permission("api_tokens.manage", scope_kind="workspace"))

    @api.post(
        "",
        status_code=status.HTTP_201_CREATED,
        response_model=MintTokenResponse,
        summary="Mint a new API token — plaintext returned once",
        dependencies=[permission_gate],
    )
    def post_tokens(
        body: MintTokenBody,
        ctx: _Ctx,
        session: _Db,
    ) -> MintTokenResponse:
        """Create a scoped or delegated API token on this workspace.

        Branches on :attr:`MintTokenBody.delegate`:

        * ``delegate=false`` (default) — scoped token. ``scopes`` is
          the flat ``{"action_key": true}`` dict. Empty is allowed on
          v1; a cd-c91 follow-up may require non-empty scopes per
          the spec's "narrowest set possible" guidance.
        * ``delegate=true`` — delegated token acting for
          ``ctx.actor_id``. ``scopes`` MUST be empty (enforced at
          the domain layer and surfaced as 422
          ``delegated_requires_empty_scopes``).

        Per-kind shape errors from the domain service collapse into
        one 422 envelope whose ``error`` code varies by clause —
        that way the spec's error taxonomy lives in one place and
        the SPA's form-level messaging keys off the stable codes.
        """
        now = SystemClock().now()
        expires_at = _resolve_expires_at(body, now)

        kind: TokenKind = "delegated" if body.delegate else "scoped"

        try:
            result: MintedToken = mint(
                session,
                ctx,
                user_id=ctx.actor_id,
                label=body.label,
                scopes=body.scopes,
                expires_at=expires_at,
                kind=kind,
                delegate_for_user_id=(ctx.actor_id if kind == "delegated" else None),
                now=now,
            )
        except TooManyTokens as exc:
            # Starlette renamed the 422 constant in a recent release;
            # use the literal so the router works across minor versions.
            raise HTTPException(
                status_code=422,
                detail={"error": "too_many_tokens", "message": str(exc)},
            ) from exc
        except TokenShapeError as exc:
            # Shape errors map to the spec's error codes:
            # * delegated + non-empty scopes → ``delegated_requires_empty_scopes``
            # * scoped + me:* key → ``me_scope_conflict``
            # The domain layer raises a single error type with a
            # human message; we branch here by inspecting the
            # request shape rather than reparsing the message.
            if kind == "delegated" and body.scopes:
                code = "delegated_requires_empty_scopes"
            elif kind == "scoped" and any(k.startswith("me.") for k in body.scopes):
                code = "me_scope_conflict"
            else:
                code = "invalid_token_shape"
            raise HTTPException(
                status_code=422,
                detail={"error": code, "message": str(exc)},
            ) from exc
        return MintTokenResponse(
            token=result.token,
            key_id=result.key_id,
            prefix=result.prefix,
            expires_at=result.expires_at,
            kind=result.kind,
        )

    @api.get(
        "",
        response_model=list[TokenSummaryResponse],
        summary="List every token on this workspace (active + revoked)",
        dependencies=[permission_gate],
    )
    def get_tokens(
        ctx: _Ctx,
        session: _Db,
    ) -> list[TokenSummaryResponse]:
        """Return every token on the workspace, most recent first."""
        summaries = list_tokens(session, ctx)
        return [_summary_to_response(s) for s in summaries]

    @api.delete(
        "/{token_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Revoke a token — idempotent",
        dependencies=[permission_gate],
    )
    def delete_token(
        token_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Flip ``revoked_at`` on ``token_id``.

        Idempotent: revoking an already-revoked token still lands a
        ``revoked_noop`` audit row but returns 204 so the UI's
        "are you sure" → Revoke loop doesn't fail on a double-click.
        """
        try:
            revoke(session, ctx, token_id=token_id)
        except InvalidToken as exc:
            # §03 management-context error: 404 rather than 401,
            # because the caller is authenticated + authorised; they
            # just named a token that doesn't live on this workspace.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "token_not_found"},
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return api


# Module-level router for the v1 app factory's eager import. Tests
# that want a fresh instance per case should call
# :func:`build_tokens_router` directly to avoid cross-test leaks on
# FastAPI's dependency-override cache.
router = build_tokens_router()
