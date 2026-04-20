"""Unit tests for :mod:`app.main`.

The FastAPI factory is exercised purely through :class:`TestClient` —
no real DB, no real network, no real SMTP. Tests that need a DB ping
live under ``tests/integration/test_main.py``.

Covers:

* factory shape — ``create_app()`` returns a :class:`FastAPI` and is
  re-callable per test with a pinned :class:`Settings`;
* bind guard — wildcard hosts refuse without the public-bind opt-in;
* ops probe mounting — ``/healthz`` is reachable and carries the
  tenancy correlation header (the ops-probe handler contracts
  themselves live in ``tests/unit/test_health.py``);
  security-headers middleware stamps every response including the
  probe surface;
* SPA catch-all — fallback HTML at ``/`` when the dist dir is absent,
  and an HTTP 404 JSON envelope for unknown ``/api/...`` paths;
* middleware ordering — tenancy + CSRF + security headers all engage
  in the expected order.

See ``docs/specs/01-architecture.md`` §"High-level picture",
``docs/specs/16-deployment-operations.md`` §"Healthchecks",
``docs/specs/15-security-privacy.md`` §"Binding policy".
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main_module
from app.config import Settings
from app.main import PublicBindRefused, create_app
from app.tenancy.middleware import CORRELATION_ID_HEADER


def _settings(
    *,
    bind_host: str = "127.0.0.1",
    bind_port: int = 8000,
    allow_public_bind: bool = False,
    worker: Literal["internal", "external"] = "internal",
    smtp_host: str | None = None,
    smtp_port: int = 587,
    smtp_from: str | None = None,
    smtp_use_tls: bool = True,
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO",
    cors_allow_origins: list[str] | None = None,
    profile: Literal["prod", "dev"] = "prod",
    vite_dev_url: str = "http://127.0.0.1:5173",
) -> Settings:
    """Return a :class:`Settings` suitable for an in-memory factory build.

    Defaults to SQLite in-memory (the factory doesn't touch the DB
    unless a test calls ``/readyz``) and an empty root key so the
    session machinery can be imported without env reads.

    Only the knobs the ``test_main`` suite actually exercises are
    exposed here — mirroring the approach in other router-layer
    tests — so mypy keeps the typed shape of ``model_construct``.
    """
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-main-root-key"),
        bind_host=bind_host,
        bind_port=bind_port,
        allow_public_bind=allow_public_bind,
        worker=worker,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_from=smtp_from,
        smtp_use_tls=smtp_use_tls,
        log_level=log_level,
        cors_allow_origins=list(cors_allow_origins or []),
        profile=profile,
        vite_dev_url=vite_dev_url,
    )


@pytest.fixture
def app_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Settings]:
    """Return a :class:`Settings` patched so ``make_uow`` never opens a real engine.

    The ops-probe unit tests hit ``/healthz`` and ``/version`` which
    don't touch the DB; nothing in this fixture short-circuits the DB
    for the ``/readyz`` path so tests that need DB behaviour should
    use the integration layer.
    """
    yield _settings()


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _mw_name(mw: object) -> str:
    """Return the readable class name of a ``Middleware`` entry.

    Starlette's ``Middleware.cls`` types as a ``_MiddlewareFactory``
    Protocol (no ``__name__``), but at runtime it's always a class.
    A plain ``getattr(..., "__name__", repr(...))`` keeps the test
    type-safe without an ``isinstance(type)`` check that doesn't
    narrow Protocols anyway.
    """
    cls = getattr(mw, "cls", None)
    return getattr(cls, "__name__", repr(cls))


# ---------------------------------------------------------------------------
# Factory shape
# ---------------------------------------------------------------------------


class TestCreateApp:
    """Public ``create_app()`` contract."""

    def test_returns_fastapi_instance(self, app_factory: Settings) -> None:
        app = create_app(settings=app_factory)
        assert isinstance(app, FastAPI)

    def test_settings_stashed_on_app_state(self, app_factory: Settings) -> None:
        app = create_app(settings=app_factory)
        assert app.state.settings is app_factory

    def test_throttle_instantiated_on_app_state(self, app_factory: Settings) -> None:
        app = create_app(settings=app_factory)
        assert app.state.throttle is not None

    def test_mailer_is_none_without_smtp_config(self, app_factory: Settings) -> None:
        app = create_app(settings=app_factory)
        assert app.state.mailer is None

    def test_mailer_present_when_smtp_configured(self) -> None:
        cfg = _settings(
            smtp_host="localhost",
            smtp_port=2525,
            smtp_from="ops@example.com",
            smtp_use_tls=False,
        )
        app = create_app(settings=cfg)
        assert app.state.mailer is not None

    def test_capabilities_probed_at_startup(self, app_factory: Settings) -> None:
        app = create_app(settings=app_factory)
        assert app.state.capabilities is not None

    def test_openapi_mounted_at_api_subpath(self, app_factory: Settings) -> None:
        """OpenAPI lives under ``/api/openapi.json`` per §12."""
        app = create_app(settings=app_factory)
        client = _client(app)
        resp = client.get("/api/openapi.json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")

    def test_factory_falls_back_to_get_settings_when_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing ``None`` pulls the default singleton via ``get_settings``."""
        sentinel = _settings()
        monkeypatch.setattr("app.api.factory.get_settings", lambda: sentinel)
        app = create_app(settings=None)
        assert app.state.settings is sentinel

    def test_log_level_threaded_from_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``settings.log_level`` is passed to ``setup_logging``.

        Records the captured ``level=`` so a future drift (hardcoded
        ``"INFO"``) fails loudly instead of silently ignoring the knob.
        """
        captured: dict[str, str] = {}

        def _spy(level: str = "INFO", **_kwargs: object) -> None:
            captured["level"] = level

        monkeypatch.setattr("app.api.factory.setup_logging", _spy)
        create_app(settings=_settings(log_level="DEBUG"))
        assert captured == {"level": "DEBUG"}

    def test_invite_router_mounted_without_smtp(self, app_factory: Settings) -> None:
        """Invite accept must stay callable in an SMTP-less deployment.

        Redeeming a magic link the operator mailed out-of-band does
        not require an outbound mailer — gating the whole invite
        router behind SMTP presence would break that flow. We assert
        at the route-table level (not via a live request) because
        the handler itself needs a full DB / session stack.
        """
        app = create_app(settings=app_factory)
        # ``app.routes`` is heterogeneous (APIRoute / Mount /
        # StaticFiles…); ``getattr`` with a string fallback keeps
        # mypy strict without forcing an isinstance cascade.
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/api/v1/invite/accept" in paths


class TestBindGuard:
    """``0.0.0.0`` / ``::`` refuse to start without ``allow_public_bind``."""

    def test_wildcard_v4_without_opt_in_raises(self) -> None:
        cfg = _settings(bind_host="0.0.0.0", allow_public_bind=False)
        with pytest.raises(PublicBindRefused):
            create_app(settings=cfg)

    def test_wildcard_v6_without_opt_in_raises(self) -> None:
        cfg = _settings(bind_host="::", allow_public_bind=False)
        with pytest.raises(PublicBindRefused):
            create_app(settings=cfg)

    def test_wildcard_v4_with_opt_in_allowed(self) -> None:
        cfg = _settings(bind_host="0.0.0.0", allow_public_bind=True)
        # Must NOT raise.
        app = create_app(settings=cfg)
        assert isinstance(app, FastAPI)

    def test_loopback_always_allowed(self) -> None:
        cfg = _settings(bind_host="127.0.0.1", allow_public_bind=False)
        app = create_app(settings=cfg)
        assert isinstance(app, FastAPI)

    def test_bind_guard_message_mentions_the_env_var(self) -> None:
        cfg = _settings(bind_host="0.0.0.0", allow_public_bind=False)
        with pytest.raises(PublicBindRefused, match="CREWDAY_ALLOW_PUBLIC_BIND"):
            create_app(settings=cfg)


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


class TestHealthzMount:
    """The factory mounts the ops-probe router and the tenancy middleware
    treats the path as a skip-path (adds the correlation header).

    The probe handler's own contract (body shape, failure modes) is
    asserted in ``tests/unit/test_health.py``; this test only asserts
    the factory-level wiring.
    """

    def test_healthz_mounted_and_tenancy_skipped(self, app_factory: Settings) -> None:
        client = _client(create_app(settings=app_factory))
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert CORRELATION_ID_HEADER in resp.headers


# ---------------------------------------------------------------------------
# SPA catch-all
# ---------------------------------------------------------------------------


class TestSpaCatchAll:
    """``GET /`` falls through to index.html (or the stub fallback)."""

    def test_root_returns_html_200(self, app_factory: Settings) -> None:
        client = _client(create_app(settings=app_factory))
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")

    def test_root_stub_fallback_when_no_dist(
        self, app_factory: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``app/web/dist`` absent → stub HTML banner (no mocks fallback)."""
        monkeypatch.setattr(
            "app.api.factory._SPA_DIST", Path("/tmp/does-not-exist-xyz")
        )
        client = _client(create_app(settings=app_factory))
        resp = client.get("/")
        assert resp.status_code == 200
        assert "SPA not built" in resp.text

    def test_missing_dist_logs_warning(
        self,
        app_factory: Settings,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A missing prod build is logged at WARNING so ops notice."""
        monkeypatch.setattr(
            "app.api.factory._SPA_DIST", Path("/tmp/does-not-exist-xyz")
        )
        with caplog.at_level("WARNING", logger="app.api.factory"):
            create_app(settings=app_factory)
        events = [
            rec
            for rec in caplog.records
            if getattr(rec, "event", None) == "spa_dist_missing"
        ]
        assert len(events) == 1

    def test_no_mocks_fallback(self, app_factory: Settings) -> None:
        """The Phase-0 ``mocks/web/dist`` fallback was retired (cd-q1be).

        ``app/main._SPA_DIST`` must point at ``app/web/dist`` only;
        a prior tuple-of-candidates shape would silently pick the
        mock build up again.
        """
        assert main_module._SPA_DIST.parts[-3:] == ("app", "web", "dist")
        # No legacy tuple survives.
        assert not hasattr(main_module, "_SPA_DIST_CANDIDATES")

    def test_deep_link_returns_spa_index(self, app_factory: Settings) -> None:
        """Deep-link routes (client-side router targets) fall through too."""
        client = _client(create_app(settings=app_factory))
        resp = client.get("/some/spa/route")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")

    def test_unknown_api_path_returns_json_404(self, app_factory: Settings) -> None:
        """API paths never fall through to the SPA."""
        client = _client(create_app(settings=app_factory))
        resp = client.get("/api/nonexistent")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")

    def test_unknown_bare_api_path_returns_json_not_html(
        self, app_factory: Settings
    ) -> None:
        """Even under the bare-host ``/api/v1`` tree, a miss is JSON."""
        client = _client(create_app(settings=app_factory))
        resp = client.get("/api/v1/nonexistent")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")
        # The canonical spec envelope (§15) keys on ``error``.
        assert resp.json() == {"error": "not_found", "detail": None}


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    """Every response gets the baseline §15 header set.

    ``Strict-Transport-Security`` is opt-in (``settings.hsts_enabled``)
    — the full on/off matrix lives in
    ``tests/unit/api/test_csp_nonce.py`` so this suite is free to
    focus on the unconditional set.
    """

    @pytest.mark.parametrize(
        "header",
        [
            "Content-Security-Policy",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Referrer-Policy",
        ],
    )
    def test_header_present_on_healthz(
        self, app_factory: Settings, header: str
    ) -> None:
        client = _client(create_app(settings=app_factory))
        resp = client.get("/healthz")
        assert header in resp.headers

    def test_xframe_is_deny(self, app_factory: Settings) -> None:
        client = _client(create_app(settings=app_factory))
        resp = client.get("/healthz")
        assert resp.headers["X-Frame-Options"] == "DENY"

    def test_xcontent_type_is_nosniff(self, app_factory: Settings) -> None:
        client = _client(create_app(settings=app_factory))
        resp = client.get("/healthz")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"

    def test_csp_forbids_frames(self, app_factory: Settings) -> None:
        client = _client(create_app(settings=app_factory))
        resp = client.get("/healthz")
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]

    def test_security_headers_also_on_spa_route(self, app_factory: Settings) -> None:
        """Every response — including the SPA index — gets stamped."""
        client = _client(create_app(settings=app_factory))
        resp = client.get("/")
        assert "X-Content-Type-Options" in resp.headers


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


class TestCors:
    """CORS is restrictive — same-origin only in v1."""

    def test_cross_origin_preflight_refused(self, app_factory: Settings) -> None:
        """An unlisted origin must not receive ``Access-Control-Allow-Origin``."""
        client = _client(create_app(settings=app_factory))
        resp = client.options(
            "/api/openapi.json",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Starlette's CORS middleware rejects the preflight without the
        # Access-Control-Allow-Origin echo when allow_origins is empty.
        assert resp.headers.get("access-control-allow-origin") is None

    def test_configured_origin_echoed_on_preflight(self) -> None:
        """``cors_allow_origins`` is honoured end-to-end (dev-proxy seam)."""
        cfg = _settings(cors_allow_origins=["http://127.0.0.1:5173"])
        client = _client(create_app(settings=cfg))
        resp = client.options(
            "/api/openapi.json",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert (
            resp.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"
        )

    def test_configured_origin_does_not_echo_other(self) -> None:
        """A configured dev origin must not broaden past its list."""
        cfg = _settings(cors_allow_origins=["http://127.0.0.1:5173"])
        client = _client(create_app(settings=cfg))
        resp = client.options(
            "/api/openapi.json",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------------------
# Middleware wiring
# ---------------------------------------------------------------------------


class TestMiddlewareWiring:
    """Ordering + presence of the mandated middleware classes."""

    def test_security_headers_middleware_registered(
        self, app_factory: Settings
    ) -> None:
        app = create_app(settings=app_factory)
        # ``mw.cls`` is typed as a Protocol (``_MiddlewareFactory``),
        # but at runtime it's the actual class. We read the name via
        # ``getattr`` with a string fallback to stay mypy-strict.
        names = {_mw_name(mw) for mw in app.user_middleware}
        assert "SecurityHeadersMiddleware" in names

    def test_tenancy_and_csrf_middleware_registered(
        self, app_factory: Settings
    ) -> None:
        app = create_app(settings=app_factory)
        names = {_mw_name(mw) for mw in app.user_middleware}
        assert "WorkspaceContextMiddleware" in names
        assert "CSRFMiddleware" in names

    def test_middleware_order_cors_outermost_csrf_innermost(
        self, app_factory: Settings
    ) -> None:
        """``add_middleware`` prepends to ``user_middleware``; the list
        index 0 is therefore the OUTERMOST wrap. CORS must be
        outermost, CSRF innermost, and SecurityHeaders/Tenancy sit
        in between — stamping headers before CSRF can reject a
        request and resolving tenancy before CSRF re-mints its
        cookie.
        """
        app = create_app(settings=app_factory)
        names = [_mw_name(mw) for mw in app.user_middleware]
        # Index 0 = outermost, last index = innermost.
        assert names[0] == "CORSMiddleware"
        assert names[-1] == "CSRFMiddleware"
        # SecurityHeaders + Tenancy live strictly between them.
        assert names.index("SecurityHeadersMiddleware") < names.index(
            "WorkspaceContextMiddleware"
        )


# ---------------------------------------------------------------------------
# Worker mode
# ---------------------------------------------------------------------------


class TestWorkerWiring:
    """``settings.worker`` toggles the scheduler hook (stubbed for v1)."""

    def test_internal_worker_mode_accepted(self) -> None:
        app = create_app(settings=_settings(worker="internal"))
        assert app.state.settings.worker == "internal"

    def test_external_worker_mode_accepted(self) -> None:
        app = create_app(settings=_settings(worker="external"))
        assert app.state.settings.worker == "external"


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


class TestStaticAssets:
    """``/assets/*`` is mounted when the dist directory is present."""

    def test_assets_mount_noop_when_dist_missing(
        self, monkeypatch: pytest.MonkeyPatch, app_factory: Settings
    ) -> None:
        """A missing dist must not crash startup — just skip the mount."""
        monkeypatch.setattr(
            "app.api.factory._SPA_DIST", Path("/tmp/does-not-exist-xyz")
        )
        # Must not raise.
        app = create_app(settings=app_factory)
        assert isinstance(app, FastAPI)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


class TestIsApiPath:
    """Unit-level coverage for the SPA/API disambiguation helper."""

    @pytest.mark.parametrize(
        "path",
        [
            "/api",
            "/api/",
            "/api/v1/ping",
            "/api/openapi.json",
            "/w/villa-sud/api/v1/tasks",
        ],
    )
    def test_api_paths_classified_as_api(self, path: str) -> None:
        assert main_module._is_api_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/dashboard",
            "/w",
            "/w/",
            "/w/villa-sud",
            "/w/villa-sud/today",
            "/assets/index-abc.js",
            "/apish-but-not-api",
        ],
    )
    def test_non_api_paths_classified_as_spa(self, path: str) -> None:
        assert main_module._is_api_path(path) is False


# ---------------------------------------------------------------------------
# Dev-profile Vite proxy
# ---------------------------------------------------------------------------


class TestDevProfileViteProxy:
    """``profile=dev`` swaps the static mount for a Vite HTTP proxy.

    We never actually hit the Vite dev server in unit tests — a
    :class:`httpx.MockTransport`-backed :class:`httpx.AsyncClient` is
    installed on ``app.state.vite_client`` after construction so the
    forwarded request lands on an in-test handler. That keeps the
    suite network-free while still exercising the full proxy path,
    including header filtering and streaming.
    """

    def _dev_app_with_mock(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
    ) -> FastAPI:
        cfg = _settings(profile="dev", vite_dev_url="http://127.0.0.1:5173")
        app = create_app(settings=cfg)
        transport = httpx.MockTransport(handler)
        app.state.vite_client = httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:5173",
            follow_redirects=False,
        )
        return app

    def test_dev_profile_proxies_root_to_vite(self) -> None:
        """``GET /`` streams the Vite upstream body + status back."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/"
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<!doctype html><title>vite dev</title>",
            )

        client = _client(self._dev_app_with_mock(handler))
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "vite dev" in resp.text

    def test_dev_profile_proxies_deep_path_and_query(self) -> None:
        """Deep paths + query strings pass through verbatim."""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["query"] = request.url.query.decode()
            return httpx.Response(200, content=b"ok")

        client = _client(self._dev_app_with_mock(handler))
        resp = client.get("/src/main.tsx?t=123&v=abc")
        assert resp.status_code == 200
        assert captured["path"] == "/src/main.tsx"
        assert captured["query"] == "t=123&v=abc"

    def test_dev_profile_api_paths_not_proxied(self) -> None:
        """``/api/*`` must still 404 JSON — not reach Vite."""
        called = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, content=b"should not be called")

        client = _client(self._dev_app_with_mock(handler))
        resp = client.get("/api/nonexistent")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")
        assert called["n"] == 0

    def test_dev_profile_openapi_still_reachable(self) -> None:
        """The API precedence survives the dev proxy registration."""

        def handler(request: httpx.Request) -> httpx.Response:
            # OpenAPI lives on a real handler — Vite must never see it.
            raise AssertionError(f"proxy reached for {request.url}")

        client = _client(self._dev_app_with_mock(handler))
        resp = client.get("/api/openapi.json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")

    def test_dev_profile_upstream_failure_returns_502(self) -> None:
        """A Vite outage surfaces as 502, not a 500 stack trace."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = _client(self._dev_app_with_mock(handler))
        resp = client.get("/anything")
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"] == "vite_unreachable"

    def test_dev_profile_strips_host_header(self) -> None:
        """``Host`` must not bleed from client → Vite — httpx re-sets it."""
        seen_host: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_host["host"] = request.headers.get("host", "")
            return httpx.Response(200, content=b"ok")

        client = _client(self._dev_app_with_mock(handler))
        client.get("/", headers={"Host": "attacker.example"})
        # httpx derives the forwarded ``Host`` from its ``base_url`` —
        # never echoes an inbound override.
        assert seen_host["host"] == "127.0.0.1:5173"

    def test_dev_profile_does_not_mount_static_assets(self) -> None:
        """No ``/assets`` StaticFiles mount when the dev profile is active.

        The static mount belongs to the prod catch-all; swapping it in
        during dev would race the Vite upstream for asset URLs.
        """
        cfg = _settings(profile="dev")
        app = create_app(settings=cfg)
        mount_names = {getattr(r, "name", None) for r in app.routes}
        assert "assets" not in mount_names

    def test_dev_profile_exposes_vite_client_on_state(self) -> None:
        """``app.state.vite_client`` is the seam tests rely on."""
        cfg = _settings(profile="dev")
        app = create_app(settings=cfg)
        assert isinstance(app.state.vite_client, httpx.AsyncClient)
        assert app.state.vite_dev_url == "http://127.0.0.1:5173"
