"""Integration tests for :mod:`app.api.v1.time`.

Exercises the shift routes through :class:`TestClient` against a
minimal FastAPI app wired with the same deps the factory uses.
Every test asserts on the HTTP boundary (status code, error
envelope, response shape) and on the side effects the domain
service emits (DB row, audit row, SSE event).

Covers:

* ``POST /shifts/open`` — happy path (201 + :class:`ShiftPayload`);
  409 ``already_open`` on a second open while the first is live;
  403 ``forbidden`` on a cross-user open without the
  ``time.edit_others`` capability.
* ``POST /shifts/{id}/close`` — worker closes own (200); cross-user
  close by a non-manager returns 403 ``forbidden``; manager close
  of a worker's shift succeeds; 404 on a missing id; 422
  ``invalid_window`` on ``ends_at < starts_at``.
* ``PATCH /shifts/{id}`` — manager-only; worker gets 403;
  ``invalid_window`` fires on zero-length; unknown id → 404.
* ``GET /shifts`` — returns ``{"items": [...]}``; filters by
  ``user_id`` + ``open_only``; tenant filter keeps a peer workspace
  invisible.
* ``GET /shifts/{id}`` — returns the view; 404 on unknown id.
* SSE: every mutation emits a :class:`ShiftChanged` event whose
  handler receives the right action + shift id.
* OpenAPI regeneration: the factory picks up the new routes and
  the merged document exposes the five operation ids.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.time.models import Shift
from app.adapters.db.workspace.models import Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1.time import router as time_router
from app.events import ShiftChanged, bus
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


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
    """Per-test in-memory SQLite engine.

    Named to avoid collision with the session-scoped ``engine``
    fixture from :mod:`tests.integration.conftest`. We don't need
    alembic here — the ORM surface is enough to exercise the router
    end-to-end.
    """
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
def reset_bus() -> Iterator[None]:
    yield
    bus._reset_for_tests()


def _bootstrap_workspace(s: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"WS {slug}",
            plan="free",
            quota_json={},
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


def _grant(s: Session, *, workspace_id: str, user_id: str, grant_role: str) -> None:
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    slug: str = "api-ws",
    grant_role: ActorGrantRole = "worker",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
) -> FastAPI:
    """Mount :data:`time_router` behind pinned ctx + db overrides."""
    app = FastAPI()
    app.include_router(time_router)

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
def worker_client(
    factory: sessionmaker[Session],
) -> tuple[TestClient, WorkspaceContext, str]:
    with factory() as s:
        ws_id = _bootstrap_workspace(s, slug="api-worker")
        user_id = _bootstrap_user(s, email="w@example.com", display_name="W")
        _grant(s, workspace_id=ws_id, user_id=user_id, grant_role="worker")
        s.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="worker")
    client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)
    return client, ctx, user_id


@pytest.fixture
def manager_client(
    factory: sessionmaker[Session],
) -> tuple[TestClient, WorkspaceContext, str]:
    with factory() as s:
        ws_id = _bootstrap_workspace(s, slug="api-mgr")
        user_id = _bootstrap_user(s, email="m@example.com", display_name="M")
        _grant(s, workspace_id=ws_id, user_id=user_id, grant_role="manager")
        s.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="manager")
    client = TestClient(_build_app(factory, ctx), raise_server_exceptions=False)
    return client, ctx, user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit_actions(
    factory: sessionmaker[Session], *, workspace_id: str, entity_kind: str
) -> list[str]:
    """Return the ordered list of ``action`` fields for shift audit rows."""
    with factory() as s:
        rows = s.scalars(
            select(AuditLog)
            .where(
                AuditLog.workspace_id == workspace_id,
                AuditLog.entity_kind == entity_kind,
            )
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    return [r.action for r in rows]


# ---------------------------------------------------------------------------
# POST /shifts/open
# ---------------------------------------------------------------------------


class TestOpenShift:
    def test_worker_opens_shift_201(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, ctx, user_id = worker_client
        resp = client.post("/shifts/open", json={})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == user_id
        assert body["ends_at"] is None
        assert body["source"] == "manual"
        assert body["workspace_id"] == ctx.workspace_id

    def test_second_open_409_already_open(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, _ctx, _uid = worker_client
        first = client.post("/shifts/open", json={})
        assert first.status_code == 201
        second = client.post("/shifts/open", json={})
        assert second.status_code == 409, second.text
        detail = second.json()["detail"]
        assert detail["error"] == "already_open"
        assert detail["existing_shift_id"] == first.json()["id"]

    def test_cross_user_open_without_manager_returns_403(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, ctx, _uid = worker_client
        with factory() as s:
            other_id = _bootstrap_user(s, email="o@example.com", display_name="O")
            _grant(
                s,
                workspace_id=ctx.workspace_id,
                user_id=other_id,
                grant_role="worker",
            )
            s.commit()
        resp = client.post("/shifts/open", json={"user_id": other_id})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "forbidden"

    def test_manager_opens_shift_for_worker(
        self,
        manager_client: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, ctx, _mid = manager_client
        with factory() as s:
            worker_id = _bootstrap_user(s, email="mw@example.com", display_name="MW")
            _grant(
                s,
                workspace_id=ctx.workspace_id,
                user_id=worker_id,
                grant_role="worker",
            )
            s.commit()
        resp = client.post(
            "/shifts/open",
            json={"user_id": worker_id, "property_id": "prop-1"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["user_id"] == worker_id
        assert body["property_id"] == "prop-1"

    def test_open_writes_audit_row(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, ctx, _uid = worker_client
        resp = client.post("/shifts/open", json={})
        assert resp.status_code == 201
        actions = _audit_actions(
            factory, workspace_id=ctx.workspace_id, entity_kind="shift"
        )
        assert actions == ["open"]

    def test_open_fires_shift_changed_event(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, _ctx, user_id = worker_client
        captured: list[ShiftChanged] = []

        @bus.subscribe(ShiftChanged)
        def _h(event: ShiftChanged) -> None:
            captured.append(event)

        resp = client.post("/shifts/open", json={})
        assert resp.status_code == 201
        assert len(captured) == 1
        assert captured[0].action == "opened"
        assert captured[0].user_id == user_id


# ---------------------------------------------------------------------------
# POST /shifts/{id}/close
# ---------------------------------------------------------------------------


class TestCloseShift:
    def test_close_own_shift(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, _ctx, _uid = worker_client
        opened = client.post("/shifts/open", json={}).json()
        resp = client.post(f"/shifts/{opened['id']}/close", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ends_at"] is not None

    def test_close_missing_shift_404(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        resp = client.post("/shifts/nope/close", json={})
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "not_found"

    def test_close_with_invalid_window_422(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, _ctx, _uid = worker_client
        opened = client.post("/shifts/open", json={}).json()
        # ``starts_at`` from the server is ~``now``; sending an
        # ``ends_at`` firmly in the past forces the boundary check.
        way_earlier = (
            datetime.fromisoformat(opened["starts_at"].replace("Z", "+00:00"))
            - timedelta(hours=24)
        ).isoformat()
        resp = client.post(
            f"/shifts/{opened['id']}/close",
            json={"ends_at": way_earlier},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_window"

    def test_worker_cannot_close_another_users_shift(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        """Two workers in the same workspace — B cannot close A's shift."""
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="x-close")
            a_id = _bootstrap_user(s, email="a@x.com", display_name="A")
            b_id = _bootstrap_user(s, email="b@x.com", display_name="B")
            _grant(s, workspace_id=ws_id, user_id=a_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=b_id, grant_role="worker")
            s.commit()

        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        client_a = TestClient(_build_app(factory, ctx_a), raise_server_exceptions=False)
        client_b = TestClient(_build_app(factory, ctx_b), raise_server_exceptions=False)
        opened = client_a.post("/shifts/open", json={}).json()
        resp = client_b.post(f"/shifts/{opened['id']}/close", json={})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "forbidden"

    def test_manager_can_close_workers_shift(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="mgr-close")
            worker_id = _bootstrap_user(s, email="w@c.com", display_name="W")
            mgr_id = _bootstrap_user(s, email="m@c.com", display_name="M")
            _grant(s, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
            s.commit()

        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        client_w = TestClient(
            _build_app(factory, ctx_worker), raise_server_exceptions=False
        )
        client_m = TestClient(
            _build_app(factory, ctx_mgr), raise_server_exceptions=False
        )
        opened = client_w.post("/shifts/open", json={}).json()
        resp = client_m.post(f"/shifts/{opened['id']}/close", json={})
        assert resp.status_code == 200, resp.text

    def test_close_fires_shift_changed_event(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        captured: list[ShiftChanged] = []

        @bus.subscribe(ShiftChanged)
        def _h(event: ShiftChanged) -> None:
            captured.append(event)

        opened = client.post("/shifts/open", json={}).json()
        client.post(f"/shifts/{opened['id']}/close", json={})
        actions = [e.action for e in captured]
        assert actions == ["opened", "closed"]


# ---------------------------------------------------------------------------
# PATCH /shifts/{id}
# ---------------------------------------------------------------------------


class TestEditShift:
    def test_worker_edit_returns_403(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        opened = client.post("/shifts/open", json={}).json()
        resp = client.patch(f"/shifts/{opened['id']}", json={"notes_md": "bump"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "forbidden"

    def test_manager_edit_200(
        self,
        manager_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = manager_client
        opened = client.post("/shifts/open", json={}).json()
        resp = client.patch(
            f"/shifts/{opened['id']}",
            json={"notes_md": "manager amendment"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["notes_md"] == "manager amendment"

    def test_edit_invalid_window_422(
        self,
        manager_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = manager_client
        opened = client.post("/shifts/open", json={}).json()
        # zero-length window — strict reject on the manager edit path.
        resp = client.patch(
            f"/shifts/{opened['id']}",
            json={"ends_at": opened["starts_at"]},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_window"

    def test_edit_missing_404(
        self,
        manager_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = manager_client
        resp = client.patch("/shifts/nope", json={"notes_md": "x"})
        assert resp.status_code == 404, resp.text

    def test_edit_fires_shift_changed_event(
        self,
        manager_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = manager_client
        captured: list[ShiftChanged] = []

        @bus.subscribe(ShiftChanged)
        def _h(event: ShiftChanged) -> None:
            captured.append(event)

        opened = client.post("/shifts/open", json={}).json()
        client.patch(f"/shifts/{opened['id']}", json={"notes_md": "e"})
        assert [e.action for e in captured] == ["opened", "edited"]


# ---------------------------------------------------------------------------
# GET /shifts + GET /shifts/{id}
# ---------------------------------------------------------------------------


class TestListShifts:
    def test_list_returns_items_envelope(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        client.post("/shifts/open", json={})
        resp = client.get("/shifts")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "items" in body
        assert len(body["items"]) == 1

    def test_list_open_only(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        opened = client.post("/shifts/open", json={}).json()
        client.post(f"/shifts/{opened['id']}/close", json={})
        # Open a second one so ``open_only`` has a thing to filter.
        client.post("/shifts/open", json={})

        all_resp = client.get("/shifts").json()
        open_resp = client.get("/shifts", params={"open_only": "true"}).json()
        assert len(all_resp["items"]) == 2
        assert len(open_resp["items"]) == 1
        assert open_resp["items"][0]["ends_at"] is None

    def test_list_filter_by_user_id(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        with factory() as s:
            ws_id = _bootstrap_workspace(s, slug="list-uid")
            a_id = _bootstrap_user(s, email="a@l.com", display_name="A")
            b_id = _bootstrap_user(s, email="b@l.com", display_name="B")
            _grant(s, workspace_id=ws_id, user_id=a_id, grant_role="worker")
            _grant(s, workspace_id=ws_id, user_id=b_id, grant_role="worker")
            s.commit()

        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        client_a = TestClient(_build_app(factory, ctx_a), raise_server_exceptions=False)
        client_b = TestClient(_build_app(factory, ctx_b), raise_server_exceptions=False)
        client_a.post("/shifts/open", json={})
        client_b.post("/shifts/open", json={})

        resp = client_a.get("/shifts", params={"user_id": a_id}).json()
        assert all(item["user_id"] == a_id for item in resp["items"])

    def test_get_one_returns_view(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        opened = client.post("/shifts/open", json={}).json()
        resp = client.get(f"/shifts/{opened['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == opened["id"]

    def test_get_one_404(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        resp = client.get("/shifts/no-such")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "not_found"


# ---------------------------------------------------------------------------
# OpenAPI surface
# ---------------------------------------------------------------------------


class TestOpenapiExposure:
    def test_routes_show_up_in_openapi(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        """FastAPI's ``/openapi.json`` should expose every time route."""
        client, *_ = worker_client
        schema = client.get("/openapi.json").json()
        paths = schema.get("paths", {})
        assert "/shifts/open" in paths
        assert "/shifts/{shift_id}/close" in paths
        assert "/shifts/{shift_id}" in paths
        assert "/shifts" in paths
        # Confirm the operation ids pinned in the router match the spec.
        op_ids: set[str] = set()
        for path in paths.values():
            for op in path.values():
                if isinstance(op, dict) and "operationId" in op:
                    op_ids.add(op["operationId"])
        expected = {
            "time.open_shift",
            "time.close_shift",
            "time.edit_shift",
            "time.list_shifts",
            "time.get_shift",
        }
        assert expected.issubset(op_ids), f"missing: {expected - op_ids}"


# ---------------------------------------------------------------------------
# DB sanity — the shift row actually persisted
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_open_persists_row(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, ctx, user_id = worker_client
        body = client.post("/shifts/open", json={}).json()
        with factory() as s:
            row = s.get(Shift, body["id"])
            assert row is not None
            assert row.workspace_id == ctx.workspace_id
            assert row.user_id == user_id
            assert row.ends_at is None

    def test_close_persists_ends_at(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
        factory: sessionmaker[Session],
    ) -> None:
        client, *_ = worker_client
        opened = client.post("/shifts/open", json={}).json()
        closed = client.post(f"/shifts/{opened['id']}/close", json={}).json()
        with factory() as s:
            row = s.get(Shift, closed["id"])
            assert row is not None
            assert row.ends_at is not None


# ---------------------------------------------------------------------------
# Cross-workspace tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_peer_workspace_shift_invisible(
        self,
        factory: sessionmaker[Session],
    ) -> None:
        """A shift in workspace A isn't visible to a ctx pinned to workspace B."""
        with factory() as s:
            ws_a = _bootstrap_workspace(s, slug="ti-a")
            ws_b = _bootstrap_workspace(s, slug="ti-b")
            user_a = _bootstrap_user(s, email="a@t.com", display_name="A")
            user_b = _bootstrap_user(s, email="b@t.com", display_name="B")
            _grant(s, workspace_id=ws_a, user_id=user_a, grant_role="worker")
            _grant(s, workspace_id=ws_b, user_id=user_b, grant_role="worker")
            s.commit()

        ctx_a = _ctx(workspace_id=ws_a, actor_id=user_a, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_b, actor_id=user_b, grant_role="worker")
        client_a = TestClient(_build_app(factory, ctx_a), raise_server_exceptions=False)
        client_b = TestClient(_build_app(factory, ctx_b), raise_server_exceptions=False)
        opened = client_a.post("/shifts/open", json={}).json()

        # GET /shifts/{id} from workspace B → 404.
        resp = client_b.get(f"/shifts/{opened['id']}")
        assert resp.status_code == 404

        # GET /shifts from workspace B → empty list.
        resp = client_b.get("/shifts").json()
        assert resp["items"] == []


# ---------------------------------------------------------------------------
# DTO — extra="forbid" keeps stray fields from leaking through
# ---------------------------------------------------------------------------


class TestDtoGuards:
    def test_unknown_open_field_422(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        resp = client.post("/shifts/open", json={"source": "manual", "bogus": 1})
        # FastAPI returns its own 422 validation envelope, not the
        # router-local ``{"error": "..."}`` shape, when the DTO
        # itself rejects the body. Confirm we bounce at the DTO.
        assert resp.status_code == 422
        payload: dict[str, Any] = resp.json()
        assert "detail" in payload

    def test_unknown_close_field_422(
        self,
        worker_client: tuple[TestClient, WorkspaceContext, str],
    ) -> None:
        client, *_ = worker_client
        opened = client.post("/shifts/open", json={}).json()
        resp = client.post(
            f"/shifts/{opened['id']}/close", json={"starts_at": _PINNED.isoformat()}
        )
        assert resp.status_code == 422
