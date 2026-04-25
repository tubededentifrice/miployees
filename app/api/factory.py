"""FastAPI application factory — composition root.

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
* the 13 per-context routers declared in
  :data:`app.api.v1.CONTEXT_ROUTERS` are mounted under
  ``/w/{slug}/api/v1/<ctx>``, and the admin tree
  (:data:`app.api.admin.admin_router`) under ``/admin/api/v1``;
* RFC 7807 ``problem+json`` exception handlers
  (:func:`app.api.errors.add_exception_handlers`) are registered
  after routers so every surface shares the §12 "Errors" envelope;
* a custom OpenAPI generator emits a 3.1 document with one tag per
  context (§12 "OpenAPI");
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

``app.main`` is a thin re-export shim that forwards :func:`create_app`
and :class:`PublicBindRefused` here (cd-ika7). All existing imports
(``from app.main import create_app``) keep working.

See ``docs/specs/01-architecture.md`` §"High-level picture",
§"Component responsibilities"; ``docs/specs/12-rest-api.md`` §"Base
URL", §"OpenAPI"; ``docs/specs/16-deployment-operations.md``
§"Environment variables", §"Healthchecks";
``docs/specs/15-security-privacy.md`` §"Binding policy",
§"HTTP security headers".
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from app.adapters.llm.openrouter import OpenRouterClient
from app.adapters.llm.ports import LLMClient
from app.adapters.mail.ports import Mailer
from app.adapters.mail.smtp import SMTPMailer
from app.adapters.storage.localfs import LocalFsStorage
from app.adapters.storage.ports import Storage
from app.api.admin import admin_router
from app.api.errors import add_exception_handlers
from app.api.health import router as health_router
from app.api.middleware import IdempotencyMiddleware, SecurityHeadersMiddleware
from app.api.transport.sse import router as sse_router
from app.api.v1 import CONTEXT_ROUTERS, WORKSPACE_ADMIN_ROUTER
from app.api.v1.auth import invite as invite_module
from app.api.v1.auth import logout as logout_module
from app.api.v1.auth import magic as magic_module
from app.api.v1.auth import me as me_module
from app.api.v1.auth import me_avatar as me_avatar_module
from app.api.v1.auth import me_tokens as me_tokens_module
from app.api.v1.auth import passkey as passkey_module
from app.api.v1.auth import recovery as recovery_module
from app.api.v1.auth import signup as signup_module
from app.api.v1.auth import tokens as tokens_module
from app.api.v1.user_work_roles import (
    build_user_work_roles_router,
    build_users_user_work_roles_router,
)
from app.api.v1.users import build_users_router
from app.api.v1.work_engagements import build_work_engagements_router
from app.api.v1.work_roles import build_work_roles_router
from app.auth._throttle import Throttle
from app.auth.csrf import CSRFMiddleware
from app.auth.keys import KeyDerivationError, derive_subkey
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
# (three levels up from this file: app/api/factory.py). Phase-0 used
# a ``mocks/web/dist`` fallback; cd-q1be retires that now that
# ``app/web/`` carries the production build verbatim — falling back to
# the mocks would mask a failed prod build behind a stale demo bundle,
# which is exactly the failure mode we want to surface loudly.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
_SPA_DIST: Final[Path] = _REPO_ROOT / "app" / "web" / "dist"

# Minimal fallback served when the SPA build is missing. 200 OK so
# health probes + smoke curls succeed; a distinctive body so a human
# hitting ``/`` understands why they don't see the app.
_SPA_STUB_HTML: Final[str] = (
    "<!doctype html><html><head><title>crewday</title></head>"
    "<body><h1>SPA not built — run pnpm build in app/web</h1></body></html>"
)

# OpenAPI document version we emit. FastAPI 0.111+ emits 3.1 by default
# but we pin the string explicitly so a future FastAPI version that
# silently regresses to 3.0.x fails our factory shape tests loudly.
_OPENAPI_VERSION: Final[str] = "3.1.0"

# Tag seeds for non-context surfaces. The Swagger UI shows one section
# per tag; FastAPI does not auto-populate tag *definitions* from
# operation tag lists, so without seeding these we'd get sections with
# no description. Keep this list short — the right home for most new
# tags is still the §01 context map via :data:`CONTEXT_ROUTERS`.
_NON_CONTEXT_OPENAPI_TAGS: Final[tuple[tuple[str, str], ...]] = (
    (
        "workspace_admin",
        (
            "Workspace-scoped admin aggregator — owner/manager-only "
            "read-only surfaces spanning multiple contexts (abuse "
            "signals, security posture). See §15 'Self-serve abuse "
            "mitigations'."
        ),
    ),
    (
        "transport",
        (
            "Non-REST workspace-scoped transports. Today: the "
            "``/w/<slug>/events`` Server-Sent Events stream that "
            "carries TanStack Query invalidation + agent lifecycle "
            "events (§11 'Agent turn lifecycle', §14 'SSE-driven "
            "invalidation')."
        ),
    ),
)


class PublicBindRefused(RuntimeError):
    """Raised when ``create_app()`` is asked to run on a refused bind.

    The caller (uvicorn entrypoint, tests, composition root) gets a
    loud failure mode — this is a configuration bug, not something
    the process should recover from. Wraps the underlying
    :class:`~app.security.BindGuardError` so ``app.main``-scoped
    callers can keep catching the narrower, factory-scoped type
    without reaching into the security module.
    """


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
    :class:`PublicBindRefused` so callers keep a single, factory-
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

    Reads the current module's ``_SPA_DIST`` global at call time
    (not at function-def time) so
    ``monkeypatch.setattr("app.api.factory._SPA_DIST", ...)`` from a
    test is visible without a process restart. Python's
    :opcode:`LOAD_GLOBAL` always resolves through the module dict, so
    the plain name reference below does the right thing.
    """
    if (_SPA_DIST / "index.html").is_file():
        return _SPA_DIST
    return None


def _is_api_path(path: str) -> bool:
    """Return ``True`` if ``path`` looks like an API request.

    An API 404 must stay JSON so agent / CLI callers get a parseable
    body; the SPA catch-all deliberately does **not** handle these
    paths. Anything under ``/api/`` or ``/w/<slug>/...`` counts —
    including ``/api`` exactly, and ``/admin/api/...`` for the
    deployment-scoped admin tree.
    """
    if path == "/api" or path.startswith("/api/"):
        return True
    if path == "/admin/api" or path.startswith("/admin/api/"):
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
    storage = _build_storage(settings)
    llm = _build_llm(settings)
    app.state.settings = settings
    app.state.throttle = throttle
    app.state.capabilities = capabilities
    app.state.mailer = mailer
    app.state.storage = storage
    app.state.llm = llm
    return mailer, throttle, capabilities


def _build_llm(settings: Settings) -> LLMClient | None:
    """Return the configured :class:`LLMClient`, or ``None``.

    The v1 wiring instantiates :class:`OpenRouterClient` when
    ``settings.openrouter_api_key`` is set; otherwise the LLM seam is
    unwired and :func:`app.api.deps.get_llm` returns 503
    ``llm_unavailable`` on the first request that asks for it. This
    matches the storage / mailer pattern: deployment misconfig
    surfaces as a request-time 503 rather than refusing to boot, so
    ``/healthz`` and the non-LLM routes still serve.

    The OCR-autofill capability layers an additional gate on
    :attr:`Settings.llm_ocr_model` — both the API key AND a model id
    must be present for ``POST /expenses/autofill`` to actually run
    the LLM. The two gates compose so a deployment can disable the
    capability by clearing either side without booting a half-wired
    state machine.
    """
    if settings.openrouter_api_key is None:
        _log.info(
            "LLM client unavailable: CREWDAY_OPENROUTER_API_KEY is unset",
            extra={"event": "llm.unwired", "reason": "missing_api_key"},
        )
        return None
    return OpenRouterClient(settings.openrouter_api_key)


def _build_storage(settings: Settings) -> Storage | None:
    """Return the configured :class:`Storage` backend, or ``None``.

    ``localfs`` wiring requires ``settings.root_key`` so the signing
    seam on :class:`~app.adapters.storage.localfs.LocalFsStorage` can
    derive its HMAC key. A deployment with no ``CREWDAY_ROOT_KEY``
    already refuses to sign magic links or sessions; storage stays
    consistent with that posture — the dep surfaces a 503 instead of
    crashing the boot, so ``/healthz`` and the non-avatar routes
    still serve.

    ``s3`` is deferred until the credentials / endpoint wiring lands
    alongside §16 Recipe B; until then the branch falls through and
    ``app.state.storage`` stays ``None``.
    """
    if settings.storage_backend != "localfs":
        # S3 backend wiring lands alongside the deployment recipe
        # (cd-6vq5 sibling). The router degrades to 503 in the
        # meantime, which is the right failure mode for a deploy
        # that flipped the env var without finishing the wiring.
        return None
    try:
        signing_key = derive_subkey(settings.root_key, purpose="storage-sign")
    except KeyDerivationError:
        # Surface the misconfig at request time, not boot — a box
        # that never actually calls an avatar / file endpoint
        # shouldn't refuse to start just because the key is unset.
        _log.warning(
            "storage backend unavailable: CREWDAY_ROOT_KEY is unset",
            extra={"event": "storage.unwired", "backend": settings.storage_backend},
        )
        return None
    return LocalFsStorage(settings.data_dir, signing_key=signing_key)


def _mount_auth_routers(
    app: FastAPI,
    *,
    settings: Settings,
    mailer: Mailer | None,
    throttle: Throttle,
    capabilities: Capabilities,
) -> None:
    """Mount the bare-host + workspace-scoped auth routers.

    Bare-host routers (signup, magic-link, recovery, invite, passkey
    login) live at ``/api/v1/...``; workspace-scoped auth routers
    (tokens, users, passkey register) nest under ``/w/{slug}/api/v1/...``
    and run behind the tenancy middleware. The signup flow's WebAuthn
    ceremony (``/api/v1/signup/passkey/{start,finish}``) is mounted
    by :func:`signup_module.build_signup_router` below (gated on SMTP
    availability); see cd-ju0q for the dual-surface retirement.
    Routers that need a mailer are skipped when SMTP is not
    configured — each one would raise on first use anyway, but
    skipping the mount keeps the OpenAPI surface honest.

    The context routers from :data:`app.api.v1.CONTEXT_ROUTERS` are
    mounted separately by :func:`_mount_context_routers` — that list
    is the forward-looking surface cd-ika7's downstream tasks fill
    in, and it stays explicit so reviewers can see which contexts
    are wired at which prefix.
    """
    bare_prefix = "/api/v1"
    scoped_prefix = "/w/{slug}/api/v1"

    # --- Bare-host auth routers (§03 "Self-serve signup", §12) ---
    app.include_router(
        passkey_module.build_login_router(throttle=throttle, settings=settings),
        prefix=bare_prefix,
    )
    # /auth/me — SPA identity-bootstrap probe. Mounted unconditionally
    # (no SMTP dependency) because every authenticated SPA load hits it.
    app.include_router(me_module.build_me_router(), prefix=bare_prefix)
    # /me/tokens — identity-scoped personal-access-token CRUD (§03).
    # Bare-host because PATs live outside any workspace; the router
    # reads the session cookie itself, matching ``/auth/me``.
    app.include_router(
        me_tokens_module.build_me_tokens_router(),
        prefix=bare_prefix,
    )
    # /me/avatar — identity-scoped avatar upload / clear (§05 "Worker
    # surface", §12 "Avatar upload"). Bare-host because avatars are
    # user-global (one face across every workspace the user belongs
    # to). Reads the session cookie itself to stay in lock-step with
    # the other ``/me`` endpoints.
    app.include_router(
        me_avatar_module.build_me_avatar_router(),
        prefix=bare_prefix,
    )
    # /auth/logout — session-teardown ceremony invoked by the SPA's
    # :mod:`useAuth.logout`. Mounted alongside /auth/me because both
    # are bare-host, tenant-agnostic, and hit on every authenticated
    # load / sign-out respectively.
    app.include_router(logout_module.build_logout_router(), prefix=bare_prefix)
    # Invite accept does NOT need SMTP — redeeming a magic link the
    # operator mailed out-of-band is still valid in an SMTP-less
    # deployment. Keep this mount unconditional so the SPA's
    # ``/invite/...`` flow survives ``CREWDAY_SMTP_HOST`` being unset.
    #
    # Two routers, one throttle bucket: the legacy singular shape
    # (``/invite/accept`` with token in body, plus ``/{invite_id}/confirm``
    # + ``/complete``) stays alive for SPA back-compat per cd-z6vm,
    # and the spec-aligned plural shape (``/invites/{token}`` GET
    # introspect + ``POST /invites/{token}/accept``) lands alongside.
    # Sharing ``throttle`` means brute-force attempts hit the same
    # consume-failure lockout regardless of which surface they probe.
    # The legacy router is deprecated for new callers and slated for
    # removal once the SPA cuts over.
    app.include_router(
        invite_module.build_invite_router(
            throttle=throttle,
            settings=settings,
        ),
        prefix=bare_prefix,
    )
    app.include_router(
        invite_module.build_invites_router(
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

    # --- Workspace-scoped auth routers (§12 "Base URL") ---
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

    # Workspace-scoped identity-adjacent routers (cd-dcfw) — work
    # engagements, work roles, user_work_roles. Their URLs sit at the
    # top of the ``/w/<slug>/api/v1/`` tree per spec §12 (they are
    # NOT nested under the ``/identity`` URL segment; that segment is
    # reserved for a separate, later surface). Each router tags its
    # operations ``identity`` + a resource-specific tag so the OpenAPI
    # schema clusters them under the identity context alongside
    # ``users`` and ``auth/tokens``.
    app.include_router(build_work_roles_router(), prefix=scoped_prefix)
    app.include_router(build_user_work_roles_router(), prefix=scoped_prefix)
    app.include_router(build_users_user_work_roles_router(), prefix=scoped_prefix)
    app.include_router(build_work_engagements_router(), prefix=scoped_prefix)


def _mount_context_routers(app: FastAPI) -> None:
    """Mount each per-context router under ``/w/{slug}/api/v1/<ctx>``.

    Order matches :data:`app.api.v1.CONTEXT_ROUTERS` — the §01
    "Context map" table. Each router is empty at cd-ika7; downstream
    Beads tasks (cd-rpxd, cd-75wp, cd-sn26, ...) fill in the real
    routes without having to re-wire this seam.

    Three non-context routers also mount here alongside the §01
    contexts — they're neither bounded contexts nor part of the
    context-map invariant, but they share the workspace or
    deployment URL prefix so keeping them co-located with the
    context fan-out is easier to audit than scattering mounts:

    * :data:`app.api.v1.WORKSPACE_ADMIN_ROUTER` under
      ``/w/{slug}/api/v1/admin`` — workspace-scoped owner/manager
      admin aggregator (§15 "Self-serve abuse mitigations", cd-g1ay).
      Declares its own ``workspace_admin`` tag so the OpenAPI
      tag list stays distinct from the deployment admin tree's
      ``admin`` tag.
    * :data:`app.api.admin.admin_router` under ``/admin/api/v1`` —
      deployment-scoped admin tree, empty for now and gated by
      the admin authz middleware (cd-jlms) when real routes land.
    """
    scoped_prefix = "/w/{slug}/api/v1"
    for context_name, router in CONTEXT_ROUTERS:
        # Each router sits at ``/<context>`` under the workspace tree.
        # FastAPI accepts a combined prefix in ``include_router`` —
        # keeping the per-context segment here (not in the router
        # module itself) lets the downstream task choose whether to
        # add sub-prefixes like ``/tasks/{id}/evidence``.
        app.include_router(router, prefix=f"{scoped_prefix}/{context_name}")

    # Workspace-scoped admin aggregator. Mounted OUTSIDE the
    # ``CONTEXT_ROUTERS`` loop so the §01 13-context invariant stays
    # intact in that data structure and the custom OpenAPI generator
    # does not seed a phantom ``admin`` tag from a context name that
    # isn't in §01. The router's own ``tags=["workspace_admin"]``
    # drives the tag that appears in the schema.
    app.include_router(WORKSPACE_ADMIN_ROUTER, prefix=f"{scoped_prefix}/admin")

    # Workspace-scoped SSE transport (``/w/<slug>/events``, cd-clz9).
    # Mounted outside the ``/api/v1`` tree because the SPA talks to
    # it by the shorter ``/w/<slug>/events`` path (§14 "SSE-driven
    # invalidation") — a single EventSource per workspace, not one
    # per bounded context. Not a §01 bounded context either, so it
    # stays outside :data:`CONTEXT_ROUTERS`; the router declares
    # ``tags=["transport"]`` which the OpenAPI merge preserves.
    app.include_router(sse_router, prefix="/w/{slug}")

    # Deployment-scoped admin tree (bare host).
    app.include_router(admin_router, prefix="/admin/api/v1")


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
            # Raise rather than return so the registered StarletteHTTPException
            # handler wraps the response in the RFC 7807 problem+json envelope
            # (§12 "Errors"). A bare JSONResponse here would bypass the seam
            # and emit the wrong Content-Type / envelope shape.
            from starlette.exceptions import HTTPException as _HTTPException

            raise _HTTPException(status_code=404)

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


def _build_custom_openapi(app: FastAPI) -> dict[str, Any]:
    """Return the merged OpenAPI 3.1 document for ``app``.

    FastAPI's default generator already covers every registered route.
    We post-process it to:

    1. Pin the ``openapi`` version to :data:`_OPENAPI_VERSION` so a
       future FastAPI release that silently regresses to 3.0.x trips
       our factory shape tests instead of breaking client codegen
       downstream (cd-1cfg, cd-3j25).
    2. Seed one tag per context from :data:`CONTEXT_ROUTERS` in the
       canonical order. Routers added later (a handler decorated with
       ``tags=["tasks"]``) still show up — FastAPI merges the sets —
       but the seed guarantees every context appears even when its
       router is empty, so `/api/openapi.json` stays a stable
       reference for client generators while routes fill in.
    3. Append tag definitions for non-context surfaces that live
       outside :data:`CONTEXT_ROUTERS` but still deserve a Swagger
       UI section with a real description (e.g. ``workspace_admin``
       from :data:`app.api.v1.WORKSPACE_ADMIN_ROUTER`). FastAPI does
       not auto-populate ``tags[]`` from operation-level tag lists,
       so without this seed the Swagger UI would render the section
       with no description.

    ``operation_id`` rewriting is deliberately NOT done here: spec
    §12 requires every handler to declare its own
    ``operation_id="<group>.<verb>"``. Factory-side synthesis would
    hide missing declarations and let non-conforming operationIds
    slip into the shipped schema; a CI check on the committed
    ``openapi.json`` is the right enforcement seam. Tracked as a
    follow-up (cd-qetp sibling-boundary contract + OpenAPI lint).
    """
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=_OPENAPI_VERSION,
        description=app.description or None,
        routes=app.routes,
    )

    # Merge the context tag seed with whatever FastAPI already
    # emitted (auth, admin, health, …) while preserving order:
    # context tags first (§01 order), then any cross-cutting surfaces
    # that carry their own tag but aren't §01 contexts, then everything
    # else already present in the schema. Deterministic ordering
    # matters for the committed ``docs/api/openapi.json`` diff.
    existing = {t.get("name"): t for t in schema.get("tags", [])}
    merged_tags: list[dict[str, Any]] = []
    seen: set[str] = set()
    for context_name, _router in CONTEXT_ROUTERS:
        merged_tags.append(
            existing.get(
                context_name,
                {"name": context_name, "description": f"{context_name} context"},
            )
        )
        seen.add(context_name)

    # Non-context tag seeds. These are surfaces that ride the same
    # ``/w/<slug>/api/v1/...`` or bare-host URL shape as the context
    # tree but are not themselves bounded contexts per §01. Keep the
    # list short — the right answer for most new surfaces is still a
    # context entry, not a new row here.
    for non_context_tag, description in _NON_CONTEXT_OPENAPI_TAGS:
        merged_tags.append(
            existing.get(
                non_context_tag, {"name": non_context_tag, "description": description}
            )
        )
        seen.add(non_context_tag)

    for name, tag in existing.items():
        if name not in seen and name is not None:
            merged_tags.append(tag)
    schema["tags"] = merged_tags
    return schema


def _install_custom_openapi(app: FastAPI) -> None:
    """Wire :func:`_build_custom_openapi` onto ``app.openapi``.

    FastAPI caches the generator's output on ``app.openapi_schema``
    — setting ``app.openapi`` swaps the generator itself so the
    cache fills on first access with our shape. Reset ``openapi_schema``
    to ``None`` so any accidental pre-fill is discarded.
    """
    app.openapi_schema = None

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = _build_custom_openapi(app)
        return app.openapi_schema

    # ``app.openapi`` is a bound method on :class:`FastAPI` and mypy's
    # strict ``method-assign`` check refuses the reassignment. FastAPI's
    # documented way to customise the OpenAPI generator is to swap this
    # attribute (see https://fastapi.tiangolo.com/how-to/extending-openapi/);
    # the ignore is load-bearing for the idiom, not masking a real error.
    app.openapi = openapi  # type: ignore[method-assign]


def _build_worker_lifespan(
    cfg: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build the ASGI lifespan that bootstraps the in-process worker.

    Gated on ``cfg.worker``:

    * ``"internal"`` (default) — the scheduler is started in-process
      at lifespan ``__aenter__`` and stopped at ``__aexit__``. Single-
      container deployments (Recipe A of §16) need no separate worker
      service; dev runs the same way for convenience.
    * ``"external"`` — the lifespan is a no-op on the scheduler side;
      a dedicated ``worker`` container (Recipes B / D) runs
      :mod:`app.worker.__main__` instead. We still install the
      lifespan seam so any future per-request state (SSE registries,
      warm caches) has one place to plug in.

    The scheduler is stashed on :attr:`FastAPI.state.scheduler` so
    tests can inject a pinned :class:`~app.util.clock.FrozenClock`
    before lifespan entry and dashboards can introspect the running
    job set without reaching into APScheduler internals. Startup is
    idempotent (see :func:`app.worker.scheduler.start`), so a
    supervised restart that re-fires the lifespan hook does not
    crash.

    ``cfg`` is captured by closure so the lifespan sees the same
    :class:`Settings` instance the rest of the factory composed
    against — tests that pass a pinned settings get deterministic
    behaviour without an env var round-trip.
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Imported lazily so the factory module stays importable
        # even if the worker module fails to load (e.g. a circular
        # import during a refactor). The lazy import also keeps the
        # apscheduler cost off the hot path for the rare caller
        # (tests, static analysis) that imports ``create_app`` just
        # for its signature without ever building the app.
        from app.worker import create_scheduler, register_jobs
        from app.worker import start as scheduler_start
        from app.worker import stop as scheduler_stop

        if cfg.worker == "internal":
            scheduler = create_scheduler()
            register_jobs(scheduler)
            scheduler_start(scheduler)
            app.state.scheduler = scheduler
            _log.info(
                "in-process scheduler started",
                extra={"event": "worker.lifespan.started", "mode": cfg.worker},
            )
        else:
            app.state.scheduler = None
            _log.info(
                "worker mode external; lifespan skipping scheduler start",
                extra={"event": "worker.lifespan.skipped", "mode": cfg.worker},
            )

        try:
            yield
        finally:
            scheduler = getattr(app.state, "scheduler", None)
            if scheduler is not None:
                # ``wait=False`` matches the ASGI shutdown shape — a
                # reverse proxy / orchestrator typically gives the
                # process 10-30 s to exit and a slow job body must
                # not block that. Jobs that need a graceful drain
                # should add their own cancellation seam (tracked
                # as a follow-up if the need actually surfaces).
                scheduler_stop(scheduler, wait=False)
                app.state.scheduler = None

    return _lifespan


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
        # Lifespan hook (§16 "Worker process"): starts the in-process
        # APScheduler when ``cfg.worker == "internal"``, no-ops
        # otherwise. Driven by :class:`TestClient` only when the
        # caller uses ``with TestClient(app) as client:`` — tests
        # that build the app for a single route hit (like the
        # existing ``/readyz`` integration suite) are unaffected.
        lifespan=_build_worker_lifespan(cfg),
    )

    # Middleware is applied OUTER → INNER at request time. FastAPI's
    # ``add_middleware`` prepends to ``user_middleware``, so the LAST
    # call lands outermost. The desired chain (reads top-down as "first
    # thing to see the request, last thing to see the response"):
    #
    # 1. CORS — reject cross-origin preflights before any stateful work.
    # 2. SecurityHeaders — stamp every response, even middleware rejects.
    # 3. WorkspaceContextMiddleware — bind the tenancy ctx + stash the
    #    resolved :class:`~app.tenancy.middleware.ActorIdentity` on
    #    ``request.state`` (so the idempotency middleware can read
    #    ``token_id`` without re-verifying the bearer token).
    # 4. IdempotencyMiddleware — replay cache for ``POST`` retries
    #    carrying ``Idempotency-Key``. Runs AFTER auth (so
    #    ``token_id`` is known) and BEFORE the handler. Spec §12
    #    "Idempotency".
    # 5. CSRFMiddleware — double-submit check on mutation verbs.
    #
    # To get that layout we register INNER → OUTER: CSRF first (ends up
    # innermost), CORS last (ends up outermost). CORS defaults to
    # same-origin only (§15 "HTTP security headers"): agent callers
    # hit ``/api/v1/...`` with a bearer token and no browser origin,
    # and a mis-set wildcard here would be a privacy regression.
    # Dev work behind a separate Vite origin can populate
    # ``CREWDAY_CORS_ALLOW_ORIGINS`` with an explicit list.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(WorkspaceContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, settings=cfg)
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
    _mount_auth_routers(
        app,
        settings=cfg,
        mailer=mailer,
        throttle=throttle,
        capabilities=capabilities,
    )
    _mount_context_routers(app)
    # Exception handlers are registered AFTER every router is mounted
    # so the :class:`DomainError` hierarchy and validation/HTTP-exception
    # handlers cover every surface — and BEFORE the custom OpenAPI
    # install so the schema generator sees the fully-assembled app
    # (spec §12 "Errors", Beads cd-waq3).
    add_exception_handlers(app)
    _install_custom_openapi(app)

    # SPA seam MUST register last — its ``/{full_path:path}`` pattern
    # (static catch-all in prod, Vite proxy in dev) would otherwise
    # swallow every subsequent route. The ``dev`` proxy is imported
    # lazily so the prod hot path never pays for the ``httpx`` seam.
    if cfg.profile == "dev":
        from app.api.proxy import register_vite_proxy

        register_vite_proxy(app, vite_dev_url=cfg.vite_dev_url)
    else:
        _register_spa_catch_all(app)

    # Worker mode lands via the lifespan hook built at the top of
    # this factory (``_build_worker_lifespan``). The log line here
    # stays so boot-time operator diagnostics + existing grep
    # recipes keep working; the actual scheduler start/stop is
    # driven by ASGI lifespan events, not this code path.
    _log.info(
        "worker mode resolved",
        extra={"event": "worker.mode", "mode": cfg.worker},
    )

    return app
