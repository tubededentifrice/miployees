"""FastAPI application factory.

``create_app(settings)`` composes the process-wide ASGI app:

* structured JSON logging (§15 "Logging and redaction") is configured
  before anything else so the subsequent wiring steps are captured
  under the same handler + redaction filter;
* the public-interface bind guard (§15 "Binding policy", §16
  "Environment variables") refuses to start on ``0.0.0.0`` unless
  :attr:`~app.config.Settings.allow_public_bind` is explicitly set;
* the middleware stack installs CORS, security headers,
  :class:`~app.tenancy.middleware.WorkspaceContextMiddleware`, and
  :class:`~app.auth.csrf.CSRFMiddleware` (§01 "web.*");
* the bare-host auth entry points + workspace-scoped ``/w/<slug>``
  sub-tree (§01 "Workspace addressing", §12 "REST API") are mounted;
* ``/healthz``, ``/readyz``, ``/version`` are the unconditional ops
  probes (§16 "Healthchecks");
* the SPA seam depends on :attr:`Settings.profile`: the ``prod`` path
  mounts ``app/web/dist/`` as :class:`StaticFiles` with a catch-all
  that returns ``index.html`` for any non-API GET (§14 "SPA
  fallback"); the ``dev`` path installs an HTTP proxy to the Vite
  dev server at :attr:`Settings.vite_dev_url` so HMR keeps working
  while an engineer edits ``app/web/src/`` (cd-q1be).

The factory is deliberately the single seam between ``Settings`` and
every other module. Tests pass a pinned :class:`Settings` via
``create_app(settings=...)`` so no test touches process env.

See ``docs/specs/01-architecture.md`` §"High-level picture",
§"Component responsibilities"; ``docs/specs/16-deployment-operations.md``
§"Environment variables", §"Healthchecks";
``docs/specs/15-security-privacy.md`` §"Binding policy",
§"HTTP security headers".
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

from app.adapters.mail.ports import Mailer
from app.adapters.mail.smtp import SMTPMailer
from app.api.health import router as health_router
from app.api.v1.auth import invite as invite_module
from app.api.v1.auth import magic as magic_module
from app.api.v1.auth import passkey as passkey_module
from app.api.v1.auth import recovery as recovery_module
from app.api.v1.auth import signup as signup_module
from app.api.v1.auth import tokens as tokens_module
from app.api.v1.users import build_users_router
from app.auth._throttle import Throttle
from app.auth.csrf import CSRFMiddleware
from app.capabilities import Capabilities
from app.capabilities import probe as probe_capabilities
from app.config import Settings, get_settings
from app.security import BindGuardError, assert_bind_allowed
from app.tenancy.middleware import WorkspaceContextMiddleware
from app.util.logging import setup_logging

__all__ = ["PublicBindRefused", "create_app"]

_log = logging.getLogger(__name__)

# Package name we look up via :mod:`importlib.metadata`. Matches
# ``pyproject.toml`` ``[project].name`` — kept as a module constant so a
# rename lands in one place.
_PACKAGE_NAME: Final[str] = "crewday"

# Fallback emitted by ``/version`` when the package isn't installed (e.g.
# running straight from a source checkout under pytest's default
# rootdir, without ``pip install -e .``). We prefer a clear sentinel over
# a crash because ``/version`` is probed in smoke tests.
_UNKNOWN_VERSION: Final[str] = "0.0.0+unknown"

# Prod-profile SPA build directory. Resolved against the repo root
# (two levels up from this file). Phase-0 used a ``mocks/web/dist``
# fallback; cd-q1be retires that now that ``app/web/`` carries the
# production build verbatim — falling back to the mocks would mask a
# failed prod build behind a stale demo bundle, which is exactly the
# failure mode we want to surface loudly.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_SPA_DIST: Final[Path] = _REPO_ROOT / "app" / "web" / "dist"

# Minimal fallback served when the SPA build is missing. 200 OK so
# health probes + smoke curls succeed; a distinctive body so a human
# hitting ``/`` understands why they don't see the app.
_SPA_STUB_HTML: Final[str] = (
    "<!doctype html><html><head><title>crewday</title></head>"
    "<body><h1>SPA not built — run pnpm build in app/web</h1></body></html>"
)

# Baseline security headers (§15 "HTTP security headers"). The
# CSP-nonce + full permissions policy is cd-wv2v's scope; here we
# install the static headers that are safe to apply on every route.
_SECURITY_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self'"
    ),
    # HSTS is only honoured over HTTPS; shipping it on every response is
    # safe — browsers ignore it on plaintext — and saves a conditional
    # branch here. ``includeSubDomains`` is deliberately omitted: a self-
    # hosted deployment can live under a subdomain of a shared apex.
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class PublicBindRefused(RuntimeError):
    """Raised when ``create_app()`` is asked to run on a refused bind.

    The caller (uvicorn entrypoint, tests, composition root) gets a
    loud failure mode — this is a configuration bug, not something
    the process should recover from. Wraps the underlying
    :class:`~app.security.BindGuardError` so :mod:`app.main`'s caller
    can keep catching the narrower, ``app.main``-scoped type without
    reaching into the security module.
    """


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Append the baseline security headers to every response.

    A full CSP with per-request nonces lands in cd-wv2v; until then
    this middleware installs the static header set from §15 "HTTP
    security headers" so the skeleton is in place. Each header is
    set only when the downstream handler didn't already emit one, so
    a future per-route override (e.g. the demo mode's relaxed
    ``frame-ancestors``) can simply pre-populate the value.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


def _resolve_version() -> str:
    """Return the installed package version or a clear sentinel.

    Wrapped so the ``/version`` handler stays trivial and we have a
    single place to swap in a git-sha / OpenAPI-hash payload once
    cd-leif lands the full build-metadata surface.
    """
    try:
        return pkg_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        return _UNKNOWN_VERSION


def _enforce_bind_guard(settings: Settings) -> None:
    """Delegate to :func:`app.security.assert_bind_allowed`.

    Spec §15 "Binding policy": loopback always passes; an address on
    an interface whose name matches :attr:`Settings.trusted_interfaces`
    passes; ``0.0.0.0`` / ``::`` and any other address require
    :attr:`Settings.allow_public_bind`. A refusal propagates as
    :class:`PublicBindRefused` so callers keep a single, ``app.main``-
    scoped exception to pattern on.
    """
    try:
        assert_bind_allowed(
            settings.bind_host,
            settings.bind_port,
            trusted_globs=list(settings.trusted_interfaces),
            allow_public=settings.allow_public_bind,
        )
    except BindGuardError as exc:
        raise PublicBindRefused(str(exc)) from exc


def _resolve_spa_dist() -> Path | None:
    """Return the prod SPA dist directory if it carries an index, else ``None``.

    Only ``app/web/dist`` is considered — the Phase-0 ``mocks/web/dist``
    fallback was retired in cd-q1be so a missing prod build surfaces as
    the stub banner instead of silently serving a demo bundle. Tests
    that need to assert the stub path swap :data:`_SPA_DIST` for a
    non-existent path.
    """
    if (_SPA_DIST / "index.html").is_file():
        return _SPA_DIST
    return None


def _is_api_path(path: str) -> bool:
    """Return ``True`` if ``path`` looks like an API request.

    An API 404 must stay JSON so agent / CLI callers get a parseable
    body; the SPA catch-all deliberately does **not** handle these
    paths. Anything under ``/api/`` or ``/w/<slug>/...`` counts —
    including ``/api`` exactly.
    """
    if path == "/api" or path.startswith("/api/"):
        return True
    # ``/w`` bare is an SPA route (the workspace picker); only
    # ``/w/<slug>/<sub>`` carries the scoped API tree.
    if path.startswith("/w/"):
        segments = [s for s in path.split("/") if s]
        # ['w', slug, 'api', ...] or deeper. Under ``/w/<slug>`` only
        # ``api/...`` is the JSON API; everything else is SPA chrome.
        return len(segments) >= 3 and segments[2] == "api"
    return False


def _wire_services(
    app: FastAPI, settings: Settings
) -> tuple[Mailer | None, Throttle, Capabilities]:
    """Instantiate process-wide services and stash them on ``app.state``.

    The throttle is always constructed (it's pure in-memory state).
    The mailer is ``None`` when SMTP isn't configured — the signup /
    magic / recovery routers each raise :class:`RuntimeError` on the
    first request in that case, which is the right failure mode for
    a deployment that forgot to set ``CREWDAY_SMTP_*``. Capabilities
    is probed without a DB session; the mutable subset is refreshed
    on the first readyz probe that actually opens a UoW.
    """
    throttle = Throttle()
    capabilities = probe_capabilities(settings)
    mailer: Mailer | None = None
    if settings.smtp_host is not None and settings.smtp_from is not None:
        mailer = SMTPMailer(
            host=settings.smtp_host,
            port=settings.smtp_port,
            from_addr=settings.smtp_from,
            user=settings.smtp_user,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
            timeout=settings.smtp_timeout,
            bounce_domain=settings.smtp_bounce_domain,
        )
    app.state.settings = settings
    app.state.throttle = throttle
    app.state.capabilities = capabilities
    app.state.mailer = mailer
    return mailer, throttle, capabilities


def _mount_routers(
    app: FastAPI,
    *,
    settings: Settings,
    mailer: Mailer | None,
    throttle: Throttle,
    capabilities: Capabilities,
) -> None:
    """Attach every v1 router onto ``app``.

    Bare-host routers (signup, magic-link, recovery, invite, passkey
    signup / login) live at ``/api/v1/...``; workspace-scoped routers
    nest under ``/w/{slug}/api/v1/...`` and run behind the tenancy
    middleware. Routers that need a mailer are skipped when SMTP is
    not configured — each one would raise on first use anyway, but
    skipping the mount keeps the OpenAPI surface honest.
    """
    bare_prefix = "/api/v1"
    scoped_prefix = "/w/{slug}/api/v1"

    # --- Bare-host auth routers (§03 "Self-serve signup", §12) ---
    app.include_router(passkey_module.signup_router, prefix=bare_prefix)
    app.include_router(
        passkey_module.build_login_router(throttle=throttle, settings=settings),
        prefix=bare_prefix,
    )
    # Invite accept does NOT need SMTP — redeeming a magic link the
    # operator mailed out-of-band is still valid in an SMTP-less
    # deployment. Keep this mount unconditional so the SPA's
    # ``/invite/...`` flow survives ``CREWDAY_SMTP_HOST`` being unset.
    app.include_router(
        invite_module.build_invite_router(
            throttle=throttle,
            settings=settings,
        ),
        prefix=bare_prefix,
    )
    if mailer is not None:
        app.include_router(
            signup_module.build_signup_router(
                mailer=mailer,
                throttle=throttle,
                capabilities=capabilities,
                settings=settings,
            ),
            prefix=bare_prefix,
        )
        app.include_router(
            magic_module.build_magic_router(
                mailer=mailer,
                throttle=throttle,
                settings=settings,
            ),
            prefix=bare_prefix,
        )
        app.include_router(
            recovery_module.build_recovery_router(
                mailer=mailer,
                throttle=throttle,
                settings=settings,
            ),
            prefix=bare_prefix,
        )

    # --- Workspace-scoped routers (§12 "Base URL") ---
    # The tenancy middleware binds the :class:`WorkspaceContext` from
    # the path's ``{slug}`` — the FastAPI prefix placeholder is the
    # contract between the two. Routes inside each router are
    # relative (e.g. ``/auth/tokens``), so the final path is
    # ``/w/{slug}/api/v1/auth/tokens``.
    app.include_router(tokens_module.build_tokens_router(), prefix=scoped_prefix)
    if mailer is not None:
        app.include_router(
            build_users_router(
                mailer=mailer,
                throttle=throttle,
                settings=settings,
            ),
            prefix=scoped_prefix,
        )

    # Workspace-scoped passkey register (authenticated "add another
    # passkey" flow). Reused verbatim — the router requires a live
    # :class:`WorkspaceContext` which the tenancy middleware installs.
    app.include_router(passkey_module.router, prefix=scoped_prefix)


def _register_ops_routes(app: FastAPI) -> None:
    """Mount the ops-probe router (§16 "Healthchecks").

    Delegates every probe shape to :mod:`app.api.health`. Each route
    lives in :data:`~app.tenancy.middleware.SKIP_PATHS` so the
    tenancy middleware passes the request through without slug
    resolution; ``/healthz`` additionally never touches the DB
    (see :mod:`app.api.health` docstring).
    """
    app.include_router(health_router)


def _register_spa_catch_all(app: FastAPI) -> None:
    """Mount the SPA static assets + fallback route (prod profile).

    Assets under ``/assets/*`` are served by
    :class:`~starlette.staticfiles.StaticFiles` when the dist dir
    exists. Every other GET falls through to the catch-all below,
    which returns ``index.html`` so client-side routing handles deep
    links; API prefixes are deliberately excluded so a missing
    ``/api/...`` route returns a JSON 404 instead of HTML.

    A missing ``app/web/dist/`` is logged at WARNING so operators
    notice — the factory still boots and the catch-all serves
    :data:`_SPA_STUB_HTML` so ``/healthz`` + API routes stay usable
    during a failed build.
    """
    dist = _resolve_spa_dist()

    if dist is None:
        # Use underscore separators in event + path keys: the JSON-log
        # redaction filter treats any three dot-separated word chunks
        # as a JWT and masks the value.
        _log.warning(
            "SPA build missing; serving stub banner",
            extra={
                "event": "spa_dist_missing",
                "expected_path": str(_SPA_DIST),
            },
        )
    else:
        assets_dir = dist / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=str(assets_dir)),
                name="assets",
            )

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_catch_all(full_path: str) -> Response:
        """Serve ``index.html`` for any non-API GET.

        ``full_path`` is the match FastAPI captures — empty on the
        root ``/``. API prefixes are already peeled off by the earlier
        routers; we defensively re-check via :func:`_is_api_path` here
        so a bad API path returns a JSON 404 rather than leaking the
        SPA shell.
        """
        path = "/" + full_path
        if _is_api_path(path):
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "detail": None},
            )

        if dist is not None:
            # Favicon / manifest / top-level static file served directly.
            if full_path:
                candidate = (dist / full_path).resolve()
                # ``resolve()`` canonicalises traversal attempts; the
                # subsequent ``relative_to`` check rejects anything
                # that escaped the dist root. Without this a crafted
                # ``../etc/passwd`` would read arbitrary files.
                try:
                    candidate.relative_to(dist.resolve())
                except ValueError:
                    pass
                else:
                    if candidate.is_file():
                        return FileResponse(candidate)

            index = dist / "index.html"
            if index.is_file():
                return FileResponse(index)

        return HTMLResponse(content=_SPA_STUB_HTML, status_code=200)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Compose and return the process-wide FastAPI application.

    Pass ``settings`` in tests and alternate composition roots to
    avoid the lru_cached :func:`~app.config.get_settings` singleton.
    Production (uvicorn ``--factory`` invocation) lets the default
    ``None`` fall through to :func:`~app.config.get_settings`.
    """
    cfg = settings if settings is not None else get_settings()

    # Logging must land before anything else emits — subsequent import-
    # time side effects (pydantic validation errors, SMTP config
    # warnings, capability probes) deserve to show up in the JSON
    # stream with the redaction filter already installed.
    setup_logging(level=cfg.log_level)

    _enforce_bind_guard(cfg)

    app = FastAPI(
        title="crewday",
        version=_resolve_version(),
        # The OpenAPI surface lives at ``/api/openapi.json`` per §12
        # "Base URL"; the default ``/openapi.json`` would shadow an
        # SPA route and confuse CDN caching rules.
        openapi_url="/api/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Middleware is applied OUTER → INNER at request time. FastAPI's
    # ``add_middleware`` prepends to ``user_middleware``, so the LAST
    # call lands outermost. The desired chain (reads top-down as "first
    # thing to see the request, last thing to see the response"):
    #
    # 1. CORS — reject cross-origin preflights before any stateful work.
    # 2. SecurityHeaders — stamp every response, even middleware rejects.
    # 3. WorkspaceContextMiddleware — bind the tenancy ctx for routers.
    # 4. CSRFMiddleware — double-submit check on mutation verbs.
    #
    # To get that layout we register INNER → OUTER: CSRF first (ends up
    # innermost), CORS last (ends up outermost). CORS defaults to
    # same-origin only (§15 "HTTP security headers"): agent callers
    # hit ``/api/v1/...`` with a bearer token and no browser origin,
    # and a mis-set wildcard here would be a privacy regression.
    # Dev work behind a separate Vite origin can populate
    # ``CREWDAY_CORS_ALLOW_ORIGINS`` with an explicit list.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(WorkspaceContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_allow_origins),
        allow_origin_regex=None,
        allow_credentials=False,
        allow_methods=["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF", "X-Request-Id"],
    )

    mailer, throttle, capabilities = _wire_services(app, cfg)

    _register_ops_routes(app)
    _mount_routers(
        app,
        settings=cfg,
        mailer=mailer,
        throttle=throttle,
        capabilities=capabilities,
    )
    # SPA seam MUST register last — its ``/{full_path:path}`` pattern
    # (static catch-all in prod, Vite proxy in dev) would otherwise
    # swallow every subsequent route. The ``dev`` proxy is imported
    # lazily so the prod hot path never pays for the ``httpx`` seam.
    if cfg.profile == "dev":
        from app.api.proxy import register_vite_proxy

        register_vite_proxy(app, vite_dev_url=cfg.vite_dev_url)
    else:
        _register_spa_catch_all(app)

    # Worker hook — cd-pnbn / cd-3p3z formalise APScheduler wiring
    # (crons, iCal pollers, digest emails). Until then we only
    # record the mode so operations can assert the factory read the
    # env var. An ``external`` deployment runs the worker in its own
    # process; ``internal`` would in-process today once the
    # scheduler lands.
    _log.info(
        "worker mode resolved",
        extra={"event": "worker.mode", "mode": cfg.worker},
    )

    return app
