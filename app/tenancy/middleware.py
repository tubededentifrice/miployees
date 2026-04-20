"""FastAPI middleware that resolves ``/w/<slug>/...`` to a WorkspaceContext.

The middleware sits between the ASGI adapter and the routers. For every
request it either:

1. Matches a **bare-host skip path** (health probes, auth entry points,
   static assets, the SPA catch-all for ``/w/<slug>`` without a trailing
   segment, ...) and passes the request through without binding a
   :class:`~app.tenancy.WorkspaceContext`.
2. Parses ``/w/<slug>/...`` at the URL root, validates the slug via
   :func:`~app.tenancy.validate_slug`, builds a ``WorkspaceContext``
   and installs it on the request-scoped ContextVar.
3. Returns **404** for every rejection path (unknown slug, reserved
   slug, consecutive-hyphen slug, missing Phase-0 workspace header).
   Never **403** — per spec §01 "Workspace addressing": an enumerator
   gets the same shape as a non-member, keeping the enumeration surface
   flat.

**Phase 0 stub.** No real identity pipeline yet: an unmodeled request
cannot be attributed to a principal, so this layer fails closed. The
membership check is replaced in ``cd-9il`` once the identity /
``user_workspace`` resolver lands. Until then:

* ``X-Test-Workspace-Id`` is REQUIRED for every scoped request; its
  value is copied onto ``WorkspaceContext.workspace_id``.
* ``X-Test-Actor-Id`` is OPTIONAL; when absent the middleware mints a
  fresh ULID so downstream code has a stable actor reference.
* Every stubbed request is treated as a ``manager``-role user with
  ``actor_was_owner_member=False`` — the most permissive shape that
  still lets repository-level filters exercise real ``workspace_id``
  constraints.

See ``docs/specs/01-architecture.md`` §"Workspace addressing",
§"WorkspaceContext" and §"Tenant filter enforcement".

**Performance note.** Starlette's :class:`BaseHTTPMiddleware` wraps the
downstream app in a per-request task, which adds a small amount of
latency vs. a pure-ASGI implementation. For v1 this overhead is
acceptable — correctness + ergonomics beat the ~µs cost on our
request volumes. A future revisit can drop to pure ASGI if the
middleware stack grows.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.slug import InvalidSlug, validate_slug
from app.util.ulid import new_ulid

__all__ = [
    "CORRELATION_ID_HEADER",
    "SKIP_PATHS",
    "TEST_ACTOR_ID_HEADER",
    "TEST_WORKSPACE_ID_HEADER",
    "WorkspaceContextMiddleware",
]


# Outgoing + incoming correlation id header. Accepted from the client
# (echoed unchanged) and minted fresh otherwise. Case-insensitive at
# the HTTP layer; we normalise to this exact spelling on the response.
CORRELATION_ID_HEADER = "X-Request-Id"

# Phase-0 stub headers. cd-9il replaces these with real session /
# token extraction.
TEST_WORKSPACE_ID_HEADER = "X-Test-Workspace-Id"
TEST_ACTOR_ID_HEADER = "X-Test-Actor-Id"


# Bare-host skip paths, derived from ``docs/specs/01-architecture.md``
# §"Workspace addressing". A request is "skipped" iff its path equals
# one of these strings OR starts with one followed by ``/`` (a child
# segment). Keep this list in sync with the reverse-proxy routing
# table and the reserved-slug list in :mod:`app.tenancy.slug`.
SKIP_PATHS: frozenset[str] = frozenset(
    {
        # Ops probes + identity surface (§01 "Workspace addressing").
        "/healthz",
        "/readyz",
        "/version",
        "/signup",
        "/login",
        "/recover",
        "/select-workspace",
        # Bare-host OpenAPI + docs (§12 "Base URL").
        "/api/openapi.json",
        "/api/v1",
        "/docs",
        "/redoc",
        # Bare-host auth surface (§03 "Self-serve signup", §12). Both
        # magic-link and passkey routers live here; keep the siblings in
        # lock-step so future routers (webauthn/*) are obvious to add.
        "/auth/magic",
        "/auth/passkey",
        # Bare-host email-change landing (§14 "Public"). Carries a
        # magic-link token, has no workspace until the swap completes.
        "/me/email/verify",
        # Bare-host admin shell + API (§14 "Admin", §12 "Admin surface").
        "/admin",
        # Static assets + SPA chrome that the reverse proxy or FastAPI
        # may serve from the bare host (§14 "Shell chrome").
        "/static",
        "/assets",
        "/styleguide",
        "/unsupported",
    }
)


_log = logging.getLogger(__name__)


def _is_skip_path(path: str) -> bool:
    """Return ``True`` if ``path`` is a bare-host route we pass through.

    Matches either the exact skip-path value (``/healthz``) or a child
    segment rooted at it (``/static/app.css``, ``/docs/swagger.json``).
    Deliberately does NOT match a longer path that merely starts with
    the same characters (``/signup-flow`` is scoped, not a child of
    ``/signup``).
    """
    if path in SKIP_PATHS:
        return True
    # Child-segment check: longest skip-path is ``/select-workspace`` —
    # a single startswith-with-separator pass is cheap.
    return any(path.startswith(f"{prefix}/") for prefix in SKIP_PATHS)


def _is_bare_w_path(path: str) -> bool:
    """Return ``True`` if ``path`` is a bare-host ``/w`` SPA catch-all.

    Matches ``/w``, ``/w/``, ``/w/<slug>``, and ``/w/<slug>/`` — i.e.
    anything that does not have a **non-empty** segment after the
    slug. Those requests are served by the SPA's catch-all, never by a
    scoped route, so the middleware must not attempt slug resolution on
    them (a mis-parse here would surface as a spurious 404 on the
    workspace-picker screen).
    """
    if path in ("/w", "/w/"):
        return True
    # ``_parse_scoped_path`` requires at least one non-empty segment
    # after the slug; when that's missing, treat it as a bare SPA hit.
    segments = path.split("/")
    # "/w/villa-sud" → ['', 'w', 'villa-sud'] (len=3)
    # "/w/villa-sud/" → ['', 'w', 'villa-sud', ''] (len=4, last='')
    if len(segments) < 4:
        return segments[:2] == ["", "w"]
    if segments[:2] != ["", "w"]:
        return False
    # Len >= 4 but the third (and all later) segments are empty ⇒ bare.
    return all(s == "" for s in segments[3:])


def _parse_scoped_path(path: str) -> str | None:
    """Extract ``<slug>`` from ``/w/<slug>/<rest>`` or ``None``.

    Returns ``None`` when the path does not have a non-empty segment
    after the slug — callers upstream have already excluded skip paths
    and bare-``/w`` paths, so this is purely the final "does the URL
    look scoped?" check.
    """
    segments = path.split("/")
    if len(segments) < 4:
        return None
    if segments[0] != "" or segments[1] != "w":
        return None
    slug = segments[2]
    if slug == "":
        return None
    # Must have at least one non-empty segment after the slug.
    if not any(s != "" for s in segments[3:]):
        return None
    return slug


def _not_found() -> JSONResponse:
    """Return the canonical 404 shape.

    Spec §01 mandates 404 (never 403) on every rejection path so the
    response is indistinguishable from a non-member hitting a real
    workspace. Body kept generic for the same reason.
    """
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


def _log_tenancy_event(
    *,
    slug: str | None,
    workspace_id: str | None,
    actor_id: str | None,
    correlation_id: str,
    skip_path: bool,
) -> None:
    """Emit the structured ``tenancy.context`` log line.

    One call site per middleware branch — extracted so the log shape
    can only drift in one place. Aggregators filter on
    ``event=tenancy.context`` and can pivot on ``skip_path`` to separate
    bare-host traffic from scoped traffic.
    """
    _log.info(
        "tenancy.context",
        extra={
            "event": "tenancy.context",
            "slug": slug,
            "workspace_id": workspace_id,
            "actor_id": actor_id,
            "correlation_id": correlation_id,
            "skip_path": skip_path,
        },
    )


class WorkspaceContextMiddleware(BaseHTTPMiddleware):
    """Resolve ``/w/<slug>/...`` to a :class:`WorkspaceContext`.

    Binds the context via :func:`app.tenancy.current.set_current` for
    the downstream handler and guarantees cleanup (``reset_current``)
    in a ``finally`` so a crashed handler cannot leak tenancy state
    into the next request served by the same worker task.

    The middleware is stateless — it holds no DB sessions, caches
    nothing, and is safe to instantiate once per ASGI app.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or new_ulid()

        # 1) Bare-host skip paths (health, signup, static, docs, ...)
        #    and requests that aren't scoped ``/w/<slug>/...`` at all.
        slug = (
            None
            if (_is_skip_path(path) or _is_bare_w_path(path))
            else (_parse_scoped_path(path))
        )
        if slug is None:
            _log_tenancy_event(
                slug=None,
                workspace_id=None,
                actor_id=None,
                correlation_id=correlation_id,
                skip_path=True,
            )
            response = await call_next(request)
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response

        # 2) Scoped request — validate the slug, then resolve identity.
        try:
            validate_slug(slug)
        except InvalidSlug:
            # Unknown / reserved / pattern-failed slug — 404, not 403.
            _log_tenancy_event(
                slug=slug,
                workspace_id=None,
                actor_id=None,
                correlation_id=correlation_id,
                skip_path=False,
            )
            response = _not_found()
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response

        # --- Phase-0 stub boundary -----------------------------------
        # cd-9il replaces this block with a real slug → workspace
        # lookup + user_workspace membership check. Until then:
        #   - The ``X-Test-Workspace-Id`` header MUST be present. No
        #     header ⇒ 404 (indistinguishable from non-member).
        #   - Actor id falls back to a fresh ULID if the test header
        #     is omitted, so downstream code always sees a non-empty
        #     string.
        workspace_id = request.headers.get(TEST_WORKSPACE_ID_HEADER)
        if workspace_id is None:
            _log_tenancy_event(
                slug=slug,
                workspace_id=None,
                actor_id=None,
                correlation_id=correlation_id,
                skip_path=False,
            )
            response = _not_found()
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response

        actor_id = request.headers.get(TEST_ACTOR_ID_HEADER) or new_ulid()
        # --- end Phase-0 stub ----------------------------------------

        ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=slug,
            actor_id=actor_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id=correlation_id,
        )

        _log_tenancy_event(
            slug=slug,
            workspace_id=workspace_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
            skip_path=False,
        )

        token = set_current(ctx)
        try:
            response = await call_next(request)
        finally:
            # Always restore — even if the downstream handler raised —
            # so the ContextVar does not leak into the next request
            # served by the same worker task.
            reset_current(token)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
