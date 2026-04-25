"""Shared fixtures for the identity HTTP router suite (cd-dcfw).

Mirrors :mod:`tests.unit.api.v1.admin.test_signups` — in-memory SQLite
engine with every model loaded, workspace + owner/worker personas
seeded via :mod:`tests.factories.identity`, a :class:`TestClient`
with the tenancy + db-session dependencies overridden to pin the ctx
+ UoW.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.api.deps import current_workspace_context, db_session
from app.tenancy import WorkspaceContext
from app.tenancy.context import ActorGrantRole
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


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
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def seed_worker_user(
    session: Session, *, workspace_id: str, email: str, display_name: str
) -> str:
    """Seed a user with a ``worker`` role grant (no owners group)."""
    user = bootstrap_user(session, email=email, display_name=display_name)
    session.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user.id,
            grant_role="worker",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()
    return user.id


def ctx_for(
    *,
    workspace_id: str,
    workspace_slug: str,
    actor_id: str,
    grant_role: ActorGrantRole = "manager",
    actor_was_owner_member: bool = True,
) -> WorkspaceContext:
    return build_workspace_context(
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=actor_was_owner_member,
    )


def build_client(
    router_mounts: list[tuple[str, APIRouter]],
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
) -> TestClient:
    """Build a :class:`TestClient` with pinned ctx + UoW overrides.

    ``router_mounts`` is a list of ``(prefix, router)`` pairs; empty
    prefix means "mount at root". Each router is mounted verbatim so
    the handler-relative paths line up with the production app's
    shape. A manual ``app.include_router(router, prefix=prefix)``
    would be fine; the list form just keeps the fixture terse when a
    test needs more than one router in the same TestClient.
    """
    app = FastAPI()
    for prefix, router in router_mounts:
        app.include_router(router, prefix=prefix)

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session] = _override_db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _redirect_default_uow_to_test_engine(
    factory: sessionmaker[Session],
) -> Iterator[None]:
    """Redirect ``make_uow`` to the per-test engine for handler use.

    Routers that open their own ``with make_uow() as session:`` block
    (e.g. the cd-9slq commit-before-send shape on
    ``POST /users/{id}/magic_link``) read
    :data:`app.adapters.db.session._default_sessionmaker_` to bind a
    session. Without this fixture they would hit whatever DB the
    default factory was last built for instead of the per-test
    in-memory engine. We redirect autouse-style and restore on
    teardown so a sibling test in the same xdist worker (e.g.
    ``tests/unit/auth/test_session.py``) sees the original default
    when the fixture exits.

    Mirrors :func:`tests.unit.auth.test_recovery.redirect_default_engine`
    and :mod:`tests.tenant.conftest`.
    """
    import app.adapters.db.session as _session_mod

    bound_engine = factory.kw.get("bind")
    assert isinstance(bound_engine, Engine), (
        "identity conftest factory must be sessionmaker-bound to an Engine; "
        f"got {bound_engine!r}"
    )
    original_engine = _session_mod._default_engine
    original_factory = _session_mod._default_sessionmaker_
    _session_mod._default_engine = bound_engine
    _session_mod._default_sessionmaker_ = factory
    try:
        yield
    finally:
        _session_mod._default_engine = original_engine
        _session_mod._default_sessionmaker_ = original_factory


@pytest.fixture
def owner_ctx(
    factory: sessionmaker[Session],
) -> tuple[WorkspaceContext, sessionmaker[Session], str]:
    """Workspace + owner user seeded via :func:`bootstrap_workspace`.

    Returns ``(ctx, factory, workspace_id)`` — the caller builds the
    TestClient against whichever router they're testing.
    """
    with factory() as s:
        owner_user = bootstrap_user(s, email="owner@example.com", display_name="Owner")
        ws = bootstrap_workspace(
            s,
            slug="ws-identity",
            name="Identity WS",
            owner_user_id=owner_user.id,
        )
        s.commit()
        owner_id, ws_id, ws_slug = owner_user.id, ws.id, ws.slug
    ctx = ctx_for(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=owner_id,
        grant_role="manager",
        actor_was_owner_member=True,
    )
    return ctx, factory, ws_id


@pytest.fixture
def worker_ctx(
    factory: sessionmaker[Session],
) -> tuple[WorkspaceContext, sessionmaker[Session], str, str]:
    """Workspace + worker user (not in owners group).

    Returns ``(ctx, factory, workspace_id, worker_user_id)``.
    """
    with factory() as s:
        owner_user = bootstrap_user(s, email="owner2@example.com", display_name="Owner")
        ws = bootstrap_workspace(
            s,
            slug="ws-worker",
            name="Worker WS",
            owner_user_id=owner_user.id,
        )
        worker_id = seed_worker_user(
            s, workspace_id=ws.id, email="worker@example.com", display_name="Worker"
        )
        s.commit()
        ws_id, ws_slug = ws.id, ws.slug
    ctx = ctx_for(
        workspace_id=ws_id,
        workspace_slug=ws_slug,
        actor_id=worker_id,
        grant_role="worker",
        actor_was_owner_member=False,
    )
    return ctx, factory, ws_id, worker_id
