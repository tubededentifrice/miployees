"""Integration tests for :mod:`app.auth.session` + :mod:`app.auth.csrf`.

End-to-end against a real engine (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``) driving a FastAPI test app with a pair
of toy routes that exercise:

* issue → cookie-validate roundtrip across requests;
* sliding-refresh visible through a second request past halflife;
* explicit revoke → subsequent request 401s;
* ``revoke_all_for_user`` → every sibling session invalidated;
* CSRF pair enforced on mutating routes.

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions" and
``docs/specs/15-security-privacy.md`` §"Cookies".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, status
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User
from app.auth.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
)
from app.auth.session import (
    SESSION_COOKIE_NAME,
    SessionExpired,
    SessionInvalid,
    issue,
    revoke,
    revoke_all_for_user,
    validate,
)
from app.config import Settings
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


def _as_utc(value: datetime) -> datetime:
    """Normalise DB-read datetimes to aware UTC.

    SQLite drops tzinfo on ``DateTime(timezone=True)`` columns.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-session-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seed_user(session_factory: sessionmaker[Session]) -> Iterator[str]:
    """Seed a user row and yield its id."""
    with session_factory() as s:
        user = bootstrap_user(s, email="flow@example.com", display_name="Flow")
        user_id = user.id
        s.commit()
    yield user_id
    # Cleanup — delete the user row and every session cascaded with it.
    with session_factory() as s:
        u = s.get(User, user_id)
        if u is not None:
            s.delete(u)
            s.commit()


def _build_app(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    now_box: dict[str, datetime],
) -> FastAPI:
    """Return a minimal FastAPI app exposing login + whoami + logout toys.

    ``now_box`` lets tests pin the clock observable by the routes
    (since the domain service reads ``now`` per call). Mutating the
    dict between requests simulates time advancing.
    """
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

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

    # FastAPI inspects annotations via ``get_type_hints``; function-scope
    # aliases don't always resolve through there reliably, so we inline
    # the ``Depends`` at each call site. B008 is suppressed per line
    # rather than for the module — these are toy routes, not
    # production ones.
    db_dep = Depends(_session)
    cookie_dep = Cookie(alias=SESSION_COOKIE_NAME, default=None)

    @app.post("/toy/login")
    def _login(
        user_id: str,
        has_owner_grant: bool,
        request: Request,
        db: Session = db_dep,
    ) -> dict[str, str]:
        result = issue(
            db,
            user_id=user_id,
            has_owner_grant=has_owner_grant,
            ua=request.headers.get("User-Agent", ""),
            ip=request.client.host if request.client else "0.0.0.0",
            now=now_box["now"],
            settings=settings,
        )
        response_payload = {
            "session_id": result.session_id,
            "cookie_value": result.cookie_value,
            "expires_at": result.expires_at.isoformat(),
        }
        # Stash the cookie on the response — TestClient persists it on
        # the client so subsequent requests auto-send it.
        from starlette.responses import JSONResponse

        resp = JSONResponse(response_payload)
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            result.cookie_value,
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return resp  # type: ignore[return-value]

    @app.get("/toy/whoami")
    def _whoami(
        db: Session = db_dep,
        crewday_session: str | None = cookie_dep,
    ) -> dict[str, str]:
        if crewday_session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "no_session"},
            )
        try:
            user_id = validate(
                db,
                cookie_value=crewday_session,
                now=now_box["now"],
                settings=settings,
            )
        except SessionInvalid as exc:
            raise HTTPException(status_code=401, detail={"error": "invalid"}) from exc
        except SessionExpired as exc:
            raise HTTPException(status_code=401, detail={"error": "expired"}) from exc
        return {"user_id": user_id}

    @app.post("/toy/logout")
    def _logout(
        db: Session = db_dep,
        crewday_session: str | None = cookie_dep,
    ) -> dict[str, str]:
        if crewday_session is None:
            raise HTTPException(status_code=401, detail={"error": "no_session"})
        from app.auth.session import hash_cookie_value

        revoke(
            db,
            session_id=hash_cookie_value(crewday_session),
            now=now_box["now"],
        )
        return {"ok": "logged_out"}

    @app.post("/toy/logout-all")
    def _logout_all(
        user_id: str,
        request: Request,
        db: Session = db_dep,
        crewday_session: str | None = cookie_dep,
    ) -> dict[str, int]:
        if crewday_session is None:
            raise HTTPException(status_code=401, detail={"error": "no_session"})
        from app.auth.session import hash_cookie_value

        count = revoke_all_for_user(
            db,
            user_id=user_id,
            except_session_id=hash_cookie_value(crewday_session),
            now=now_box["now"],
        )
        return {"count": count}

    return app


def _priming_get(client: TestClient) -> str:
    """Hit a skip-path GET to fetch a CSRF token without auth."""
    # ``/healthz`` is in SKIP_PATHS; the middleware still mints a
    # fresh CSRF cookie on the way out. We mount a trivial handler
    # because the app has no /healthz of its own in these tests.
    r = client.get("/toy/whoami")
    # /toy/whoami is not a skip path but is idempotent (GET), so the
    # middleware mints the cookie. It 401s because there's no session
    # yet; we only care about the cookie.
    assert CSRF_COOKIE_NAME in r.cookies
    return r.cookies[CSRF_COOKIE_NAME]


class TestSessionEndToEnd:
    """Login → whoami → logout round-trip across HTTP requests."""

    def test_login_then_whoami_returns_user(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        now_box = {"now": datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)}
        app = _build_app(session_factory, settings, now_box=now_box)
        with TestClient(app, base_url="https://testserver") as client:
            csrf = _priming_get(client)
            r = client.post(
                f"/toy/login?user_id={seed_user}&has_owner_grant=false",
                headers={CSRF_HEADER_NAME: csrf},
            )
            assert r.status_code == 200, r.text
            # The session cookie is now on the client; a follow-up GET
            # carries it automatically.
            r2 = client.get("/toy/whoami")
            assert r2.status_code == 200
            assert r2.json() == {"user_id": seed_user}

    def test_sliding_refresh_extends_expires_at(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        start = datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)
        now_box = {"now": start}
        app = _build_app(session_factory, settings, now_box=now_box)
        with TestClient(app, base_url="https://testserver") as client:
            csrf = _priming_get(client)
            r = client.post(
                f"/toy/login?user_id={seed_user}&has_owner_grant=true",
                headers={CSRF_HEADER_NAME: csrf},
            )
            assert r.status_code == 200
            original_expires = datetime.fromisoformat(r.json()["expires_at"])

            # Advance past the 3.5-day halflife (5 days in).
            now_box["now"] = start + timedelta(days=5)
            r2 = client.get("/toy/whoami")
            assert r2.status_code == 200

            # Row's expires_at should now be at ``now + 7 days``.
            with session_factory() as s:
                row = s.scalars(select(SessionRow)).one()
                assert _as_utc(row.expires_at) > original_expires
                assert _as_utc(row.expires_at) == now_box["now"] + timedelta(days=7)

    def test_logout_invalidates_session(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        now_box = {"now": datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)}
        app = _build_app(session_factory, settings, now_box=now_box)
        with TestClient(app, base_url="https://testserver") as client:
            csrf = _priming_get(client)
            client.post(
                f"/toy/login?user_id={seed_user}&has_owner_grant=false",
                headers={CSRF_HEADER_NAME: csrf},
            )
            # Logout — still need fresh CSRF from latest response.
            csrf2 = client.cookies[CSRF_COOKIE_NAME]
            r = client.post("/toy/logout", headers={CSRF_HEADER_NAME: csrf2})
            assert r.status_code == 200
            # Subsequent whoami is 401.
            r2 = client.get("/toy/whoami")
            assert r2.status_code == 401

    def test_logout_all_invalidates_concurrent_sessions(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        """``revoke_all_for_user`` from one client wipes the other's session."""
        now_box = {"now": datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)}
        app = _build_app(session_factory, settings, now_box=now_box)
        # Two independent clients simulate two browsers / devices.
        with (
            TestClient(app, base_url="https://testserver") as client_a,
            TestClient(app, base_url="https://testserver") as client_b,
        ):
            csrf_a = _priming_get(client_a)
            client_a.post(
                f"/toy/login?user_id={seed_user}&has_owner_grant=false",
                headers={CSRF_HEADER_NAME: csrf_a},
            )
            csrf_b = _priming_get(client_b)
            client_b.post(
                f"/toy/login?user_id={seed_user}&has_owner_grant=false",
                headers={CSRF_HEADER_NAME: csrf_b},
            )

            # Both clients are logged in.
            assert client_a.get("/toy/whoami").status_code == 200
            assert client_b.get("/toy/whoami").status_code == 200

            # Client A issues "Sign out of every other device".
            csrf_a2 = client_a.cookies[CSRF_COOKIE_NAME]
            r = client_a.post(
                f"/toy/logout-all?user_id={seed_user}",
                headers={CSRF_HEADER_NAME: csrf_a2},
            )
            assert r.status_code == 200
            assert r.json()["count"] == 1

            # Client A still works (its session was kept by except_);
            # Client B is now invalid.
            assert client_a.get("/toy/whoami").status_code == 200
            assert client_b.get("/toy/whoami").status_code == 401


class TestCSRFIntegration:
    """CSRF double-submit enforced on non-GET routes end-to-end."""

    def test_post_without_csrf_header_is_403(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        now_box = {"now": datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)}
        app = _build_app(session_factory, settings, now_box=now_box)
        with TestClient(app, base_url="https://testserver") as client:
            # Prime the cookie via a GET, then POST without the header.
            _priming_get(client)
            r = client.post(
                f"/toy/login?user_id={seed_user}&has_owner_grant=false",
            )
            assert r.status_code == 403
            assert r.json() == {"detail": "csrf_mismatch"}

    def test_post_with_matching_pair_reaches_handler(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        seed_user: str,
    ) -> None:
        now_box = {"now": datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)}
        app = _build_app(session_factory, settings, now_box=now_box)
        with TestClient(app, base_url="https://testserver") as client:
            csrf = _priming_get(client)
            r = client.post(
                f"/toy/login?user_id={seed_user}&has_owner_grant=false",
                headers={CSRF_HEADER_NAME: csrf},
            )
            assert r.status_code == 200
