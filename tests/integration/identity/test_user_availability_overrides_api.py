"""Integration tests for ``/user_availability_overrides`` (cd-uqw1).

Exercises the router through :class:`TestClient` against a real DB
engine with the same model surface the production schema ships. Each
test asserts on:

* HTTP boundary: status code, response shape, error envelope.
* Persistence: the row lands in ``user_availability_override``;
  tombstones survive in the table when soft-deleted.
* Audit: the corresponding ``user_availability_override.<action>``
  row lands in the same transaction.
* Hybrid approval: the §06 matrix produces the right
  ``approval_required`` value end-to-end.

Pattern matches :mod:`tests.integration.identity.test_user_leaves_api`:
a per-test in-memory SQLite engine + ``Base.metadata.create_all``
keeps the fixture cost low without sacrificing the integration-tier
guarantee that the **real** ORM seam fires (tenant filter, audit row,
FK checks).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.availability.models import (
    UserAvailabilityOverride,
    UserWeeklyAvailability,
)
from app.adapters.db.base import Base
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.api.deps import current_workspace_context, db_session
from app.api.v1.user_availability_overrides import (
    build_user_availability_overrides_router,
)
from app.tenancy import WorkspaceContext, registry
from app.tenancy.context import ActorGrantRole
from app.tenancy.orm_filter import install_tenant_filter
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<ctx>.models`` module."""
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
    """Per-test in-memory SQLite engine."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def api_factory(api_engine: Engine) -> sessionmaker[Session]:
    """``sessionmaker`` with the tenant filter installed."""
    factory = sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Register the workspace-scoped tables this module touches."""
    registry.register("user_availability_override")
    registry.register("user_weekly_availability")
    registry.register("audit_log")
    registry.register("role_grant")
    registry.register("permission_group")
    registry.register("permission_group_member")


def _ctx(
    *,
    workspace_id: str,
    workspace_slug: str,
    actor_id: str,
    grant_role: ActorGrantRole = "manager",
    actor_was_owner_member: bool = True,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=actor_was_owner_member,
        audit_correlation_id=new_ulid(),
    )


def _build_app(factory: sessionmaker[Session], ctx: WorkspaceContext) -> FastAPI:
    """Mount the router behind pinned ctx + UoW overrides."""
    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response as StarletteResponse

    from app.tenancy.current import reset_current, set_current

    app = FastAPI()

    class _PinCtxMiddleware(BaseHTTPMiddleware):
        async def dispatch(
            self, request: Request, call_next: RequestResponseEndpoint
        ) -> StarletteResponse:
            token = set_current(ctx)
            try:
                response = await call_next(request)
                assert isinstance(response, StarletteResponse)
                return response
            finally:
                reset_current(token)

    app.add_middleware(_PinCtxMiddleware)
    app.include_router(build_user_availability_overrides_router())

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


def _seed_workspace_with_owner(
    factory: sessionmaker[Session], *, slug: str
) -> tuple[str, str, str]:
    """Seed a workspace + an owner user. Returns (ws_id, ws_slug, owner_id)."""
    with factory() as s:
        user = bootstrap_user(
            s, email=f"{slug}-owner@example.com", display_name=f"Owner {slug}"
        )
        ws = bootstrap_workspace(s, slug=slug, name=f"WS {slug}", owner_user_id=user.id)
        s.commit()
        return ws.id, ws.slug, user.id


def _seed_worker(factory: sessionmaker[Session], *, ws_id: str, email: str) -> str:
    with factory() as s:
        user = bootstrap_user(s, email=email, display_name=email.split("@")[0])
        s.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=ws_id,
                user_id=user.id,
                grant_role="worker",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        s.commit()
        return user.id


def _seed_weekly(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    user_id: str,
    weekday: int,
    starts_local: time | None,
    ends_local: time | None,
) -> None:
    with factory() as s:
        s.add(
            UserWeeklyAvailability(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user_id,
                weekday=weekday,
                starts_local=starts_local,
                ends_local=ends_local,
                updated_at=_PINNED,
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_worker_narrows_then_owner_approves(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Worker requests narrowed hours → pending → owner approve flips state.

        Asserts the full state transition + audit chain (created +
        approved) lands.
        """
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="uao-int-1"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="uao-int-1-w@example.com"
        )
        # 2026-05-04 is a Monday (weekday=0) — seed working pattern.
        _seed_weekly(
            api_factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )

        worker_ctx_obj = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = TestClient(
            _build_app(api_factory, worker_ctx_obj),
            raise_server_exceptions=False,
        )
        post = worker_client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "12:00:00",
                "reason": "Doctor",
            },
        )
        assert post.status_code == 201, post.text
        override_id = post.json()["id"]
        assert post.json()["approval_required"] is True
        assert post.json()["approved_at"] is None

        # Persisted row is pending — read back through a fresh UoW
        # under the worker's ctx.
        with UnitOfWorkImpl(session_factory=api_factory) as s:
            assert isinstance(s, Session)
            from app.tenancy.current import reset_current, set_current

            t = set_current(worker_ctx_obj)
            try:
                row = s.get(UserAvailabilityOverride, override_id)
            finally:
                reset_current(t)
        assert row is not None
        assert row.approved_at is None
        assert row.approval_required is True
        assert row.reason == "Doctor"

        owner_ctx_obj = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=owner_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        owner_client = TestClient(
            _build_app(api_factory, owner_ctx_obj),
            raise_server_exceptions=False,
        )
        approve = owner_client.post(
            f"/user_availability_overrides/{override_id}/approve"
        )
        assert approve.status_code == 200, approve.text
        assert approve.json()["approved_at"] is not None
        assert approve.json()["approved_by"] == owner_id

        # Audit chain.
        with UnitOfWorkImpl(session_factory=api_factory) as s:
            assert isinstance(s, Session)
            from app.tenancy.current import reset_current, set_current

            t = set_current(owner_ctx_obj)
            try:
                actions = sorted(
                    r.action
                    for r in s.scalars(
                        select(AuditLog).where(AuditLog.entity_id == override_id)
                    ).all()
                )
            finally:
                reset_current(t)
        assert "user_availability_override.created" in actions
        assert "user_availability_override.approved" in actions

    def test_worker_extends_hours_auto_approves(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Worker requesting wider hours auto-approves end-to-end."""
        ws_id, ws_slug, _ = _seed_workspace_with_owner(api_factory, slug="uao-int-2")
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="uao-int-2-w@example.com"
        )
        _seed_weekly(
            api_factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )

        ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        client = TestClient(_build_app(api_factory, ctx), raise_server_exceptions=False)
        resp = client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": True,
                "starts_local": "09:00:00",
                "ends_local": "19:00:00",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approval_required"] is False
        assert body["approved_at"] is not None
        assert body["approved_by"] == worker_id

    def test_reject_soft_deletes_and_folds_reason(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Reject soft-deletes the row and folds reason into ``reason``."""
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="uao-int-3"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="uao-int-3-w@example.com"
        )
        _seed_weekly(
            api_factory,
            workspace_id=ws_id,
            user_id=worker_id,
            weekday=0,
            starts_local=time(9, 0),
            ends_local=time(17, 0),
        )
        worker_ctx_obj = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=worker_id,
            grant_role="worker",
            actor_was_owner_member=False,
        )
        worker_client = TestClient(
            _build_app(api_factory, worker_ctx_obj),
            raise_server_exceptions=False,
        )
        override = worker_client.post(
            "/user_availability_overrides",
            json={
                "date": "2026-05-04",
                "available": False,
                "reason": "Original request",
            },
        ).json()
        assert override["approval_required"] is True

        owner_ctx_obj = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=owner_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        owner_client = TestClient(
            _build_app(api_factory, owner_ctx_obj),
            raise_server_exceptions=False,
        )
        rej = owner_client.post(
            f"/user_availability_overrides/{override['id']}/reject",
            json={"reason_md": "Coverage gap"},
        )
        assert rej.status_code == 200, rej.text

        # Row tombstoned with combined reason.
        with UnitOfWorkImpl(session_factory=api_factory) as s:
            assert isinstance(s, Session)
            from app.tenancy.current import reset_current, set_current

            t = set_current(owner_ctx_obj)
            try:
                row = s.get(UserAvailabilityOverride, override["id"])
            finally:
                reset_current(t)
        assert row is not None
        assert row.deleted_at is not None
        assert "Original request" in (row.reason or "")
        assert "Rejected: Coverage gap" in (row.reason or "")

    def test_cross_workspace_invisible(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """A row in workspace A is invisible from B's caller."""
        ws_a_id, ws_a_slug, owner_a = _seed_workspace_with_owner(
            api_factory, slug="uao-int-a"
        )
        ws_b_id, ws_b_slug, owner_b = _seed_workspace_with_owner(
            api_factory, slug="uao-int-b"
        )
        ctx_a = _ctx(
            workspace_id=ws_a_id,
            workspace_slug=ws_a_slug,
            actor_id=owner_a,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client_a = TestClient(
            _build_app(api_factory, ctx_a), raise_server_exceptions=False
        )
        override = client_a.post(
            "/user_availability_overrides",
            json={"date": "2026-08-01", "available": False},
        ).json()

        ctx_b = _ctx(
            workspace_id=ws_b_id,
            workspace_slug=ws_b_slug,
            actor_id=owner_b,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client_b = TestClient(
            _build_app(api_factory, ctx_b), raise_server_exceptions=False
        )
        listing = client_b.get("/user_availability_overrides").json()
        assert listing["data"] == []
        for r in (
            client_b.patch(
                f"/user_availability_overrides/{override['id']}",
                json={"reason": "x"},
            ),
            client_b.post(f"/user_availability_overrides/{override['id']}/approve"),
            client_b.post(f"/user_availability_overrides/{override['id']}/reject"),
            client_b.delete(f"/user_availability_overrides/{override['id']}"),
        ):
            assert r.status_code == 404, r.text

    def test_pagination(self, api_factory: sessionmaker[Session]) -> None:
        """Cursor envelope walks forward across pages."""
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="uao-int-pag"
        )
        ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=owner_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client = TestClient(_build_app(api_factory, ctx), raise_server_exceptions=False)
        for i in range(5):
            r = client.post(
                "/user_availability_overrides",
                json={
                    "date": f"2026-09-{i + 1:02d}",
                    "available": False,
                },
            )
            assert r.status_code == 201, r.text

        page1 = client.get("/user_availability_overrides?limit=2").json()
        assert page1["has_more"] is True
        assert len(page1["data"]) == 2
        page2 = client.get(
            f"/user_availability_overrides?cursor={page1['next_cursor']}&limit=2"
        ).json()
        assert page2["has_more"] is True
        assert len(page2["data"]) == 2
        page3 = client.get(
            f"/user_availability_overrides?cursor={page2['next_cursor']}&limit=2"
        ).json()
        assert page3["has_more"] is False
        assert len(page3["data"]) == 1
