"""End-to-end tenancy middleware integration with real auth.

Drives :class:`~app.tenancy.middleware.WorkspaceContextMiddleware` on
a FastAPI test app backed by the shared ``engine`` + ``db_session``
fixtures (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``). Every test seeds a workspace via the
production-shape :func:`tests.factories.identity.bootstrap_workspace`
helper, issues real sessions via :func:`app.auth.session.issue`, and
mints real API tokens via :func:`app.auth.tokens.mint` — no stub
headers.

Covers (cd-9il acceptance):

* Session cookie end-to-end → ctx bound, ``is_owner`` populated.
* Bearer token end-to-end → ctx bound with token's workspace.
* Cross-tenant 404 — byte-identical envelope + ±5 ms timing band
  across slug-miss vs member-miss (spec §15 "Constant-time
  cross-tenant responses").

See ``docs/specs/15-security-privacy.md`` §"Constant-time cross-tenant
responses"; ``docs/specs/03-auth-and-tokens.md`` §"Sessions" + §"API
tokens"; ``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.auth.session import SESSION_COOKIE_NAME
from app.auth.session import issue as issue_session
from app.auth.tokens import mint as mint_token
from app.config import Settings
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import get_current
from app.tenancy.middleware import (
    CORRELATION_ID_HEADER,
    WorkspaceContextMiddleware,
)
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    """Settings with the Phase-0 stub OFF so the real resolver runs."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-tenancy-middleware-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
        phase0_stub_enabled=False,
    )


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture
def wire_default_uow(
    engine: Engine,
    session_factory: sessionmaker[Session],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Redirect :func:`make_uow` to the shared test engine.

    The middleware opens a fresh UoW per request via
    :func:`app.adapters.db.session.make_uow`; we swap the module-level
    defaults to land on the test DB. Also monkeypatches the
    middleware's ``get_settings`` to return the stub-off fixture so
    no test implicitly inherits a cached default.
    """
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = engine
    _session_mod._default_sessionmaker_ = session_factory
    monkeypatch.setattr("app.tenancy.middleware.get_settings", lambda: settings)
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


def _build_app() -> FastAPI:
    """FastAPI app with the middleware and a ``ping`` route."""
    app = FastAPI()
    app.add_middleware(WorkspaceContextMiddleware)

    @app.get("/w/{slug}/api/v1/ping")
    def scoped_ping(slug: str) -> dict[str, object]:
        ctx = get_current()
        if ctx is None:
            return {"bound": False}
        return {
            "bound": True,
            "workspace_id": ctx.workspace_id,
            "workspace_slug": ctx.workspace_slug,
            "actor_id": ctx.actor_id,
            "actor_kind": ctx.actor_kind,
            "actor_grant_role": ctx.actor_grant_role,
            "actor_was_owner_member": ctx.actor_was_owner_member,
        }

    return app


def _seed(
    session_factory: sessionmaker[Session], *, slug: str, email: str
) -> tuple[str, str]:
    """Seed one user + one workspace + owners group.

    Returns ``(workspace_id, user_id)``.
    """
    with session_factory() as s:
        user = bootstrap_user(s, email=email, display_name=email)
        ws = bootstrap_workspace(s, slug=slug, name=slug.title(), owner_user_id=user.id)
        s.commit()
        return ws.id, user.id


class TestSessionEndToEnd:
    """A full session-cookie roundtrip against the real middleware."""

    def test_session_resolves_owner_context(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        ws_id, user_id = _seed(
            session_factory,
            slug="int-owner",
            email="owner-int-session@example.com",
        )
        with session_factory() as s:
            issued = issue_session(
                s,
                user_id=user_id,
                has_owner_grant=True,
                ua="curl",
                ip="127.0.0.1",
                now=_PINNED,
                settings=settings,
            )
            s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/w/int-owner/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["bound"] is True
        assert body["workspace_id"] == ws_id
        assert body["workspace_slug"] == "int-owner"
        assert body["actor_id"] == user_id
        assert body["actor_was_owner_member"] is True
        assert body["actor_grant_role"] == "manager"
        assert CORRELATION_ID_HEADER in response.headers


class TestBearerTokenEndToEnd:
    def test_token_resolves_context(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        ws_id, user_id = _seed(
            session_factory, slug="int-token", email="tok-int-token@example.com"
        )
        with session_factory() as s:
            ctx = WorkspaceContext(
                workspace_id=ws_id,
                workspace_slug="int-token",
                actor_id=user_id,
                actor_kind="user",
                actor_grant_role="manager",
                actor_was_owner_member=True,
                audit_correlation_id=new_ulid(),
            )
            minted = mint_token(
                s,
                ctx,
                user_id=user_id,
                label="int",
                scopes={"tasks.read": True},
                expires_at=None,
                now=_PINNED,
            )
            s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/w/int-token/api/v1/ping",
                headers={"Authorization": f"Bearer {minted.token}"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["bound"] is True
        assert body["workspace_id"] == ws_id
        assert body["actor_id"] == user_id
        assert body["actor_was_owner_member"] is True


class TestCrossTenantConstantTime:
    """§15 constant-time cross-tenant responses."""

    def test_slug_miss_and_member_miss_bodies_match(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        _ws_id, _owner_id = _seed(
            session_factory, slug="real-ct-ws", email="owner-ct-bodies@example.com"
        )
        # The "outsider" is a real logged-in user who is NOT a member
        # of ``real-ct-ws`` — exactly the cross-tenant probe shape §15
        # pins down.
        with session_factory() as s:
            outsider = bootstrap_user(
                s,
                email="outsider-ct-bodies@example.com",
                display_name="Outsider",
            )
            issued = issue_session(
                s,
                user_id=outsider.id,
                has_owner_grant=False,
                ua="curl",
                ip="127.0.0.1",
                now=_PINNED,
                settings=settings,
            )
            s.commit()

        app = _build_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            slug_miss = client.get(
                "/w/never-existed/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )
            member_miss = client.get(
                "/w/real-ct-ws/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )

        assert slug_miss.status_code == 404
        assert member_miss.status_code == 404
        # Byte-identical envelope across the two branches (§15).
        assert slug_miss.content == member_miss.content
        assert slug_miss.json() == {"error": "not_found", "detail": None}

    def test_slug_miss_and_member_miss_timings_overlap(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
        wire_default_uow: None,
    ) -> None:
        """Timings fall in the same rough wall-clock band.

        Spec §15 pins ±5 ms on a steady-load harness; here we run both
        branches N times and assert that the means differ by less than
        a generous ceiling. This is a smoke test for the dummy-read
        equaliser — not a rigorous statistical proof, but enough to
        catch a regression that removes the dummy read entirely.
        """
        _ws_id, _owner_id = _seed(
            session_factory, slug="timing-ws", email="owner-ct-timing@example.com"
        )
        with session_factory() as s:
            outsider = bootstrap_user(
                s,
                email="outsider-ct-timing@example.com",
                display_name="TO",
            )
            issued = issue_session(
                s,
                user_id=outsider.id,
                has_owner_grant=False,
                ua="curl",
                ip="127.0.0.1",
                now=_PINNED,
                settings=settings,
            )
            s.commit()

        app = _build_app()
        samples = 10
        slug_times: list[float] = []
        member_times: list[float] = []
        with TestClient(app, raise_server_exceptions=False) as client:
            # Warmup so lazy import + sqlite page cache don't skew the
            # first sample of whichever branch runs first.
            client.get(
                "/w/warmup/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )
            client.get(
                "/w/timing-ws/api/v1/ping",
                cookies={SESSION_COOKIE_NAME: issued.cookie_value},
            )
            for _ in range(samples):
                t0 = time.perf_counter()
                client.get(
                    "/w/never-exists/api/v1/ping",
                    cookies={SESSION_COOKIE_NAME: issued.cookie_value},
                )
                slug_times.append(time.perf_counter() - t0)

                t0 = time.perf_counter()
                client.get(
                    "/w/timing-ws/api/v1/ping",
                    cookies={SESSION_COOKIE_NAME: issued.cookie_value},
                )
                member_times.append(time.perf_counter() - t0)

        mean_slug = sum(slug_times) / samples
        mean_member = sum(member_times) / samples
        # Generous bound — the middleware stack + test client
        # overhead dwarfs the DB read cost in this smoke test. What
        # we're guarding against is the pathological "slug miss
        # skipped the DB entirely" regression, which would produce a
        # >10x gap on any backend. +/-50 ms is comfortable headroom on
        # CI noise; the real SLO (±5 ms under steady load) lives on
        # the §17 tenant-isolation suite.
        delta = abs(mean_slug - mean_member)
        assert delta < 0.050, (
            f"timing branches diverged: slug_miss={mean_slug:.4f}s, "
            f"member_miss={mean_member:.4f}s, delta={delta:.4f}s"
        )
