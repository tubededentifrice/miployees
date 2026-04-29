"""Integration tests for workspace-scoped messaging API routes."""

from __future__ import annotations

import importlib
import pkgutil
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
from app.adapters.db.messaging.models import Notification, PushToken
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1.messaging import build_messaging_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
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


def _seed_user(s: Session) -> tuple[str, str, str]:
    workspace_id = new_ulid()
    user_id = new_ulid()
    other_user_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Messaging Routes",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    for user_id_value, email in (
        (user_id, "worker@example.com"),
        (other_user_id, "other@example.com"),
    ):
        s.add(
            User(
                id=user_id_value,
                email=email,
                email_lower=canonicalise_email(email),
                display_name=email.split("@", maxsplit=1)[0],
                created_at=_PINNED,
            )
        )
    s.flush()
    for user_id_value in (user_id, other_user_id):
        s.add(
            UserWorkspace(
                user_id=user_id_value,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
    s.flush()
    return workspace_id, user_id, other_user_id


def _ctx(workspace_id: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="messaging",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
) -> FastAPI:
    app = FastAPI()
    app.include_router(build_messaging_router())

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


def _add_notification(
    s: Session,
    *,
    workspace_id: str,
    recipient_user_id: str,
    subject: str,
    read_at: datetime | None = None,
) -> str:
    notification_id = new_ulid()
    s.add(
        Notification(
            id=notification_id,
            workspace_id=workspace_id,
            recipient_user_id=recipient_user_id,
            kind="task_assigned",
            subject=subject,
            body_md="Please review",
            read_at=read_at,
            created_at=_PINNED,
            payload_json={"task_id": "task-1"},
        )
    )
    s.flush()
    return notification_id


def _add_push_token(
    s: Session,
    *,
    workspace_id: str,
    user_id: str,
) -> str:
    token_id = new_ulid()
    s.add(
        PushToken(
            id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint="https://fcm.googleapis.com/fcm/send/token",
            p256dh="p256dh",
            auth="auth",
            user_agent="pytest",
            created_at=_PINNED,
            last_used_at=None,
        )
    )
    s.flush()
    return token_id


def test_notifications_list_get_patch_and_bulk_mark_read(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id, user_id, other_user_id = _seed_user(s)
        mine = _add_notification(
            s,
            workspace_id=workspace_id,
            recipient_user_id=user_id,
            subject="Mine",
        )
        other = _add_notification(
            s,
            workspace_id=workspace_id,
            recipient_user_id=other_user_id,
            subject="Other",
        )
        s.commit()
    client = TestClient(
        _build_app(factory, _ctx(workspace_id, user_id)),
        raise_server_exceptions=False,
    )

    listed = client.get("/notifications")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [mine]
    assert listed.json()["total_estimate"] == 1

    hidden = client.get(f"/notifications/{other}")
    assert hidden.status_code == 404

    patched = client.patch(f"/notifications/{mine}", json={"read": True})
    assert patched.status_code == 200
    assert patched.json()["read_at"] is not None

    bulk = client.post("/notifications:mark-read", json={"ids": [mine, other]})
    assert bulk.status_code == 200
    assert [row["id"] for row in bulk.json()["data"]] == [mine]
    with factory() as s:
        row = s.get(Notification, mine)
        assert row is not None
        assert row.read_at is not None
        assert (
            "messaging.notification.read_state_changed"
            in s.scalars(select(AuditLog.action)).all()
        )


def test_push_tokens_list_delete_and_native_post_unavailable(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id, user_id, other_user_id = _seed_user(s)
        mine = _add_push_token(s, workspace_id=workspace_id, user_id=user_id)
        other = _add_push_token(s, workspace_id=workspace_id, user_id=other_user_id)
        s.commit()
    client = TestClient(
        _build_app(factory, _ctx(workspace_id, user_id)),
        raise_server_exceptions=False,
    )

    listed = client.get("/notifications/push/tokens")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [mine]

    unavailable = client.post(
        "/notifications/push/tokens",
        json={"platform": "ios", "token": "native-token"},
    )
    assert unavailable.status_code == 501
    assert unavailable.json()["detail"]["error"] == "push_unavailable"

    assert client.delete(f"/notifications/push/tokens/{other}").status_code == 204
    assert client.delete(f"/notifications/push/tokens/{mine}").status_code == 204
    with factory() as s:
        assert s.get(PushToken, mine) is None
        assert s.get(PushToken, other) is not None
        audit = s.scalars(
            select(AuditLog).where(AuditLog.action == "messaging.push_token.deleted")
        ).one()
        assert audit.diff == {"endpoint_host": "fcm.googleapis.com"}
