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
        # ``time`` carries routes as of cd-whl; ``tasks`` as of cd-sn26;
        # ``expenses`` as of cd-t6y2; ``messaging`` as of cd-0bnz.
        # Add any further implemented contexts here as they land. Every
        # name in this set must still appear in :data:`CONTEXT_ROUTERS`
        # so the tag seed check above keeps firing. The workspace-scoped
        # admin aggregator (cd-g1ay) does NOT belong here — it lives
        # outside ``CONTEXT_ROUTERS`` and mounts through its own seam.
        implemented_contexts = {"expenses", "messaging", "tasks", "time"}
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
        """``CONTEXT_ROUTERS`` contains exactly the 13 §01 entries.

        The workspace-scoped admin aggregator (cd-g1ay) is a separate
        export (:data:`WORKSPACE_ADMIN_ROUTER`) — it isn't one of the
        §01 bounded contexts and folding it in here would dilute that
        invariant and seed a phantom ``admin`` tag in the OpenAPI.
        """
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

        The stable assertion is that :func:`create_app` does not
        swap the ``admin_router`` reference mid-factory — the
        downstream admin Beads tasks (cd-jlms et al.) will import
        ``admin_router`` from the same module and add routes to it,
        expecting those to reach the live app. cd-xgmu landed a
        single throwaway ``GET /_ping`` probe used by the auth-dep
        integration tests; the route is hidden from OpenAPI and
        does not change the public surface.
        """
        # Defensive: ensure the module-level admin_router is still an
        # APIRouter (not replaced by the factory).
        from fastapi import APIRouter
        from fastapi.routing import APIRoute

        from app.api.admin import admin_router as admin_router_module

        assert isinstance(admin_router_module, APIRouter)
        # The throwaway cd-xgmu probe route is the only route on the
        # admin_router today. Pin its presence + hidden-schema flag
        # so a future regression that exposes it on /api/openapi.json
        # trips this test loudly.
        ping_routes = [
            r
            for r in admin_router_module.routes
            if isinstance(r, APIRoute) and r.path == "/_ping"
        ]
        assert len(ping_routes) == 1
        assert ping_routes[0].include_in_schema is False

    def test_unknown_admin_api_path_returns_json_404(self) -> None:
        """The admin mount answers 404 with the RFC 7807 envelope (§12)."""
        client = _client(create_app(settings=_settings()))
        resp = client.get("/admin/api/v1/nonexistent")
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/problem+json")
        body = resp.json()
        assert body["type"] == "https://crewday.dev/errors/not_found"
        assert body["status"] == 404


# ---------------------------------------------------------------------------
# Workspace-scoped admin aggregator (cd-g1ay)
# ---------------------------------------------------------------------------


class TestWorkspaceAdminMount:
    """The workspace-admin aggregator mounts alongside context routers
    but is neither in ``CONTEXT_ROUTERS`` nor sharing the
    deployment-admin tree's ``admin`` tag.
    """

    def test_workspace_admin_router_exported(self) -> None:
        """:data:`WORKSPACE_ADMIN_ROUTER` is re-exported from
        :mod:`app.api.v1` for the factory to import.

        Name parity with :mod:`app.api.v1.admin` ``router`` matters —
        swapping the reference would lose route registrations made
        later on the imported symbol.
        """
        from fastapi import APIRouter

        from app.api.v1 import WORKSPACE_ADMIN_ROUTER
        from app.api.v1.admin import router as admin_module_router

        assert isinstance(WORKSPACE_ADMIN_ROUTER, APIRouter)
        assert WORKSPACE_ADMIN_ROUTER is admin_module_router

    def test_admin_is_not_a_context(self) -> None:
        """``admin`` must not appear in ``CONTEXT_ROUTERS`` — doing so
        would seed a phantom OpenAPI tag and claim the URL segment
        through the context-fan-out loop.
        """
        names = {name for name, _ in CONTEXT_ROUTERS}
        assert "admin" not in names

    def test_workspace_admin_tag_is_workspace_admin_not_admin(self) -> None:
        """Operations from :data:`WORKSPACE_ADMIN_ROUTER` tag as
        ``workspace_admin`` — the deployment admin tree owns ``admin``.
        """
        client = _client(create_app(settings=_settings()))
        schema = client.get("/api/openapi.json").json()
        admin_signups = (
            schema["paths"].get("/w/{slug}/api/v1/admin/signups", {}).get("get")
        )
        assert admin_signups is not None, "workspace admin signups mount missing"
        assert "workspace_admin" in admin_signups.get("tags", []), (
            "expected workspace-admin ops tagged 'workspace_admin', "
            f"got {admin_signups.get('tags')}"
        )
        assert "admin" not in admin_signups.get("tags", []), (
            "workspace-admin ops must not tag 'admin' — that clashes "
            "with the deployment-admin tree's tag"
        )

    def test_workspace_admin_operation_id_prefix(self) -> None:
        """Operation IDs use ``workspace_admin.*`` — ``admin.*`` is
        reserved for the host-CLI-only ``crewday admin`` group (§13).
        """
        client = _client(create_app(settings=_settings()))
        schema = client.get("/api/openapi.json").json()
        op = schema["paths"]["/w/{slug}/api/v1/admin/signups"]["get"]
        assert op["operationId"] == "workspace_admin.signups.list"

    def test_workspace_admin_cli_group_not_reserved(self) -> None:
        """``x-cli.group`` is ``workspace-admin`` — neither the
        host-only ``admin`` nor the deployment-HTTP ``deploy`` (§13).
        """
        client = _client(create_app(settings=_settings()))
        schema = client.get("/api/openapi.json").json()
        op = schema["paths"]["/w/{slug}/api/v1/admin/signups"]["get"]
        cli = op.get("x-cli", {})
        assert cli.get("group") == "workspace-admin"
        assert cli.get("group") != "admin"
        assert cli.get("group") != "deploy"

    def test_workspace_admin_tag_is_defined_with_description(self) -> None:
        """Schema-level ``tags[]`` carries a ``workspace_admin``
        definition — without this, Swagger UI renders the section
        with no description since FastAPI doesn't auto-populate tag
        definitions from operation-level tag references.
        """
        client = _client(create_app(settings=_settings()))
        schema = client.get("/api/openapi.json").json()
        tag_defs = {t["name"]: t for t in schema.get("tags", [])}
        assert "workspace_admin" in tag_defs
        assert tag_defs["workspace_admin"].get("description")


# ---------------------------------------------------------------------------
# Storage dep wiring (cd-6vq5)
# ---------------------------------------------------------------------------


class TestStorageWiring:
    """``_build_storage`` composes the :class:`Storage` backend and stashes
    it on :attr:`app.state.storage` so the :func:`get_storage` dep resolves
    without a 503. Covers the degraded paths (missing root key, s3 backend)
    too — each one must surface a ``None`` that the dep turns into a 503
    rather than crashing the boot.
    """

    def test_localfs_backend_wires_storage_on_app_state(self) -> None:
        """Default ``localfs`` + a root key → a live :class:`Storage` on app state."""
        from app.adapters.storage.localfs import LocalFsStorage

        app = create_app(settings=_settings())
        # ``app.state.storage`` is the seam ``get_storage`` reads.
        storage = getattr(app.state, "storage", None)
        assert isinstance(storage, LocalFsStorage), (
            f"expected LocalFsStorage on app.state.storage, got {type(storage)!r}"
        )

    def test_build_storage_localfs_returns_localfs_impl(self) -> None:
        """``localfs`` + a root key composes :class:`LocalFsStorage`.

        Exercises :func:`_build_storage` directly rather than the full
        factory so the test does not depend on the passkey / mailer /
        lifespan wiring that also reads ``Settings``. If
        :func:`_build_storage` ever grows new branches, this is the
        narrow seam that pins the default path.
        """
        from app.adapters.storage.localfs import LocalFsStorage
        from app.api.factory import _build_storage

        storage = _build_storage(_settings())
        assert isinstance(storage, LocalFsStorage), (
            f"expected LocalFsStorage, got {type(storage)!r}"
        )

    def test_build_storage_without_root_key_returns_none(self) -> None:
        """Missing ``root_key`` surfaces as ``None``, not a crash.

        :func:`app.auth.keys.derive_subkey` raises
        :class:`KeyDerivationError` when the root key is absent; the
        factory catches it and returns ``None``. The rest of the boot
        path (passkey router, CSRF, …) still needs the root key and
        will refuse to build — but that's a separate failure mode; the
        storage helper alone must degrade gracefully so an operator
        who only inspects ``CREWDAY_ROOT_KEY unset`` sees one
        coherent warning per concern.
        """
        from app.api.factory import _build_storage

        cfg = _settings()
        cfg_no_key = Settings.model_construct(**{**cfg.model_dump(), "root_key": None})
        assert _build_storage(cfg_no_key) is None

    def test_build_storage_s3_backend_returns_none(self) -> None:
        """``s3`` is a valid setting value but the wiring is deferred
        (cd-6vq5 sibling recipe). Until then :func:`_build_storage`
        returns ``None`` and the router's dep turns that into a 503
        — exactly the shape a deploy that flipped the env var without
        finishing the credentials wiring deserves.
        """
        from app.api.factory import _build_storage

        cfg = _settings()
        cfg_s3 = Settings.model_construct(
            **{**cfg.model_dump(), "storage_backend": "s3"}
        )
        assert _build_storage(cfg_s3) is None

    def test_get_storage_dep_returns_live_backend_in_default_build(self) -> None:
        """End-to-end: a fresh :class:`TestClient` resolves the storage
        dep without the 503 ``storage_unavailable`` shape. Exercises
        the full dep chain (``app.state.storage`` hydration +
        :func:`get_storage` read) so a future refactor that moves the
        wiring elsewhere still trips this assertion.

        The smoke path POSTs to a route that depends on ``get_storage``
        (``/api/v1/me/avatar``) with no session cookie — we expect a
        401, not a 503. A 503 here means the storage dep fired before
        the auth check did, which is the exact regression we're
        guarding against.
        """
        client = _client(create_app(settings=_settings()))
        resp = client.post(
            "/api/v1/me/avatar",
            files={"image": ("a.png", b"pngbytes", "image/png")},
        )
        # 401 because no cookie; anything else (especially 503) would
        # mean the storage wiring regressed.
        assert resp.status_code == 401, resp.text
