"""Integration smoke for the merged OpenAPI 3.1 document (cd-ika7).

Unit tests (``tests/unit/api/test_factory.py``) cover the tag
seeding + classifier surface against an in-memory factory; this
suite boots :func:`app.main.create_app` against the integration
harness's DB so the final schema (tag seed + auth routers + admin
mount + every middleware) is exercised end-to-end.

Covers:

* ``GET /api/openapi.json`` serves a valid 3.1 document;
* the 13 §01 "Context map" tags are all present;
* bare-host auth routes (`/api/v1/signup/*`, `/api/v1/auth/*`,
  `/api/v1/invite/*`) survive the factory move;
* empty scoped context routers 404 cleanly under
  ``/w/<slug>/api/v1/<ctx>`` (unknown workspace → tenancy 404);
* the admin tree is reachable at ``/admin/api/v1`` with the same
  canonical 404 envelope.

See ``docs/specs/12-rest-api.md`` §"Base URL", §"OpenAPI";
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
from app.api.v1 import CONTEXT_ROUTERS
from app.config import Settings
from app.main import create_app
from app.tenancy.orm_filter import install_tenant_filter

pytestmark = pytest.mark.integration


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    """Settings bound to the integration harness DB URL."""
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-openapi-shape-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
        smtp_host=None,
        smtp_from=None,
        demo_mode=False,
        demo_frame_ancestors=None,
        hsts_enabled=False,
    )


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Point the process-wide UoW at the integration engine."""
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


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# OpenAPI shape
# ---------------------------------------------------------------------------


class TestOpenapiIntegration:
    """OpenAPI shape against a live-DB factory."""

    def test_openapi_json_served(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        resp = _client(pinned_settings).get("/api/openapi.json")
        assert resp.status_code == 200
        body = resp.json()
        assert body["openapi"] == "3.1.0"
        assert body.get("info", {}).get("title") == "crewday"

    def test_every_context_has_a_tag(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        resp = _client(pinned_settings).get("/api/openapi.json")
        tag_names = {tag["name"] for tag in resp.json().get("tags", [])}
        for context_name, _router in CONTEXT_ROUTERS:
            assert context_name in tag_names

    def test_context_tags_lead_in_spec_order(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """§01 "Context map" ordering leads the ``tags`` array."""
        resp = _client(pinned_settings).get("/api/openapi.json")
        names = [tag["name"] for tag in resp.json().get("tags", [])]
        expected = [name for name, _ in CONTEXT_ROUTERS]
        assert names[: len(expected)] == expected


# ---------------------------------------------------------------------------
# Bare-host auth surface survives the factory refactor
# ---------------------------------------------------------------------------


class TestBareHostAuthRoutes:
    """The bare-host auth surface listed in spec §12 "Auth" stays live.

    No SMTP is configured in the integration fixture so signup,
    magic-link, and recovery routers are skipped by ``_mount_auth_routers``
    — we probe only the routers the factory mounts unconditionally
    (passkey, invite). Full SMTP-active coverage lives in
    ``tests/integration/auth/``.
    """

    def test_invite_accept_route_present_in_openapi(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        resp = _client(pinned_settings).get("/api/openapi.json")
        paths = resp.json().get("paths", {})
        # ``build_invite_router(prefix="/invite")`` mounted under
        # ``/api/v1`` — the accept endpoint lives at
        # ``/api/v1/invite/accept``. Spec §12 "Invite token endpoints".
        assert "/api/v1/invite/accept" in paths

    def test_passkey_login_begin_present_in_openapi(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        resp = _client(pinned_settings).get("/api/openapi.json")
        paths = resp.json().get("paths", {})
        # ``build_login_router`` mounts under ``/api/v1/auth/passkey/login``.
        has_passkey_login = any(
            p.startswith("/api/v1/auth/passkey/login") for p in paths
        )
        assert has_passkey_login


# ---------------------------------------------------------------------------
# Scoped empty routers: 404 via tenancy middleware
# ---------------------------------------------------------------------------


class TestScopedEmptyContextReturns404:
    """An unknown workspace slug hitting a context route 404s.

    The tenancy middleware returns 404 before the router sees the
    request (spec §01 "Workspace addressing") — every workspace-scoped
    context path under ``/w/<unknown-slug>/api/v1/<ctx>/...`` must
    collapse to the canonical 404 JSON envelope.
    """

    def test_unknown_slug_scoped_context_path_404s(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        client = _client(pinned_settings)
        # Any of the 13 contexts will do — pick ``tasks`` for realism.
        resp = client.get("/w/nosuch/api/v1/tasks/ping")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")
        # §15 "Constant-time cross-tenant responses" canonical envelope.
        assert resp.json() == {"error": "not_found", "detail": None}

    def test_admin_api_unknown_path_returns_canonical_404(
        self, pinned_settings: Settings, real_make_uow: None
    ) -> None:
        """``/admin/api/v1/...`` 404s with the JSON envelope too."""
        client = _client(pinned_settings)
        resp = client.get("/admin/api/v1/nonexistent")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")
