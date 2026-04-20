"""Session-cookie header builder — spec-pinned chokepoint.

One module, one function: :func:`build_session_cookie`. Every code
path that emits a ``Set-Cookie: __Host-crewday_session=...`` header
goes through here so the §15 "Cookies" flag set is enforced in
exactly one place.

**Why a dedicated module.** The domain service
(:mod:`app.auth.session`) owns the session row lifecycle; the HTTP
router owns the response. The cookie header is the seam between the
two. Keeping the builder in its own file means future surfaces (CLI
sign-in, delegated token exchange, ...) reuse the exact same flag
string without re-deriving it, and a bug in the flag set is a
one-file change instead of N call sites.

Flag set enforced (spec §03 "Sessions" / §15 "Cookies"):

* Name: ``__Host-crewday_session`` (secure mode) or
  ``crewday_session`` (dev opt-out — see ``secure=False`` below).
* ``Secure`` — required by the ``__Host-`` prefix; browsers drop a
  prefixed cookie that lacks it.
* ``HttpOnly`` — no JS access, containing the blast radius of a
  shared-origin XSS on a sibling route.
* ``SameSite=Lax`` — defeats CSRF on navigation while still allowing
  the SPA's own fetches (same-origin) and external bookmark opens.
* ``Path=/`` — required by the ``__Host-`` prefix.
* **NO** ``Domain`` attribute — required by the ``__Host-`` prefix;
  emitting one makes the browser refuse the cookie silently.
* ``Max-Age`` + ``Expires`` — both, to tolerate the handful of
  browsers that honour only one (see §15 "Cookies").

**``secure=False`` opt-out.** For local HTTP dev loops (no TLS
terminator in front of uvicorn) the operator can flip ``secure=False``;
we drop the ``__Host-`` prefix in that case and use the bare
``crewday_session`` name, because the prefix requires ``Secure``.
A warning is logged on every build so the operator can't miss it.
**Production deployments MUST never flip this** — the bind guard
(``docs/specs/16``) and CI both refuse a public-interface deploy
without TLS in front.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Final

from app.util.clock import SystemClock

__all__ = [
    "DEV_SESSION_COOKIE_NAME",
    "SESSION_COOKIE_NAME",
    "build_session_cookie",
]


# Cookie names — spec §15 "Cookies". The ``__Host-`` prefix pins the
# cookie to the exact origin that Set-it (no ``Domain`` allowed,
# ``Secure`` required, ``Path=/`` required). Violating any of those
# makes the browser refuse the cookie silently.
SESSION_COOKIE_NAME: Final[str] = "__Host-crewday_session"

# Dev-only fallback when ``secure=False`` is explicitly chosen by the
# operator (plain-HTTP loopback). Drops the ``__Host-`` prefix because
# the prefix requires ``Secure``; everything else still applies.
DEV_SESSION_COOKIE_NAME: Final[str] = "crewday_session"


_log = logging.getLogger(__name__)


def build_session_cookie(
    cookie_value: str,
    expires_at: datetime,
    *,
    secure: bool = True,
    path: str = "/",
    samesite: str = "Lax",
    domain: str | None = None,
) -> str:
    """Return a spec-compliant ``Set-Cookie`` header value.

    Defaults to the production shape — ``__Host-crewday_session=<v>;
    Secure; HttpOnly; SameSite=Lax; Path=/``. ``Max-Age`` + ``Expires``
    are derived from ``expires_at``.

    ``secure=False`` is a **dev-only** escape hatch: a bare
    ``crewday_session`` cookie (no ``__Host-`` prefix) is emitted
    without the ``Secure`` attribute so a plain-HTTP loopback workflow
    can still round-trip the cookie. Logs a WARNING on every build so
    the operator can't miss it.

    ``domain`` is always refused — the ``__Host-`` prefix (secure mode)
    forbids it, and the dev fallback stays consistent. A caller that
    genuinely needs a shared-subdomain cookie should use a different
    name shape and justify the relaxation in a PR.

    ``path`` and ``samesite`` are parameterised for future flexibility
    (e.g. narrowing to ``/admin`` for an admin-only cookie), but every
    current call site passes the defaults. The ``__Host-`` prefix
    requires ``Path=/``; a non-default path with ``secure=True`` is
    rejected.

    Raises :class:`ValueError` on:

    * ``Domain=`` supplied (forbidden by ``__Host-``);
    * ``secure=True`` with ``path != "/"`` (``__Host-`` requires
      ``Path=/``);
    * a naive :class:`datetime` (no ``tzinfo``) — round-tripping
      through ``strftime`` below needs an aware value.
    """
    if domain is not None:
        # Emitting ``Domain=`` would force the browser to reject the
        # cookie (under the ``__Host-`` prefix) or silently narrow its
        # scope (dev fallback). Neither is what a caller meant; fail
        # loud so the bug surfaces at emit time, not at browser time.
        raise ValueError(
            "session cookies never carry a Domain attribute; "
            "the __Host- prefix forbids it and the dev fallback "
            "stays consistent."
        )

    if secure and path != "/":
        raise ValueError(
            f"__Host- cookie requires Path=/, got {path!r}; "
            "either accept the default or drop secure=False for dev."
        )

    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be an aware datetime; got naive input")

    if not secure:
        # Dev-only path. The ``__Host-`` prefix requires ``Secure``, so
        # we drop the prefix and log a warning so the operator can't
        # miss it. Production deployments must never hit this branch —
        # the bind guard (§16) refuses a public-interface deploy
        # without TLS.
        _log.warning(
            "build_session_cookie: secure=False — falling back to the "
            "dev cookie name %r without Secure. This is plain-HTTP "
            "loopback only; a production deploy hitting this branch is "
            "a blocker bug.",
            DEV_SESSION_COOKIE_NAME,
        )
        cookie_name = DEV_SESSION_COOKIE_NAME
    else:
        cookie_name = SESSION_COOKIE_NAME

    # IMF-fixdate (RFC 7231 §7.1.1.1) — day-name, day, month-name,
    # year, hh:mm:ss, "GMT". ``strftime`` yields the right byte shape
    # once the datetime is in UTC.
    expiry_utc = expires_at.astimezone(UTC)
    imf = expiry_utc.strftime("%a, %d %b %Y %H:%M:%S GMT")

    # ``Max-Age`` belt-and-braces with ``Expires``: browsers that
    # dislike clock skew prefer one over the other; emitting both
    # keeps the cookie behaviour consistent across stacks. We sample
    # a fresh UTC "now" via :class:`SystemClock` because callers into
    # a header builder rarely have a :class:`Clock` handy, and the
    # value is seconds-precision — sub-second drift is immaterial.
    max_age = max(0, int((expiry_utc - SystemClock().now()).total_seconds()))

    attrs: list[str] = [f"{cookie_name}={cookie_value}"]
    if secure:
        attrs.append("Secure")
    attrs.extend(
        [
            "HttpOnly",
            f"SameSite={samesite}",
            f"Path={path}",
            f"Max-Age={max_age}",
            f"Expires={imf}",
        ]
    )
    return "; ".join(attrs)
