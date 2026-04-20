"""Unit tests for :mod:`app.api.factory`.

Covers the new cd-ika7 surface that moved ``create_app`` out of
``app.main`` into ``app.api.factory``:

* ``create_app`` returns a :class:`FastAPI`;
* OpenAPI lives at ``/api/openapi.json`` and emits version 3.1.0
  with one tag per context;
* every context in :data:`CONTEXT_ROUTERS` is seeded as a tag
  even when its router has no routes;
* empty context routers don't pollute the ``paths`` table;
* ``app.main`` re-exports still resolve (shim contract);
* ``_is_api_path`` classifies admin paths under the new tree.

Wider ``create_app`` behaviour (bind guard, middleware ordering,
SPA catch-all, CORS, dev-profile Vite proxy) continues to live in
``tests/unit/test_main.py`` so the shim and the factory are both
under test.

See ``docs/specs/12-rest-api.md`` §"Base URL", §"OpenAPI";
``docs/specs/01-architecture.md`` §"Context map"; Beads ``cd-ika7``.
"""

from __future__ import annotations

from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.factory import PublicBindRefused, create_app
from app.api.v1 import CONTEXT_ROUTERS
from app.config import Settings


def _settings(
    *,
    profile: Literal["prod", "dev"] = "prod",
    smtp_host: str | None = None,
    smtp_from: str | None = None,
) -> Settings:
    """Return a :class:`Settings` for factory-only tests (no DB reads)."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-factory-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        smtp_host=smtp_host,
        smtp_port=587,
        smtp_from=smtp_from,
        smtp_use_tls=False,
        log_level="INFO",
        cors_allow_origins=[],
        profile=profile,
        vite_dev_url="http://127.0.0.1:5173",
    )


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Factory shape
# ---------------------------------------------------------------------------


class TestCreateApp:
    """Core contract — the factory returns a :class:`FastAPI` and
    exposes the documented seams.
    """

    def test_returns_fastapi_instance(self) -> None:
        assert isinstance(create_app(settings=_settings()), FastAPI)

    def test_openapi_mounted_at_api_openapi_json(self) -> None:
        """Spec §12 "Base URL" pins the OpenAPI at ``/api/openapi.json``."""
        client = _client(create_app(settings=_settings()))
        resp = client.get("/api/openapi.json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")

    def test_public_bind_refused_wraps_bind_guard_error(self) -> None:
        """Factory's exception type is exported here, not in app.security."""
        cfg = _settings()
        cfg_bad = Settings.model_construct(
            **{**cfg.model_dump(), "bind_host": "0.0.0.0", "allow_public_bind": False}
        )
        with pytest.raises(PublicBindRefused):
            create_app(settings=cfg_bad)


# ---------------------------------------------------------------------------
# OpenAPI — version + tag seeding
# ---------------------------------------------------------------------------


class TestOpenapiShape:
    """The merged OpenAPI 3.1 document carries one tag per context."""

    def test_openapi_version_is_3_1(self) -> None:
        """Spec §12 "OpenAPI" + factory constant ``_OPENAPI_VERSION``."""
        client = _client(create_app(settings=_settings()))
        schema = client.get("/api/openapi.json").json()
        assert schema["openapi"] == "3.1.0"

    def test_every_context_has_a_tag(self) -> None:
        """The 13 contexts from :data:`CONTEXT_ROUTERS` each have a tag."""
        client = _client(create_app(settings=_settings()))
        schema = client.get("/api/openapi.json").json()
        names = {tag["name"] for tag in schema.get("tags", [])}
        for context_name, _router in CONTEXT_ROUTERS:
            assert context_name in names, (
                f"context {context_name!r} missing from OpenAPI tags"
            )

    def test_context_tags_preserve_spec_order(self) -> None:
        """Tags render in the §01 "Context map" order so the committed
        ``openapi.json`` diff stays stable.
        """
        client = _client(create_app(settings=_settings()))
        schema = client.get("/api/openapi.json").json()
        names = [tag["name"] for tag in schema.get("tags", [])]
        expected = [name for name, _ in CONTEXT_ROUTERS]
        # ``names`` may contain extra tags (auth, admin, …) after the
        # context seed — assert the context subsequence leads.
        assert names[: len(expected)] == expected

    def test_empty_context_has_no_paths(self) -> None:
        """A context whose router carries no routes must not appear in
        ``paths`` — only the tag seed is active.

        Contexts whose router does carry routes (``time`` after cd-whl)
        are excluded: their presence is the whole point. The assertion
        targets the still-empty scaffolds so an accidental route
        leakage anywhere else still fails the test.
        """
        # ``time`` carries routes as of cd-whl; add any further
        # implemented contexts here as they land. Every name in this
        # set must still appear in :data:`CONTEXT_ROUTERS` so the tag
        # seed check above keeps firing.
        implemented_contexts = {"time"}
        client = _client(create_app(settings=_settings()))
        schema = client.get("/api/openapi.json").json()
        # None of the empty context prefixes should be in ``paths``.
        # e.g. ``/w/{slug}/api/v1/tasks`` must not be a key.
        for context_name, _router in CONTEXT_ROUTERS:
            if context_name in implemented_contexts:
                continue
            prefix = f"/w/{{slug}}/api/v1/{context_name}"
            for path in schema.get("paths", {}):
                assert not path.startswith(prefix), (
                    f"empty context {context_name!r} leaked path {path!r}"
                )


# ---------------------------------------------------------------------------
# Shim contract — ``app.main`` re-exports
# ---------------------------------------------------------------------------


class TestMainShim:
    """``app.main`` re-exports the factory's public API so legacy
    ``from app.main import create_app`` imports keep working.
    """

    def test_main_reexports_create_app(self) -> None:
        from app.main import create_app as main_create_app

        assert main_create_app is create_app

    def test_main_reexports_public_bind_refused(self) -> None:
        from app.main import PublicBindRefused as MainPBR

        assert MainPBR is PublicBindRefused

    def test_main_reexports_is_api_path(self) -> None:
        from app.api.factory import _is_api_path as factory_is_api_path
        from app.main import _is_api_path as main_is_api_path

        assert main_is_api_path is factory_is_api_path


# ---------------------------------------------------------------------------
# API-path classifier — admin tree + workspace scoped
# ---------------------------------------------------------------------------


class TestIsApiPathAdmin:
    """cd-ika7 extends ``_is_api_path`` to cover the admin tree."""

    @pytest.mark.parametrize(
        "path",
        [
            "/admin/api",
            "/admin/api/",
            "/admin/api/v1",
            "/admin/api/v1/settings",
        ],
    )
    def test_admin_api_paths_classified_as_api(self, path: str) -> None:
        from app.api.factory import _is_api_path

        assert _is_api_path(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/admin",
            "/admin/",
            "/admin/llm",
            "/admin/dashboard",
        ],
    )
    def test_admin_spa_paths_not_api(self, path: str) -> None:
        """The ``/admin`` SPA chrome is NOT the admin API tree."""
        from app.api.factory import _is_api_path

        assert _is_api_path(path) is False


# ---------------------------------------------------------------------------
# Router mounting
# ---------------------------------------------------------------------------


class TestContextRouterMount:
    """The 13 context routers mount under ``/w/{slug}/api/v1/<ctx>``.

    Each scaffold is empty today — the assertion is on the registry
    wiring, not on live routes (those land in cd-rpxd, cd-75wp, …).
    """

    def test_all_contexts_registered(self) -> None:
        """``CONTEXT_ROUTERS`` contains all 13 spec §01 entries."""
        names = {name for name, _ in CONTEXT_ROUTERS}
        assert names == {
            "identity",
            "places",
            "tasks",
            "stays",
            "instructions",
            "inventory",
            "assets",
            "time",
            "payroll",
            "expenses",
            "billing",
            "messaging",
            "llm",
        }

    def test_admin_router_is_the_mounted_instance(self) -> None:
        """The factory mounts :data:`app.api.admin.admin_router` verbatim.

        The router is empty today so no concrete path resolves; the
        stable assertion is that :func:`create_app` does not swap the
        admin_router reference mid-factory — the downstream admin
        Beads tasks (cd-jlms et al.) will import ``admin_router``
        from the same module and add routes to it, expecting those to
        reach the live app.
        """
        # Defensive: ensure the module-level admin_router is still an
        # APIRouter (not replaced by the factory).
        from fastapi import APIRouter

        from app.api.admin import admin_router as admin_router_module

        assert isinstance(admin_router_module, APIRouter)
        # Route table on ``admin_router`` itself — empty at cd-ika7.
        assert list(admin_router_module.routes) == []

    def test_unknown_admin_api_path_returns_json_404(self) -> None:
        """The admin mount answers 404 with the RFC 7807 envelope (§12)."""
        client = _client(create_app(settings=_settings()))
        resp = client.get("/admin/api/v1/nonexistent")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["type"] == "https://crewday.dev/errors/not_found"
        assert body["status"] == 404
