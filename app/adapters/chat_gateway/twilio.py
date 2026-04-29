"""Twilio inbound SMS adapter."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qsl

from app.adapters.chat_gateway._hmac import compare, header, hmac_base64
from app.domain.messaging.gateway_types import NormalizedInboundMessage


class TwilioAdapter:
    provider = "twilio"
    channel_source = "sms"

    def verify(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        secret: str,
        *,
        url: str,
    ) -> bool:
        signature = header(headers, "X-Twilio-Signature")
        if not signature:
            return False
        params = dict(parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True))
        signed = url + "".join(f"{key}{params[key]}" for key in sorted(params))
        expected = hmac_base64(secret, signed.encode("utf-8"), digest="sha1")
        return compare(signature, expected)

    def normalize(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> NormalizedInboundMessage:
        del headers
        params = dict(parse_qsl(raw_body.decode("utf-8"), keep_blank_values=True))
        external_contact = _required(params, "From")
        provider_message_id = (
            params.get("MessageSid")
            or params.get("SmsMessageSid")
            or params.get("SmsSid")
        )
        if not provider_message_id:
            raise ValueError("twilio payload missing message sid")
        body = params.get("Body", "").strip()
        if not body:
            raise ValueError("twilio payload missing body")
        return NormalizedInboundMessage(
            provider=self.provider,
            external_contact=external_contact,
            author_label=external_contact,
            body_md=body,
            provider_message_id=provider_message_id,
            provider_metadata={
                "to": params.get("To", ""),
                "account_sid": params.get("AccountSid", ""),
            },
            raw=dict(params),
        )


def _required(params: Mapping[str, str], key: str) -> str:
    value = params.get(key, "").strip()
    if not value:
        raise ValueError(f"twilio payload missing {key}")
    return value
