"""HTTP-boundary tests for the messaging push router (cd-0bnz).

Exercises the web-push subscription endpoints through
:class:`TestClient` against a minimal FastAPI app wired with the
same deps the production factory uses. Every test asserts on the
HTTP boundary (status code, error envelope, response shape) and on
the side effects the domain service emits (DB row, audit row).

Covers:

* ``GET /notifications/push/vapid-key`` — 200 + ``{"key": ...}`` on
  first hit; cached response on a second hit within the 5-minute
  window (fake monotonic clock proves the DB SELECT fires exactly
  once); 503 ``vapid_not_configured`` when the setting is missing.
* ``POST /notifications/push/subscribe`` — 201 + payload on happy
  path; 422 ``endpoint_not_allowed`` on a non-provider host; 422
  ``endpoint_scheme_invalid`` on ``http://``; idempotent on a
  second call against the same ``(user_id, endpoint)``.
* ``POST /notifications/push/unsubscribe`` — 204 after a prior
  subscribe; 204 even when no prior row existed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import PushToken
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1.messaging import (
    _reset_vapid_cache_for_tests,
    build_messaging_router,
)
from app.domain.messaging.push_tokens import SETTINGS_KEY_VAPID_PUBLIC
from app.tenancy.context import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def api_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(api_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)


@pytest.fixture(autouse=True)
def reset_cache() -> Iterator[None]:
    """Drop the module-level VAPID cache between tests.

    The cache is workspace-keyed but the bootstrap fixtures mint a
    fresh workspace_id per test (ULID), so a leak is rare in
    practice; the explicit reset keeps behaviour deterministic
    regardless of test ordering or factory seed collisions.
    """
    _reset_vapid_cache_for_tests()
    yield
    _reset_vapid_cache_for_tests()


def _bootstrap_workspace(
    s: Session,
    *,
    slug: str,
    vapid_public: str | None = "vapid-pub-test-key",
) -> str:
    workspace_id = new_ulid()
    settings_json: dict[str, str] = {}
    if vapid_public is not None:
        settings_json[SETTINGS_KEY_VAPID_PUBLIC] = vapid_public
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"WS {slug}",
            plan="free",
            quota_json={},
            settings_json=settings_json,
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(s: Session, *, email: str, display_name: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=display_name,
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    slug: str = "api-push",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


class _FakeMonotonic:
    """Controllable monotonic clock for TTL tests.

    The router expects a bare ``Callable[[], float]``; this class
    carries the elapsed seconds on a mutable attribute so the test
    can advance time deterministically without patching
    :func:`time.monotonic` globally.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now: float = start

    def __call__(self) -> float:
        return self.now


def _build_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    *,
    monotonic: _FakeMonotonic | None = None,
) -> FastAPI:
    """Mount the messaging router behind pinned ctx + db overrides."""
    app = FastAPI()
    # Use the factory entry point so tests can inject a fake monotonic
    # without touching the production ``router`` module-level singleton.
    r = build_messaging_router(
        monotonic=monotonic if monotonic is not None else None,
    )
    app.include_router(r)

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return app


@pytest.fixture
def client_env(
    factory: sessionmaker[Session],
) -> tuple[TestClient, WorkspaceContext, str]:
    with factory() as s:
        ws_id = _bootstrap_workspace(s, slug="push-main")
        user_id = _bootstrap_user(s, email="w@example.com", display_name="W")
        s.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
    client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)
    return client, ctx, user_id


# ---------------------------------------------------------------------------
# Subscribe
# ---------------------------------------------------------------------------


class TestSubscribe:
    def test_happy_path_201(
        self,
        client_env: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, ctx, user_id = client_env
        resp = client.post(
            "/notifications/push/subscribe",
            json={
                "endpoint": "https://fcm.googleapis.com/fcm/send/abc",
                "keys": {"p256dh": "p256-x", "auth": "auth-x"},
                "ua": "Mozilla/5.0 Test",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == user_id
        assert body["workspace_id"] == ctx.workspace_id
        assert body["endpoint"] == "https://fcm.googleapis.com/fcm/send/abc"
        assert body["user_agent"] == "Mozilla/5.0 Test"

    def test_endpoint_not_allowed_422(
        self,
        client_env: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = client_env
        resp = client.post(
            "/notifications/push/subscribe",
            json={
                "endpoint": "https://attacker.example/push/sink",
                "keys": {"p256dh": "p", "auth": "a"},
            },
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "endpoint_not_allowed"

    def test_http_scheme_422(
        self,
        client_env: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = client_env
        resp = client.post(
            "/notifications/push/subscribe",
            json={
                "endpoint": "http://fcm.googleapis.com/fcm/send/xyz",
                "keys": {"p256dh": "p", "auth": "a"},
            },
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "endpoint_scheme_invalid"

    def test_idempotent_second_subscribe(
        self,
        client_env: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, ctx, _uid = client_env
        endpoint = "https://updates.push.services.mozilla.com/abc"
        first = client.post(
            "/notifications/push/subscribe",
            json={
                "endpoint": endpoint,
                "keys": {"p256dh": "p1", "auth": "a1"},
            },
        )
        assert first.status_code == 201
        second = client.post(
            "/notifications/push/subscribe",
            json={
                "endpoint": endpoint,
                "keys": {"p256dh": "p2", "auth": "a2"},
            },
        )
        assert second.status_code == 201
        # Same row id both times — idempotent upsert.
        assert first.json()["id"] == second.json()["id"]

        with factory() as s:
            rows = list(
                s.scalars(
                    select(PushToken).where(PushToken.workspace_id == ctx.workspace_id)
                ).all()
            )
        assert len(rows) == 1

        # Exactly one subscribe audit row — the second call was a
        # benign refresh.
        with factory() as s:
            count = len(
                list(
                    s.scalars(
                        select(AuditLog).where(
                            AuditLog.workspace_id == ctx.workspace_id,
                            AuditLog.action == "messaging.push.subscribed",
                        )
                    ).all()
                )
            )
        assert count == 1


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------


class TestUnsubscribe:
    def test_unsubscribe_after_subscribe_204(
        self,
        client_env: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, ctx, _uid = client_env
        endpoint = "https://web.push.apple.com/opaque-token"
        client.post(
            "/notifications/push/subscribe",
            json={
                "endpoint": endpoint,
                "keys": {"p256dh": "p", "auth": "a"},
            },
        )
        resp = client.post(
            "/notifications/push/unsubscribe",
            json={"endpoint": endpoint},
        )
        assert resp.status_code == 204, resp.text
        # Row deleted.
        with factory() as s:
            rows = list(
                s.scalars(
                    select(PushToken).where(PushToken.workspace_id == ctx.workspace_id)
                ).all()
            )
        assert rows == []

    def test_unsubscribe_on_missing_still_204(
        self,
        client_env: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = client_env
        resp = client.post(
            "/notifications/push/unsubscribe",
            json={"endpoint": "https://fcm.googleapis.com/fcm/send/ghost"},
        )
        assert resp.status_code == 204, resp.text

    def test_unsubscribe_extra_field_rejected_422(
        self,
        client_env: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        """A typo'd payload key is rejected, not silently treated as empty.

        Locks the ``extra='forbid'`` posture: a caller that sends
        ``{"url": "..."}`` instead of ``{"endpoint": "..."}`` gets
        a Pydantic 422 rather than a 204 with no side effect (which
        would mask a UI bug indefinitely).
        """
        client, *_ = client_env
        resp = client.post(
            "/notifications/push/unsubscribe",
            json={
                "url": "https://fcm.googleapis.com/fcm/send/abc",
            },
        )
        assert resp.status_code == 422, resp.text

    def test_unsubscribe_missing_endpoint_422(
        self,
        client_env: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        """Empty payload is a 422 — the endpoint is required."""
        client, *_ = client_env
        resp = client.post(
            "/notifications/push/unsubscribe",
            json={},
        )
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# VAPID public key
# ---------------------------------------------------------------------------


class TestVapidKey:
    def test_returns_key_200(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(
                s, slug="vapid-200", vapid_public="operator-pubkey"
            )
            user_id = _bootstrap_user(s, email="v@example.com", display_name="V")
            s.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)

        resp = client.get("/notifications/push/vapid-key")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"key": "operator-pubkey"}

    def test_cached_on_second_call(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        """Two calls within the 5-min window — second is a cache hit.

        We prove caching by asserting the returned key tracks the
        cached value even after the backing setting changes: a DB
        mutation between calls is invisible until TTL expiry. The
        fake monotonic clock drives the TTL deterministically.
        """
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="vapid-cache", vapid_public="v1")
            user_id = _bootstrap_user(s, email="c@example.com", display_name="C")
            s.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        clock = _FakeMonotonic(start=0.0)
        client = TestClient(
            _build_app(factory, ctx, monotonic=clock),
            raise_server_exceptions=False,
        )

        first = client.get("/notifications/push/vapid-key")
        assert first.status_code == 200
        assert first.json() == {"key": "v1"}

        # Rotate the setting in the DB — the cached key should win
        # until the TTL expires.
        with factory() as s:
            ws = s.scalars(select(Workspace).where(Workspace.id == ws_id)).one()
            ws.settings_json = {SETTINGS_KEY_VAPID_PUBLIC: "v2"}
            s.commit()

        # Advance by 120 seconds — inside the 300s TTL, should be
        # a cache hit.
        clock.now = 120.0
        second = client.get("/notifications/push/vapid-key")
        assert second.status_code == 200
        assert second.json() == {"key": "v1"}, "second call was not a cache hit"

        # Jump past TTL — cache miss refreshes and picks up v2.
        clock.now = 600.0
        third = client.get("/notifications/push/vapid-key")
        assert third.status_code == 200
        assert third.json() == {"key": "v2"}

    def test_missing_setting_503(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="vapid-missing", vapid_public=None)
            user_id = _bootstrap_user(s, email="m@example.com", display_name="M")
            s.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)

        resp = client.get("/notifications/push/vapid-key")
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"]["error"] == "vapid_not_configured"
