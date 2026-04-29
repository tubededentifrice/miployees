"""Async handoff for inbound chat-gateway messages.

The webhook ingest path publishes ``chat.message.received`` while its
database transaction is still open. This module keeps the event handler
cheap: it schedules a message-id job and returns. The scheduled worker
then opens a committed view of the row, claims it idempotently, hands a
payload to the agent-runtime queue seam, and stamps the audit timestamp.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.messaging.models import ChatGatewayBinding, ChatMessage
from app.audit import write_audit
from app.events.bus import EventBus
from app.events.types import ChatMessageReceived
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AgentDispatchJob",
    "AgentDispatchPayload",
    "AgentDispatchScheduler",
    "AgentRuntimeEnqueue",
    "ChatGatewayDispatcher",
    "DispatchResult",
    "dispatch_inbound_message",
    "register_chat_gateway_dispatcher",
]

_LOG = logging.getLogger(__name__)
_SUBSCRIBED_BUSES: weakref.WeakKeyDictionary[EventBus, ChatGatewayDispatcher] = (
    weakref.WeakKeyDictionary()
)
_SUBSCRIBED_BUSES_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class AgentDispatchJob:
    """Lightweight job scheduled directly from ``chat.message.received``."""

    message_id: str
    capability: Literal["inbound_chat"] = "inbound_chat"


@dataclass(frozen=True, slots=True)
class AgentDispatchPayload:
    """Committed chat message context handed to the agent-runtime queue."""

    message_id: str
    workspace_id: str
    channel_id: str
    binding_id: str
    source_provider: str
    body_md: str
    capability: Literal["inbound_chat"]
    language_hint: str | None = None


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of one dispatch-job processing attempt."""

    status: Literal["enqueued", "skipped", "failed"]
    message_id: str
    dispatched_to_agent_at: datetime | None
    failure_reason: str | None = None


AgentDispatchScheduler = Callable[[AgentDispatchJob], None]
AgentRuntimeEnqueue = Callable[[AgentDispatchPayload], None]


class ChatGatewayDispatcher:
    """Event-bus subscriber that schedules an agent dispatch job."""

    def __init__(self, schedule: AgentDispatchScheduler) -> None:
        self._schedule = schedule

    def handle_message_received(self, event: ChatMessageReceived) -> None:
        self._schedule(AgentDispatchJob(message_id=event.message_id))


def register_chat_gateway_dispatcher(
    event_bus: EventBus,
    *,
    schedule: AgentDispatchScheduler,
) -> ChatGatewayDispatcher:
    """Subscribe the chat-gateway dispatcher to ``event_bus`` once."""
    with _SUBSCRIBED_BUSES_LOCK:
        existing = _SUBSCRIBED_BUSES.get(event_bus)
        if existing is not None:
            return existing
        dispatcher = ChatGatewayDispatcher(schedule)
        event_bus.subscribe(ChatMessageReceived)(dispatcher.handle_message_received)
        _SUBSCRIBED_BUSES[event_bus] = dispatcher
        return dispatcher


def dispatch_inbound_message(
    session: Session,
    job: AgentDispatchJob,
    *,
    enqueue: AgentRuntimeEnqueue,
    clock: Clock | None = None,
) -> DispatchResult:
    """Claim ``job.message_id`` and enqueue it for the agent runtime.

    Idempotency is pinned on ``ChatMessage.dispatched_to_agent_at``:
    once a row is stamped, re-processing the same event exits without
    calling ``enqueue`` again. Enqueue failures are audited and leave
    the stamp empty so the future sweep job can retry.
    """
    eff_clock = clock if clock is not None else SystemClock()
    with tenant_agnostic():
        message = session.scalar(
            select(ChatMessage)
            .where(ChatMessage.id == job.message_id)
            .with_for_update()
        )
        if message is None:
            return DispatchResult(
                status="failed",
                message_id=job.message_id,
                dispatched_to_agent_at=None,
                failure_reason="message_not_found",
            )
        if message.dispatched_to_agent_at is not None:
            return DispatchResult(
                status="skipped",
                message_id=message.id,
                dispatched_to_agent_at=_as_utc(message.dispatched_to_agent_at),
            )
        binding = (
            session.get(ChatGatewayBinding, message.gateway_binding_id)
            if message.gateway_binding_id is not None
            else None
        )

    ctx = _system_ctx(message.workspace_id)
    if binding is None:
        reason = "binding_not_found"
        _audit_failure(
            session,
            ctx=ctx,
            message=message,
            binding_id=message.gateway_binding_id,
            reason=reason,
            clock=eff_clock,
        )
        session.flush()
        return DispatchResult(
            status="failed",
            message_id=message.id,
            dispatched_to_agent_at=None,
            failure_reason=reason,
        )
    if binding.workspace_id != message.workspace_id:
        reason = "binding_workspace_mismatch"
        _audit_failure(
            session,
            ctx=ctx,
            message=message,
            binding_id=binding.id,
            reason=reason,
            clock=eff_clock,
        )
        session.flush()
        return DispatchResult(
            status="failed",
            message_id=message.id,
            dispatched_to_agent_at=None,
            failure_reason=reason,
        )

    payload = AgentDispatchPayload(
        message_id=message.id,
        workspace_id=message.workspace_id,
        channel_id=message.channel_id,
        binding_id=binding.id,
        source_provider=message.source,
        body_md=message.body_md,
        capability=job.capability,
        language_hint=_language_hint(binding),
    )
    try:
        enqueue(payload)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _LOG.exception(
            "chat gateway dispatch enqueue failed",
            extra={
                "event": "chat_gateway.dispatch.failed",
                "message_id": message.id,
                "workspace_id": message.workspace_id,
                "binding_id": binding.id,
            },
        )
        _audit_failure(
            session,
            ctx=ctx,
            message=message,
            binding_id=binding.id,
            reason=reason,
            clock=eff_clock,
        )
        session.flush()
        return DispatchResult(
            status="failed",
            message_id=message.id,
            dispatched_to_agent_at=None,
            failure_reason=reason,
        )

    dispatched_at = eff_clock.now()
    message.dispatched_to_agent_at = dispatched_at
    session.flush()
    return DispatchResult(
        status="enqueued",
        message_id=message.id,
        dispatched_to_agent_at=dispatched_at,
    )


def _system_ctx(workspace_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="chat-gateway",
        actor_id="system:chat_gateway",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
        principal_kind="system",
    )


def _audit_failure(
    session: Session,
    *,
    ctx: WorkspaceContext,
    message: ChatMessage,
    binding_id: str | None,
    reason: str,
    clock: Clock,
) -> None:
    write_audit(
        session,
        ctx,
        entity_kind="chat_message",
        entity_id=message.id,
        action="chat_gateway.dispatch.failed",
        diff={
            "provider": message.source,
            "binding_id": binding_id,
            "channel_id": message.channel_id,
            "reason": reason,
        },
        via="worker",
        clock=clock,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _language_hint(binding: ChatGatewayBinding) -> str | None:
    value = binding.provider_metadata_json.get("language_hint")
    return value if isinstance(value, str) and value else None
