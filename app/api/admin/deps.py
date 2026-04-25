"""FastAPI deps for the deployment-scoped admin tree.

Mounted at ``/admin/api/v1/...`` (§12 "Admin surface"), the admin
tree authorises its callers via *one of two* principal arms:

1. A passkey session whose user holds an active
   ``role_grant`` row with ``scope_kind='deployment'`` (resolved
   through :func:`app.authz.deployment_admin.is_deployment_admin`).
   Holds the full :data:`~app.tenancy.deployment.DEPLOYMENT_SCOPE_CATALOG`.
2. A deployment-scoped API token (§03 "API tokens"):
   * a ``scoped`` row whose ``scope_json`` carries one or more
     ``deployment:*`` keys (§12 family);
   * a ``delegated`` row whose delegating user is a deployment
     admin — the agent inherits the human's deployment grants.

Mixing ``deployment:*`` keys with workspace scopes on a single
``scoped`` token is a 422 ``deployment_scope_conflict`` per §12 —
the dep returns the typed error so every downstream admin route
shares one rejection envelope.

**404, not 403.** Spec §12 "Admin surface": *"the surface does not
advertise its own existence to tenants."* Every "not authorised"
shape (no auth material, session for a non-admin, scoped token with
only workspace scopes, …) collapses into the canonical
``{"error": "not_found"}`` 404 envelope. The 422 mixed-scope path
is the **only** non-404 rejection — it is reachable only when the
caller already proved knowledge of a token row whose existence is
deployment-side, so leaking "this row exists" does not enumerate
tenant data.

The dep does **not** authorise *individual* admin actions; that is
the per-route :func:`require_deployment_scope` factory's job, which
asserts a specific ``deployment:*`` key sits in
:attr:`DeploymentContext.deployment_scopes`. Session-principal
admins always pass; token-principal admins are bounded by the row's
scope set.

See ``docs/specs/12-rest-api.md`` §"Admin surface" and
``docs/specs/03-auth-and-tokens.md`` §"API tokens".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Final

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.auth import session as auth_session
from app.auth import tokens as auth_tokens
from app.auth.session_cookie import DEV_SESSION_COOKIE_NAME
from app.authz.deployment_admin import is_deployment_admin
from app.tenancy import (
    DEPLOYMENT_SCOPE_CATALOG,
    DEPLOYMENT_SCOPE_PREFIX,
    DeploymentContext,
)

__all__ = [
    "DEPLOYMENT_SCOPE_CONFLICT_ERROR",
    "current_deployment_admin_principal",
    "require_deployment_scope",
]


# Canonical error code for the 422 mixed-scope rejection. §12
# "Admin surface" pins the spelling; surfaced as a module constant
# so the dep + tests + future router copy share one literal.
DEPLOYMENT_SCOPE_CONFLICT_ERROR: Final[str] = "deployment_scope_conflict"


# Bearer-token header prefix. RFC 6750 — case-insensitive on the
# scheme word, exactly one space before the token. We compare with
# ``.lower()`` so a caller sending ``BEARER`` or ``bearer`` both
# resolve.
_BEARER_PREFIX: Final[str] = "bearer "


_Db = Annotated[Session, Depends(db_session)]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _not_found() -> HTTPException:
    """Return the canonical 404 ``HTTPException`` for the admin surface.

    Wraps the spec §12 "the surface does not advertise its own
    existence to tenants" envelope — ``{"error": "not_found"}``.
    The exception handler in :mod:`app.api.errors` projects this
    onto the RFC 7807 problem+json shape with ``type=not_found``,
    ``status=404``, matching the constant-time response the
    workspace tenancy middleware also emits.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found"},
    )


def _scope_conflict() -> HTTPException:
    """Return the canonical 422 ``HTTPException`` for a mixed-scope token.

    Triggered when a ``scoped`` token's ``scope_json`` carries both a
    ``deployment:*`` key and at least one non-``deployment:*`` key.
    Distinct from the 404 wall because the caller has already proved
    knowledge of a real token row — leaking "this row exists" does
    not enumerate tenant data, and a typed 422 is the only signal
    that lets the operator fix the misconfigured mint.
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "error": DEPLOYMENT_SCOPE_CONFLICT_ERROR,
            "message": (
                "deployment-scoped tokens must not mix deployment:* with "
                "workspace scopes"
            ),
        },
    )


def _client_headers(request: Request) -> tuple[str, str]:
    """Return ``(ua, accept_language)`` for :func:`auth_session.validate`.

    Mirrors :func:`app.api.v1.auth.me._client_headers`. Empty strings
    are tolerated by :func:`validate` (the fingerprint gate is
    skipped on a missing header pair); the HTTP edge passes through
    whatever the browser sent so the gate fires when both headers
    are present.
    """
    return (
        request.headers.get("user-agent", ""),
        request.headers.get("accept-language", ""),
    )


def _resolve_session_principal(
    session: Session,
    *,
    request: Request,
    cookie_value: str,
) -> DeploymentContext | None:
    """Try to build a session-principal :class:`DeploymentContext`.

    Returns ``None`` when the cookie is unknown, expired, or the
    user has no active deployment grant — the caller wraps that into
    the canonical 404. Raised exceptions
    (:class:`SessionInvalid` / :class:`SessionExpired`) are caught
    here so the dep keeps a flat ``None``-or-context return shape;
    the caller does the 404 mapping in one place.

    Session-principal admins carry the full
    :data:`DEPLOYMENT_SCOPE_CATALOG` because the spec collapses every
    fine-grained admin capability onto the
    ``scope_kind='deployment'`` grant in v1. A future split into
    fine-grained deployment groups would narrow this here without
    changing the dep's caller contract.
    """
    ua, accept_language = _client_headers(request)
    try:
        user_id = auth_session.validate(
            session,
            cookie_value=cookie_value,
            ua=ua,
            accept_language=accept_language,
        )
    except (auth_session.SessionInvalid, auth_session.SessionExpired):
        return None
    if not is_deployment_admin(session, user_id=user_id):
        return None
    # ``hash_cookie_value`` re-derives the session row's PK without a
    # second DB read — same idiom the workspace tenancy middleware
    # uses (see :func:`app.tenancy.middleware.resolve_actor`).
    session_id = auth_session.hash_cookie_value(cookie_value)
    return DeploymentContext(
        principal=session_id,
        user_id=user_id,
        actor_kind="user",
        deployment_scopes=DEPLOYMENT_SCOPE_CATALOG,
    )


def _split_token_scopes(
    raw: object,
) -> tuple[frozenset[str], frozenset[str]]:
    """Split a token's ``scope_json`` into deployment / non-deployment keys.

    The DB column is a free-form JSON value; on the happy path it's a
    ``{key: True}`` mapping, but defensive handling keeps the
    classifier honest if a future mint shape lands an unexpected
    container. A non-mapping returns two empty sets — the caller
    interprets that as "no deployment scopes" and 404s.
    """
    if not isinstance(raw, dict):
        return frozenset(), frozenset()
    deployment: set[str] = set()
    other: set[str] = set()
    for key in raw:
        if not isinstance(key, str):
            # A non-string key is an upstream invariant violation; treat
            # it as a non-deployment scope so the dep never elevates an
            # unparseable row.
            other.add(repr(key))
            continue
        if key.startswith(DEPLOYMENT_SCOPE_PREFIX):
            deployment.add(key)
        else:
            other.add(key)
    return frozenset(deployment), frozenset(other)


def _resolve_token_principal(
    session: Session,
    *,
    bearer: str,
) -> DeploymentContext | None:
    """Try to build a token-principal :class:`DeploymentContext`.

    Returns ``None`` for every "not a deployment admin" path so the
    caller maps a single 404 envelope:

    * malformed / unknown / revoked / expired token;
    * delegated / personal / archived-user liveness failures (we
      treat all of them as 404 here because the admin surface is
      invisible to tenants — a typed 401 would itself enumerate);
    * scoped token with no ``deployment:*`` keys at all;
    * delegated token whose delegating user is **not** a deployment
      admin.

    **Raises** :class:`HTTPException` with a 422
    ``deployment_scope_conflict`` body when a ``scoped`` token mixes
    ``deployment:*`` keys with workspace scopes — the only non-404
    rejection. The check requires verifying the token first (so we
    only leak 422 to a caller who already proved knowledge of the
    secret), matching the precedent set by the workspace tokens
    router for ``me_scope_conflict``.

    Delegated tokens are accepted on the deployment surface when the
    delegating user is a deployment admin; the agent inherits the
    human's grants and we stamp the full
    :data:`DEPLOYMENT_SCOPE_CATALOG` on the context to mirror the
    session-principal arm.
    """
    try:
        verified = auth_tokens.verify(session, token=bearer)
    except (
        auth_tokens.InvalidToken,
        auth_tokens.TokenExpired,
        auth_tokens.TokenRevoked,
        auth_tokens.DelegatingUserArchived,
        auth_tokens.SubjectUserArchived,
    ):
        return None

    # Personal access tokens cannot reach the deployment surface —
    # §03 reserves them for ``me:*`` scopes and §12 admits only
    # ``scoped`` + ``delegated`` shapes. Defensive collapse to 404.
    if verified.kind == "personal":
        return None

    if verified.kind == "delegated":
        # The agent inherits the delegating human's authority — the
        # human must be a deployment admin for the token to admit on
        # the admin surface. ``delegate_for_user_id`` is guaranteed
        # populated for this kind by :func:`auth_tokens.mint`'s shape
        # validators; a defensive ``None`` check keeps the dep honest
        # against a hand-edited row.
        delegating_user_id = verified.delegate_for_user_id
        if delegating_user_id is None:
            return None
        if not is_deployment_admin(session, user_id=delegating_user_id):
            return None
        return DeploymentContext(
            principal=verified.key_id,
            user_id=delegating_user_id,
            actor_kind="delegated",
            deployment_scopes=DEPLOYMENT_SCOPE_CATALOG,
        )

    # Remaining branch: ``kind == "scoped"``. Walk ``scope_json``
    # and split deployment-prefixed keys from the rest.
    deployment_scopes, other_scopes = _split_token_scopes(verified.scopes)

    # No deployment keys at all → 404 (workspace-only token probing
    # the admin surface). Pure deployment keys → admit. Mixed →
    # raise 422 ``deployment_scope_conflict`` per §12.
    if not deployment_scopes:
        return None
    if other_scopes:
        raise _scope_conflict()
    return DeploymentContext(
        principal=verified.key_id,
        user_id=verified.user_id,
        actor_kind="agent",
        deployment_scopes=deployment_scopes,
    )


# ---------------------------------------------------------------------------
# Public deps
# ---------------------------------------------------------------------------


def current_deployment_admin_principal(
    request: Request,
    session: _Db,
    session_cookie_primary: Annotated[
        str | None,
        Cookie(alias=auth_session.SESSION_COOKIE_NAME),
    ] = None,
    session_cookie_dev: Annotated[
        str | None,
        Cookie(alias=DEV_SESSION_COOKIE_NAME),
    ] = None,
) -> DeploymentContext:
    """Resolve the caller to a :class:`DeploymentContext` or 404.

    Resolution order:

    1. ``Authorization: Bearer <token>`` — pass to
       :func:`_resolve_token_principal`. A 422
       ``deployment_scope_conflict`` short-circuits here for a
       mixed-scope token; every other failure path returns ``None``
       and the dep falls through to the session arm.
    2. Session cookie — pass to :func:`_resolve_session_principal`.
    3. Otherwise → 404 ``not_found``.

    The bearer arm runs before the session arm so an agent that
    sends both (a session cookie tagged onto a programmatic curl, a
    delegated token the SPA happens to forward) is authorised by the
    token's authority — same precedent as the workspace tenancy
    middleware (see :func:`app.tenancy.middleware.resolve_actor`).
    The 422 path therefore reaches the wire even when a fallback
    session would have admitted; that's intentional, the token is
    misconfigured and we want the operator to see the typed error.

    The ``Authorization`` header is read off ``request.headers``
    directly rather than via a ``Header(...)`` dep — the lookup is
    case-insensitive (Starlette's :class:`Headers` already is) and
    keeps the dep usable from programmatic test calls that pin a
    raw header without going through FastAPI's parameter binding.
    """
    auth_header = request.headers.get("authorization")
    if auth_header is not None and auth_header.lower().startswith(_BEARER_PREFIX):
        token_value = auth_header[len(_BEARER_PREFIX) :].strip()
        if token_value:
            ctx = _resolve_token_principal(session, bearer=token_value)
            if ctx is not None:
                return ctx
            # Fall through to the session arm — a malformed / unknown
            # bearer with a co-sent session cookie should still admit
            # if the cookie is valid. The 422 path raised above would
            # have already short-circuited; reaching here means the
            # token failed for a 404-mapped reason.

    cookie_value = session_cookie_primary or session_cookie_dev
    if cookie_value:
        ctx = _resolve_session_principal(
            session, request=request, cookie_value=cookie_value
        )
        if ctx is not None:
            return ctx

    raise _not_found()


def require_deployment_scope(
    scope_name: str,
) -> Callable[[DeploymentContext], DeploymentContext]:
    """Return a FastAPI dep that asserts ``scope_name`` is granted.

    Pair with :func:`current_deployment_admin_principal` on a route
    to gate an action on a specific ``deployment:*`` key:

    .. code-block:: python

        @router.get(
            "/llm/providers",
            dependencies=[Depends(require_deployment_scope("deployment.llm:read"))],
        )

    Session-principal admins always pass — their context carries the
    full :data:`DEPLOYMENT_SCOPE_CATALOG`. Token-principal admins
    pass iff their context's :attr:`deployment_scopes` includes
    ``scope_name``.

    A miss collapses to the same canonical 404 the dep emits for
    "not an admin at all" — surface-invisibility extends to "wrong
    scope" too, per spec §12. A 403 here would tell an authenticated
    agent which scopes it does *not* hold; 404 keeps the matrix
    flat.
    """

    def _check(
        ctx: Annotated[DeploymentContext, Depends(current_deployment_admin_principal)],
    ) -> DeploymentContext:
        if scope_name not in ctx.deployment_scopes:
            raise _not_found()
        return ctx

    return _check
