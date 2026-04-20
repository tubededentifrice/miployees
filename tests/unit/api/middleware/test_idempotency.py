"""Unit tests for :mod:`app.api.middleware.idempotency`.

Covers the pure helpers (body hash canonicalisation, exempt-path
match) and the middleware's short-circuit paths (non-POST,
missing header, missing token, exempt path) against an in-memory
SQLite engine without the full FastAPI factory. The full end-to-
end behaviour (cache hit, conflict, replay header, exempt bypass,
concurrent retries) lives in
``tests/integration/api/test_idempotency_replay.py``.

See ``docs/specs/12-rest-api.md`` §"Idempotency" and
``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import app.adapters.db.session as _session_mod
from app.adapters.db.base import Base
from app.adapters.db.ops.models import IdempotencyKey
from app.adapters.db.session import make_engine
from app.api.middleware.idempotency import (
    EXEMPT_PATH_PATTERNS,
    IDEMPOTENCY_HEADER,
    IDEMPOTENCY_REPLAY_HEADER,
    IDEMPOTENCY_TTL_HOURS,
    IdempotencyMiddleware,
    canonical_body_hash,
    is_exempt_path,
    prune_expired_idempotency_keys,
)
from app.tenancy.middleware import ACTOR_STATE_ATTR, ActorIdentity
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

# ---------------------------------------------------------------------------
# canonical_body_hash
# ---------------------------------------------------------------------------


class TestCanonicalBodyHash:
    """Pure-function tests for :func:`canonical_body_hash`."""

    def test_empty_body_hashes_empty_string(self) -> None:
        """Empty body collapses to the sha256 of ``b""`` deterministically."""
        # sha256('') — known constant.
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert canonical_body_hash(b"") == expected

    def test_json_whitespace_invariant(self) -> None:
        """Two JSON bodies differing only in whitespace hash identically."""
        a = b'{"a":1,"b":2}'
        b = b'{\n  "a": 1,\n  "b": 2\n}'
        assert canonical_body_hash(a) == canonical_body_hash(b)

    def test_json_key_order_invariant(self) -> None:
        """Same keys, different order → same hash."""
        a = b'{"a":1,"b":2}'
        b = b'{"b":2,"a":1}'
        assert canonical_body_hash(a) == canonical_body_hash(b)

    def test_json_different_values_different_hash(self) -> None:
        """Different values → different hashes (obvious, but pinned)."""
        a = b'{"a":1}'
        b = b'{"a":2}'
        assert canonical_body_hash(a) != canonical_body_hash(b)

    def test_non_json_falls_back_to_raw_bytes(self) -> None:
        """Non-JSON bodies hash the raw bytes directly."""
        # Truncated JPEG header: a real file upload's prefix.
        a = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        b = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        assert canonical_body_hash(a) == canonical_body_hash(b)
        assert canonical_body_hash(a) != canonical_body_hash(b + b"x")

    def test_unicode_in_json_hashed_same(self) -> None:
        """Non-ASCII strings survive the canonical form."""
        a = '{"name":"École"}'.encode()
        b = '{"name":"\u00c9cole"}'.encode()
        # Both should canonicalise to the same form.
        assert canonical_body_hash(a) == canonical_body_hash(b)


# ---------------------------------------------------------------------------
# is_exempt_path
# ---------------------------------------------------------------------------


class TestIsExemptPath:
    """Exempt allow-list — pinned so a typo or regex change fails loudly."""

    def test_payout_manifest_workspace_scoped_is_exempt(self) -> None:
        assert is_exempt_path(
            "/w/villa-sud/api/v1/payslips/01HX000000000000000000PAY/payout_manifest"
        )

    def test_payout_manifest_trailing_slash_is_exempt(self) -> None:
        assert is_exempt_path(
            "/w/villa-sud/api/v1/payslips/01HX000000000000000000PAY/payout_manifest/"
        )

    def test_payout_manifest_bare_is_exempt(self) -> None:
        """Future bare-host form (no ``/w/<slug>/`` prefix) also exempts."""
        assert is_exempt_path("/payslips/01HX000000000000000000PAY/payout_manifest")

    def test_payout_manifest_api_v1_only_is_exempt(self) -> None:
        """``/api/v1/payslips/{id}/payout_manifest`` — bare-host /api/v1 shape."""
        assert is_exempt_path(
            "/api/v1/payslips/01HX000000000000000000PAY/payout_manifest"
        )

    def test_other_payslip_route_not_exempt(self) -> None:
        assert not is_exempt_path(
            "/w/villa-sud/api/v1/payslips/01HX000000000000000000PAY/issue"
        )

    def test_generic_post_route_not_exempt(self) -> None:
        assert not is_exempt_path("/w/villa-sud/api/v1/tasks")

    def test_pattern_list_has_only_known_entries(self) -> None:
        """Pin the exemption count so new entries land with an explicit update."""
        assert len(EXEMPT_PATH_PATTERNS) == 1


# ---------------------------------------------------------------------------
# Middleware short-circuit paths (no DB interaction)
# ---------------------------------------------------------------------------


def _build_stub_app(
    *,
    actor: ActorIdentity | None,
) -> Starlette:
    """Return a :class:`Starlette` app with a single POST route and the middleware.

    The inner route echoes the parsed JSON back as the response body
    so tests can assert the handler actually ran (vs a cache replay).
    ``actor`` is stamped onto ``request.state`` by a wrapping
    middleware so the idempotency middleware sees it exactly the way
    the production tenancy middleware would set it.
    """

    async def echo_handler(request: Request) -> Response:
        raw = await request.body()
        payload: dict[str, object] = await request.json() if raw else {}
        return JSONResponse({"echo": payload, "at": "handler"})

    async def stamp_actor(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Emulates the tenancy middleware stashing the resolved
        # ActorIdentity onto ``request.state``.
        setattr(request.state, ACTOR_STATE_ATTR, actor)
        return await call_next(request)

    app = Starlette(
        routes=[
            Route("/echo", echo_handler, methods=["POST", "GET"]),
            Route(
                "/payslips/{pid}/payout_manifest",
                echo_handler,
                methods=["POST"],
            ),
        ]
    )
    app.add_middleware(IdempotencyMiddleware)
    # Stamp actor INSIDE the idempotency middleware's view.
    # ``add_middleware`` prepends, so the last add is outermost —
    # our stamp-middleware must be registered AFTER the idempotency
    # one so it wraps it.
    from starlette.middleware.base import BaseHTTPMiddleware

    app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_actor)
    return app


@pytest.fixture
def memory_engine() -> Iterator[Engine]:
    """In-memory SQLite engine with every app table created."""
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def memory_factory(memory_engine: Engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=memory_engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture
def redirect_default_engine(
    memory_engine: Engine,
    memory_factory: sessionmaker[Session],
) -> Iterator[None]:
    """Point ``make_uow()`` at the in-memory engine for the duration of a test.

    Mirrors the pattern in ``tests/unit/test_tenancy_middleware.py`` so
    the middleware's own :func:`app.adapters.db.session.make_uow`
    calls land on the harness DB without reaching for env vars.
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


def _actor(token_id: str | None) -> ActorIdentity:
    return ActorIdentity(
        user_id="01HX00000000000000000USR000",
        kind="user",
        workspace_id="01HX00000000000000000WS0000",
        token_id=token_id,
        session_id=None if token_id is not None else "01HX00000000000000000SES000",
    )


class TestShortCircuitPaths:
    """Every path that should NOT touch the DB cache."""

    def test_get_request_passes_through(self, redirect_default_engine: None) -> None:
        """GET requests are untouched — spec §12 "Idempotency" covers POST only."""
        app = _build_stub_app(actor=_actor("01HXTOK00000000000000TOK00"))
        with TestClient(app) as client:
            resp = client.get("/echo", headers={IDEMPOTENCY_HEADER: "abc"})
        assert resp.status_code == 200
        assert resp.headers.get(IDEMPOTENCY_REPLAY_HEADER) is None

    def test_post_without_header_passes_through(
        self, redirect_default_engine: None, memory_factory: sessionmaker[Session]
    ) -> None:
        """No ``Idempotency-Key`` → no caching, no replay."""
        app = _build_stub_app(actor=_actor("01HXTOK00000000000000TOK00"))
        with TestClient(app) as client:
            resp = client.post("/echo", json={"x": 1})
        assert resp.status_code == 200
        # Nothing was persisted.
        with memory_factory() as session:
            assert session.query(IdempotencyKey).count() == 0

    def test_post_without_token_passes_through(
        self, redirect_default_engine: None, memory_factory: sessionmaker[Session]
    ) -> None:
        """Session-only auth (no token) → no caching."""
        app = _build_stub_app(actor=_actor(None))
        with TestClient(app) as client:
            resp = client.post(
                "/echo", json={"x": 1}, headers={IDEMPOTENCY_HEADER: "abc"}
            )
        assert resp.status_code == 200
        with memory_factory() as session:
            assert session.query(IdempotencyKey).count() == 0

    def test_post_without_actor_state_passes_through(
        self, redirect_default_engine: None, memory_factory: sessionmaker[Session]
    ) -> None:
        """Anonymous / no actor state → no caching."""
        app = _build_stub_app(actor=None)
        with TestClient(app) as client:
            resp = client.post(
                "/echo", json={"x": 1}, headers={IDEMPOTENCY_HEADER: "abc"}
            )
        assert resp.status_code == 200
        with memory_factory() as session:
            assert session.query(IdempotencyKey).count() == 0

    def test_exempt_path_bypasses_cache(
        self, redirect_default_engine: None, memory_factory: sessionmaker[Session]
    ) -> None:
        """Exempt path (``/payslips/{id}/payout_manifest``) skips the cache
        but still accepts the header."""
        app = _build_stub_app(actor=_actor("01HXTOK00000000000000TOK00"))
        with TestClient(app) as client:
            resp = client.post(
                "/payslips/01HXPAY00000000000000PAY00/payout_manifest",
                json={"confirm": True},
                headers={IDEMPOTENCY_HEADER: "abc"},
            )
        assert resp.status_code == 200
        with memory_factory() as session:
            assert session.query(IdempotencyKey).count() == 0


# ---------------------------------------------------------------------------
# Caching + replay (unit-level, exercised through the Starlette stub app)
# ---------------------------------------------------------------------------


class TestCacheWriteAndRead:
    """Full round-trip through the Starlette stub app."""

    def test_first_post_persists_and_second_replays(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        """First POST runs the handler, second with same body replays from cache.

        The stub handler stamps ``at=handler`` on its output; a replayed
        response carries the same bytes so the body still reads
        ``handler``. The replay is distinguished by the
        :data:`IDEMPOTENCY_REPLAY_HEADER` header on the response.
        """
        app = _build_stub_app(actor=_actor("01HXTOK00000000000000TOK00"))
        with TestClient(app) as client:
            first = client.post(
                "/echo",
                json={"x": 1},
                headers={IDEMPOTENCY_HEADER: "key-1"},
            )
            assert first.status_code == 200
            assert first.headers.get(IDEMPOTENCY_REPLAY_HEADER) is None
            first_body = first.json()

            second = client.post(
                "/echo",
                json={"x": 1},
                headers={IDEMPOTENCY_HEADER: "key-1"},
            )
            assert second.status_code == 200
            assert second.headers[IDEMPOTENCY_REPLAY_HEADER] == "true"
            assert second.json() == first_body

        # Exactly one row in the cache.
        with memory_factory() as session:
            rows = session.query(IdempotencyKey).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.token_id == "01HXTOK00000000000000TOK00"
            assert row.key == "key-1"
            assert row.status == 200

    def test_same_key_different_body_returns_409(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        """Second retry with the same key but a different body → 409
        ``idempotency_conflict`` as an RFC 7807 problem+json response.

        The middleware synthesises the envelope in-place (because
        :class:`BaseHTTPMiddleware` swallows exceptions), so a plain
        :class:`TestClient` — even without FastAPI's exception
        handlers wired — still sees the 409 body.
        """
        app = _build_stub_app(actor=_actor("01HXTOK00000000000000TOK00"))
        with TestClient(app) as client:
            client.post(
                "/echo",
                json={"x": 1},
                headers={IDEMPOTENCY_HEADER: "key-1"},
            )
            resp = client.post(
                "/echo",
                json={"x": 2},
                headers={IDEMPOTENCY_HEADER: "key-1"},
            )
        assert resp.status_code == 409
        body = resp.json()
        assert body["status"] == 409
        assert body["title"] == "Idempotency conflict"
        assert body["idempotency_key"] == "key-1"
        assert resp.headers["content-type"].startswith("application/problem+json")

        # Original cache row unchanged.
        with memory_factory() as session:
            rows = session.query(IdempotencyKey).all()
            assert len(rows) == 1
            # Body hash matches the first request's hash, not the second's.
            assert rows[0].body_hash == canonical_body_hash(
                json.dumps({"x": 1}).encode()
            )

    def test_different_token_same_key_are_independent(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        """``(token_id, key)`` is the uniqueness — a second token reusing the same
        key must not collide with the first."""
        app_a = _build_stub_app(actor=_actor("01HXTOKAAAAAAAAAAAAAATOK00"))
        app_b = _build_stub_app(actor=_actor("01HXTOKBBBBBBBBBBBBBBTOK00"))
        with TestClient(app_a) as client:
            client.post("/echo", json={"x": 1}, headers={IDEMPOTENCY_HEADER: "shared"})
        with TestClient(app_b) as client:
            resp = client.post(
                "/echo", json={"x": 2}, headers={IDEMPOTENCY_HEADER: "shared"}
            )
            # Fresh write, not a replay.
            assert resp.headers.get(IDEMPOTENCY_REPLAY_HEADER) is None

        with memory_factory() as session:
            rows = session.query(IdempotencyKey).order_by(IdempotencyKey.token_id).all()
            assert len(rows) == 2
            assert rows[0].token_id != rows[1].token_id


# ---------------------------------------------------------------------------
# Status-class and header-replay policies
# ---------------------------------------------------------------------------


class TestStatusClassAndHeaderPolicy:
    """5xx responses must not poison the cache; ``Set-Cookie`` never replays."""

    def test_5xx_response_not_cached(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        """A handler transient failure (503, 500, ...) must NOT populate
        the replay cache — otherwise the retry is pinned to the failure
        for 24 h and the whole idempotency mechanism becomes a foot-gun.

        Client-error (4xx) responses are still cached (they are
        terminal). Server-error (5xx) responses are transient and
        must be re-executed on retry.
        """

        async def flaky_handler(request: Request) -> Response:
            return JSONResponse({"err": "boom"}, status_code=503)

        async def stamp_actor(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            setattr(
                request.state,
                ACTOR_STATE_ATTR,
                _actor("01HXTOK00000000000000TOK00"),
            )
            return await call_next(request)

        app = Starlette(routes=[Route("/boom", flaky_handler, methods=["POST"])])
        app.add_middleware(IdempotencyMiddleware)
        from starlette.middleware.base import BaseHTTPMiddleware

        app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_actor)

        with TestClient(app) as client:
            resp = client.post("/boom", json={}, headers={IDEMPOTENCY_HEADER: "k-5xx"})
        assert resp.status_code == 503
        # Nothing persisted: a retry must re-execute the handler.
        with memory_factory() as session:
            assert session.query(IdempotencyKey).count() == 0

    def test_4xx_response_is_cached(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        """A 4xx response is terminal client-side and IS cached — the
        retry replays the same problem+json the first call produced."""

        async def reject_handler(request: Request) -> Response:
            return JSONResponse({"err": "bad"}, status_code=422)

        async def stamp_actor(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            setattr(
                request.state,
                ACTOR_STATE_ATTR,
                _actor("01HXTOK00000000000000TOK00"),
            )
            return await call_next(request)

        app = Starlette(routes=[Route("/bad", reject_handler, methods=["POST"])])
        app.add_middleware(IdempotencyMiddleware)
        from starlette.middleware.base import BaseHTTPMiddleware

        app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_actor)

        with TestClient(app) as client:
            first = client.post("/bad", json={}, headers={IDEMPOTENCY_HEADER: "k-4xx"})
            assert first.status_code == 422
            second = client.post("/bad", json={}, headers={IDEMPOTENCY_HEADER: "k-4xx"})
            assert second.status_code == 422
            assert second.headers[IDEMPOTENCY_REPLAY_HEADER] == "true"

        with memory_factory() as session:
            rows = session.query(IdempotencyKey).all()
            assert len(rows) == 1
            assert rows[0].status == 422

    def test_set_cookie_header_not_replayed(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        """``Set-Cookie`` from the original response must NOT leak into a
        replay — replaying a stale session cookie would be a privacy
        regression. The replay path only re-emits the narrow payload-
        identity header subset; every other header is re-stamped by
        downstream middleware on every response.
        """

        async def cookie_handler(request: Request) -> Response:
            resp = JSONResponse({"ok": True})
            resp.set_cookie("session", "fresh-value", httponly=True, samesite="lax")
            return resp

        async def stamp_actor(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            setattr(
                request.state,
                ACTOR_STATE_ATTR,
                _actor("01HXTOK00000000000000TOK00"),
            )
            return await call_next(request)

        app = Starlette(routes=[Route("/ck", cookie_handler, methods=["POST"])])
        app.add_middleware(IdempotencyMiddleware)
        from starlette.middleware.base import BaseHTTPMiddleware

        app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_actor)

        with TestClient(app) as client:
            first = client.post("/ck", json={}, headers={IDEMPOTENCY_HEADER: "k-ck"})
            assert first.status_code == 200
            assert "set-cookie" in {h.lower() for h in first.headers}
            second = client.post("/ck", json={}, headers={IDEMPOTENCY_HEADER: "k-ck"})
            assert second.status_code == 200
            assert second.headers[IDEMPOTENCY_REPLAY_HEADER] == "true"
            # The stored row must NOT carry Set-Cookie.
            replayed_headers = {h.lower() for h in second.headers}
            assert "set-cookie" not in replayed_headers

        with memory_factory() as session:
            row = session.query(IdempotencyKey).one()
            stored_headers = {k.lower() for k in row.headers}
            assert "set-cookie" not in stored_headers


# ---------------------------------------------------------------------------
# Concurrent-retry (IntegrityError) path
# ---------------------------------------------------------------------------


class TestIntegrityErrorRace:
    """Simulate the "second writer wins on uniqueness" branch.

    The happy-path tests above cover the sequential case where the
    second POST reads the winning row before doing any write. This
    class forces the opposite ordering: the second POST's read misses
    (row not yet committed), the handler runs, and only then the
    INSERT trips UNIQUE — which the middleware must recover from by
    re-reading and replaying.
    """

    def test_integrity_error_triggers_replay(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Inject a pre-existing cache row, then run a POST whose read
        sees ``None`` (via monkey-patched :func:`_read_cached`). The
        insert fires ``IntegrityError``; the middleware re-reads and
        replays the pre-existing row instead of double-writing."""
        from app.api.middleware import idempotency as mw

        token_id = "01HXTOK00000000000000TOK00"
        key = "race-integrity"
        cached_body = b'{"cached": true}'
        # Seed a row that the replay must surface.
        with memory_factory() as session:
            session.add(
                IdempotencyKey(
                    id=new_ulid(),
                    token_id=token_id,
                    key=key,
                    status=200,
                    body_hash=canonical_body_hash(b'{"x":1}'),
                    body=cached_body,
                    headers={"content-type": "application/json"},
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()

        # Force the first read to miss so the handler executes and the
        # insert path runs against the already-populated row.
        real_read_cached = mw._read_cached
        call_counter = {"n": 0}

        def fake_read_cached(
            db_session: Session, *, token_id: str, key: str
        ) -> IdempotencyKey | None:
            call_counter["n"] += 1
            # First read miss; every subsequent read (the recovery
            # re-read after IntegrityError) falls through to the real
            # helper so it can return the seeded row.
            if call_counter["n"] == 1:
                return None
            return real_read_cached(db_session, token_id=token_id, key=key)

        monkeypatch.setattr(mw, "_read_cached", fake_read_cached)

        handler_calls = {"n": 0}

        async def echo_handler(request: Request) -> Response:
            handler_calls["n"] += 1
            return JSONResponse({"echo": "fresh"})

        async def stamp_actor(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            setattr(request.state, ACTOR_STATE_ATTR, _actor(token_id))
            return await call_next(request)

        app = Starlette(routes=[Route("/r", echo_handler, methods=["POST"])])
        app.add_middleware(IdempotencyMiddleware)
        from starlette.middleware.base import BaseHTTPMiddleware

        app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_actor)

        with TestClient(app) as client:
            resp = client.post("/r", json={"x": 1}, headers={IDEMPOTENCY_HEADER: key})

        # Handler ran (first read missed), but the response the client
        # sees is the cached one because the insert collided.
        assert handler_calls["n"] == 1
        assert resp.status_code == 200
        assert resp.headers[IDEMPOTENCY_REPLAY_HEADER] == "true"
        assert resp.content == cached_body

        # Still exactly one row in the table — the losing insert was
        # rolled back.
        with memory_factory() as session:
            assert session.query(IdempotencyKey).count() == 1

    def test_integrity_error_with_different_body_returns_conflict(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the winner's row records a *different* body hash, the
        losing writer surfaces the same 409 ``idempotency_conflict``
        envelope the pre-handler conflict path emits — no double-standard
        between single- and concurrent-writer conflict detection."""
        from app.api.middleware import idempotency as mw

        token_id = "01HXTOK00000000000000TOK00"
        key = "race-conflict"
        with memory_factory() as session:
            session.add(
                IdempotencyKey(
                    id=new_ulid(),
                    token_id=token_id,
                    key=key,
                    status=200,
                    body_hash=canonical_body_hash(b'{"other": "body"}'),
                    body=b'{"other":"body"}',
                    headers={"content-type": "application/json"},
                    created_at=datetime.now(UTC),
                )
            )
            session.commit()

        real_read_cached = mw._read_cached
        call_counter = {"n": 0}

        def fake_read_cached(
            db_session: Session, *, token_id: str, key: str
        ) -> IdempotencyKey | None:
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return None
            return real_read_cached(db_session, token_id=token_id, key=key)

        monkeypatch.setattr(mw, "_read_cached", fake_read_cached)

        async def echo_handler(request: Request) -> Response:
            return JSONResponse({"echo": "fresh"})

        async def stamp_actor(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            setattr(request.state, ACTOR_STATE_ATTR, _actor(token_id))
            return await call_next(request)

        app = Starlette(routes=[Route("/rc", echo_handler, methods=["POST"])])
        app.add_middleware(IdempotencyMiddleware)
        from starlette.middleware.base import BaseHTTPMiddleware

        app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_actor)

        with TestClient(app) as client:
            resp = client.post("/rc", json={"x": 1}, headers={IDEMPOTENCY_HEADER: key})
        assert resp.status_code == 409
        body = resp.json()
        assert body["title"] == "Idempotency conflict"
        assert body["idempotency_key"] == key


# ---------------------------------------------------------------------------
# TTL sweep
# ---------------------------------------------------------------------------


class TestPruneExpired:
    """Deletion semantics of :func:`prune_expired_idempotency_keys`."""

    def test_prune_deletes_only_expired_rows(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        """Row older than ``IDEMPOTENCY_TTL_HOURS`` is deleted; fresher row stays."""
        now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
        expired_cutoff = now - timedelta(hours=IDEMPOTENCY_TTL_HOURS + 1)
        fresh_cutoff = now - timedelta(hours=IDEMPOTENCY_TTL_HOURS - 1)
        with memory_factory() as session:
            session.add(
                IdempotencyKey(
                    id=new_ulid(),
                    token_id="t-expired",
                    key="k1",
                    status=200,
                    body_hash="deadbeef",
                    body=b"{}",
                    headers={"content-type": "application/json"},
                    created_at=expired_cutoff,
                )
            )
            session.add(
                IdempotencyKey(
                    id=new_ulid(),
                    token_id="t-fresh",
                    key="k2",
                    status=200,
                    body_hash="cafef00d",
                    body=b"{}",
                    headers={"content-type": "application/json"},
                    created_at=fresh_cutoff,
                )
            )
            session.commit()

        deleted = prune_expired_idempotency_keys(now=now)
        assert deleted == 1

        with memory_factory() as session:
            rows = session.query(IdempotencyKey).all()
            assert len(rows) == 1
            assert rows[0].token_id == "t-fresh"

    def test_prune_empty_table_returns_zero(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        assert prune_expired_idempotency_keys() == 0

    def test_prune_with_custom_ttl(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        """Custom ``ttl`` override is honoured."""
        now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
        with memory_factory() as session:
            session.add(
                IdempotencyKey(
                    id=new_ulid(),
                    token_id="t",
                    key="k",
                    status=200,
                    body_hash="x",
                    body=b"{}",
                    headers={},
                    created_at=now - timedelta(hours=2),
                )
            )
            session.commit()

        # 1-hour TTL: the 2-hour-old row is expired and gets purged.
        deleted = prune_expired_idempotency_keys(now=now, ttl=timedelta(hours=1))
        assert deleted == 1

    def test_prune_accepts_external_session(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        """Caller-owned session: the helper uses it without opening a new UoW."""
        now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
        with memory_factory() as session:
            session.add(
                IdempotencyKey(
                    id=new_ulid(),
                    token_id="t",
                    key="k",
                    status=200,
                    body_hash="x",
                    body=b"{}",
                    headers={},
                    created_at=now - timedelta(hours=100),
                )
            )
            session.commit()
            # Pass the live session through; helper does not commit.
            deleted = prune_expired_idempotency_keys(db_session=session, now=now)
            assert deleted == 1
            # The helper leaves commit to the caller when given a session.
            session.commit()

        with memory_factory() as session:
            assert session.query(IdempotencyKey).count() == 0


# ---------------------------------------------------------------------------
# Clock injection
# ---------------------------------------------------------------------------


class TestClockInjection:
    """The middleware's ``created_at`` is pinned via the injected clock."""

    def test_created_at_comes_from_injected_clock(
        self,
        redirect_default_engine: None,
        memory_factory: sessionmaker[Session],
    ) -> None:
        frozen = datetime(2026, 4, 20, 9, 30, 0, tzinfo=UTC)

        async def echo_handler(request: Request) -> Response:
            return JSONResponse({"ok": True})

        async def stamp_actor(
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            setattr(
                request.state,
                ACTOR_STATE_ATTR,
                _actor("01HXTOK00000000000000TOK00"),
            )
            return await call_next(request)

        app = Starlette(routes=[Route("/echo", echo_handler, methods=["POST"])])
        # Inject the frozen clock directly via the constructor.
        app.add_middleware(IdempotencyMiddleware, clock=FrozenClock(frozen))
        from starlette.middleware.base import BaseHTTPMiddleware

        app.add_middleware(BaseHTTPMiddleware, dispatch=stamp_actor)

        with TestClient(app) as client:
            resp = client.post("/echo", json={}, headers={IDEMPOTENCY_HEADER: "k"})
            assert resp.status_code == 200

        with memory_factory() as session:
            rows = session.query(IdempotencyKey).all()
            assert len(rows) == 1
            # SQLite's DATETIME column may return a naive datetime;
            # compare the UTC components pairwise so timezone-naive
            # storage does not trip the assertion.
            stored = rows[0].created_at
            assert stored.replace(tzinfo=None) == frozen.replace(tzinfo=None)
