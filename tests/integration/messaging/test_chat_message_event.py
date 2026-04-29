"""Integration coverage for chat-message typed events."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.messaging.models import ChatMessage
from app.adapters.db.messaging.repositories import (
    SqlAlchemyChatChannelRepository,
    SqlAlchemyChatMessageRepository,
)
from app.domain.messaging.channels import ChatChannelCreate, ChatChannelService
from app.domain.messaging.messages import ChatMessageService
from app.events.bus import EventBus
from app.events.registry import get_event_type
from app.events.types import ChatMessageSent
from app.util.clock import FrozenClock
from tests._fakes.storage import InMemoryStorage
from tests.api.messaging.test_chat_message_api import (
    _PINNED,
    _bootstrap_user,
    _bootstrap_workspace,
    _ctx,
    _record,
)


def test_send_commits_message_audit_and_one_typed_event(
    db_session: Session,
) -> None:
    workspace_id = _bootstrap_workspace(db_session)
    manager_id = _bootstrap_user(
        db_session,
        workspace_id=workspace_id,
        email="manager@example.com",
        role="manager",
    )
    db_session.commit()
    ctx = _ctx(workspace_id=workspace_id, actor_id=manager_id, role="manager")
    event_bus = EventBus()
    events = _record(event_bus)
    channel_repo = SqlAlchemyChatChannelRepository(db_session)
    message_repo = SqlAlchemyChatMessageRepository(db_session)
    channel = ChatChannelService(ctx, clock=FrozenClock(_PINNED)).create(
        channel_repo,
        ChatChannelCreate(kind="staff", title="Staff"),
    )

    view = ChatMessageService(
        ctx,
        storage=InMemoryStorage(),
        clock=FrozenClock(_PINNED),
        event_bus=event_bus,
    ).send(
        message_repo,
        channel_repo,
        channel.id,
        "hello",
    )
    db_session.commit()

    assert get_event_type("chat.message.sent") is ChatMessageSent
    assert len(events) == 1
    event_payload = events[0].model_dump(
        include={"channel_id", "message_id", "channel_kind"}
    )
    assert event_payload == {
        "channel_id": channel.id,
        "message_id": view.id,
        "channel_kind": "staff",
    }
    assert db_session.get(ChatMessage, view.id) is not None
    assert "messaging.message.sent" in db_session.scalars(select(AuditLog.action)).all()
