"""Unit tests for :class:`SqlAlchemyCapabilityChecker` (cd-v3jp).

The expenses-context capability checker wraps :func:`app.authz.require`
with a fixed ``(session, ctx)`` pair so the domain modules don't
transitively pull :mod:`app.adapters.db.authz.models` via
:mod:`app.authz.membership` / :mod:`app.authz.owners`. This file pins
the three behaviours the seam contract guarantees:

* a granted capability returns ``None``;
* a denied capability raises :class:`SeamPermissionDenied` (NOT the
  underlying :class:`app.authz.PermissionDenied`);
* a misconfigured action key (unknown / wrong scope) raises
  :class:`RuntimeError` so the router surfaces 500, not 403.

Mirrors the cd-r5j2 ``test_user_availability_overrides`` /
``test_user_leave_seam`` adapter tests in shape.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.expenses.repositories import SqlAlchemyCapabilityChecker
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import Workspace
from app.domain.expenses.ports import SeamPermissionDenied
from app.tenancy.context import WorkspaceContext

_PINNED = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_WORKSPACE_ID = "01HWA00000000000000000WS01"
_USER_ID = "01HWA00000000000000000USR1"


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
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(
        dbapi_connection: object, _connection_record: object
    ) -> None:
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = factory()
    try:
        yield s
    finally:
        s.close()


def _bootstrap(session: Session, *, grant_role: str) -> WorkspaceContext:
    """Seed a workspace + user + role grant; return a matching context."""
    session.add(
        Workspace(
            id=_WORKSPACE_ID,
            slug="cap-ws",
            name="Cap WS",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    session.add(
        User(
            id=_USER_ID,
            email="cap@example.com",
            email_lower="cap@example.com",
            display_name="Cap",
            locale=None,
            timezone=None,
            created_at=_PINNED,
        )
    )
    session.flush()
    session.add(
        RoleGrant(
            id="01HWA0GRANT00000000000000",
            workspace_id=_WORKSPACE_ID,
            user_id=_USER_ID,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.commit()
    return WorkspaceContext(
        workspace_id=_WORKSPACE_ID,
        workspace_slug="cap-ws",
        actor_id=_USER_ID,
        actor_kind="user",
        actor_grant_role=grant_role,  # type: ignore[arg-type]
        actor_was_owner_member=False,
        audit_correlation_id="01HWA0CORR00000000000000",
    )


class TestRequire:
    def test_manager_holds_expenses_approve(self, session: Session) -> None:
        ctx = _bootstrap(session, grant_role="manager")
        checker = SqlAlchemyCapabilityChecker(session, ctx)
        # Should not raise — managers hold expenses.approve by default
        # (see action catalog).
        checker.require("expenses.approve")

    def test_worker_lacks_expenses_approve_raises_seam_denied(
        self, session: Session
    ) -> None:
        ctx = _bootstrap(session, grant_role="worker")
        checker = SqlAlchemyCapabilityChecker(session, ctx)
        with pytest.raises(SeamPermissionDenied):
            checker.require("expenses.approve")

    def test_unknown_action_key_raises_runtime_error(self, session: Session) -> None:
        ctx = _bootstrap(session, grant_role="manager")
        checker = SqlAlchemyCapabilityChecker(session, ctx)
        with pytest.raises(RuntimeError, match="catalog misconfigured"):
            checker.require("expenses.bogus_action_key_does_not_exist")
