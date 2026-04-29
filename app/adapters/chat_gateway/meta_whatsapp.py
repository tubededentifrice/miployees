"""Meta WhatsApp Cloud API inbound adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.adapters.chat_gateway._hmac import compare, header, hmac_hex
from app.domain.messaging.gateway_types import NormalizedInboundMessage


class MetaWhatsAppAdapter:
    provider = "meta_whatsapp"
    channel_source = "whatsapp"

    def verify(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        secret: str,
        *,
        url: str,
    ) -> bool:
        del url
        signature = header(headers, "X-Hub-Signature-256")
        if signature is None or not signature.startswith("sha256="):
            return False
        expected = "sha256=" + hmac_hex(secret, raw_body)
        return compare(signature, expected)

    def normalize(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> NormalizedInboundMessage:
        del headers
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("meta whatsapp payload must be an object")
        value = _first_change_value(payload)
        messages = value.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("meta whatsapp payload missing messages")
        message = messages[0]
        if not isinstance(message, dict):
            raise ValueError("meta whatsapp message must be an object")
        provider_message_id = _required_str(message, "id")
        external_contact = _contact(value, message)
        body = _message_body(message)
        return NormalizedInboundMessage(
            provider=self.provider,
            external_contact=external_contact,
            author_label=external_contact,
            body_md=body,
            provider_message_id=provider_message_id,
            provider_metadata={
                "phone_number_id": _metadata_value(value, "phone_number_id"),
                "display_phone_number": _metadata_value(value, "display_phone_number"),
            },
            raw=payload,
        )


def _first_change_value(payload: dict[str, Any]) -> dict[str, Any]:
    entries = payload.get("entry")
    if not isinstance(entries, list) or not entries:
        raise ValueError("meta whatsapp payload missing entry")
    entry = entries[0]
    if not isinstance(entry, dict):
        raise ValueError("meta whatsapp entry must be an object")
    changes = entry.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("meta whatsapp payload missing changes")
    change = changes[0]
    if not isinstance(change, dict):
        raise ValueError("meta whatsapp change must be an object")
    value = change.get("value")
    if not isinstance(value, dict):
        raise ValueError("meta whatsapp change value must be an object")
    return value


def _contact(value: dict[str, Any], message: dict[str, Any]) -> str:
    contacts = value.get("contacts")
    if isinstance(contacts, list) and contacts and isinstance(contacts[0], dict):
        wa_id = contacts[0].get("wa_id")
        if isinstance(wa_id, str) and wa_id.strip():
            return wa_id.strip()
    return _required_str(message, "from")


def _message_body(message: dict[str, Any]) -> str:
    text = message.get("text")
    body = text.get("body") if isinstance(text, dict) else None
    if isinstance(body, str):
        return body.strip()
    button = message.get("button")
    button_text = button.get("text") if isinstance(button, dict) else None
    if isinstance(button_text, str):
        return button_text.strip()
    interactive = message.get("interactive")
    if isinstance(interactive, dict):
        reply = interactive.get("button_reply") or interactive.get("list_reply")
        title = reply.get("title") if isinstance(reply, dict) else None
        if isinstance(title, str):
            return title.strip()
    raise ValueError("meta whatsapp payload missing supported message body")


def _metadata_value(value: dict[str, Any], key: str) -> str:
    metadata = value.get("metadata")
    metadata_value = metadata.get(key) if isinstance(metadata, dict) else None
    if isinstance(metadata_value, str):
        return metadata_value
    return ""


def _required_str(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"meta whatsapp payload missing {key}")
    return value.strip()
