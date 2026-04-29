"""Value objects shared by chat gateway adapters and domain service."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NormalizedInboundMessage:
    provider: str
    external_contact: str
    author_label: str
    body_md: str
    provider_message_id: str
    provider_metadata: dict[str, object]
    raw: dict[str, object]
