"""Integration test — :func:`Permission` wired into a toy FastAPI route.

Confirms the full stack:

* :class:`TestClient` hits a route gated by ``Permission("scope.view",
  scope_kind="workspace")``.
* Under-permissioned caller → HTTP 403 with the spec's error body
  shape.
* Manager-grant caller → HTTP 200.
* Owner caller → HTTP 200.

The :class:`WorkspaceContext` + DB session are overridden exactly like
the existing passkey-router tests (cd-8m4) so the route is exercised
against real rows, not a fake.

See ``docs/specs/02-domain-model.md`` §"Permission resolution" and
``docs/specs/05-employees-and-roles.md`` §"Action catalog".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import Workspace
from app.api.deps import current_workspace_context, db_session
from app.authz import Permission
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def authz_engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine — the router dep round-trips a
    live session, not a fake.

    Named to avoid the session-scoped ``engine`` fixture provided by
    ``tests/integration/conftest.py`` (that one is bound to the
    alembic-upgraded test DB and migrated once per session; we don't
    need the migration layer here because we're exercising the ORM
    surface only).
    """
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(authz_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=authz_engine, expire_on_commit=False, class_=Session)


def _seed(session: Session, *, grant_role: str | None) -> tuple[str, str]:
    """Seed a workspace + user + optional role_grant.

    Returns ``(workspace_id, user_id)`` so the test can mint a ctx.
    ``grant_role=None`` creates a stranger with no grants and no
    owners-group membership.
    """
    workspace_id = new_ulid()
    user_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug="router-authz",
            name="Router Authz",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    email = f"{grant_role or 'stranger'}@example.com"
    session.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=(grant_role or "Stranger").capitalize(),
            created_at=_PINNED,
        )
    )
    session.flush()
    if grant_role is not None:
        session.add(
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
        session.flush()
    return workspace_id, user_id


def _seed_owner(session: Session) -> tuple[str, str]:
    """Seed workspace + user + owners-group membership + manager grant.

    Mirrors :func:`seed_owners_system_group` without emitting an audit
    row (the audit helpers need a fuller fixture; this test cares only
    about the authz decision).
    """
    workspace_id, user_id = _seed(session, grant_role="manager")
    owners_group = PermissionGroup(
        id=new_ulid(),
        workspace_id=workspace_id,
        slug="owners",
        name="Owners",
        system=True,
        capabilities_json={"all": True},
        created_at=_PINNED,
    )
    session.add(owners_group)
    session.flush()
    session.add(
        PermissionGroupMember(
            group_id=owners_group.id,
            user_id=user_id,
            workspace_id=workspace_id,
            added_at=_PINNED,
            added_by_user_id=None,
        )
    )
    session.flush()
    return workspace_id, user_id


def _ctx(
    *,
    workspace_id: str,
    user_id: str,
    grant_role: ActorGrantRole,
    was_owner: bool,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="router-authz",
        actor_id=user_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=was_owner,
        audit_correlation_id=new_ulid(),
    )


def _build_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
) -> FastAPI:
    """Minimal FastAPI app with one route gated by ``Permission``.

    Matches the pattern used by ``tests/unit/auth/test_passkey_router.py``:
    override the two deps the gate consumes (context + session) and
    attach the dependency to a toy ``GET /test/gate`` handler.
    """
    app = FastAPI()

    @app.get("/test/gate")
    def _gated(
        _: Annotated[
            None,
            Depends(Permission("scope.view", scope_kind="workspace")),
        ],
    ) -> dict[str, str]:
        return {"status": "allowed"}

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


class TestGateAllows:
    """Callers matched by ``scope.view``'s default_allow reach the handler."""

    def test_manager_gets_200(self, factory: sessionmaker[Session]) -> None:
        with factory() as s:
            workspace_id, user_id = _seed(s, grant_role="manager")
            s.commit()
        ctx = _ctx(
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role="manager",
            was_owner=False,
        )
        client = TestClient(_build_app(factory, ctx))
        resp = client.get("/test/gate")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"status": "allowed"}

    def test_owner_gets_200(self, factory: sessionmaker[Session]) -> None:
        """Owners pass every default that includes ``owners`` — and
        ``scope.view`` lists them first.
        """
        with factory() as s:
            workspace_id, user_id = _seed_owner(s)
            s.commit()
        ctx = _ctx(
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role="manager",
            was_owner=True,
        )
        client = TestClient(_build_app(factory, ctx))
        resp = client.get("/test/gate")
        assert resp.status_code == 200, resp.text

    def test_worker_gets_200(self, factory: sessionmaker[Session]) -> None:
        """``scope.view`` defaults include ``all_workers``."""
        with factory() as s:
            workspace_id, user_id = _seed(s, grant_role="worker")
            s.commit()
        ctx = _ctx(
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role="worker",
            was_owner=False,
        )
        client = TestClient(_build_app(factory, ctx))
        resp = client.get("/test/gate")
        assert resp.status_code == 200, resp.text


class TestGateDenies:
    """A caller with no grants and no owners membership hits the 403 path."""

    def test_stranger_gets_403(self, factory: sessionmaker[Session]) -> None:
        with factory() as s:
            workspace_id, user_id = _seed(s, grant_role=None)
            s.commit()
        ctx = _ctx(
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role="guest",
            was_owner=False,
        )
        client = TestClient(_build_app(factory, ctx))
        resp = client.get("/test/gate")
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["detail"]["error"] == "permission_denied"
        assert body["detail"]["action_key"] == "scope.view"


class TestGateMisuse:
    """Caller-bug errors surface as 422 so the developer notices early."""

    def test_unknown_action_returns_422(self, factory: sessionmaker[Session]) -> None:
        """Registering a gate with an action key that isn't in the
        catalog is a caller bug; the dep must translate to 422 rather
        than leak a 500.
        """
        with factory() as s:
            workspace_id, user_id = _seed(s, grant_role="manager")
            s.commit()
        ctx = _ctx(
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role="manager",
            was_owner=False,
        )
        app = FastAPI()

        @app.get("/test/bad-gate")
        def _bad_gate(
            _: Annotated[
                None,
                Depends(Permission("not.a.real.action", scope_kind="workspace")),
            ],
        ) -> dict[str, str]:
            return {"status": "unreachable"}

        def _override_ctx() -> WorkspaceContext:
            return ctx

        def _override_db() -> Iterator[Session]:
            uow = UnitOfWorkImpl(session_factory=factory)
            with uow as s:
                assert isinstance(s, Session)
                yield s

        app.dependency_overrides[current_workspace_context] = _override_ctx
        app.dependency_overrides[db_session] = _override_db

        resp = TestClient(app).get("/test/bad-gate")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "unknown_action_key"
