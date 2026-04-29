"""Unit tests for the inbound chat-gateway dispatcher."""

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
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.chat_gateway.dispatcher import (
    AgentDispatchJob,
    AgentDispatchPayload,
    dispatch_inbound_message,
    register_chat_gateway_dispatcher,
)
from app.events.bus import EventBus
from app.events.types import ChatMessageReceived
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


def _seed_inbound_message(
    s: Session,
    *,
    binding_metadata: dict[str, object] | None = None,
) -> str:
    workspace_id = new_ulid()
    channel_id = new_ulid()
    binding_id = new_ulid()
    message_id = new_ulid()
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
    s.add(
        ChatChannel(
            id=channel_id,
            workspace_id=workspace_id,
            kind="chat_gateway",
            source="sms",
            title="SMS",
            created_at=_PINNED,
            archived_at=None,
        )
    )
    s.add(
        ChatGatewayBinding(
            id=binding_id,
            workspace_id=workspace_id,
            provider="twilio",
            external_contact="+15551234567",
            channel_id=channel_id,
            display_label="+15551234567",
            provider_metadata_json=dict(binding_metadata or {}),
            created_at=_PINNED,
            last_message_at=_PINNED,
        )
    )
    s.add(
        ChatMessage(
            id=message_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            author_user_id=None,
            author_label="+15551234567",
            body_md="Need help",
            attachments_json=[],
            source="twilio",
            provider_message_id="SM1",
            gateway_binding_id=binding_id,
            dispatched_to_agent_at=None,
            created_at=_PINNED,
        )
    )
    s.flush()
    return message_id


def test_dispatch_enqueues_payload_and_stamps_message(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        message_id = _seed_inbound_message(
            s,
            binding_metadata={"language_hint": "fr-CA"},
        )
        payloads: list[AgentDispatchPayload] = []

        result = dispatch_inbound_message(
            s,
            AgentDispatchJob(message_id=message_id),
            enqueue=payloads.append,
            clock=FrozenClock(_PINNED),
        )

        message = s.get(ChatMessage, message_id)
        assert result.status == "enqueued"
        assert result.dispatched_to_agent_at == _PINNED
        assert message is not None
        assert message.dispatched_to_agent_at is not None
        assert message.dispatched_to_agent_at.replace(tzinfo=UTC) == _PINNED
        assert payloads == [
            AgentDispatchPayload(
                message_id=message_id,
                workspace_id=message.workspace_id,
                channel_id=message.channel_id,
                binding_id=message.gateway_binding_id or "",
                source_provider="twilio",
                body_md="Need help",
                capability="inbound_chat",
                language_hint="fr-CA",
            )
        ]


def test_dispatch_failure_leaves_message_unstamped_and_audits(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        message_id = _seed_inbound_message(s)

        def _raise(_payload: AgentDispatchPayload) -> None:
            raise RuntimeError("runtime queue unavailable")

        result = dispatch_inbound_message(
            s,
            AgentDispatchJob(message_id=message_id),
            enqueue=_raise,
            clock=FrozenClock(_PINNED),
        )

        message = s.get(ChatMessage, message_id)
        assert result.status == "failed"
        assert message is not None
        assert message.dispatched_to_agent_at is None
        actions = s.scalars(select(AuditLog.action)).all()
        assert actions == ["chat_gateway.dispatch.failed"]


def test_dispatch_is_idempotent_once_message_is_stamped(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        message_id = _seed_inbound_message(s)
        payloads: list[AgentDispatchPayload] = []

        first = dispatch_inbound_message(
            s,
            AgentDispatchJob(message_id=message_id),
            enqueue=payloads.append,
            clock=FrozenClock(_PINNED),
        )
        second = dispatch_inbound_message(
            s,
            AgentDispatchJob(message_id=message_id),
            enqueue=payloads.append,
            clock=FrozenClock(_PINNED),
        )

        assert first.status == "enqueued"
        assert second.status == "skipped"
        assert len(payloads) == 1


def test_register_dispatcher_is_idempotent_for_same_bus() -> None:
    event_bus = EventBus()
    scheduled: list[AgentDispatchJob] = []

    first = register_chat_gateway_dispatcher(event_bus, schedule=scheduled.append)
    second = register_chat_gateway_dispatcher(
        event_bus,
        schedule=lambda job: scheduled.append(
            AgentDispatchJob(message_id=f"duplicate:{job.message_id}")
        ),
    )

    event_bus.publish(
        ChatMessageReceived(
            workspace_id=new_ulid(),
            actor_id="system:chat_gateway",
            correlation_id=new_ulid(),
            occurred_at=_PINNED,
            channel_id=new_ulid(),
            message_id="msg_1",
            author_user_id=None,
            channel_kind="chat_gateway",
            binding_id=new_ulid(),
            source="twilio",
        )
    )

    assert second is first
    assert scheduled == [AgentDispatchJob(message_id="msg_1")]
