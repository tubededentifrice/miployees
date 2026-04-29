"""Postmark inbound email adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.adapters.chat_gateway._hmac import compare, header, hmac_hex
from app.domain.messaging.gateway_types import NormalizedInboundMessage


class PostmarkInboundAdapter:
    provider = "postmark"
    channel_source = "email"

    def verify(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        secret: str,
        *,
        url: str,
    ) -> bool:
        del url
        signature = header(headers, "X-Postmark-Signature")
        if signature is None:
            return False
        expected = hmac_hex(secret, raw_body)
        candidates = {expected, f"sha256={expected}"}
        return any(compare(signature, candidate) for candidate in candidates)

    def normalize(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> NormalizedInboundMessage:
        del headers
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("postmark payload must be an object")
        external_contact = _from_email(payload)
        body = _body(payload)
        provider_message_id = _string(payload, "MessageID") or _string(
            payload, "MessageId"
        )
        if not provider_message_id:
            raise ValueError("postmark payload missing MessageID")
        return NormalizedInboundMessage(
            provider=self.provider,
            external_contact=external_contact,
            author_label=external_contact,
            body_md=body,
            provider_message_id=provider_message_id,
            provider_metadata={
                "to": _string(payload, "To"),
                "subject": _string(payload, "Subject"),
            },
            raw=payload,
        )


def _from_email(payload: dict[str, Any]) -> str:
    from_full = payload.get("FromFull")
    if isinstance(from_full, dict):
        email = from_full.get("Email")
        if isinstance(email, str) and email.strip():
            return email.strip()
    value = _string(payload, "From")
    if value:
        return value
    raise ValueError("postmark payload missing sender")


def _body(payload: dict[str, Any]) -> str:
    text = _string(payload, "TextBody")
    if text:
        return text.strip()
    html = _string(payload, "HtmlBody")
    if html:
        return html.strip()
    raise ValueError("postmark payload missing body")


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""
