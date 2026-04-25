"""Unit tests for the bare-host ``/api/v1/me/workspaces`` router (cd-y5z3).

Exercises :func:`app.api.v1.auth.me.build_me_workspaces_router` against
a minimal FastAPI instance — no factory, no tenancy middleware, no
CSRF middleware. Mirrors the shape of
:mod:`tests.unit.api.v1.auth.test_me_avatar`: in-memory SQLite, a real
session-cookie issued via :func:`app.auth.session.issue`, and per-test
seeding via :mod:`tests.factories.identity`.

Coverage:

* GET without a session cookie → 401.
* GET returns only the caller's workspaces (cross-user isolation).
* GET returns the dedicated switcher shape: ``workspace_id``, ``slug``,
  ``name``, ``current_role``, ``last_seen_at``, ``settings_override``.
* GET returns ``[]`` for a freshly-signed-up user with no memberships.

See ``docs/specs/12-rest-api.md`` §"Auth" — ``GET /api/v1/me/workspaces``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

# Import every model-bearing package so :data:`Base.metadata` resolves
# every FK the identity tables reference (mirrors test_me_avatar).
from app.adapters.db import audit, authz, workspace  # noqa: F401
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import me as me_module
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

_TEST_UA: str = "pytest-me-workspaces"
_TEST_ACCEPT_LANGUAGE: str = "en"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-me-workspaces-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
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
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Mount the workspaces router on a minimal FastAPI.

    Pinned ``User-Agent`` + ``Accept-Language`` headers so every request
    through the client carries the fingerprint the seeded session was
    minted under (matches the validate gate).
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(
        me_module.build_me_workspaces_router(),
        prefix="/api/v1",
    )

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


def _issue_cookie(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a live session for ``user_id`` and return the raw cookie value."""
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


def _seed_owner_workspace(
    session_factory: sessionmaker[Session],
    *,
    slug: str,
    name: str,
    email: str,
) -> tuple[str, str]:
    """Seed (workspace, owner user) and return ``(user_id, workspace_id)``."""
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name=email.split("@")[0])
        ws = bootstrap_workspace(s, slug=slug, name=name, owner_user_id=user.id)
        s.commit()
        return user.id, ws.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSessionGate:
    """The endpoint is bare-host and requires a session cookie."""

    def test_no_cookie_returns_401(self, client: TestClient) -> None:
        r = client.get("/api/v1/me/workspaces")
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_required"

    def test_invalid_cookie_returns_401(self, client: TestClient) -> None:
        client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session")
        r = client.get("/api/v1/me/workspaces")
        assert r.status_code == 401, r.text
        assert r.json()["detail"]["error"] == "session_invalid"


class TestPayloadShape:
    """Switcher payload carries the cd-y5z3 fields per row."""

    def test_returns_caller_workspaces_with_richer_shape(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id, workspace_id = _seed_owner_workspace(
            session_factory,
            slug="ws-alpha",
            name="Alpha",
            email="alpha-owner@example.com",
        )
        cookie = _issue_cookie(session_factory, user_id=user_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        r = client.get("/api/v1/me/workspaces")
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, list)
        assert len(body) == 1
        row = body[0]
        # Critical fields per cd-y5z3 task spec.
        assert set(row.keys()) >= {
            "workspace_id",
            "slug",
            "name",
            "current_role",
            "last_seen_at",
            "settings_override",
        }
        assert row["workspace_id"] == workspace_id
        assert row["slug"] == "ws-alpha"
        assert row["name"] == "Alpha"
        # The owner is in the `owners` permission group; per §03 the
        # SPA expects the role to surface as ``manager`` for governance
        # routing — owners-group membership collapses into the manager
        # surface for v1.
        assert row["current_role"] == "manager"
        # last_seen_at is the freshly-issued session's last_seen_at —
        # the cookie was just minted, so the timestamp must be present.
        # ``Session.workspace_id`` is None at issue time (the user has
        # not picked a workspace yet), so the JOIN does NOT pick up
        # the row — we expect ``None`` here, validating that
        # ``last_seen_at`` reflects per-workspace activity rather than
        # any session for the user.
        assert row["last_seen_at"] is None
        # ``settings_override`` is empty for a fresh workspace; never
        # ``None`` — the contract is "always a dict".
        assert row["settings_override"] == {}

    def test_returns_empty_list_for_user_without_memberships(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A signed-up user with no workspace memberships sees ``[]``."""
        with session_factory() as s:
            user = bootstrap_user(
                s,
                email="loner@example.com",
                display_name="Loner",
            )
            s.commit()
            user_id = user.id
        cookie = _issue_cookie(session_factory, user_id=user_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        r = client.get("/api/v1/me/workspaces")
        assert r.status_code == 200, r.text
        assert r.json() == []


class TestIsolation:
    """Cross-user / cross-workspace isolation."""

    def test_only_returns_callers_workspaces(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """Workspaces another user owns are NOT visible on the caller's payload."""
        # Seed two unrelated workspaces with different owners.
        owner_a, ws_a_id = _seed_owner_workspace(
            session_factory,
            slug="ws-alpha",
            name="Alpha",
            email="alpha-owner@example.com",
        )
        _seed_owner_workspace(
            session_factory,
            slug="ws-beta",
            name="Beta",
            email="beta-owner@example.com",
        )
        cookie = _issue_cookie(session_factory, user_id=owner_a, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)

        body = client.get("/api/v1/me/workspaces").json()
        assert {row["workspace_id"] for row in body} == {ws_a_id}
        assert {row["slug"] for row in body} == {"ws-alpha"}


class TestRoleResolution:
    """``current_role`` reflects the caller's surface grant per workspace."""

    def test_worker_grant_surfaces_as_worker(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        """A worker (no owners-group membership) shows ``current_role='worker'``.

        The owner of the workspace seeds the workspace; we then attach
        a fresh worker user with a ``worker`` :class:`RoleGrant` row +
        a :class:`UserWorkspace` membership. The workspace switcher
        should surface that user as a worker, NOT promote them to
        manager.
        """
        from datetime import UTC, datetime

        owner_id, ws_id = _seed_owner_workspace(
            session_factory,
            slug="ws-roles",
            name="Roles",
            email="roles-owner@example.com",
        )
        del owner_id  # only the workspace bootstrap is needed
        with session_factory() as s:
            worker = bootstrap_user(
                s,
                email="rolesworker@example.com",
                display_name="Roles Worker",
            )
            now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
            s.add(
                RoleGrant(
                    id=new_ulid(),
                    workspace_id=ws_id,
                    user_id=worker.id,
                    grant_role="worker",
                    scope_property_id=None,
                    created_at=now,
                    created_by_user_id=None,
                )
            )
            s.add(
                UserWorkspace(
                    user_id=worker.id,
                    workspace_id=ws_id,
                    source="workspace_grant",
                    added_at=now,
                )
            )
            s.commit()
            worker_id = worker.id

        cookie = _issue_cookie(session_factory, user_id=worker_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        body = client.get("/api/v1/me/workspaces").json()
        assert len(body) == 1
        assert body[0]["current_role"] == "worker"


class TestSettingsOverride:
    """``settings_override`` is the workspace's ``settings_json`` blob."""

    def test_settings_override_round_trips(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        user_id, ws_id = _seed_owner_workspace(
            session_factory,
            slug="ws-settings",
            name="Settings",
            email="settings-owner@example.com",
        )
        # Stamp a non-empty settings_json so the round-trip is real.
        with session_factory() as s, tenant_agnostic():
            ws = s.get(Workspace, ws_id)
            assert ws is not None
            ws.settings_json = {"branding.primary_color": "#123456"}
            s.commit()
        cookie = _issue_cookie(session_factory, user_id=user_id, settings=settings)
        client.cookies.set(SESSION_COOKIE_NAME, cookie)
        body = client.get("/api/v1/me/workspaces").json()
        assert body[0]["settings_override"] == {"branding.primary_color": "#123456"}
