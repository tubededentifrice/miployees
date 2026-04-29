"""Unit tests for inbound chat gateway persistence."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatGatewayBinding,
    ChatMessage,
)
from app.adapters.db.messaging.repositories import SqlAlchemyChatGatewayRepository
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.messaging.gateway import ChatGatewayService
from app.domain.messaging.gateway_types import NormalizedInboundMessage
from app.events.bus import EventBus
from app.events.types import ChatMessageReceived
from app.tenancy import WorkspaceContext
from app.util.clock import FrozenClock
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


def _workspace(s: Session) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Gateway Test",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _ctx(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="gateway",
        actor_id="system:chat_gateway",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
        principal_kind="system",
    )


def _inbound(provider_message_id: str = "SM1") -> NormalizedInboundMessage:
    return NormalizedInboundMessage(
        provider="twilio",
        external_contact="+15551234567",
        author_label="+15551234567",
        body_md="Need help",
        provider_message_id=provider_message_id,
        provider_metadata={"to": "+15557654321"},
        raw={"MessageSid": provider_message_id},
    )


def test_receive_creates_gateway_channel_binding_message_audit_and_event(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _workspace(s)
        event_bus = EventBus()
        events: list[ChatMessageReceived] = []

        @event_bus.subscribe(ChatMessageReceived)
        def _append(event: ChatMessageReceived) -> None:
            events.append(event)

        result = ChatGatewayService(
            _ctx(workspace_id),
            clock=FrozenClock(_PINNED),
            event_bus=event_bus,
        ).receive(
            SqlAlchemyChatGatewayRepository(s),
            _inbound(),
            channel_source="sms",
        )

        assert result.duplicate is False
        channel = s.get(ChatChannel, result.channel_id)
        assert channel is not None
        assert channel.kind == "chat_gateway"
        assert channel.source == "sms"
        binding = s.get(ChatGatewayBinding, result.binding_id)
        assert binding is not None
        assert binding.channel_id == channel.id
        assert binding.last_message_at is not None
        assert binding.last_message_at.replace(tzinfo=UTC) == _PINNED
        message = s.get(ChatMessage, result.message_id)
        assert message is not None
        assert message.author_user_id is None
        assert message.author_label == "+15551234567"
        assert message.source == "twilio"
        assert message.provider_message_id == "SM1"
        assert message.gateway_binding_id == binding.id
        assert [event.message_id for event in events] == [message.id]
        assert (
            "chat_gateway.message.received" in s.scalars(select(AuditLog.action)).all()
        )


def test_receive_is_idempotent_on_provider_message_id(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _workspace(s)
        service = ChatGatewayService(_ctx(workspace_id), clock=FrozenClock(_PINNED))
        repo = SqlAlchemyChatGatewayRepository(s)

        first = service.receive(repo, _inbound("SM-replay"), channel_source="sms")
        second = service.receive(repo, _inbound("SM-replay"), channel_source="sms")

        assert second.duplicate is True
        assert second.message_id == first.message_id
        message = s.scalar(select(ChatMessage).where(ChatMessage.source == "twilio"))
        assert message is not None
        assert message.id == first.message_id
        assert (
            s.scalars(select(AuditLog.action))
            .all()
            .count("chat_message.duplicate_inbound")
            == 1
        )
