"""Unit tests for chat-message send/list."""

from __future__ import annotations

import importlib
import io
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Literal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import ChatMessage
from app.adapters.db.messaging.repositories import (
    SqlAlchemyChatChannelRepository,
    SqlAlchemyChatMessageRepository,
)
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.domain.messaging.channels import (
    ChatChannelCreate,
    ChatChannelNotFound,
    ChatChannelService,
)
from app.domain.messaging.messages import (
    ChatMessageAttachmentMissing,
    ChatMessageService,
    sanitize_markdown,
)
from app.events.bus import EventBus
from app.events.types import ChatMessageSent
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
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


def _seed(factory: sessionmaker[Session]) -> tuple[str, str, str]:
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


def test_sanitizer_strips_raw_html_and_preserves_markdown_autolinks() -> None:
    body = "**Hi** <script>alert('x')</script><b>there</b> <https://example.com>"

    assert sanitize_markdown(body) == "**Hi** there <https://example.com>"


def test_sanitizer_does_not_replace_user_text_that_looks_like_token() -> None:
    body = "CREWDAY_AUTOLINK_0 <https://example.com>"

    assert sanitize_markdown(body) == "CREWDAY_AUTOLINK_0 <https://example.com>"


def test_send_persists_author_attachment_audit_and_event_once(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id, _worker_id = _seed(factory)
    ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager")
    storage = InMemoryStorage()
    blob_hash = "a" * 64
    _put_blob(storage, blob_hash)
    event_bus = EventBus()
    events = _record(event_bus)

    with factory() as s:
        channel_repo = SqlAlchemyChatChannelRepository(s)
        channel = ChatChannelService(ctx, clock=FrozenClock(_PINNED)).create(
            channel_repo,
            ChatChannelCreate(kind="staff", title="Staff"),
        )
        view = ChatMessageService(
            ctx,
            storage=storage,
            clock=FrozenClock(_PINNED),
            event_bus=event_bus,
        ).send(
            SqlAlchemyChatMessageRepository(s),
            channel_repo,
            channel.id,
            "Hello <script>bad()</script>**team**",
            attachments=[blob_hash],
        )
        s.commit()

    with factory() as s:
        row = s.get(ChatMessage, view.id)
        assert row is not None
        assert row.author_user_id == manager_id
        assert row.author_label == "manager"
        assert row.body_md == "Hello **team**"
        assert row.attachments_json == [{"blob_hash": blob_hash}]
        audit_actions = s.scalars(select(AuditLog.action)).all()
        assert "messaging.message.sent" in audit_actions
    assert len(events) == 1
    assert events[0].message_id == view.id
    assert events[0].channel_id == channel.id
    assert events[0].author_user_id == manager_id
    assert events[0].channel_kind == "staff"


def test_missing_attachment_rejects_before_persistence_event_or_audit(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id, _worker_id = _seed(factory)
    ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager")
    event_bus = EventBus()
    events = _record(event_bus)

    with factory() as s:
        channel_repo = SqlAlchemyChatChannelRepository(s)
        channel = ChatChannelService(ctx, clock=FrozenClock(_PINNED)).create(
            channel_repo,
            ChatChannelCreate(kind="staff", title="Staff"),
        )
        with pytest.raises(ChatMessageAttachmentMissing):
            ChatMessageService(
                ctx,
                storage=InMemoryStorage(),
                clock=FrozenClock(_PINNED),
                event_bus=event_bus,
            ).send(
                SqlAlchemyChatMessageRepository(s),
                channel_repo,
                channel.id,
                "Hello",
                attachments=["b" * 64],
            )
        assert s.scalars(select(ChatMessage)).all() == []
        assert [
            action
            for action in s.scalars(select(AuditLog.action)).all()
            if action == "messaging.message.sent"
        ] == []
    assert events == []


def test_invalid_attachment_hash_is_rejected_before_storage_call(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id, _worker_id = _seed(factory)
    ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager")

    with factory() as s:
        channel_repo = SqlAlchemyChatChannelRepository(s)
        channel = ChatChannelService(ctx, clock=FrozenClock(_PINNED)).create(
            channel_repo,
            ChatChannelCreate(kind="staff", title="Staff"),
        )
        with pytest.raises(ValueError, match="64-character lowercase sha256"):
            ChatMessageService(
                ctx,
                storage=InMemoryStorage(),
                clock=FrozenClock(_PINNED),
            ).send(
                SqlAlchemyChatMessageRepository(s),
                channel_repo,
                channel.id,
                "Hello",
                attachments=["missing"],
            )


def test_list_orders_same_millisecond_messages_by_id_tiebreak(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id, _worker_id = _seed(factory)
    ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager")
    clock = FrozenClock(_PINNED)

    with factory() as s:
        channel_repo = SqlAlchemyChatChannelRepository(s)
        message_repo = SqlAlchemyChatMessageRepository(s)
        channel = ChatChannelService(ctx, clock=clock).create(
            channel_repo,
            ChatChannelCreate(kind="staff", title="Staff"),
        )
        service = ChatMessageService(ctx, storage=InMemoryStorage(), clock=clock)
        first = service.send(message_repo, channel_repo, channel.id, "first")
        second = service.send(message_repo, channel_repo, channel.id, "second")
        listed = service.list(message_repo, channel_repo, channel.id)

    assert first.created_at == second.created_at == _PINNED
    assert [message.id for message in listed] == sorted(
        [first.id, second.id],
        reverse=True,
    )
    assert [message.body_md for message in listed] == ["second", "first"]


def test_worker_cannot_send_to_hidden_manager_channel(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id, worker_id = _seed(factory)
    manager_ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager")
    worker_ctx = _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker")

    with factory() as s:
        channel_repo = SqlAlchemyChatChannelRepository(s)
        channel = ChatChannelService(manager_ctx, clock=FrozenClock(_PINNED)).create(
            channel_repo,
            ChatChannelCreate(kind="manager", title="Managers"),
        )
        with pytest.raises(ChatChannelNotFound):
            ChatMessageService(
                worker_ctx,
                storage=InMemoryStorage(),
                clock=FrozenClock(_PINNED),
            ).send(
                SqlAlchemyChatMessageRepository(s),
                channel_repo,
                channel.id,
                "nope",
            )


def test_hidden_channel_rejects_before_attachment_storage_check(
    factory: sessionmaker[Session],
) -> None:
    workspace_id, manager_id, worker_id = _seed(factory)
    manager_ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager")
    worker_ctx = _ctx(workspace_id=workspace_id, actor_id=worker_id, role="worker")

    with factory() as s:
        channel_repo = SqlAlchemyChatChannelRepository(s)
        channel = ChatChannelService(manager_ctx, clock=FrozenClock(_PINNED)).create(
            channel_repo,
            ChatChannelCreate(kind="manager", title="Managers"),
        )
        with pytest.raises(ChatChannelNotFound):
            ChatMessageService(worker_ctx, clock=FrozenClock(_PINNED)).send(
                SqlAlchemyChatMessageRepository(s),
                channel_repo,
                channel.id,
                "nope",
                attachments=["missing"],
            )


def test_chat_message_sent_declares_possible_in_app_recipient_roles() -> None:
    assert ChatMessageSent.allowed_roles == ("manager", "worker")
