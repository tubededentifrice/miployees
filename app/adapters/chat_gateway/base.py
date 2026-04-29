"""Shared inbound chat gateway adapter protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from app.domain.messaging.gateway_types import NormalizedInboundMessage


class ChatGatewayAdapter(Protocol):
    provider: str
    channel_source: str

    def verify(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        secret: str,
        *,
        url: str,
    ) -> bool:
        """Return true when the provider signature is valid."""
        ...

    def normalize(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> NormalizedInboundMessage:
        """Parse provider payload bytes into the gateway's common shape."""
        ...


class UnsupportedProvider(LookupError):
    """No adapter is registered for the requested provider slug."""


def get_adapter(provider: str) -> ChatGatewayAdapter:
    normalized = provider.strip().lower().replace("-", "_")
    if normalized in {"twilio", "sms"}:
        from app.adapters.chat_gateway.twilio import TwilioAdapter

        return TwilioAdapter()
    if normalized in {"meta", "whatsapp", "meta_whatsapp"}:
        from app.adapters.chat_gateway.meta_whatsapp import MetaWhatsAppAdapter

        return MetaWhatsAppAdapter()
    if normalized in {"postmark", "postmark_inbound", "email"}:
        from app.adapters.chat_gateway.postmark import PostmarkInboundAdapter

        return PostmarkInboundAdapter()
    raise UnsupportedProvider(provider)
