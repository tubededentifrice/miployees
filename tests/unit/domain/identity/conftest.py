"""Shared fixtures for the identity-domain unit suite (cd-dcfw).

Mirrors :mod:`tests.unit.services.test_service_employees` — same
in-memory SQLite engine, same ``Base.metadata.create_all`` bootstrap,
same :class:`FrozenClock`. Each test gets a fresh engine so the
tenancy-filter state cannot leak between cases.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import (
    UserWorkspace,
    WorkEngagement,
    WorkRole,
    Workspace,
)
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` module.

    Without this walk ``Base.metadata`` only knows the tables already
    imported by the test module; FKs resolving to an unloaded table
    would fail ``create_all``.
    """
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
    """Fresh in-memory SQLite engine per test."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Per-test session (tenant filter not installed at unit scope)."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Seeding helpers — kept terse so tests can focus on the behaviour
# ---------------------------------------------------------------------------


def make_workspace(session: Session, *, slug: str) -> Workspace:
    ws = Workspace(
        id=new_ulid(),
        slug=slug,
        name=f"Workspace {slug}",
        plan="free",
        quota_json={},
        settings_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


def make_user(session: Session, *, email: str, display_name: str) -> User:
    user = User(
        id=new_ulid(),
        email=email,
        email_lower=canonicalise_email(email),
        display_name=display_name,
        locale=None,
        timezone=None,
        created_at=_PINNED,
    )
    session.add(user)
    session.flush()
    return user


def attach_user(session: Session, *, user_id: str, workspace_id: str) -> None:
    """Seed a ``user_workspace`` row — "is this user in this workspace?"."""
    session.add(
        UserWorkspace(
            user_id=user_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )
    session.flush()


def make_work_role(session: Session, *, ws: Workspace, key: str) -> WorkRole:
    row = WorkRole(
        id=new_ulid(),
        workspace_id=ws.id,
        key=key,
        name=key.title(),
        description_md="",
        default_settings_json={},
        icon_name="",
        created_at=_PINNED,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    return row


def make_engagement(
    session: Session,
    *,
    user_id: str,
    workspace_id: str,
    kind: str = "payroll",
    supplier_org_id: str | None = None,
    archived_on: date | None = None,
) -> WorkEngagement:
    row = WorkEngagement(
        id=new_ulid(),
        user_id=user_id,
        workspace_id=workspace_id,
        engagement_kind=kind,
        supplier_org_id=supplier_org_id,
        pay_destination_id=None,
        reimbursement_destination_id=None,
        started_on=_PINNED.date(),
        archived_on=archived_on,
        notes_md="",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def ctx_for(*, workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a manager/owner ctx — enough for unit-scope service calls."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )
