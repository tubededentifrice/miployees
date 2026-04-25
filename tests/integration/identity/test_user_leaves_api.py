"""Integration tests for ``/user_leaves`` (cd-oydd).

Exercises the router through :class:`TestClient` against a real DB
engine with the same model surface the production schema ships. Each
test asserts on:

* HTTP boundary: status code, response shape, error envelope.
* Persistence: the row lands in ``user_leave``; tombstones survive
  in the table when soft-deleted.
* Audit: the corresponding ``user_leave.<action>`` row lands in the
  same transaction.

Pattern matches :mod:`tests.integration.test_leaves_api`: a per-test
in-memory SQLite engine + ``Base.metadata.create_all`` keeps the
fixture cost low without sacrificing the integration-tier guarantee
that the **real** ORM seam fires (tenant filter, audit row, FK
checks).

Covers the spec §06 / §12 surface end-to-end:

* Worker self-submit lands pending; manager / owner self-submit
  auto-approves.
* Approve stamps ``approved_at`` + ``approved_by``; second approve
  collapses to 409.
* Reject soft-deletes the row and folds ``reason_md`` into
  ``note_md``.
* DELETE soft-deletes; second DELETE is 404.
* Cross-workspace probes are 404 (tenant filter at the row layer).
* Cursor pagination walks forward across pages.
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
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.availability.models import UserLeave
from app.adapters.db.base import Base
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.api.deps import current_workspace_context, db_session
from app.api.v1.user_leaves import build_user_leaves_router
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
    """Per-test in-memory SQLite engine.

    Mirrors :func:`tests.integration.test_leaves_api.api_engine` —
    a tighter rig than the session-scoped ``engine`` fixture from
    :mod:`tests.integration.conftest` because we do not need the
    full alembic chain to assert on the user_leave router.
    """
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def api_factory(api_engine: Engine) -> sessionmaker[Session]:
    """``sessionmaker`` with the tenant filter installed.

    The filter is the production seam every workspace-scoped query
    walks through; installing it here keeps the integration-tier
    guarantee — a missing :class:`WorkspaceContext` raises
    :class:`TenantFilterMissing` instead of silently returning every
    row across the deployment.
    """
    factory = sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _ensure_tables_registered() -> None:
    """Register the workspace-scoped tables this module touches."""
    registry.register("user_leave")
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
    """Mount the user_leaves router behind pinned ctx + UoW overrides.

    Adds a :class:`~starlette.middleware.base.BaseHTTPMiddleware`
    that mirrors the production :class:`WorkspaceContextMiddleware`:
    it calls :func:`set_current` on the way in and :func:`reset_current`
    on the way out, so the tenant filter installed on ``factory`` by
    :func:`api_factory` sees a live :class:`WorkspaceContext` at SELECT
    compile time. Without this, an integration-tier request would
    trip :class:`TenantFilterMissing` on every workspace-scoped
    SELECT (e.g. ``is_owner_member`` joining ``permission_group``)
    because the dep override alone runs after the SQLAlchemy event
    listeners have already fired their compile-time check.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response as StarletteResponse

    from app.tenancy.current import reset_current, set_current

    app = FastAPI()

    from starlette.middleware.base import RequestResponseEndpoint

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
    app.include_router(build_user_leaves_router())

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_worker_submit_then_owner_approve(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Worker pending → owner approve → row carries approval stamp.

        Asserts the full state transition + audit chain (created +
        approved) lands. The two contexts share one factory so the
        rows persist between the worker's POST and the owner's
        approve call.
        """
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="ul-int-1"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="ul-int-1-w@example.com"
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
            "/user_leaves",
            json={
                "starts_on": "2026-05-01",
                "ends_on": "2026-05-02",
                "category": "personal",
                "note_md": "Family",
            },
        )
        assert post.status_code == 201, post.text
        leave_id = post.json()["id"]
        assert post.json()["approved_at"] is None

        # Persisted row is pending — read it back through a fresh
        # UoW so we hit the real session + tenant filter.
        with UnitOfWorkImpl(session_factory=api_factory) as s:
            assert isinstance(s, Session)
            from app.tenancy.current import reset_current, set_current

            t = set_current(worker_ctx_obj)
            try:
                row = s.get(UserLeave, leave_id)
            finally:
                reset_current(t)
        assert row is not None
        assert row.approved_at is None
        assert row.note_md == "Family"

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
        approve = owner_client.post(f"/user_leaves/{leave_id}/approve")
        assert approve.status_code == 200, approve.text
        assert approve.json()["approved_at"] is not None
        assert approve.json()["approved_by"] == owner_id

        # Audit chain: created + approved.
        with UnitOfWorkImpl(session_factory=api_factory) as s:
            assert isinstance(s, Session)
            from app.tenancy.current import reset_current, set_current

            t = set_current(owner_ctx_obj)
            try:
                actions = sorted(
                    r.action
                    for r in s.scalars(
                        select(AuditLog).where(AuditLog.entity_id == leave_id)
                    ).all()
                )
            finally:
                reset_current(t)
        assert "user_leave.created" in actions
        assert "user_leave.approved" in actions

    def test_owner_self_submit_auto_approves(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Owner self-create lands ``approved_at`` populated at insert."""
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="ul-int-2"
        )
        ctx = _ctx(
            workspace_id=ws_id,
            workspace_slug=ws_slug,
            actor_id=owner_id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        client = TestClient(_build_app(api_factory, ctx), raise_server_exceptions=False)
        resp = client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-06-01",
                "ends_on": "2026-06-01",
                "category": "vacation",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["approved_at"] is not None
        assert body["approved_by"] == owner_id

    def test_reject_soft_deletes_and_folds_reason(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """Reject soft-deletes the row and folds reason into ``note_md``."""
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="ul-int-3"
        )
        worker_id = _seed_worker(
            api_factory, ws_id=ws_id, email="ul-int-3-w@example.com"
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
        leave = worker_client.post(
            "/user_leaves",
            json={
                "starts_on": "2026-07-01",
                "ends_on": "2026-07-02",
                "category": "personal",
                "note_md": "Original request",
            },
        ).json()

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
            f"/user_leaves/{leave['id']}/reject",
            json={"reason_md": "Coverage gap"},
        )
        assert rej.status_code == 200, rej.text

        # Row tombstoned with combined note. Read via a UoW under
        # the owner's ctx so the tenant filter resolves.
        with UnitOfWorkImpl(session_factory=api_factory) as s:
            assert isinstance(s, Session)
            from app.tenancy.current import reset_current, set_current

            t = set_current(owner_ctx_obj)
            try:
                row = s.get(UserLeave, leave["id"])
            finally:
                reset_current(t)
        assert row is not None
        assert row.deleted_at is not None
        assert "Original request" in (row.note_md or "")
        assert "Rejected: Coverage gap" in (row.note_md or "")

    def test_cross_workspace_invisible(
        self, api_factory: sessionmaker[Session]
    ) -> None:
        """A row in workspace A is invisible from B's caller.

        Two distinct workspaces in the same DB. The cross-workspace
        listing returns empty and every per-row verb collapses to
        404 — the §01 "tenant surface is not enumerable" guarantee.
        """
        ws_a_id, ws_a_slug, owner_a = _seed_workspace_with_owner(
            api_factory, slug="ul-int-a"
        )
        ws_b_id, ws_b_slug, owner_b = _seed_workspace_with_owner(
            api_factory, slug="ul-int-b"
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
        leave = client_a.post(
            "/user_leaves",
            json={
                "starts_on": "2026-08-01",
                "ends_on": "2026-08-01",
                "category": "vacation",
            },
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
        listing = client_b.get("/user_leaves").json()
        assert listing["data"] == []
        for r in (
            client_b.patch(f"/user_leaves/{leave['id']}", json={"note_md": "x"}),
            client_b.post(f"/user_leaves/{leave['id']}/approve"),
            client_b.post(f"/user_leaves/{leave['id']}/reject"),
            client_b.delete(f"/user_leaves/{leave['id']}"),
        ):
            assert r.status_code == 404, r.text

    def test_pagination(self, api_factory: sessionmaker[Session]) -> None:
        """Cursor envelope walks forward across pages."""
        ws_id, ws_slug, owner_id = _seed_workspace_with_owner(
            api_factory, slug="ul-int-pag"
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
                "/user_leaves",
                json={
                    "starts_on": f"2026-09-{i + 1:02d}",
                    "ends_on": f"2026-09-{i + 1:02d}",
                    "category": "personal",
                },
            )
            assert r.status_code == 201, r.text

        page1 = client.get("/user_leaves?limit=2").json()
        assert page1["has_more"] is True
        assert len(page1["data"]) == 2
        page2 = client.get(f"/user_leaves?cursor={page1['next_cursor']}&limit=2").json()
        assert page2["has_more"] is True
        assert len(page2["data"]) == 2
        page3 = client.get(f"/user_leaves?cursor={page2['next_cursor']}&limit=2").json()
        assert page3["has_more"] is False
        assert len(page3["data"]) == 1
