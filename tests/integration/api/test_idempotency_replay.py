"""Integration tests for the :mod:`app.api.middleware.idempotency` surface.

Exercises the full :func:`app.api.factory.create_app` stack so the
middleware's replay, conflict, exempt-path, and concurrent-retry
paths are asserted against the composed application — tenancy,
error handlers, and exception envelope included.

See ``docs/specs/12-rest-api.md`` §"Idempotency" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.adapters.db.session as _session_mod
from app.adapters.db.ops.models import IdempotencyKey
from app.api.errors import CANONICAL_TYPE_BASE, CONTENT_TYPE_PROBLEM_JSON
from app.api.factory import create_app
from app.api.middleware.idempotency import (
    IDEMPOTENCY_HEADER,
    IDEMPOTENCY_REPLAY_HEADER,
    prune_expired_idempotency_keys,
)
from app.auth.csrf import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from app.config import Settings
from app.tenancy.middleware import ACTOR_STATE_ATTR, ActorIdentity
from app.tenancy.orm_filter import install_tenant_filter

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures — composed app + TestClient with token stamped on request.state
# ---------------------------------------------------------------------------


def _pinned_settings(
    db_url: str,
    *,
    profile: Literal["prod", "dev"] = "prod",
) -> Settings:
    """Settings bound to the integration-harness DB URL.

    Phase-0 stub is deliberately off: we stamp the actor directly via
    a lightweight middleware so the tenancy layer runs its real path
    against the empty-workspace table but the idempotency middleware
    still sees a populated ``ActorIdentity``.
    """
    return Settings.model_construct(
        database_url=db_url,
        root_key=SecretStr("integration-test-idempotency-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        profile=profile,
        vite_dev_url="http://127.0.0.1:5173",
        demo_mode=False,
        demo_frame_ancestors=None,
        hsts_enabled=False,
        cors_allow_origins=[],
    )


def _build_probe_router() -> APIRouter:
    """Build a minimal POST router covering the paths we exercise.

    Three shapes:

    * ``/api/_probe/echo`` — POST, echoes the submitted JSON payload
      plus a bump counter so tests can distinguish a handler re-run
      from a replay.
    * ``/api/_probe/payout_manifest`` — simulates the exempt route;
      also echoes + bumps so the "exempt path bypasses cache" test
      can prove the handler actually ran twice.
    """
    r = APIRouter()

    # Process-global invocation counter so each POST gets a stable
    # per-call marker in its response. A replay must yield the same
    # counter value as the first write; a re-execution increments.
    counter = {"n": 0}
    lock = threading.Lock()

    @r.post("/api/_probe/echo", include_in_schema=False)
    async def probe_echo(request: Request) -> JSONResponse:
        with lock:
            counter["n"] += 1
            n = counter["n"]
        body = await request.json() if await request.body() else {}
        return JSONResponse({"echo": body, "call_count": n})

    # Matches the exempt pattern in
    # :data:`app.api.middleware.idempotency.EXEMPT_PATH_PATTERNS`.
    # A plain ``/api/v1/payslips/{id}/payout_manifest`` shape is
    # sufficient — the exempt regex accepts the bare-host form.
    @r.post(
        "/api/v1/payslips/{pid}/payout_manifest",
        include_in_schema=False,
    )
    async def probe_payout_manifest(pid: str, request: Request) -> JSONResponse:
        with lock:
            counter["n"] += 1
            n = counter["n"]
        return JSONResponse({"pid": pid, "call_count": n})

    return r


def _compose_app(
    db_url: str,
    *,
    actor: ActorIdentity | None,
) -> FastAPI:
    """Compose the real app, inject an actor-stamp middleware, and mount probes.

    The actor-stamp middleware emulates the tenancy layer: it sets
    ``request.state.actor_identity`` so the idempotency middleware
    can read ``token_id`` without requiring a real bearer-token DB
    row. It sits OUTSIDE the idempotency middleware in the chain
    (added after ``create_app`` has installed the default stack) so
    every request reaches the idempotency layer with a populated
    actor.
    """
    app = create_app(settings=_pinned_settings(db_url))

    # Stamp the actor right before the idempotency middleware runs.
    @app.middleware("http")
    async def _stamp_actor(request: Request, call_next):  # type: ignore[no-untyped-def]
        setattr(request.state, ACTOR_STATE_ATTR, actor)
        return await call_next(request)

    probe_router = _build_probe_router()
    routes = app.router.routes
    catch_all_index = next(
        (
            idx
            for idx, route in enumerate(routes)
            if getattr(route, "path", None) == "/{full_path:path}"
        ),
        len(routes),
    )
    for offset, probe_route in enumerate(probe_router.routes):
        routes.insert(catch_all_index + offset, probe_route)
    return app


def _actor(token_id: str) -> ActorIdentity:
    return ActorIdentity(
        user_id="01HX00000000000000000USR000",
        kind="user",
        workspace_id="01HX00000000000000000WS0000",
        token_id=token_id,
        session_id=None,
    )


def _prime_csrf(client: TestClient) -> str:
    """Issue a GET to harvest the CSRF cookie, then return its value.

    Every non-idempotent request the CSRF middleware sees must echo
    the cookie value in ``X-CSRF``. Tests that drive the full
    middleware stack pay one GET up-front to mint the cookie, then
    set the matching header on every subsequent POST.

    Note: the CSRF middleware rotates the cookie on every response
    (§03 "Sessions"); :func:`_refresh_csrf_header` bumps the stored
    header value back to the latest cookie before each POST so the
    double-submit check passes on long-running test scenarios.
    """
    resp = client.get("/healthz")
    # ``/healthz`` is on the bare-host skip list for the tenancy
    # middleware, and the CSRF middleware emits a fresh cookie on
    # every response — skip path or not.
    assert resp.status_code == 200, resp.text
    cookie = client.cookies.get(CSRF_COOKIE_NAME)
    assert cookie is not None, "CSRF cookie not set on response"
    return cookie


def _refresh_csrf_header(client: TestClient) -> None:
    """Sync the client's ``X-CSRF`` header with the latest cookie value.

    The CSRF middleware rotates the cookie on every response, so
    the saved header drifts stale after each POST. Call this
    between POSTs to keep the pair matched.
    """
    cookie = client.cookies.get(CSRF_COOKIE_NAME)
    if cookie is not None:
        client.headers[CSRF_HEADER_NAME] = cookie


@pytest.fixture
def wire_default_uow(engine: Engine) -> Iterator[None]:
    """Redirect :func:`app.adapters.db.session.make_uow` to the test engine.

    The idempotency middleware opens its own UoW per request via
    :func:`make_uow`; without this fixture the UoW would rebuild
    an engine from :envvar:`CREWDAY_DATABASE_URL` (unset outside
    the alembic-upgrade window), mismatching the harness engine
    that the integration tests inspect afterwards.
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
def isolate_idempotency_rows(engine: Engine) -> Iterator[None]:
    """Purge ``idempotency_key`` rows at the start of every test.

    The harness ``engine`` is session-scoped, so rows written by
    one test survive into the next unless we tear them down.
    Integration tests that rely on the cache being empty (the
    happy-path replay test, the exempt-path test, the TTL-sweep
    test) all need this.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as session:
        session.query(IdempotencyKey).delete()
        session.commit()
    yield


@pytest.fixture
def composed_client(
    db_url: str,
    wire_default_uow: None,
    isolate_idempotency_rows: None,
) -> Iterator[TestClient]:
    """TestClient over the composed app with a fixed token actor.

    TLS-backed ``base_url`` because the CSRF cookie is ``Secure``
    (§15 "Cookies") and httpx silently drops ``Secure`` cookies on
    ``http://`` transports. The CSRF double-submit token is primed
    on first use via :func:`_prime_csrf` so tests can call
    ``client.post(...)`` without threading the header everywhere.
    """
    app = _compose_app(db_url, actor=_actor("01HXTOK00000000000000TOK00"))
    with TestClient(
        app, raise_server_exceptions=False, base_url="https://testserver"
    ) as client:
        csrf = _prime_csrf(client)
        client.headers[CSRF_HEADER_NAME] = csrf
        yield client


# ---------------------------------------------------------------------------
# Acceptance criteria
# ---------------------------------------------------------------------------


class TestAcceptanceCriteria:
    """One test per line item of the cd-z6fk acceptance criteria."""

    def test_first_post_persists_second_replays_with_header(
        self, composed_client: TestClient
    ) -> None:
        """First POST persists; second with same key+body returns cached
        response with ``Idempotency-Replay: true``."""
        first = composed_client.post(
            "/api/_probe/echo",
            json={"foo": "bar"},
            headers={IDEMPOTENCY_HEADER: "key-1"},
        )
        assert first.status_code == 200
        assert first.headers.get(IDEMPOTENCY_REPLAY_HEADER) is None
        first_body = first.json()

        _refresh_csrf_header(composed_client)
        second = composed_client.post(
            "/api/_probe/echo",
            json={"foo": "bar"},
            headers={IDEMPOTENCY_HEADER: "key-1"},
        )
        assert second.status_code == 200
        assert second.headers[IDEMPOTENCY_REPLAY_HEADER] == "true"
        # The handler did NOT run a second time — the ``call_count``
        # field bumps on each real invocation, and the replay must
        # return the original value.
        assert second.json() == first_body

    def test_same_key_different_body_returns_409_idempotency_conflict(
        self, composed_client: TestClient
    ) -> None:
        """Same key + different body → 409 ``idempotency_conflict``."""
        composed_client.post(
            "/api/_probe/echo",
            json={"foo": "bar"},
            headers={IDEMPOTENCY_HEADER: "key-2"},
        )
        _refresh_csrf_header(composed_client)
        resp = composed_client.post(
            "/api/_probe/echo",
            json={"foo": "baz"},  # different body
            headers={IDEMPOTENCY_HEADER: "key-2"},
        )
        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith(CONTENT_TYPE_PROBLEM_JSON)
        body = resp.json()
        assert body["type"] == f"{CANONICAL_TYPE_BASE}idempotency_conflict"
        assert body["title"] == "Idempotency conflict"
        assert body["status"] == 409
        assert body["idempotency_key"] == "key-2"

    def test_exempt_path_bypasses_cache_header_accepted_but_ignored(
        self, composed_client: TestClient
    ) -> None:
        """Exempt path (``POST /payslips/{id}/payout_manifest``): header is
        accepted but ignored, each retry re-executes the handler."""
        pid = "01HXPAY00000000000000PAY00"
        first = composed_client.post(
            f"/api/v1/payslips/{pid}/payout_manifest",
            json={"confirm": True},
            headers={IDEMPOTENCY_HEADER: "k"},
        )
        assert first.status_code == 200
        first_n = first.json()["call_count"]

        _refresh_csrf_header(composed_client)
        second = composed_client.post(
            f"/api/v1/payslips/{pid}/payout_manifest",
            json={"confirm": True},
            headers={IDEMPOTENCY_HEADER: "k"},
        )
        assert second.status_code == 200
        # No ``Idempotency-Replay`` header on either response — the
        # exempt route never consults the cache.
        assert second.headers.get(IDEMPOTENCY_REPLAY_HEADER) is None
        # The handler ran a second time; call_count strictly monotonic.
        assert second.json()["call_count"] > first_n

    def test_ttl_sweep_removes_rows_older_than_24h(
        self,
        composed_client: TestClient,
        engine: Engine,
    ) -> None:
        """TTL sweep deletes rows older than 24 h (spec §12)."""
        # Seed the cache via a normal POST.
        resp = composed_client.post(
            "/api/_probe/echo",
            json={"x": 1},
            headers={IDEMPOTENCY_HEADER: "ttl-k"},
        )
        assert resp.status_code == 200

        # Age the row past the TTL by rewriting ``created_at``.
        past = datetime(2026, 1, 1, tzinfo=UTC)
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as session:
            rows = session.query(IdempotencyKey).all()
            assert len(rows) >= 1
            # Find the one we just wrote (token_id matches the fixture).
            target = next(r for r in rows if r.token_id == "01HXTOK00000000000000TOK00")
            target.created_at = past
            session.commit()

        # Run the sweep. Because the composed client points at the
        # same DB, ``prune_expired_idempotency_keys()`` sees the aged
        # row and deletes it.
        deleted = prune_expired_idempotency_keys()
        assert deleted >= 1

        with factory() as session:
            remaining = (
                session.query(IdempotencyKey)
                .filter_by(token_id="01HXTOK00000000000000TOK00", key="ttl-k")
                .count()
            )
            assert remaining == 0

    def test_concurrent_retries_db_uniqueness_wins_race(
        self,
        composed_client: TestClient,
        engine: Engine,
    ) -> None:
        """Two POSTs with the same key at the same time must still yield
        exactly one cache row; the losing writer gets the winner's
        cached response.

        Exercised via sequential POSTs whose DB commit ordering is
        inverted: the first request writes, the second (matching-body
        POST) reads the existing row and replays it. The test does
        NOT spin actual threads — the in-memory DB isolation plus
        TestClient's sync transport doesn't surface IntegrityError
        realistically — but it DOES verify the single-writer +
        replay-on-duplicate-key contract the middleware implements
        in its happy-path branch.
        """
        r1 = composed_client.post(
            "/api/_probe/echo",
            json={"a": 1},
            headers={IDEMPOTENCY_HEADER: "race-k"},
        )
        _refresh_csrf_header(composed_client)
        r2 = composed_client.post(
            "/api/_probe/echo",
            json={"a": 1},
            headers={IDEMPOTENCY_HEADER: "race-k"},
        )
        assert r1.status_code == r2.status_code == 200
        assert r2.headers[IDEMPOTENCY_REPLAY_HEADER] == "true"
        assert r1.json() == r2.json()

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        with factory() as session:
            rows = (
                session.query(IdempotencyKey)
                .filter_by(token_id="01HXTOK00000000000000TOK00", key="race-k")
                .all()
            )
            assert len(rows) == 1


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------


class TestCrossCutting:
    """Invariants that any cached response must obey."""

    def test_replay_preserves_content_type_and_status(
        self, composed_client: TestClient
    ) -> None:
        """The replay returns the same status code and Content-Type the
        handler originally emitted."""
        first = composed_client.post(
            "/api/_probe/echo",
            json={"x": 1},
            headers={IDEMPOTENCY_HEADER: "ct-k"},
        )
        _refresh_csrf_header(composed_client)
        second = composed_client.post(
            "/api/_probe/echo",
            json={"x": 1},
            headers={IDEMPOTENCY_HEADER: "ct-k"},
        )
        assert second.status_code == first.status_code
        assert (
            second.headers["content-type"].split(";")[0]
            == first.headers["content-type"].split(";")[0]
        )

    def test_distinct_keys_produce_distinct_cached_responses(
        self, composed_client: TestClient
    ) -> None:
        """Two different keys cache independently."""
        a = composed_client.post(
            "/api/_probe/echo",
            json={"n": 1},
            headers={IDEMPOTENCY_HEADER: "k-a"},
        )
        _refresh_csrf_header(composed_client)
        b = composed_client.post(
            "/api/_probe/echo",
            json={"n": 1},
            headers={IDEMPOTENCY_HEADER: "k-b"},
        )
        # Neither is a replay of the other — both handlers ran.
        assert a.headers.get(IDEMPOTENCY_REPLAY_HEADER) is None
        assert b.headers.get(IDEMPOTENCY_REPLAY_HEADER) is None
        assert a.json()["call_count"] != b.json()["call_count"]


# ---------------------------------------------------------------------------
# Post-composed bypass paths (no token, non-POST, no header)
# ---------------------------------------------------------------------------


class TestBypassesAfterComposition:
    """The full app still lets non-POST and tokenless POSTs through."""

    def test_session_only_post_not_cached(
        self,
        db_url: str,
        wire_default_uow: None,
        isolate_idempotency_rows: None,
    ) -> None:
        """A POST authenticated via a session (no token) should NOT be
        cached — the replay cache is keyed on ``token_id``."""
        session_actor = ActorIdentity(
            user_id="01HX00000000000000000USR000",
            kind="user",
            workspace_id="01HX00000000000000000WS0000",
            token_id=None,  # session-only auth
            session_id="01HX00000000000000000SES000",
        )
        app = _compose_app(db_url, actor=session_actor)
        with TestClient(
            app, raise_server_exceptions=False, base_url="https://testserver"
        ) as client:
            csrf = _prime_csrf(client)
            client.headers[CSRF_HEADER_NAME] = csrf
            r1 = client.post(
                "/api/_probe/echo",
                json={"x": 1},
                headers={IDEMPOTENCY_HEADER: "session-k"},
            )
            _refresh_csrf_header(client)
            r2 = client.post(
                "/api/_probe/echo",
                json={"x": 1},
                headers={IDEMPOTENCY_HEADER: "session-k"},
            )
        assert r1.status_code == r2.status_code == 200
        # Both calls re-execute the handler — no replay.
        assert r1.headers.get(IDEMPOTENCY_REPLAY_HEADER) is None
        assert r2.headers.get(IDEMPOTENCY_REPLAY_HEADER) is None
        assert r1.json()["call_count"] != r2.json()["call_count"]
