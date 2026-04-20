"""CSRF double-submit middleware + helpers.

Cross-Site Request Forgery defence for the authenticated SPA surface.
Per §03 "Sessions" and §15 "Cookies":

* Every response sets a ``crewday_csrf`` cookie carrying a fresh
  random token (``Secure; SameSite=Strict; Path=/``; **not**
  ``HttpOnly`` — the browser's JS client must be able to read it to
  populate the matching header; **no** ``__Host-`` prefix because the
  CSRF token carries no authority by itself).
* Every non-idempotent request (non ``GET``/``HEAD``/``OPTIONS``) must
  echo the cookie's current value as the ``X-CSRF`` header. The
  middleware compares cookie vs header; a mismatch short-circuits to
  ``403 csrf_mismatch`` before the router ever sees the request.

**Why double-submit and not a stateful CSRF token.** The double-
submit pattern is stateless — no DB read, no per-session storage —
and relies on the same-origin policy: an attacker at a third-party
origin cannot read the victim's ``crewday_csrf`` cookie (same-site
restriction), and therefore cannot mint the matching header. The
``SameSite=Strict`` flag on the CSRF cookie doubles as a belt: the
browser refuses to send the cookie on cross-site requests at all,
even if the attacker guesses the right URL shape.

**Skip paths.** Reuses :data:`app.tenancy.middleware.SKIP_PATHS` and
its child-segment match so the bare-host ops probes
(``/healthz``, ``/readyz``, ``/version``), auth entry points
(``/auth/magic``, ``/auth/passkey``, ``/signup``, ``/login``,
``/recover``), OpenAPI docs, and static assets stay callable from
tools that don't carry the cookie (health checkers, agent scripts
hitting ``/api/v1`` with a bearer token — which has its own auth
shape, not a cookie session).

**Exposed helpers:**

* :class:`CSRFMiddleware` — the :class:`BaseHTTPMiddleware` subclass
  that enforces the pair.
* :func:`verify_csrf` — synchronous check a router can call when it
  wants to gate a specific endpoint outside the middleware (e.g. a
  route mounted before the middleware chain).
* :data:`CSRF_COOKIE_NAME`, :data:`CSRF_HEADER_NAME`, :data:`CSRF_TOKEN_BYTES`
  — constants the router layer or templates can import instead of
  inlining the strings.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.tenancy.middleware import SKIP_PATHS

__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "CSRF_TOKEN_BYTES",
    "IDEMPOTENT_METHODS",
    "CSRFMiddleware",
    "mint_csrf_token",
    "verify_csrf",
]


# ---------------------------------------------------------------------------
# Constants — spec-pinned
# ---------------------------------------------------------------------------


# Cookie name (§03 "Sessions" / §15 "Cookies"). No ``__Host-`` prefix
# because the token carries no authority on its own — it's matched
# against the inbound header, not trusted standalone.
CSRF_COOKIE_NAME: Final[str] = "crewday_csrf"

# Header the SPA must echo on non-idempotent requests.
CSRF_HEADER_NAME: Final[str] = "X-CSRF"

# 24 bytes → 192-bit random token, same shape as the session cookie.
# Overkill for CSRF (32 bits would suffice for a per-session token),
# but the shared constant keeps the audit-log entropy story simple.
CSRF_TOKEN_BYTES: Final[int] = 24

# Methods the middleware treats as "read-only": no header required,
# no cookie check. ``TRACE`` is deliberately absent — every modern
# framework rejects it at the ASGI layer anyway, and letting it
# through the CSRF gate would be one more footgun for a future
# reverse-proxy misconfig to trip on.
IDEMPOTENT_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def mint_csrf_token() -> str:
    """Return a fresh random CSRF token.

    Exposed publicly so the sign-in flow (first cookie emission) can
    mint one without importing :mod:`secrets` itself — keeps the
    token-shape contract in one place.
    """
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def _is_skip_path(path: str) -> bool:
    """Return ``True`` if ``path`` should bypass the CSRF check.

    Mirrors the tenancy middleware's skip rule (exact match or
    ``/prefix/...`` child segment). We deliberately re-derive the
    check here instead of importing the private tenancy helper so the
    two middlewares stay decoupled — a future change to one set
    shouldn't silently widen or narrow the other. If the two lists
    diverge in intent, split this constant out of
    :mod:`app.tenancy.middleware` and let both import from there.
    """
    if path in SKIP_PATHS:
        return True
    return any(path.startswith(f"{prefix}/") for prefix in SKIP_PATHS)


def verify_csrf(request: Request) -> bool:
    """Return ``True`` if the ``X-CSRF`` header matches the cookie.

    Constant-time comparison to avoid exposing a timing oracle on the
    token; :func:`secrets.compare_digest` is the standard-library
    seam for this.

    Returns ``False`` when either the header or the cookie is
    missing, so a caller using this helper outside the middleware
    gets a uniform "rejected" signal without branching on the
    specific failure mode.
    """
    header = request.headers.get(CSRF_HEADER_NAME)
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if not header or not cookie:
        return False
    return secrets.compare_digest(header, cookie)


def _csrf_mismatch_response() -> JSONResponse:
    """Return the canonical 403 shape for a CSRF failure.

    Error symbol is ``csrf_mismatch`` — the §12 REST error vocabulary
    uses bare snake_case symbols without a nested ``error`` wrapper
    on the top-level body, but every existing middleware in this
    codebase emits ``{"detail": "..."}`` so we match that shape to
    avoid a special case in the error-handler middleware.
    """
    return JSONResponse(
        status_code=403,
        content={"detail": "csrf_mismatch"},
    )


def _build_csrf_cookie(value: str) -> str:
    """Return the ``Set-Cookie`` header value for the CSRF cookie.

    ``Secure; SameSite=Strict; Path=/``, no ``HttpOnly`` (JS must
    read the cookie to populate the matching header), no ``Domain``
    (defensive — we don't want subdomain surfaces sharing the token),
    no ``Max-Age`` / ``Expires`` (session cookie lifetime — browsers
    clear it on tab close, and the middleware re-mints on every
    response anyway).
    """
    return f"{CSRF_COOKIE_NAME}={value}; Secure; SameSite=Strict; Path=/"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class CSRFMiddleware(BaseHTTPMiddleware):
    """Enforce the double-submit CSRF pair + mint the cookie on responses.

    Responsibilities:

    1. **Skip paths** — bare-host ops / auth / static routes are
       passed through unchanged (the SKIP_PATHS union with the
       tenancy middleware's own list, see :func:`_is_skip_path`).
    2. **Idempotent methods** — ``GET``/``HEAD``/``OPTIONS`` skip the
       cookie-vs-header check but still re-mint the cookie on the
       response so the client always has a fresh value.
    3. **Mutation methods** — ``POST``/``PUT``/``PATCH``/``DELETE``
       (and anything else) must carry a ``X-CSRF`` header whose value
       equals the ``crewday_csrf`` cookie. Missing either one, or a
       mismatch, short-circuits to ``403 csrf_mismatch``.
    4. **Cookie freshness** — every response gets a freshly-minted
       ``crewday_csrf`` value. A client that somehow lost the cookie
       (incognito tab refresh, DevTools wipe) recovers on the next
       safe request without a round-trip to ``/auth``.

    The middleware is stateless; a single instance is safe to register
    once on the FastAPI app. It does **not** read the DB and does
    **not** depend on a :class:`WorkspaceContext` — CSRF defence runs
    orthogonal to tenancy.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        skip = _is_skip_path(path)
        method = request.method.upper()

        if not skip and method not in IDEMPOTENT_METHODS and not verify_csrf(request):
            # Refresh the cookie on the rejection response too, so a
            # legitimate caller whose cookie raced the page load gets
            # a matching token in time for their retry.
            resp = _csrf_mismatch_response()
            resp.headers.append("Set-Cookie", _build_csrf_cookie(mint_csrf_token()))
            return resp

        response = await call_next(request)

        # Always emit a fresh cookie on the way out, even on skipped
        # paths — a user who lands on ``/login`` (a skip path) must
        # still receive the token so the subsequent authenticated POST
        # can be verified.
        response.headers.append("Set-Cookie", _build_csrf_cookie(mint_csrf_token()))
        return response
