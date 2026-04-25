"""Unit tests for :mod:`app.api.admin.deps` (cd-xgmu).

Exercises :func:`current_deployment_admin_principal` end-to-end against
an in-memory SQLite engine with :class:`Base.metadata` schema. The dep
is mounted on a throwaway FastAPI app whose only route is the cd-xgmu
``GET /admin/api/v1/_ping`` probe carried by
:mod:`app.api.admin.__init__`; we drive the dep through that route
rather than calling the function directly so the test exercises the
production wiring (cookie aliases, header lookup, ``request.headers``
read) verbatim.

Coverage matches the cd-xgmu acceptance criteria:

* Session principal — admin grant present → 200, full
  :data:`DEPLOYMENT_SCOPE_CATALOG` returned;
* Session principal — non-admin user → 404;
* Session principal — invalid / unknown cookie → 404;
* Token principal — scoped token with ``deployment.llm:read`` → 200,
  scope set narrowed to the row's keys;
* Token principal — scoped token with workspace-only scopes → 404;
* Token principal — scoped token mixing ``deployment:*`` with workspace
  scopes → 422 ``deployment_scope_conflict``;
* Token principal — delegated token whose delegating user is a
  deployment admin → 200, full catalogue;
* Token principal — delegated token whose delegating user is NOT a
  deployment admin → 404;
* No auth material at all → 404.

See ``docs/specs/12-rest-api.md`` §"Admin surface" and
``docs/specs/03-auth-and-tokens.md`` §"API tokens".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

# Eagerly load every ORM model the production factory imports so
# ``Base.metadata.create_all`` below stays consistent regardless of
# pytest collection order. Without these imports the suite passes
# when this module collects first (Base only carries the explicit
# imports below) but fails when an integration test runs first and
# fills Base.metadata with rows whose FKs target tables we'd
# otherwise skip (e.g. ``places.property_work_role_assignment`` →
# ``payroll.pay_rule``).
import app.adapters.db.payroll.models
import app.adapters.db.places.models  # noqa: F401
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.api.admin import admin_router
from app.api.admin.deps import DEPLOYMENT_SCOPE_CONFLICT_ERROR
from app.api.deps import db_session as db_session_dep
from app.api.errors import add_exception_handlers
from app.auth import tokens as auth_tokens
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import (
    DEPLOYMENT_SCOPE_CATALOG,
    WorkspaceContext,
    tenant_agnostic,
)
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_TEST_UA = "pytest-deployment-admin"
_TEST_ACCEPT_LANGUAGE = "en"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    """Pinned :class:`Settings` so the session pepper is deterministic."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-deployment-admin-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Per-test in-memory engine with every ORM table created."""
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """:class:`TestClient` mounting the admin router under ``/admin/api/v1``.

    Patches :func:`app.auth.session.get_settings` so the session pepper
    matches between the seed :func:`issue` and the dep's
    :func:`validate`. A dep override on
    :func:`app.api.deps.db_session` plumbs every request through a
    rolled-back ``Session`` bound to ``engine`` so subsequent
    assertions read from the same store the dep wrote to.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(admin_router, prefix="/admin/api/v1")
    add_exception_handlers(app)

    def _session() -> Iterator[Session]:
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[db_session_dep] = _session

    with TestClient(
        app,
        base_url="https://testserver",
        headers={
            "User-Agent": _TEST_UA,
            "Accept-Language": _TEST_ACCEPT_LANGUAGE,
        },
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_user(session: Session, *, tag: str) -> str:
    """Insert a :class:`User` row; return its id."""
    user = bootstrap_user(
        session,
        email=f"{tag}@example.com",
        display_name=tag.capitalize(),
    )
    return user.id


def _seed_workspace(session: Session, *, slug: str) -> str:
    """Insert a workspace row; return its id."""
    workspace_id = new_ulid()
    with tenant_agnostic():
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=slug.title(),
                plan="free",
                quota_json={},
                created_at=_PINNED,
            )
        )
        session.flush()
    return workspace_id


def _grant_deployment_admin(session: Session, *, user_id: str) -> None:
    """Plant a deployment-scope ``role_grant`` row for ``user_id``."""
    with tenant_agnostic():
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=None,
                user_id=user_id,
                grant_role="manager",
                scope_kind="deployment",
                created_at=_PINNED,
            )
        )
        session.flush()


def _issue_session(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a real session row; return the raw cookie value."""
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
    """Mint a scoped token. Returns the plaintext token string."""
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
            label="test scoped token",
            scopes=scopes,
            expires_at=None,
            kind="scoped",
            now=_PINNED,
        )
        s.commit()
        return result.token


def _mint_delegated_token(
    session_factory: sessionmaker[Session],
    *,
    minter_user_id: str,
    delegate_for_user_id: str,
    workspace_id: str,
    workspace_slug: str,
) -> str:
    """Mint a delegated token. Returns the plaintext token string."""
    with session_factory() as s:
        ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
            actor_id=minter_user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id=new_ulid(),
        )
        result = auth_tokens.mint(
            s,
            ctx,
            user_id=minter_user_id,
            label="test delegated token",
            scopes={},
            expires_at=None,
            kind="delegated",
            delegate_for_user_id=delegate_for_user_id,
            now=_PINNED,
        )
        s.commit()
        return result.token


# ---------------------------------------------------------------------------
# Session-principal arm
# ---------------------------------------------------------------------------


class TestSessionPrincipal:
    """Session-cookie auth path."""

    def test_admin_session_resolves_to_full_catalogue(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A session for a deployment-admin user admits with the full catalogue."""
        with session_factory() as s:
            user_id = _seed_user(s, tag="depl-admin")
            _grant_deployment_admin(s, user_id=user_id)
            s.commit()

        cookie_value = _issue_session(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        resp = client.get("/admin/api/v1/_ping")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["actor_kind"] == "user"
        assert body["user_id"] == user_id
        # Session principals carry the full catalogue.
        assert set(body["scopes"]) == DEPLOYMENT_SCOPE_CATALOG

    def test_non_admin_session_returns_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A live session whose user has no deployment grant 404s."""
        with session_factory() as s:
            user_id = _seed_user(s, tag="not-admin")
            s.commit()

        cookie_value = _issue_session(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        resp = client.get("/admin/api/v1/_ping")
        assert resp.status_code == 404, resp.text
        body = resp.json()
        # Problem+json envelope from the registered error handler.
        assert body["status"] == 404
        assert body["type"].endswith("/not_found")
        # The dep's ``error`` payload survives ``HTTPException.detail``
        # → envelope ``extra`` lift in :mod:`app.api.errors`.
        assert body.get("error") == "not_found"

    def test_unknown_session_cookie_returns_404(self, client: TestClient) -> None:
        """A cookie value that does not match any row 404s."""
        client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-cookie-value")

        resp = client.get("/admin/api/v1/_ping")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"

    def test_no_auth_material_returns_404(self, client: TestClient) -> None:
        """A request with neither cookie nor header 404s."""
        resp = client.get("/admin/api/v1/_ping")
        assert resp.status_code == 404
        assert resp.json().get("error") == "not_found"


# ---------------------------------------------------------------------------
# Token-principal arm
# ---------------------------------------------------------------------------


class TestTokenPrincipal:
    """Bearer-token auth path."""

    def test_scoped_token_with_deployment_scope_admits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A scoped token carrying a single ``deployment.*`` key admits."""
        with session_factory() as s:
            user_id = _seed_user(s, tag="agent-owner")
            workspace_id = _seed_workspace(s, slug="agent-ws")
            s.commit()

        token = _mint_scoped_token(
            session_factory,
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_slug="agent-ws",
            scopes={"deployment.llm:read": True},
        )

        resp = client.get(
            "/admin/api/v1/_ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["actor_kind"] == "agent"
        assert body["user_id"] == user_id
        # Scoped tokens carry only the row's scope set, not the
        # full catalogue.
        assert body["scopes"] == ["deployment.llm:read"]

    def test_scoped_token_with_only_workspace_scopes_returns_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A workspace-only scoped token probing the admin surface 404s."""
        with session_factory() as s:
            user_id = _seed_user(s, tag="ws-agent")
            workspace_id = _seed_workspace(s, slug="ws-only")
            s.commit()

        token = _mint_scoped_token(
            session_factory,
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_slug="ws-only",
            scopes={"tasks:read": True},
        )

        resp = client.get(
            "/admin/api/v1/_ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json().get("error") == "not_found"

    def test_mixed_scopes_return_422_deployment_scope_conflict(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """Mixing ``deployment:*`` with workspace scopes triggers 422."""
        with session_factory() as s:
            user_id = _seed_user(s, tag="mixed-agent")
            workspace_id = _seed_workspace(s, slug="mixed-ws")
            s.commit()

        token = _mint_scoped_token(
            session_factory,
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_slug="mixed-ws",
            scopes={"deployment.llm:read": True, "tasks:read": True},
        )

        resp = client.get(
            "/admin/api/v1/_ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 422, resp.text
        body = resp.json()
        assert body["status"] == 422
        # The detail dict ``error`` key is lifted into the envelope's
        # extra fields by :mod:`app.api.errors`.
        assert body.get("error") == DEPLOYMENT_SCOPE_CONFLICT_ERROR

    def test_unknown_bearer_token_returns_404(self, client: TestClient) -> None:
        """A malformed / unknown bearer token 404s."""
        resp = client.get(
            "/admin/api/v1/_ping",
            headers={"Authorization": "Bearer mip_notreal_notreal"},
        )
        assert resp.status_code == 404

    def test_delegated_token_for_admin_admits_with_full_catalogue(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A delegated token whose delegating user is a deployment admin admits."""
        with session_factory() as s:
            admin_id = _seed_user(s, tag="depl-admin-deleg")
            _grant_deployment_admin(s, user_id=admin_id)
            workspace_id = _seed_workspace(s, slug="deleg-ws")
            s.commit()

        token = _mint_delegated_token(
            session_factory,
            minter_user_id=admin_id,
            delegate_for_user_id=admin_id,
            workspace_id=workspace_id,
            workspace_slug="deleg-ws",
        )

        resp = client.get(
            "/admin/api/v1/_ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["actor_kind"] == "delegated"
        assert body["user_id"] == admin_id
        assert set(body["scopes"]) == DEPLOYMENT_SCOPE_CATALOG

    def test_delegated_token_for_non_admin_returns_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """A delegated token whose delegating user is not a deployment admin 404s."""
        with session_factory() as s:
            user_id = _seed_user(s, tag="not-admin-deleg")
            workspace_id = _seed_workspace(s, slug="not-admin-deleg-ws")
            s.commit()

        token = _mint_delegated_token(
            session_factory,
            minter_user_id=user_id,
            delegate_for_user_id=user_id,
            workspace_id=workspace_id,
            workspace_slug="not-admin-deleg-ws",
        )

        resp = client.get(
            "/admin/api/v1/_ping",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json().get("error") == "not_found"


# ---------------------------------------------------------------------------
# Bearer header parsing edge cases
# ---------------------------------------------------------------------------


class TestBearerHeaderParsing:
    """Defensive parsing around the ``Authorization`` header."""

    def test_lowercase_bearer_scheme_admits(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        """``bearer`` (lowercase) is the same as ``Bearer`` — RFC 6750."""
        with session_factory() as s:
            user_id = _seed_user(s, tag="case-bearer")
            workspace_id = _seed_workspace(s, slug="case-bearer-ws")
            s.commit()

        token = _mint_scoped_token(
            session_factory,
            user_id=user_id,
            workspace_id=workspace_id,
            workspace_slug="case-bearer-ws",
            scopes={"deployment.llm:read": True},
        )

        resp = client.get(
            "/admin/api/v1/_ping",
            headers={"Authorization": f"bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    def test_empty_bearer_token_falls_through_to_session(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """``Bearer `` with no value is treated as no bearer — session arm wins."""
        with session_factory() as s:
            user_id = _seed_user(s, tag="empty-bearer")
            _grant_deployment_admin(s, user_id=user_id)
            s.commit()

        cookie_value = _issue_session(
            session_factory, user_id=user_id, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        resp = client.get(
            "/admin/api/v1/_ping",
            headers={"Authorization": "Bearer "},
        )
        # Empty bearer → fallthrough to session cookie → admin session
        # → 200.
        assert resp.status_code == 200, resp.text
        assert resp.json()["actor_kind"] == "user"
