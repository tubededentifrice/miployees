"""Tests for the ``/w/<slug>/...`` tenancy middleware.

Two layers:

* **Stub layer** — exercises the skip-path / bare-``/w`` / slug-pattern
  / correlation-id / ContextVar-cleanup mechanics against the
  Phase-0 stub path, enabled per-test by flipping
  :attr:`~app.config.Settings.phase0_stub_enabled` on. No DB.
* **Real-resolver layer** — exercises :func:`resolve_actor` and
  :func:`resolve_workspace` against an in-memory SQLite engine with
  the schema created from the declarative metadata. Covers session
  cookies, bearer tokens, membership lookups, cross-workspace
  rejection, owner-role derivation, and the timing-equalisation
  dummy read.

See docs/specs/01-architecture.md §"Workspace addressing" and
§"WorkspaceContext"; docs/specs/03-auth-and-tokens.md §"Sessions" +
§"API tokens"; docs/specs/15-security-privacy.md §"Constant-time
cross-tenant responses".
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.authz.bootstrap import seed_owners_system_group
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.auth.session import SESSION_COOKIE_NAME
from app.auth.session import issue as issue_session
from app.auth.tokens import mint as mint_token
from app.config import Settings, get_settings
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import _current_ctx, get_current
from app.tenancy.middleware import (
    CORRELATION_ID_HEADER,
    SKIP_PATHS,
    TEST_ACTOR_ID_HEADER,
    TEST_WORKSPACE_ID_HEADER,
    ActorIdentity,
    WorkspaceContextMiddleware,
    resolve_actor,
    resolve_workspace,
)
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

# ULID is 26 chars of Crockford base32: 0-9 and A-Z minus I, L, O, U.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_current_ctx() -> Iterator[None]:
    """Guarantee no WorkspaceContext bleeds between tests."""
    token = _current_ctx.set(None)
    try:
        yield
    finally:
        _current_ctx.reset(token)


@pytest.fixture
def stub_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Return a :class:`Settings` with the Phase-0 stub **enabled**.

    Monkeypatches :func:`app.config.get_settings` so the middleware
    (which calls ``get_settings()`` fresh per request) sees the flag
    on without paying the lru_cache clear dance. Flip is scoped to
    the test.
    """
    settings = Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-middleware-root-key"),
        phase0_stub_enabled=True,
    )
    monkeypatch.setattr("app.tenancy.middleware.get_settings", lambda: settings)
    yield settings


@pytest.fixture
def real_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Return a :class:`Settings` with the Phase-0 stub **disabled**.

    Production default — the real resolver runs, ``X-Test-*`` headers
    are ignored. Exposed as a fixture rather than implicit so the
    intent is obvious at the test site.
    """
    settings = Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-middleware-root-key"),
        phase0_stub_enabled=False,
    )
    monkeypatch.setattr("app.tenancy.middleware.get_settings", lambda: settings)
    yield settings


@pytest.fixture
def memory_engine() -> Iterator[Engine]:
    """Shared in-memory SQLite engine with every app table created."""
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def memory_factory(memory_engine: Engine) -> sessionmaker[Session]:
    """Session factory bound to the in-memory engine with tenant filter installed."""
    factory = sessionmaker(bind=memory_engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture
def real_make_uow(
    monkeypatch: pytest.MonkeyPatch,
    memory_engine: Engine,
    memory_factory: sessionmaker[Session],
) -> Iterator[None]:
    """Redirect ``make_uow()`` inside the middleware to the in-memory engine.

    The middleware's real-resolver path opens a
    :class:`UnitOfWorkImpl` per scoped request; patching the module-
    level default engine + sessionmaker keeps the HTTP test suite
    self-contained without touching env vars.
    """
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = memory_engine
    _session_mod._default_sessionmaker_ = memory_factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory
    # Defensive: clear lru_cache on the real settings loader in case
    # a test under this fixture ever triggered it (most don't).
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Test-app scaffolding
# ---------------------------------------------------------------------------


def _build_app(*, captured: list[WorkspaceContext | None] | None = None) -> FastAPI:
    """Construct a minimal FastAPI app with the middleware installed."""
    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware)

    @app.get("/w/{slug}/api/v1/ping")
    def scoped_ping(slug: str) -> dict[str, object]:
        ctx = get_current()
        if captured is not None:
            captured.append(ctx)
        if ctx is None:
            return {"bound": False, "slug_from_path": slug}
        return {
            "bound": True,
            "workspace_id": ctx.workspace_id,
            "workspace_slug": ctx.workspace_slug,
            "actor_id": ctx.actor_id,
            "actor_kind": ctx.actor_kind,
            "actor_grant_role": ctx.actor_grant_role,
            "actor_was_owner_member": ctx.actor_was_owner_member,
            "audit_correlation_id": ctx.audit_correlation_id,
        }

    @app.get("/w/{slug}/api/v1/boom")
    def scoped_boom(slug: str) -> dict[str, str]:
        raise ValueError("kaboom")

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    @app.get("/signup")
    def signup_get() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    @app.post("/signup/start")
    def signup_start() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    @app.get("/w")
    def bare_w() -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None}

    @app.get("/w/{slug}")
    def bare_w_slug(slug: str) -> dict[str, object]:
        return {"ok": True, "bound": get_current() is not None, "slug": slug}

    return app


def _client(app: FastAPI | None = None) -> TestClient:
    return TestClient(app or _build_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Phase-0 stub — happy path + mechanics
# ---------------------------------------------------------------------------


class TestPhase0Stub:
    """Legacy stub behaviour, preserved behind ``phase0_stub_enabled=True``."""

    def test_scoped_request_binds_workspace_context(
        self, stub_settings: Settings
    ) -> None:
        captured: list[WorkspaceContext | None] = []
        app = _build_app(captured=captured)

        with _client(app) as client:
            response = client.get(
                "/w/villa-sud/api/v1/ping",
                headers={
                    TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A",
                    TEST_ACTOR_ID_HEADER: "01US000000000000000000000B",
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["bound"] is True
        assert body["workspace_id"] == "01WS000000000000000000000A"
        assert body["workspace_slug"] == "villa-sud"
        assert body["actor_id"] == "01US000000000000000000000B"
        assert body["actor_kind"] == "user"
        assert body["actor_grant_role"] == "manager"
        assert body["actor_was_owner_member"] is False

        correlation_id = response.headers[CORRELATION_ID_HEADER]
        assert correlation_id == body["audit_correlation_id"]
        assert _ULID_RE.match(correlation_id)

        assert len(captured) == 1
        assert captured[0] is not None
        assert get_current() is None

    def test_actor_id_defaults_to_fresh_ulid_when_header_missing(
        self, stub_settings: Settings
    ) -> None:
        app = _build_app()
        with _client(app) as client:
            response = client.get(
                "/w/villa-sud/api/v1/ping",
                headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
            )
        assert response.status_code == 200
        actor_id = response.json()["actor_id"]
        assert _ULID_RE.match(actor_id)

    def test_missing_phase0_workspace_header_returns_404(
        self, stub_settings: Settings
    ) -> None:
        with _client() as client:
            response = client.get("/w/villa-sud/api/v1/ping")
        assert response.status_code == 404
        assert response.json() == {"error": "not_found", "detail": None}
        assert CORRELATION_ID_HEADER in response.headers

    def test_stub_header_ignored_when_flag_off(
        self, real_settings: Settings, real_make_uow: None
    ) -> None:
        """With the flag off, ``X-Test-Workspace-Id`` is ignored.

        The real resolver runs, finds no authenticated actor, and 404s.
        This is the production-safety guarantee of cd-iwsv — a binary
        deployed with the stub off cannot be tricked into synthesising
        a manager-level context by a spoofed header.
        """
        with _client() as client:
            response = client.get(
                "/w/villa-sud/api/v1/ping",
                headers={
                    TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A",
                    TEST_ACTOR_ID_HEADER: "01US000000000000000000000B",
                },
            )
        assert response.status_code == 404
        assert response.json() == {"error": "not_found", "detail": None}


# ---------------------------------------------------------------------------
# Rejection paths — every branch is 404 (never 403)
# ---------------------------------------------------------------------------


class TestSlugRejection:
    """Every slug-pattern / reserved-list miss renders a constant 404."""

    def test_invalid_slug_pattern_returns_404(self, stub_settings: Settings) -> None:
        with _client() as client:
            response = client.get(
                "/w/UPPER/api/v1/ping",
                headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
            )
        assert response.status_code == 404
        assert CORRELATION_ID_HEADER in response.headers
        assert response.json() == {"error": "not_found", "detail": None}

    def test_reserved_slug_returns_404(self, stub_settings: Settings) -> None:
        with _client() as client:
            response = client.get(
                "/w/admin/api/v1/ping",
                headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
            )
        assert response.status_code == 404

    def test_consecutive_hyphen_slug_returns_404(self, stub_settings: Settings) -> None:
        with _client() as client:
            response = client.get(
                "/w/foo--bar/api/v1/ping",
                headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
            )
        assert response.status_code == 404

    def test_url_encoded_bad_slug_returns_404(self, stub_settings: Settings) -> None:
        with _client() as client:
            response = client.get(
                "/w/%20badslug/api/v1/ping",
                headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
            )
        assert response.status_code == 404

    def test_empty_slug_returns_404(self, stub_settings: Settings) -> None:
        with _client() as client:
            response = client.get(
                "/w//api/v1/ping",
                headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
            )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Skip paths pass through un-scoped
# ---------------------------------------------------------------------------


class TestSkipPaths:
    """Bare-host routes never enter resolver code."""

    def test_healthz_is_skip_path(self) -> None:
        with _client() as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "bound": False}
        assert CORRELATION_ID_HEADER in response.headers
        assert _ULID_RE.match(response.headers[CORRELATION_ID_HEADER])

    def test_signup_get_is_skip_path(self) -> None:
        with _client() as client:
            response = client.get("/signup")
        assert response.status_code == 200
        assert response.json()["bound"] is False

    def test_signup_post_child_is_skip_path(self) -> None:
        with _client() as client:
            response = client.post("/signup/start")
        assert response.status_code == 200
        assert response.json()["bound"] is False

    def test_bare_w_is_skip_path(self) -> None:
        with _client() as client:
            response = client.get("/w")
        assert response.status_code == 200
        assert response.json()["bound"] is False

    def test_bare_w_slug_is_skip_path(self) -> None:
        with _client() as client:
            response = client.get("/w/villa-sud")
        assert response.status_code == 200
        assert response.json()["bound"] is False
        assert response.json()["slug"] == "villa-sud"

    def test_spec_bare_host_routes_are_skip_paths(self) -> None:
        """Every bare-host surface called out by specs §01/§12/§14 is listed."""
        for path in (
            "/healthz",
            "/readyz",
            "/version",
            "/signup",
            "/login",
            "/recover",
            "/select-workspace",
            "/api/openapi.json",
            "/docs",
            "/redoc",
        ):
            assert path in SKIP_PATHS, (
                f"§01 bare-host route {path} missing from SKIP_PATHS"
            )
        assert "/api/v1" in SKIP_PATHS
        assert "/admin" in SKIP_PATHS
        assert "/auth/magic" in SKIP_PATHS
        assert "/auth/passkey" in SKIP_PATHS
        assert "/me/email/verify" in SKIP_PATHS

    def test_auth_passkey_child_is_skip_path(self) -> None:
        app = _build_app()

        @app.post("/auth/passkey/signup/register/start")
        def passkey_child() -> dict[str, object]:
            return {"ok": True, "bound": get_current() is not None}

        with _client(app) as client:
            response = client.post("/auth/passkey/signup/register/start")
        assert response.status_code == 200
        assert response.json()["bound"] is False

    def test_admin_child_is_skip_path(self) -> None:
        app = _build_app()

        @app.get("/admin/dashboard")
        def admin_dashboard() -> dict[str, object]:
            return {"ok": True, "bound": get_current() is not None}

        with _client(app) as client:
            response = client.get("/admin/dashboard")
        assert response.status_code == 200
        assert response.json()["bound"] is False

    def test_bare_w_slug_trailing_slash_is_skip_path(self) -> None:
        with _client() as client:
            response = client.get("/w/villa-sud/", follow_redirects=True)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Correlation id propagation
# ---------------------------------------------------------------------------


class TestCorrelationId:
    def test_correlation_id_echoed_from_request(self, stub_settings: Settings) -> None:
        incoming = "01RQ000000000000000000000C"
        with _client() as client:
            response = client.get(
                "/w/villa-sud/api/v1/ping",
                headers={
                    TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A",
                    CORRELATION_ID_HEADER: incoming,
                },
            )
        assert response.status_code == 200
        assert response.headers[CORRELATION_ID_HEADER] == incoming
        assert response.json()["audit_correlation_id"] == incoming

    def test_correlation_id_echoed_on_skip_path(self) -> None:
        incoming = "01RQ000000000000000000000D"
        with _client() as client:
            response = client.get("/healthz", headers={CORRELATION_ID_HEADER: incoming})
        assert response.status_code == 200
        assert response.headers[CORRELATION_ID_HEADER] == incoming

    def test_correlation_id_minted_when_missing(self) -> None:
        with _client() as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        minted = response.headers[CORRELATION_ID_HEADER]
        assert _ULID_RE.match(minted)


# ---------------------------------------------------------------------------
# ContextVar cleanup
# ---------------------------------------------------------------------------


class TestContextVarCleanup:
    def test_context_does_not_leak_across_requests(
        self, stub_settings: Settings
    ) -> None:
        captured: list[WorkspaceContext | None] = []
        app = _build_app(captured=captured)
        with _client(app) as client:
            first = client.get(
                "/w/villa-sud/api/v1/ping",
                headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
            )
            second = client.get(
                "/w/villa-sud/api/v1/ping",
                headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000B"},
            )
        assert first.status_code == 200
        assert second.status_code == 200
        assert len(captured) == 2
        assert captured[0] is not None
        assert captured[1] is not None
        assert captured[0] is not captured[1]
        assert captured[0].workspace_id == "01WS000000000000000000000A"
        assert captured[1].workspace_id == "01WS000000000000000000000B"
        assert get_current() is None

    def test_context_cleanup_on_handler_exception(
        self, stub_settings: Settings
    ) -> None:
        app = _build_app()
        with _client(app) as client:
            response = client.get(
                "/w/villa-sud/api/v1/boom",
                headers={TEST_WORKSPACE_ID_HEADER: "01WS000000000000000000000A"},
            )
        assert response.status_code == 500
        assert get_current() is None


# ---------------------------------------------------------------------------
# Real resolver — pure-function layer
# ---------------------------------------------------------------------------


def _seed_workspace_and_member(
    db: Session,
    *,
    slug: str,
    owner_email: str,
    make_owner: bool = True,
) -> tuple[Workspace, str]:
    """Seed a workspace + one user with an optional owners-group membership.

    Returns ``(workspace, user_id)``. ``user_workspace`` is always
    populated so the resolver finds an active membership row. The
    ``owners@<ws>`` anchor is seeded via
    :func:`seed_owners_system_group` when ``make_owner`` is True — it
    needs the user_id, so we stage it after the user + workspace land.
    """
    from app.tenancy import tenant_agnostic

    # justification: test seeding runs before any WorkspaceContext
    # exists; the workspace/user_workspace/permission_group writes
    # need the tenant filter off.
    user = bootstrap_user(db, email=owner_email, display_name=owner_email)
    workspace_id = new_ulid()
    with tenant_agnostic():
        ws = Workspace(
            id=workspace_id,
            slug=slug,
            name=slug.replace("-", " ").title(),
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
        db.add(ws)
        db.flush()
        db.add(
            UserWorkspace(
                user_id=user.id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
        db.flush()
        if make_owner:
            ctx = WorkspaceContext(
                workspace_id=workspace_id,
                workspace_slug=slug,
                actor_id=user.id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id=new_ulid(),
            )
            seed_owners_system_group(
                db,
                ctx,
                workspace_id=workspace_id,
                owner_user_id=user.id,
            )
    db.flush()
    return ws, user.id


class TestResolveWorkspaceUnit:
    """:func:`resolve_workspace` — pure-function behaviour."""

    def test_happy_path_owner(self, memory_factory: sessionmaker[Session]) -> None:
        """An owners-group member resolves with ``actor_was_owner_member=True``."""
        with memory_factory() as db:
            ws, user_id = _seed_workspace_and_member(
                db, slug="owner-path", owner_email="owner@example.com"
            )
            db.commit()
        with memory_factory() as db:
            actor = ActorIdentity(
                user_id=user_id,
                kind="user",
                workspace_id=None,
                token_id=None,
                session_id="sess-pk",
            )
            ctx = resolve_workspace(
                "/w/owner-path/api/v1/ping",
                actor,
                db,
                audit_correlation_id="corr-1",
            )
        assert ctx is not None
        assert ctx.workspace_id == ws.id
        assert ctx.workspace_slug == "owner-path"
        assert ctx.actor_id == user_id
        assert ctx.actor_kind == "user"
        assert ctx.actor_was_owner_member is True
        # Owners fall back to ``manager`` when no explicit role_grant.
        assert ctx.actor_grant_role == "manager"
        assert ctx.audit_correlation_id == "corr-1"

    def test_non_owner_member_is_not_owner(
        self, memory_factory: sessionmaker[Session]
    ) -> None:
        """A member without an owners-group row gets ``is_owner=False``."""
        with memory_factory() as db:
            # Owner seeds the workspace + owners anchor; a second user
            # is added to ``user_workspace`` without owners membership.
            ws, owner_id = _seed_workspace_and_member(
                db, slug="mixed-team", owner_email="alice@example.com"
            )
            other = bootstrap_user(db, email="bob@example.com", display_name="Bob")
            from app.tenancy import tenant_agnostic

            # justification: seeding a secondary membership pre-ctx —
            # the ORM tenant filter has no context to enforce here.
            with tenant_agnostic():
                db.add(
                    UserWorkspace(
                        user_id=other.id,
                        workspace_id=ws.id,
                        source="workspace_grant",
                        added_at=_PINNED,
                    )
                )
                db.add(
                    RoleGrant(
                        id=new_ulid(),
                        workspace_id=ws.id,
                        user_id=other.id,
                        grant_role="worker",
                        scope_property_id=None,
                        created_at=_PINNED,
                        created_by_user_id=owner_id,
                    )
                )
            db.commit()
        with memory_factory() as db:
            actor = ActorIdentity(
                user_id=other.id,
                kind="user",
                workspace_id=None,
                token_id=None,
                session_id="sess-bob",
            )
            ctx = resolve_workspace(
                "/w/mixed-team/api/v1/ping",
                actor,
                db,
                audit_correlation_id="corr-2",
            )
        assert ctx is not None
        assert ctx.actor_was_owner_member is False
        assert ctx.actor_grant_role == "worker"

    def test_highest_priority_grant_wins(
        self, memory_factory: sessionmaker[Session]
    ) -> None:
        """Multiple role grants collapse to the highest-priority role."""
        with memory_factory() as db:
            ws, owner_id = _seed_workspace_and_member(
                db, slug="multi-role", owner_email="root@example.com"
            )
            polymath = bootstrap_user(db, email="many@example.com", display_name="Many")
            from app.tenancy import tenant_agnostic

            # justification: seeding a secondary membership + grants
            # pre-ctx — the ORM tenant filter has no context yet.
            with tenant_agnostic():
                db.add(
                    UserWorkspace(
                        user_id=polymath.id,
                        workspace_id=ws.id,
                        source="workspace_grant",
                        added_at=_PINNED,
                    )
                )
                for role in ("worker", "client", "manager"):
                    db.add(
                        RoleGrant(
                            id=new_ulid(),
                            workspace_id=ws.id,
                            user_id=polymath.id,
                            grant_role=role,
                            scope_property_id=None,
                            created_at=_PINNED,
                            created_by_user_id=owner_id,
                        )
                    )
            db.commit()
        with memory_factory() as db:
            actor = ActorIdentity(
                user_id=polymath.id,
                kind="user",
                workspace_id=None,
                token_id=None,
                session_id="sess-many",
            )
            ctx = resolve_workspace(
                "/w/multi-role/api/v1/ping",
                actor,
                db,
                audit_correlation_id="corr-3",
            )
        assert ctx is not None
        assert ctx.actor_grant_role == "manager"

    def test_slug_miss_returns_none(
        self, memory_factory: sessionmaker[Session]
    ) -> None:
        with memory_factory() as db:
            # No workspaces seeded — any slug misses.
            actor = ActorIdentity(
                user_id="01US0000000000000000000ONE",
                kind="user",
                workspace_id=None,
                token_id=None,
                session_id="sess-x",
            )
            ctx = resolve_workspace(
                "/w/ghost-workspace/api/v1/ping",
                actor,
                db,
                audit_correlation_id="corr-miss",
            )
        assert ctx is None

    def test_member_miss_returns_none(
        self, memory_factory: sessionmaker[Session]
    ) -> None:
        """A logged-in user who is not a member of the slug's workspace 404s."""
        with memory_factory() as db:
            _ws, _owner_id = _seed_workspace_and_member(
                db, slug="members-only", owner_email="alice@example.com"
            )
            outsider = bootstrap_user(
                db, email="outsider@example.com", display_name="Outsider"
            )
            db.commit()
        with memory_factory() as db:
            actor = ActorIdentity(
                user_id=outsider.id,
                kind="user",
                workspace_id=None,
                token_id=None,
                session_id="sess-outsider",
            )
            ctx = resolve_workspace(
                "/w/members-only/api/v1/ping",
                actor,
                db,
                audit_correlation_id="corr-nonmem",
            )
        assert ctx is None

    def test_bearer_token_workspace_mismatch_returns_none(
        self, memory_factory: sessionmaker[Session]
    ) -> None:
        """A bearer token bound to workspace A against ``/w/slugB/`` 404s."""
        with memory_factory() as db:
            ws_a, user_id = _seed_workspace_and_member(
                db, slug="ws-a", owner_email="alice@example.com"
            )
            ws_b, _user_b = _seed_workspace_and_member(
                db, slug="ws-b", owner_email="bob@example.com"
            )
            # Membership of A matters for the happy-path check — but
            # we exercise the cross-workspace rejection, so this actor
            # is also in A, with a token bound to A, hitting B.
            db.commit()
        with memory_factory() as db:
            actor = ActorIdentity(
                user_id=user_id,
                kind="user",
                workspace_id=ws_a.id,  # token bound to A
                token_id="tok-1",
                session_id=None,
            )
            ctx = resolve_workspace(
                f"/w/{ws_b.slug}/api/v1/ping",  # hitting B
                actor,
                db,
                audit_correlation_id="corr-xws",
            )
        assert ctx is None

    def test_anonymous_on_known_slug_returns_none(
        self, memory_factory: sessionmaker[Session]
    ) -> None:
        with memory_factory() as db:
            _seed_workspace_and_member(
                db, slug="public-ish", owner_email="alice@example.com"
            )
            db.commit()
        with memory_factory() as db:
            ctx = resolve_workspace(
                "/w/public-ish/api/v1/ping",
                None,  # no actor
                db,
                audit_correlation_id="corr-anon",
            )
        assert ctx is None


# ---------------------------------------------------------------------------
# Real resolver — HTTP-layer
# ---------------------------------------------------------------------------


class TestRealResolverHTTP:
    """End-to-end middleware dispatch against the in-memory DB."""

    def test_session_cookie_resolves_workspace(
        self,
        real_settings: Settings,
        memory_factory: sessionmaker[Session],
        real_make_uow: None,
    ) -> None:
        """A valid session cookie + active membership → 200 with a bound ctx."""
        with memory_factory() as db:
            ws, user_id = _seed_workspace_and_member(
                db, slug="session-path", owner_email="alice@example.com"
            )
            # Stamp the session with the same UA + Accept-Language the
            # TestClient will echo below — cd-geqp's fingerprint gate
            # now fires in the middleware, so the issue-time pair has to
            # match the inbound headers or ``validate`` raises
            # :class:`SessionInvalid`.
            issued = issue_session(
                db,
                user_id=user_id,
                has_owner_grant=True,
                ua="testclient",
                ip="127.0.0.1",
                accept_language="",
                now=_PINNED,
                settings=real_settings,
            )
            db.commit()

        app = _build_app()
        with _client(app) as client:
            response = client.get(
                "/w/session-path/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["bound"] is True
        assert body["workspace_id"] == ws.id
        assert body["workspace_slug"] == "session-path"
        assert body["actor_id"] == user_id
        assert body["actor_was_owner_member"] is True
        assert body["actor_grant_role"] == "manager"

    def test_bearer_token_resolves_workspace(
        self,
        real_settings: Settings,
        memory_factory: sessionmaker[Session],
        real_make_uow: None,
    ) -> None:
        """A valid PAT scoped to the slug's workspace → 200 with a bound ctx."""
        with memory_factory() as db:
            ws, user_id = _seed_workspace_and_member(
                db, slug="token-path", owner_email="alice@example.com"
            )
            ctx = WorkspaceContext(
                workspace_id=ws.id,
                workspace_slug=ws.slug,
                actor_id=user_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id=new_ulid(),
            )
            minted = mint_token(
                db,
                ctx,
                user_id=user_id,
                label="bearer-test",
                scopes={"tasks.read": True},
                expires_at=None,
                now=_PINNED,
            )
            db.commit()

        app = _build_app()
        with _client(app) as client:
            response = client.get(
                "/w/token-path/api/v1/ping",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["bound"] is True
        assert body["workspace_id"] == ws.id
        assert body["workspace_slug"] == "token-path"
        assert body["actor_id"] == user_id
        assert body["actor_kind"] == "user"

    def test_bearer_wrong_workspace_returns_404(
        self,
        real_settings: Settings,
        memory_factory: sessionmaker[Session],
        real_make_uow: None,
    ) -> None:
        """A PAT bound to ws-a used against ``/w/ws-b/`` 404s."""
        with memory_factory() as db:
            ws_a, user_id = _seed_workspace_and_member(
                db, slug="ws-alpha", owner_email="alice@example.com"
            )
            ws_b, _owner_b = _seed_workspace_and_member(
                db, slug="ws-beta", owner_email="bob@example.com"
            )
            ctx_a = WorkspaceContext(
                workspace_id=ws_a.id,
                workspace_slug=ws_a.slug,
                actor_id=user_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id=new_ulid(),
            )
            minted = mint_token(
                db,
                ctx_a,
                user_id=user_id,
                label="cross-ws",
                scopes={"tasks.read": True},
                expires_at=None,
                now=_PINNED,
            )
            db.commit()

        app = _build_app()
        with _client(app) as client:
            response = client.get(
                f"/w/{ws_b.slug}/api/v1/ping",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        assert response.status_code == 404
        assert response.json() == {"error": "not_found", "detail": None}

    def test_no_auth_returns_404_not_401(
        self,
        real_settings: Settings,
        memory_factory: sessionmaker[Session],
        real_make_uow: None,
    ) -> None:
        """Anonymous hit on a known scoped path 404s (never 401).

        Spec §01 "Workspace addressing" pins this — a 401 would tell
        an attacker the slug is valid; 404 collapses "unknown slug"
        and "not a member" into one response shape.
        """
        with memory_factory() as db:
            _seed_workspace_and_member(
                db, slug="real-ws", owner_email="alice@example.com"
            )
            db.commit()

        app = _build_app()
        with _client(app) as client:
            response = client.get("/w/real-ws/api/v1/ping")
        assert response.status_code == 404
        assert response.json() == {"error": "not_found", "detail": None}

    def test_session_expired_returns_404(
        self,
        real_settings: Settings,
        memory_factory: sessionmaker[Session],
        real_make_uow: None,
    ) -> None:
        """An expired session cookie collapses to the same 404."""
        with memory_factory() as db:
            _ws, user_id = _seed_workspace_and_member(
                db, slug="expired-path", owner_email="alice@example.com"
            )
            # Issue the session, then hand-roll the expiry into the past.
            issued = issue_session(
                db,
                user_id=user_id,
                has_owner_grant=False,
                ua="test-ua",
                ip="127.0.0.1",
                now=_PINNED,
                settings=real_settings,
            )
            from app.adapters.db.identity.models import Session as SessionRow

            row = db.get(SessionRow, issued.session_id)
            assert row is not None
            row.expires_at = datetime(2020, 1, 1, tzinfo=UTC)
            db.flush()
            db.commit()

        app = _build_app()
        with _client(app) as client:
            response = client.get(
                "/w/expired-path/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )
        assert response.status_code == 404

    def test_session_for_non_member_returns_404(
        self,
        real_settings: Settings,
        memory_factory: sessionmaker[Session],
        real_make_uow: None,
    ) -> None:
        """A valid session, but for a user not in the slug's workspace → 404."""
        with memory_factory() as db:
            _ws, _owner_id = _seed_workspace_and_member(
                db, slug="only-members", owner_email="alice@example.com"
            )
            outsider = bootstrap_user(
                db, email="outsider@example.com", display_name="Outsider"
            )
            issued = issue_session(
                db,
                user_id=outsider.id,
                has_owner_grant=False,
                ua="test-ua",
                ip="127.0.0.1",
                now=_PINNED,
                settings=real_settings,
            )
            db.commit()

        app = _build_app()
        with _client(app) as client:
            response = client.get(
                "/w/only-members/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )
        assert response.status_code == 404
        assert response.json() == {"error": "not_found", "detail": None}

    def test_bearer_invalid_token_returns_404(
        self,
        real_settings: Settings,
        memory_factory: sessionmaker[Session],
        real_make_uow: None,
    ) -> None:
        """A syntactically-valid but unknown bearer token 404s without leak."""
        with memory_factory() as db:
            _seed_workspace_and_member(
                db, slug="real-ws-2", owner_email="alice@example.com"
            )
            db.commit()

        app = _build_app()
        with _client(app) as client:
            response = client.get(
                "/w/real-ws-2/api/v1/ping",
                headers={"Authorization": "Bearer mip_unknown_deadbeef"},
            )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# resolve_actor — direct tests
# ---------------------------------------------------------------------------


class TestResolveActor:
    """Direct tests for :func:`resolve_actor`, bypassing the HTTP layer."""

    def test_no_auth_returns_none(
        self, real_settings: Settings, memory_factory: sessionmaker[Session]
    ) -> None:
        from starlette.datastructures import Headers
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/w/foo/api/v1/ping",
            "headers": Headers({}).raw,
            "query_string": b"",
        }
        with memory_factory() as db:
            actor = resolve_actor(Request(scope), db, real_settings)
        assert actor is None

    def test_session_cookie_populates_session_id(
        self, real_settings: Settings, memory_factory: sessionmaker[Session]
    ) -> None:
        """The resolver stamps the session row's PK on the identity."""
        from starlette.datastructures import Headers
        from starlette.requests import Request

        from app.auth.session import hash_cookie_value

        with memory_factory() as db:
            _ws, user_id = _seed_workspace_and_member(
                db, slug="actor-check", owner_email="alice@example.com"
            )
            issued = issue_session(
                db,
                user_id=user_id,
                has_owner_grant=False,
                ua="ua",
                ip="ip",
                now=_PINNED,
                settings=real_settings,
            )
            db.commit()
        headers = Headers({"cookie": f"{SESSION_COOKIE_NAME}={issued.cookie_value}"})
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/w/actor-check/api/v1/ping",
            "headers": headers.raw,
            "query_string": b"",
        }
        with memory_factory() as db:
            actor = resolve_actor(Request(scope), db, real_settings)
        assert actor is not None
        assert actor.user_id == user_id
        assert actor.kind == "user"
        assert actor.workspace_id is None  # sessions are tenant-agnostic
        assert actor.token_id is None
        assert actor.session_id == hash_cookie_value(issued.cookie_value)

    def test_bearer_token_populates_token_id_and_workspace(
        self, real_settings: Settings, memory_factory: sessionmaker[Session]
    ) -> None:
        from starlette.datastructures import Headers
        from starlette.requests import Request

        with memory_factory() as db:
            ws, user_id = _seed_workspace_and_member(
                db, slug="tok-check", owner_email="alice@example.com"
            )
            ctx = WorkspaceContext(
                workspace_id=ws.id,
                workspace_slug=ws.slug,
                actor_id=user_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id=new_ulid(),
            )
            minted = mint_token(
                db,
                ctx,
                user_id=user_id,
                label="actor-tok",
                scopes={"tasks.read": True},
                expires_at=None,
                now=_PINNED,
            )
            db.commit()
        headers = Headers({"authorization": f"Bearer {minted.token}"})
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/w/tok-check/api/v1/ping",
            "headers": headers.raw,
            "query_string": b"",
        }
        with memory_factory() as db:
            actor = resolve_actor(Request(scope), db, real_settings)
        assert actor is not None
        assert actor.user_id == user_id
        assert actor.kind == "user"
        assert actor.workspace_id == ws.id
        assert actor.token_id == minted.key_id
        assert actor.session_id is None


# ---------------------------------------------------------------------------
# Constant-time envelope: slug-miss and membership-miss match bytes
# ---------------------------------------------------------------------------


class TestConstantTimeEnvelope:
    """Spec §15 — every 404 branch returns the identical body bytes."""

    def test_slug_miss_and_member_miss_envelopes_match(
        self,
        real_settings: Settings,
        memory_factory: sessionmaker[Session],
        real_make_uow: None,
    ) -> None:
        with memory_factory() as db:
            _ws, _owner_id = _seed_workspace_and_member(
                db, slug="members-club", owner_email="alice@example.com"
            )
            outsider = bootstrap_user(
                db, email="outsider@example.com", display_name="Outsider"
            )
            issued = issue_session(
                db,
                user_id=outsider.id,
                has_owner_grant=False,
                ua="ua",
                ip="ip",
                now=_PINNED,
                settings=real_settings,
            )
            db.commit()

        app = _build_app()
        with _client(app) as client:
            slug_miss = client.get(
                "/w/unknown-slug/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )
            member_miss = client.get(
                "/w/members-club/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )

        assert slug_miss.status_code == 404
        assert member_miss.status_code == 404
        # Byte-identical envelopes (§15 tenant-isolation test suite).
        assert slug_miss.content == member_miss.content
        assert slug_miss.json() == {"error": "not_found", "detail": None}
