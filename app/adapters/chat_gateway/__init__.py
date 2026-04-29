"""Provider adapters for inbound chat gateway webhooks."""

from app.adapters.chat_gateway.base import (
    ChatGatewayAdapter,
    UnsupportedProvider,
    get_adapter,
)
from app.domain.messaging.gateway_types import NormalizedInboundMessage

__all__ = [
    "ChatGatewayAdapter",
    "NormalizedInboundMessage",
    "UnsupportedProvider",
    "get_adapter",
]
