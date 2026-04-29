"""Inbound chat gateway persistence service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.audit import write_audit
from app.domain.messaging.gateway_types import NormalizedInboundMessage
from app.domain.messaging.ports import ChatGatewayBindingRow, ChatGatewayRepository
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import ChatMessageReceived
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid


@dataclass(frozen=True, slots=True)
class InboundChatResult:
    message_id: str
    binding_id: str
    channel_id: str
    duplicate: bool


class ChatGatewayService:
    """Normalise gateway adapter output into channel/binding/message rows."""

    def __init__(
        self,
        ctx: WorkspaceContext,
        *,
        clock: Clock | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._ctx = ctx
        self._clock = clock if clock is not None else SystemClock()
        self._event_bus = event_bus if event_bus is not None else default_event_bus

    def receive(
        self,
        repo: ChatGatewayRepository,
        inbound: NormalizedInboundMessage,
        *,
        channel_source: str,
    ) -> InboundChatResult:
        now = self._clock.now()
        existing = repo.find_message_by_provider_id(
            source=inbound.provider,
            provider_message_id=inbound.provider_message_id,
        )
        binding = self._binding(repo, inbound, channel_source=channel_source, now=now)
        if existing is not None:
            write_audit(
                repo.session,
                self._ctx,
                entity_kind="chat_message",
                entity_id=existing.id,
                action="chat_message.duplicate_inbound",
                diff={
                    "provider": inbound.provider,
                    "binding_id": binding.id,
                    "provider_message_id": inbound.provider_message_id,
                },
                via="api",
                clock=self._clock,
            )
            return InboundChatResult(
                message_id=existing.id,
                binding_id=binding.id,
                channel_id=existing.channel_id,
                duplicate=True,
            )

        message = repo.insert_inbound_message(
            message_id=new_ulid(),
            workspace_id=binding.workspace_id,
            channel_id=binding.channel_id,
            gateway_binding_id=binding.id,
            source=inbound.provider,
            provider_message_id=inbound.provider_message_id,
            author_label=inbound.author_label,
            body_md=inbound.body_md,
            created_at=now,
        )
        repo.touch_binding(binding_id=binding.id, last_message_at=now)
        write_audit(
            repo.session,
            self._ctx,
            entity_kind="chat_message",
            entity_id=message.id,
            action="chat_gateway.message.received",
            diff={
                "provider": inbound.provider,
                "binding_id": binding.id,
                "channel_id": binding.channel_id,
                "provider_message_id": inbound.provider_message_id,
            },
            via="api",
            clock=self._clock,
        )
        repo.session.flush()
        self._event_bus.publish(
            ChatMessageReceived(
                workspace_id=binding.workspace_id,
                actor_id=self._ctx.actor_id,
                correlation_id=self._ctx.audit_correlation_id,
                occurred_at=now,
                channel_id=binding.channel_id,
                message_id=message.id,
                author_user_id=None,
                channel_kind="chat_gateway",
                binding_id=binding.id,
                source=inbound.provider,
            )
        )
        return InboundChatResult(
            message_id=message.id,
            binding_id=binding.id,
            channel_id=binding.channel_id,
            duplicate=False,
        )

    def _binding(
        self,
        repo: ChatGatewayRepository,
        inbound: NormalizedInboundMessage,
        *,
        channel_source: str,
        now: datetime,
    ) -> ChatGatewayBindingRow:
        existing = repo.find_binding(
            provider=inbound.provider,
            external_contact=inbound.external_contact,
        )
        if existing is not None:
            return existing
        return repo.insert_binding_with_channel(
            binding_id=new_ulid(),
            channel_id=new_ulid(),
            workspace_id=self._ctx.workspace_id,
            provider=inbound.provider,
            external_contact=inbound.external_contact,
            channel_source=channel_source,
            display_label=inbound.author_label,
            provider_metadata_json=inbound.provider_metadata,
            created_at=now,
        )
