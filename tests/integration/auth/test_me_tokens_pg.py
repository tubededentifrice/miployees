"""Integration test for ``/api/v1/me/tokens`` — personal access token surface.

Exercises the bare-host PAT router end-to-end against a real engine
(SQLite by default; Postgres when ``CREWDAY_TEST_DB=postgres``),
driving the FastAPI router with a live passkey session.

Coverage:

* ``POST /me/tokens`` happy path — returns plaintext once, row
  carries ``kind='personal'`` + ``workspace_id=NULL`` +
  ``subject_user_id`` populated.
* ``POST /me/tokens`` validation — empty scopes (422
  ``scopes_required``); a workspace scope mixed in (422
  ``me_scope_conflict``).
* ``POST /me/tokens`` cap — 6th PAT for the same user returns 422
  ``too_many_personal_tokens``.
* ``GET /me/tokens`` — returns only the caller's PATs, never
  someone else's.
* ``DELETE /me/tokens/{id}`` — revokes the caller's own PAT;
  revoking another user's PAT or a workspace token id returns 404.
* Auth — a request without a session cookie returns 401.

See ``docs/specs/03-auth-and-tokens.md`` §"Personal access tokens"
and ``docs/specs/12-rest-api.md`` §"Auth / me / tokens".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.identity.models import Session as SessionRow
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import me_tokens as me_tokens_module
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from app.tenancy import tenant_agnostic
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


# Pinned UA / Accept-Language so the :func:`validate` fingerprint
# gate agrees with the seed :func:`issue` call. Matches the shape in
# test_logout.py.
_TEST_UA: str = "pytest-me-tokens"
_TEST_ACCEPT_LANGUAGE: str = "en"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-me-tokens-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seed_user(session_factory: sessionmaker[Session]) -> Iterator[str]:
    """Seed a user row and yield its id. Cleans up on teardown."""
    from app.util.ulid import new_ulid

    tag = new_ulid()[-8:].lower()
    email = f"pat-{tag}@example.com"
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name="PAT User")
        user_id = user.id
        s.commit()
    yield user_id
    # Cascade wipe: ApiToken, Session, User. No workspace to clean.
    with session_factory() as s, tenant_agnostic():
        for tok in s.scalars(select(ApiToken).where(ApiToken.user_id == user_id)).all():
            s.delete(tok)
        for sess in s.scalars(
            select(SessionRow).where(SessionRow.user_id == user_id)
        ).all():
            s.delete(sess)
        u = s.get(User, user_id)
        if u is not None:
            s.delete(u)
        s.commit()


@pytest.fixture
def client(
    engine: Engine,
    settings: Settings,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """:class:`TestClient` mounted on the me_tokens router.

    Patches :func:`app.auth.session.get_settings` so the session
    pepper matches between the seed :func:`issue` and the router's
    :func:`validate`. Uses a dep override for the UoW so writes land
    on ``engine`` and subsequent assertions read the committed rows.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(
        me_tokens_module.build_me_tokens_router(),
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


def _issue_session(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a real session row for ``user_id``; return the raw cookie value."""
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestMeTokensHttpFlow:
    """Full POST → GET → DELETE loop for personal access tokens."""

    def test_mint_then_list_then_revoke(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        # 1. Mint
        r = client.post(
            "/api/v1/me/tokens",
            json={
                "label": "kitchen-printer",
                "scopes": {"me.tasks:read": True, "me.bookings:read": True},
                "expires_at_days": 90,
            },
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["token"].startswith("mip_")
        assert body["kind"] == "personal"
        key_id = body["key_id"]

        # Row carries the PAT shape.
        with session_factory() as s:
            row = s.get(ApiToken, key_id)
            assert row is not None
            assert row.kind == "personal"
            assert row.workspace_id is None
            assert row.subject_user_id == seed_user
            assert row.delegate_for_user_id is None

        # 2. List — the PAT appears in the caller's /me/tokens list.
        r_list = client.get("/api/v1/me/tokens")
        assert r_list.status_code == 200, r_list.text
        rows = r_list.json()
        assert len(rows) == 1
        assert rows[0]["key_id"] == key_id
        assert rows[0]["kind"] == "personal"
        # The subject-side list never surfaces the delegate_for_user_id
        # discriminator because the surface is dedicated to PATs.
        assert "delegate_for_user_id" not in rows[0]
        # §03 "Personal access tokens": plaintext `token` is returned
        # ONLY on the 201 mint response — never again. cd-rpxd
        # acceptance criterion #3 — regression-pinned so a future
        # schema edit that re-surfaces the secret fails loudly.
        assert "token" not in rows[0]

        # 3. Revoke — 204.
        r_del = client.delete(f"/api/v1/me/tokens/{key_id}")
        assert r_del.status_code == 204, r_del.text

        # Listing still includes the row, flagged revoked.
        r_list2 = client.get("/api/v1/me/tokens")
        assert r_list2.status_code == 200
        rows2 = r_list2.json()
        assert rows2[0]["key_id"] == key_id
        assert rows2[0]["revoked_at"] is not None

    def test_empty_scopes_is_422_scopes_required(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post(
            "/api/v1/me/tokens",
            json={"label": "bad", "scopes": {}},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "scopes_required"

    def test_workspace_scope_is_422_me_scope_conflict(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.post(
            "/api/v1/me/tokens",
            json={
                "label": "bad",
                "scopes": {"me.tasks:read": True, "tasks:read": True},
            },
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "me_scope_conflict"

    def test_sixth_pat_is_422_too_many_personal(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        for i in range(5):
            r = client.post(
                "/api/v1/me/tokens",
                json={
                    "label": f"pat-{i}",
                    "scopes": {"me.tasks:read": True},
                },
            )
            assert r.status_code == 201, r.text
        r = client.post(
            "/api/v1/me/tokens",
            json={"label": "6th", "scopes": {"me.tasks:read": True}},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["error"] == "too_many_personal_tokens"

    def test_no_session_cookie_is_401(self, client: TestClient) -> None:
        client.cookies.clear()
        r = client.post(
            "/api/v1/me/tokens",
            json={"label": "no-sess", "scopes": {"me.tasks:read": True}},
        )
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"

    def test_get_without_session_cookie_is_401(self, client: TestClient) -> None:
        """``GET /me/tokens`` shares the session-required gate."""
        client.cookies.clear()
        r = client.get("/api/v1/me/tokens")
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"

    def test_delete_without_session_cookie_is_401(self, client: TestClient) -> None:
        """``DELETE /me/tokens/{id}`` shares the session-required gate."""
        client.cookies.clear()
        r = client.delete("/api/v1/me/tokens/01HWA00000000000000000NOPE")
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "session_required"

    def test_delete_unknown_token_is_404(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        r = client.delete("/api/v1/me/tokens/01HWA00000000000000000NOPE")
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "token_not_found"
