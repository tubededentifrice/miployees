"""Integration test for the deployment-admin auth dep (cd-xgmu).

Boots the full :func:`app.api.factory.create_app` against the
integration harness's DB and drives the throwaway
``GET /admin/api/v1/_ping`` probe (mounted by
:mod:`app.api.admin.__init__`) end-to-end. Verifies the dep wires
through every middleware (CORS, security headers, workspace tenancy
skip-paths, idempotency, CSRF) and that the SKIP_PATHS contract
holds — ``/admin/api/v1/...`` does NOT try to resolve a workspace
slug, the dep alone authorises the request.

Coverage:

* Session principal — deployment admin → 200, full catalogue;
* Token principal — scoped token with ``deployment.audit:read`` →
  200, scope set narrowed to the row's keys;
* No auth → 404;
* Non-admin session → 404;
* Mixed-scope token → 422 ``deployment_scope_conflict``.

See ``docs/specs/12-rest-api.md`` §"Admin surface".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.admin.deps import DEPLOYMENT_SCOPE_CONFLICT_ERROR
from app.auth import tokens as auth_tokens
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.main import create_app
from app.tenancy import (
    DEPLOYMENT_SCOPE_CATALOG,
    WorkspaceContext,
    tenant_agnostic,
)
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


_TEST_UA = "pytest-admin-auth-integration"
_TEST_ACCEPT_LANGUAGE = "en"
_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_settings(db_url: str) -> Settings:
    """Settings bound to the integration harness DB URL."""
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-admin-auth-root-key"),
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
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def real_make_uow(engine: Engine) -> Iterator[None]:
    """Point the process-wide UoW at the integration engine.

    Mirrors :func:`tests.integration.api.test_openapi_shape.real_make_uow`
    — the production factory boots without a session-factory override,
    so we redirect :data:`app.adapters.db.session._default_sessionmaker_`
    at the integration engine for the test's lifetime.
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


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    pinned_settings: Settings,
    real_make_uow: None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Live :class:`TestClient` against :func:`create_app`.

    Patches :func:`app.auth.session.get_settings` so the session
    pepper matches between the seed :func:`issue` calls and the
    factory's middleware-side validates.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: pinned_settings)
    app = create_app(settings=pinned_settings)
    with TestClient(
        app,
        base_url="https://testserver",
        headers={
            "User-Agent": _TEST_UA,
            "Accept-Language": _TEST_ACCEPT_LANGUAGE,
        },
        raise_server_exceptions=False,
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_admin(session_factory: sessionmaker[Session], *, tag: str) -> str:
    """Seed a user + active deployment grant; return the user id."""
    with session_factory() as s:
        user = bootstrap_user(
            s, email=f"{tag}@example.com", display_name=tag.capitalize()
        )
        with tenant_agnostic():
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=None,
                    user_id=user.id,
                    grant_role="manager",
                    scope_kind="deployment",
                    created_at=_PINNED,
                )
            )
            s.flush()
        s.commit()
        return user.id


def _seed_user(session_factory: sessionmaker[Session], *, tag: str) -> str:
    """Seed a plain user (no deployment grant). Return the user id."""
    with session_factory() as s:
        user = bootstrap_user(
            s, email=f"{tag}@example.com", display_name=tag.capitalize()
        )
        s.commit()
        return user.id


def _seed_workspace(session_factory: sessionmaker[Session], *, slug: str) -> str:
    """Seed a minimal workspace; return its id."""
    with session_factory() as s:
        workspace_id = new_ulid()
        with tenant_agnostic():
            s.add(
                Workspace(
                    id=workspace_id,
                    slug=slug,
                    name=slug.title(),
                    plan="free",
                    quota_json={},
                    created_at=_PINNED,
                )
            )
            s.flush()
        s.commit()
        return workspace_id


def _issue_session(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a real session row; return the cookie value."""
    with session_factory() as s:
        result = issue(
            s,
            user_id=user_id,
            has_owner_grant=False,
            ua=_TEST_UA,
            ip="127.0.0.1",
            accept_language=_TEST_ACCEPT_LANGUAGE,
            settings=settings,
        )
        s.commit()
        return result.cookie_value


def _mint_scoped_token(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    workspace_id: str,
    workspace_slug: str,
    scopes: dict[str, object],
) -> str:
    """Mint a scoped token; return the plaintext token."""
    with session_factory() as s:
        ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
            actor_id=user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id=new_ulid(),
        )
        result = auth_tokens.mint(
            s,
            ctx,
            user_id=user_id,
            label="integration scoped token",
            scopes=scopes,
            expires_at=None,
            kind="scoped",
        )
        s.commit()
        return result.token


def _wipe(session_factory: sessionmaker[Session]) -> None:
    """Sweep rows committed during a test so siblings see a clean slate."""
    with session_factory() as s, tenant_agnostic():
        for model in (
            ApiToken,
            SessionRow,
            UserWorkspace,
            RoleGrant,
            AuditLog,
            Workspace,
            User,
        ):
            for row in s.scalars(select(model)).all():
                s.delete(row)
        s.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdminPingDep:
    """``GET /admin/api/v1/_ping`` end-to-end through the production factory."""

    def test_admin_session_returns_200_with_full_catalogue(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        try:
            user_id = _seed_admin(session_factory, tag="adm-int")
            cookie_value = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

            resp = client.get("/admin/api/v1/_ping")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["actor_kind"] == "user"
            assert body["user_id"] == user_id
            assert set(body["scopes"]) == DEPLOYMENT_SCOPE_CATALOG
        finally:
            _wipe(session_factory)

    def test_scoped_token_with_deployment_scope_admits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        try:
            user_id = _seed_user(session_factory, tag="agent-int")
            workspace_id = _seed_workspace(session_factory, slug="agent-int-ws")

            token = _mint_scoped_token(
                session_factory,
                user_id=user_id,
                workspace_id=workspace_id,
                workspace_slug="agent-int-ws",
                scopes={"deployment.audit:read": True},
            )

            resp = client.get(
                "/admin/api/v1/_ping",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["actor_kind"] == "agent"
            assert body["scopes"] == ["deployment.audit:read"]
        finally:
            _wipe(session_factory)

    def test_no_auth_returns_404(self, client: TestClient) -> None:
        """A ping request with no auth material 404s through the canonical envelope."""
        resp = client.get("/admin/api/v1/_ping")
        assert resp.status_code == 404, resp.text
        body = resp.json()
        # The factory wires :func:`add_exception_handlers` so the
        # 404 surfaces through the RFC 7807 envelope (§12 "Errors").
        assert body["status"] == 404
        assert body["type"].endswith("/not_found")
        assert body.get("error") == "not_found"

    def test_non_admin_session_returns_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        pinned_settings: Settings,
    ) -> None:
        """A live session whose user has no deployment grant 404s."""
        try:
            user_id = _seed_user(session_factory, tag="non-admin-int")
            cookie_value = _issue_session(
                session_factory, user_id=user_id, settings=pinned_settings
            )
            client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

            resp = client.get("/admin/api/v1/_ping")
            assert resp.status_code == 404, resp.text
            assert resp.json().get("error") == "not_found"
        finally:
            _wipe(session_factory)

    def test_mixed_scope_token_returns_422_deployment_scope_conflict(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Mixing ``deployment:*`` with workspace scopes triggers the typed 422."""
        try:
            user_id = _seed_user(session_factory, tag="mixed-int")
            workspace_id = _seed_workspace(session_factory, slug="mixed-int-ws")

            token = _mint_scoped_token(
                session_factory,
                user_id=user_id,
                workspace_id=workspace_id,
                workspace_slug="mixed-int-ws",
                scopes={
                    "deployment.audit:read": True,
                    "tasks:read": True,
                },
            )

            resp = client.get(
                "/admin/api/v1/_ping",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 422, resp.text
            body = resp.json()
            assert body["status"] == 422
            assert body.get("error") == DEPLOYMENT_SCOPE_CONFLICT_ERROR
        finally:
            _wipe(session_factory)
