"""Integration tests for :mod:`app.main` against a real DB.

Unit tests cover the factory's wiring + middleware shape against an
in-memory SQLite URL (no DB reads). The integration suite exists to
verify the bits that require a live engine:

* the OpenAPI surface is reachable once the full factory (middleware
  + routers) is wired against a real engine.

Ops-probe behaviour against a live DB (``/healthz``, ``/readyz``,
``/version``) lives in ``tests/integration/test_health.py`` per the
cd-leif refactor — those probes now live in :mod:`app.api.health`
and that suite covers the full probe surface.

See ``docs/specs/16-deployment-operations.md`` §"Healthchecks",
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.config import Settings
from app.main import create_app
from app.tenancy.orm_filter import install_tenant_filter

pytestmark = pytest.mark.integration


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    """:class:`Settings` bound to the integration harness's DB URL."""
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-main-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
    )


@pytest.fixture
def real_make_uow(monkeypatch: pytest.MonkeyPatch, engine: Engine) -> Iterator[None]:
    """Redirect the process-wide default UoW to the integration engine.

    The factory's ``/readyz`` handler opens ``make_uow()`` directly —
    it does not know about FastAPI dep overrides. Patching the
    module-level defaults keeps the integration test self-contained
    without touching env vars. The original values are restored on
    teardown so no state leaks into sibling tests.
    """
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


class TestFactoryAgainstRealDb:
    """Factory wiring holds up once a live engine is attached."""

    def test_api_openapi_json_reachable_with_live_db(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """The OpenAPI surface stays reachable after full factory wiring."""
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/openapi.json")
        assert resp.status_code == 200
        body = resp.json()
        # FastAPI-emitted OpenAPI always carries an ``info.title``;
        # we pinned it to ``crewday`` in the factory.
        assert body["info"]["title"] == "crewday"


class TestSpaProdMountAgainstRealDist:
    """Prod-profile SPA mount delivers the real ``app/web/dist`` bundle.

    cd-q1be cut over from the Phase-0 ``mocks/web/dist`` fallback to
    the production build at ``app/web/dist``. This suite exercises the
    full HTTP round-trip so a future regression (wrong path, missing
    assets mount, shadowed API route) fails here instead of only
    showing up in dev.
    """

    def test_root_returns_spa_index(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """``GET /`` serves ``index.html`` from ``app/web/dist``."""
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        # The real index carries the SPA module script; the stub
        # banner doesn't — we assert the former so a silent fall-through
        # to the stub fails here loudly.
        assert "SPA not built" not in resp.text

    def test_deep_link_returns_spa_index(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """Deep-link paths fall through to ``index.html`` (client-side routing).

        A path that does not trigger the tenancy middleware — ``/dashboard``
        here — exercises the static catch-all cleanly; ``/w/<slug>/...``
        paths are intercepted by
        :class:`~app.tenancy.middleware.WorkspaceContextMiddleware` and
        answered with the canonical 404 envelope when the slug is
        unknown, so they belong in the tenancy suite, not this one.
        """
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "SPA not built" not in resp.text

    def test_api_404_stays_json(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """``/api/*`` routes are not shadowed by the SPA catch-all.

        FastAPI-handled 404s go through the RFC 7807 seam (§12).
        """
        app = create_app(settings=pinned_settings)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/v1/does-not-exist")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/problem+json")
