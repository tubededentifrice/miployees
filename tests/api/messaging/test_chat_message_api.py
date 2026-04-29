"""HTTP-boundary tests for chat messages."""

from __future__ import annotations

import importlib
import io
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import ChatMessage
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import current_workspace_context, db_session
from app.api.v1.messaging import build_messaging_router
from app.events.bus import EventBus
from app.events.types import ChatMessageSent
from app.tenancy.context import WorkspaceContext
from app.util.ulid import new_ulid
from tests._fakes.storage import InMemoryStorage

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


def _bootstrap_workspace(s: Session) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Messaging Test",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(
    s: Session,
    *,
    workspace_id: str,
    email: str,
    role: Literal["manager", "worker", "client", "guest"],
) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=email.split("@", maxsplit=1)[0],
            created_at=_PINNED,
        )
    )
    s.flush()
    s.add(
        UserWorkspace(
            user_id=user_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=role,
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()
    return user_id


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    role: Literal["manager", "worker", "client", "guest"],
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="messaging",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=role,
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _build_app(
    factory: sessionmaker[Session],
    ctx: WorkspaceContext,
    storage: InMemoryStorage | None,
    event_bus: EventBus,
) -> FastAPI:
    app = FastAPI()
    app.include_router(build_messaging_router(event_bus=event_bus))
    if storage is not None:
        app.state.storage = storage

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
def seeded(factory: sessionmaker[Session]) -> tuple[str, str, str]:
    with factory() as s:
        workspace_id = _bootstrap_workspace(s)
        manager_id = _bootstrap_user(
            s,
            workspace_id=workspace_id,
            email="manager@example.com",
            role="manager",
        )
        worker_id = _bootstrap_user(
            s,
            workspace_id=workspace_id,
            email="worker@example.com",
            role="worker",
        )
        s.commit()
    return workspace_id, manager_id, worker_id


def _put_blob(storage: InMemoryStorage, blob_hash: str) -> None:
    storage.put(blob_hash, io.BytesIO(b"payload"), content_type="image/png")


def _record(bus: EventBus) -> list[ChatMessageSent]:
    events: list[ChatMessageSent] = []

    @bus.subscribe(ChatMessageSent)
    def _append(event: ChatMessageSent) -> None:
        events.append(event)

    return events


def test_send_and_list_message(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    storage = InMemoryStorage()
    blob_hash = "a" * 64
    _put_blob(storage, blob_hash)
    event_bus = EventBus()
    events = _record(event_bus)
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
            storage,
            event_bus,
        ),
        raise_server_exceptions=False,
    )
    channel = client.post(
        "/chat/channels",
        json={"kind": "staff", "title": "Staff"},
    ).json()

    resp = client.post(
        f"/chat/channels/{channel['id']}/messages",
        json={
            "body_md": "Hello <script>bad()</script>**team**",
            "attachments": [blob_hash],
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["body_md"] == "Hello **team**"
    assert body["attachments"] == [{"blob_hash": blob_hash}]
    assert len(events) == 1
    assert events[0].message_id == body["id"]

    listed = client.get(f"/chat/channels/{channel['id']}/messages")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [body["id"]]


def test_missing_attachment_is_404_and_writes_nothing(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    event_bus = EventBus()
    events = _record(event_bus)
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
            InMemoryStorage(),
            event_bus,
        ),
        raise_server_exceptions=False,
    )
    channel = client.post(
        "/chat/channels",
        json={"kind": "staff", "title": "Staff"},
    ).json()

    resp = client.post(
        f"/chat/channels/{channel['id']}/messages",
        json={"body_md": "Hello", "attachments": ["b" * 64]},
    )

    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "attachment_not_found"
    assert events == []
    with factory() as s:
        assert s.scalars(select(ChatMessage)).all() == []
        assert [
            action
            for action in s.scalars(select(AuditLog.action)).all()
            if action == "messaging.message.sent"
        ] == []


def test_text_only_message_does_not_require_storage(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
            None,
            EventBus(),
        ),
        raise_server_exceptions=False,
    )
    channel = client.post(
        "/chat/channels",
        json={"kind": "staff", "title": "Staff"},
    ).json()

    resp = client.post(
        f"/chat/channels/{channel['id']}/messages",
        json={"body_md": "Hello"},
    )

    assert resp.status_code == 201
    assert resp.json()["body_md"] == "Hello"


def test_invalid_attachment_hash_is_422_before_storage_lookup(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
            None,
            EventBus(),
        ),
        raise_server_exceptions=False,
    )
    channel = client.post(
        "/chat/channels",
        json={"kind": "staff", "title": "Staff"},
    ).json()

    resp = client.post(
        f"/chat/channels/{channel['id']}/messages",
        json={"body_md": "Hello", "attachments": ["missing"]},
    )

    assert resp.status_code == 422


def test_archived_channel_rejects_send_and_list(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
            InMemoryStorage(),
            EventBus(),
        ),
        raise_server_exceptions=False,
    )
    channel = client.post(
        "/chat/channels",
        json={"kind": "staff", "title": "Staff"},
    ).json()
    archived = client.patch(
        f"/chat/channels/{channel['id']}",
        json={"archived": True},
    )
    assert archived.status_code == 200

    send_resp = client.post(
        f"/chat/channels/{channel['id']}/messages",
        json={"body_md": "Hello"},
    )
    list_resp = client.get(f"/chat/channels/{channel['id']}/messages")

    assert send_resp.status_code == 404
    assert list_resp.status_code == 404


def test_before_cursor_paginates_by_created_at_and_id(
    factory: sessionmaker[Session],
    seeded: tuple[str, str, str],
) -> None:
    workspace_id, manager_id, _worker_id = seeded
    client = TestClient(
        _build_app(
            factory,
            _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager"),
            InMemoryStorage(),
            EventBus(),
        ),
        raise_server_exceptions=False,
    )
    channel = client.post(
        "/chat/channels",
        json={"kind": "staff", "title": "Staff"},
    ).json()
    first = client.post(
        f"/chat/channels/{channel['id']}/messages",
        json={"body_md": "first"},
    ).json()
    second = client.post(
        f"/chat/channels/{channel['id']}/messages",
        json={"body_md": "second"},
    ).json()

    page1 = client.get(f"/chat/channels/{channel['id']}/messages", params={"limit": 1})
    assert page1.status_code == 200
    assert [item["id"] for item in page1.json()["data"]] == [second["id"]]
    assert page1.json()["has_more"] is True

    page2 = client.get(
        f"/chat/channels/{channel['id']}/messages",
        params={"limit": 1, "before": page1.json()["next_cursor"]},
    )
    assert page2.status_code == 200
    assert [item["id"] for item in page2.json()["data"]] == [first["id"]]
    assert page2.json()["has_more"] is False
