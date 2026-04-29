"""Event-bus integration test for the chat-gateway dispatcher."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.base import Base
from app.adapters.db.messaging.models import ChatMessage
from app.adapters.db.messaging.repositories import SqlAlchemyChatGatewayRepository
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.chat_gateway.dispatcher import (
    AgentDispatchJob,
    AgentDispatchPayload,
    dispatch_inbound_message,
    register_chat_gateway_dispatcher,
)
from app.domain.messaging.gateway import ChatGatewayService
from app.domain.messaging.gateway_types import NormalizedInboundMessage
from app.events.bus import EventBus
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
def local_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(local_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=local_engine, expire_on_commit=False, class_=Session)


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


def test_received_event_schedules_one_job_and_processor_stamps_after_commit(
    factory: sessionmaker[Session],
) -> None:
    event_bus = EventBus()
    scheduled: list[AgentDispatchJob] = []
    register_chat_gateway_dispatcher(event_bus, schedule=scheduled.append)
    with factory() as s:
        workspace_id = _workspace(s)
        result = ChatGatewayService(
            _ctx(workspace_id),
            clock=FrozenClock(_PINNED),
            event_bus=event_bus,
        ).receive(
            SqlAlchemyChatGatewayRepository(s),
            _inbound(),
            channel_source="sms",
        )
        s.commit()

    assert scheduled == [AgentDispatchJob(message_id=result.message_id)]
    payloads: list[AgentDispatchPayload] = []
    with factory() as s:
        dispatch_result = dispatch_inbound_message(
            s,
            scheduled[0],
            enqueue=payloads.append,
            clock=FrozenClock(_PINNED),
        )
        s.commit()

    assert dispatch_result.status == "enqueued"
    assert [payload.message_id for payload in payloads] == [result.message_id]
    with factory() as s:
        message = s.scalar(
            select(ChatMessage).where(ChatMessage.id == result.message_id)
        )
        assert message is not None
        assert message.dispatched_to_agent_at is not None
        assert message.dispatched_to_agent_at.replace(tzinfo=UTC) == _PINNED
