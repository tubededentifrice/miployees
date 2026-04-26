"""Integration tests for ``POST /api/v1/auth/logout``.

Exercises the bare-host logout router end-to-end against a real engine
(SQLite by default; Postgres when ``CREWDAY_TEST_DB=postgres``). The
FastAPI :class:`TestClient` drives the router exactly as the SPA
would, and every assertion reads from the same DB the router writes
against.

Coverage:

* **Happy path.** A live session cookie yields 204 + clear Set-Cookie,
  the session row flips to invalidated, a follow-up ``GET /auth/me``
  with the same cookie returns 401, and one
  ``audit.session.invalidated`` row lands with ``cause="logout"``.
* **No-cookie path.** A request without the session cookie still
  returns 204 + clear Set-Cookie (best-effort), and no
  ``session.invalidated`` audit row is written.
* **Invalid cookie path.** A bogus cookie value still returns 204 +
  clear Set-Cookie and writes no audit row.
* **Already-invalidated cookie.** A cookie whose session was
  invalidated by a prior logout still returns 204 on the retry and
  does NOT double-audit.

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions",
``docs/specs/12-rest-api.md`` §"Auth", and
``docs/specs/15-security-privacy.md`` §"Session-invalidation causes".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import ApiToken, User
from app.adapters.db.identity.models import Session as SessionRow
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth import logout as logout_module
from app.api.v1.auth import me as me_module
from app.auth import session as auth_session
from app.auth.session import SESSION_COOKIE_NAME, issue
from app.config import Settings
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-logout-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


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
    """FastAPI :class:`TestClient` mounted on logout + me routers.

    ``/auth/me`` is included so the "follow-up GET returns 401" leg of
    the acceptance criterion has a real endpoint to hit. Both routers
    share the bare-host ``/api/v1`` prefix the production factory
    uses, so the URL shapes match the SPA's expectations.

    Each HTTP request opens its own Session against ``engine``,
    commits on clean exit, rolls back on exception — mirroring the
    production UoW shape so the router's writes are visible to the
    subsequent assertion reads.

    ``app.auth.session.get_settings`` is patched to return the test
    settings so the session-hash pepper is deterministic across the
    ``issue`` seed-step and the ``validate`` check the router runs.
    """
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    app = FastAPI()
    app.include_router(logout_module.build_logout_router(), prefix="/api/v1")
    app.include_router(me_module.build_me_router(), prefix="/api/v1")

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
            # Pinned UA + Accept-Language match the seed ``issue``
            # pair (see :data:`_TEST_UA` / :data:`_TEST_ACCEPT_LANGUAGE`)
            # so :func:`auth_session.validate` does not trip on the
            # §15 fingerprint gate.
            "User-Agent": _TEST_UA,
            "Accept-Language": _TEST_ACCEPT_LANGUAGE,
        },
    ) as c:
        yield c

    # Sweep committed rows so sibling integration tests see a clean
    # slate. Scoped to the families this test touches.
    with session_factory() as s:
        for table_model in (SessionRow, ApiToken, AuditLog, User):
            s.execute(delete(table_model))
        s.commit()


@pytest.fixture
def seed_user(session_factory: sessionmaker[Session]) -> Iterator[str]:
    """Seed one user row and yield its id.

    The row is cleaned up at teardown along with any sessions the
    test issued against it (cascade delete via the ``user_id`` FK on
    ``session``).
    """
    with session_factory() as s:
        user = bootstrap_user(s, email="logout@example.com", display_name="Logout")
        user_id = user.id
        s.commit()
    yield user_id
    with session_factory() as s:
        s.execute(delete(SessionRow).where(SessionRow.user_id == user_id))
        s.execute(delete(ApiToken).where(ApiToken.user_id == user_id))
        s.execute(delete(User).where(User.id == user_id))
        s.commit()


# Pinned UA + Accept-Language the test client stamps onto every
# request (see :func:`client` fixture). The router's fingerprint gate
# hashes both headers, so the seed ``issue`` call MUST use the same
# pair to keep :func:`validate` happy on the subsequent request — a
# mismatch trips ``SessionInvalid`` and the happy-path 200 never
# lands. Pinning here keeps the contract visible in one place.
_TEST_UA: str = "pytest-logout-test"
_TEST_ACCEPT_LANGUAGE: str = "en"


def _issue_session(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    settings: Settings,
) -> str:
    """Issue a live session for ``user_id`` and return the raw cookie value.

    Uses the domain :func:`issue` primitive directly so the session
    row is real and carries the same fingerprint / ua-hash shape the
    router's :func:`validate` call expects. Returns the opaque
    (base64url) cookie value the client would carry.
    """
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


def _audit_rows_for(
    session_factory: sessionmaker[Session], *, action: str
) -> list[AuditLog]:
    """Return every ``AuditLog`` row with ``action == action``."""
    with session_factory() as s:
        return list(s.scalars(select(AuditLog).where(AuditLog.action == action)).all())


def _session_row(
    session_factory: sessionmaker[Session], *, session_id: str
) -> SessionRow | None:
    """Return the persisted session row by id or ``None``."""
    with session_factory() as s:
        return s.get(SessionRow, session_id)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestLogoutHappyPath:
    """Valid session cookie → 204 + session row invalidated + audit row."""

    def test_returns_204_with_clear_set_cookie(
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

        response = client.post("/api/v1/auth/logout")

        assert response.status_code == 204
        # Empty body — Response(204) has no content.
        assert response.content == b""

        # Exactly one Set-Cookie header that drops the session cookie.
        set_cookies = response.headers.get_list("set-cookie")
        assert any(
            header.startswith(f"{SESSION_COOKIE_NAME}=;")
            and "Max-Age=0" in header
            and "Path=/" in header
            and "Secure" in header
            and "HttpOnly" in header
            and "SameSite=Lax" in header
            for header in set_cookies
        ), f"no clear-cookie header found in {set_cookies!r}"

    def test_session_row_flips_to_invalidated(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        session_id = auth_session.hash_cookie_value(cookie_value)
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 204

        row = _session_row(session_factory, session_id=session_id)
        assert row is not None, "session row was deleted; should be invalidated only"
        assert row.invalidated_at is not None
        assert row.invalidation_cause == "logout"

    def test_follow_up_auth_me_returns_401(
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

        # Pre-logout sanity: the cookie works on /auth/me.
        pre = client.get("/api/v1/auth/me")
        assert pre.status_code == 200, pre.text

        # httpx's ``TestClient`` auto-clears cookies from Set-Cookie
        # responses. Stash the raw cookie value so we can re-present
        # it on the follow-up request — the whole point of the check
        # is to prove the server rejects the *same cookie value* once
        # the session row is invalidated.
        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 204

        # Re-present the original cookie value on a fresh request.
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        follow_up = client.get("/api/v1/auth/me")
        assert follow_up.status_code == 401

    def test_one_audit_row_with_cause_logout(
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

        # Snapshot the pre-logout audit count for this action so any
        # rows from ``issue`` (``session.created``) don't pollute the
        # check.
        before = len(_audit_rows_for(session_factory, action="session.invalidated"))

        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 204

        rows = _audit_rows_for(session_factory, action="session.invalidated")
        assert len(rows) == before + 1
        row = rows[-1]
        assert row.diff["cause"] == "logout"
        assert row.diff["user_id"] == seed_user
        assert row.diff["count"] == 1


# ---------------------------------------------------------------------------
# No-cookie path
# ---------------------------------------------------------------------------


class TestLogoutNoCookie:
    """Best-effort: no cookie still returns 204 + clear header, no audit."""

    def test_no_cookie_returns_204_with_clear_header(
        self,
        client: TestClient,
    ) -> None:
        # No cookies set on the client.
        client.cookies.clear()

        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 204

        set_cookies = response.headers.get_list("set-cookie")
        assert any(
            header.startswith(f"{SESSION_COOKIE_NAME}=;") and "Max-Age=0" in header
            for header in set_cookies
        ), f"no clear-cookie header in {set_cookies!r}"

    def test_no_cookie_writes_no_audit_row(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        before = len(_audit_rows_for(session_factory, action="session.invalidated"))
        client.cookies.clear()

        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 204

        after = len(_audit_rows_for(session_factory, action="session.invalidated"))
        assert after == before, "logout with no cookie must not write an audit row"


# ---------------------------------------------------------------------------
# Invalid-cookie path
# ---------------------------------------------------------------------------


class TestLogoutInvalidCookie:
    """Bogus cookie values are 204 + clear + no audit (enumeration-proof)."""

    def test_bogus_cookie_returns_204_no_audit(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
    ) -> None:
        before = len(_audit_rows_for(session_factory, action="session.invalidated"))
        client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session-token")

        response = client.post("/api/v1/auth/logout")
        assert response.status_code == 204

        set_cookies = response.headers.get_list("set-cookie")
        assert any(
            header.startswith(f"{SESSION_COOKIE_NAME}=;") for header in set_cookies
        )

        after = len(_audit_rows_for(session_factory, action="session.invalidated"))
        assert after == before

    def test_double_logout_is_idempotent(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """Second logout with the same now-invalidated cookie → 204, no audit."""
        cookie_value = _issue_session(
            session_factory, user_id=seed_user, settings=settings
        )
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)

        first = client.post("/api/v1/auth/logout")
        assert first.status_code == 204

        before = len(_audit_rows_for(session_factory, action="session.invalidated"))

        # Re-present the same (now-invalidated) cookie.
        client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        second = client.post("/api/v1/auth/logout")
        assert second.status_code == 204

        # No new audit row — ``validate`` rejects the invalidated
        # cookie before :func:`invalidate_for_user` runs.
        after = len(_audit_rows_for(session_factory, action="session.invalidated"))
        assert after == before
