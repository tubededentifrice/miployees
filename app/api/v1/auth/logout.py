"""``POST /api/v1/auth/logout`` — invalidate the caller's session cookie.

Bare-host route, tenant-agnostic. The SPA's :mod:`useAuth.logout` hits
this after the user clicks "Sign out"; a 204 response teardown pairs
with a ``Set-Cookie`` header that clears the ``__Host-crewday_session``
cookie, driving the local :mod:`authStore` back to the unauthenticated
state.

**Best-effort semantics.** The endpoint returns 204 in every non-5xx
outcome:

* Valid session cookie → the underlying session row is marked
  invalidated via :func:`app.auth.session.invalidate_for_user` with
  cause ``"logout"`` (see §15 "Session-invalidation causes") and a
  ``audit.session.invalidated`` row lands. The clearing Set-Cookie is
  always emitted.
* No cookie / invalid cookie / expired cookie → no audit row is
  written (nothing happened server-side), but the clearing Set-Cookie
  is still emitted so the client drops whatever stale value it had.

Only a 5xx (DB unreachable, etc.) propagates as an error — anything
else would let a stolen-but-stale cookie keep the client stuck in an
authenticated shell the server can't see.

**Cookie clear shape (§15 "Cookies").** The clear header reuses
:func:`app.auth.session_cookie.build_session_cookie` with an empty
value and a 1970 expiry so the ``Max-Age=0`` / ``Expires`` pair lands
exactly like every other session-cookie emission. Any change to the
§15 flag set lives in that one file; this module never re-derives it.

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions",
``docs/specs/12-rest-api.md`` §"Auth", and
``docs/specs/15-security-privacy.md`` §"Cookies" /
§"Session-invalidation causes".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Response, status
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.auth import session as auth_session
from app.auth.session_cookie import (
    DEV_SESSION_COOKIE_NAME,
    build_session_cookie,
)

__all__ = ["build_logout_router"]


_Db = Annotated[Session, Depends(db_session)]

# Epoch-ish timestamp used as the clear-cookie's ``expires_at``. Any
# aware past UTC datetime works — :func:`build_session_cookie` clamps
# ``Max-Age`` to zero and emits the matching ``Expires`` in IMF-fixdate
# form. Pinning this to a constant (rather than ``datetime.now()``)
# keeps the emitted header byte-for-byte deterministic across calls
# so smoke tests can compare against a fixed string.
_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)

# Cause stamped on invalidated session rows + carried in the audit
# diff. Matches the §15 "Session-invalidation causes" catalogue.
_LOGOUT_CAUSE: str = "logout"


def build_logout_router() -> APIRouter:
    """Return the router that serves ``POST /api/v1/auth/logout``.

    Factory shape matches every other auth router in this package so
    the app factory's wiring seam stays uniform and tests can mount
    the endpoint against an isolated FastAPI instance.
    """
    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``auth`` stays for fine-grained client filtering.
    router = APIRouter(prefix="/auth", tags=["identity", "auth"])

    @router.post(
        "/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="auth.logout",
        summary="Invalidate the caller's session and clear the session cookie",
        openapi_extra={
            # Logout is a session-teardown ceremony: it only makes
            # sense for an interactive caller holding the session
            # cookie the endpoint is about to clear. Bearer tokens
            # (PATs, delegated) have no cookie surface to drop and
            # their lifecycle is managed via the token revoke
            # endpoints (§12), so the CLI generator would emit a verb
            # that could never do anything useful — mark ``hidden``.
            # ``x-interactive-only`` satisfies §12's "mutating route"
            # rule for the same reason: the caller MUST be a session
            # to have anything to log out from.
            "x-cli": {
                "group": "auth",
                "verb": "logout",
                "summary": "Invalidate the caller's session",
                "mutates": True,
                "hidden": True,
            },
            "x-interactive-only": True,
        },
    )
    def post_logout(
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
        """Invalidate the caller's session (best-effort) and clear the cookie.

        The handler always returns 204 with a clearing Set-Cookie
        header — a missing, invalid, or expired cookie still ends up
        with the client's cookie cleared, matching the SPA's
        expectation that any non-401 response is success. Only a 5xx
        (DB unreachable, constraint violation mid-invalidate) escapes
        as an error.
        """
        cookie_value = session_cookie_primary or session_cookie_dev

        if cookie_value:
            # Best-effort: resolve the cookie → session → user and
            # invalidate every active session for that user with
            # cause="logout". Invalid / expired cookies land here as
            # :class:`SessionInvalid` / :class:`SessionExpired`; we
            # silently drop those (the cookie clear still goes out)
            # rather than surfacing 401, because the SPA's state is
            # already being reset and a 401 would add noise without
            # changing the outcome.
            #
            # We validate (not ``get``) so sliding-refresh + the
            # fingerprint gate still run with their usual semantics —
            # a fingerprint mismatch, for example, already cut the
            # session at the security layer and we just need to clear
            # the cookie here. :func:`validate` also rejects
            # already-invalidated rows, so a double-click logout
            # gracefully no-ops on the second call.
            try:
                user_id = auth_session.validate(
                    session,
                    cookie_value=cookie_value,
                )
            except (auth_session.SessionInvalid, auth_session.SessionExpired):
                user_id = None

            if user_id is not None:
                auth_session.invalidate_for_user(
                    session,
                    user_id=user_id,
                    cause=_LOGOUT_CAUSE,
                )

        # Always emit the clear-cookie header, even on the no-cookie
        # path: a client that ended up here without a cookie has
        # nothing to drop, but the header is a no-op for them and
        # keeps the response shape uniform. secure=True matches the
        # login emission — a dev deployment that uses the plain
        # ``crewday_session`` cookie name will ignore the Secure
        # attribute on HTTP, and the dev cookie clears naturally on
        # the next reload; a dedicated dev-branch clear is a follow-up
        # if the friction ever actually shows up.
        clear_header = build_session_cookie(
            cookie_value="",
            expires_at=_EPOCH,
        )
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.headers.append("set-cookie", clear_header)
        return response

    return router
