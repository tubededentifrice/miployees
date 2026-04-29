"""Provider-adapter tests for inbound chat gateway payloads."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from app.adapters.chat_gateway._hmac import hmac_base64, hmac_hex
from app.adapters.chat_gateway.meta_whatsapp import MetaWhatsAppAdapter
from app.adapters.chat_gateway.postmark import PostmarkInboundAdapter
from app.adapters.chat_gateway.twilio import TwilioAdapter


def test_twilio_verifies_signature_and_normalizes_real_shape_payload() -> None:
    adapter = TwilioAdapter()
    secret = "twilio-secret"
    url = "http://testserver/webhooks/chat/twilio"
    params = {
        "AccountSid": "AC123",
        "From": "+15551234567",
        "To": "+15557654321",
        "Body": "Need help with room 4",
        "MessageSid": "SMaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    }
    raw = urlencode(params).encode("utf-8")
    signed = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    signature = hmac_base64(secret, signed.encode("utf-8"), digest="sha1")

    assert adapter.verify({"X-Twilio-Signature": signature}, raw, secret, url=url)
    assert not adapter.verify({"X-Twilio-Signature": "bad"}, raw, secret, url=url)

    inbound = adapter.normalize({}, raw)
    assert inbound.provider == "twilio"
    assert inbound.external_contact == "+15551234567"
    assert inbound.author_label == "+15551234567"
    assert inbound.body_md == "Need help with room 4"
    assert inbound.provider_message_id == "SMaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert inbound.provider_metadata["to"] == "+15557654321"


def test_meta_whatsapp_verifies_signature_and_normalizes_text_message() -> None:
    adapter = MetaWhatsAppAdapter()
    secret = "meta-secret"
    raw = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": "123",
                                    "display_phone_number": "+15557654321",
                                },
                                "contacts": [{"wa_id": "15551234567"}],
                                "messages": [
                                    {
                                        "id": "wamid.HBgLMTU1NTEyMzQ1Njc=",
                                        "from": "15551234567",
                                        "text": {"body": "Bonjour"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    ).encode("utf-8")
    signature = "sha256=" + hmac_hex(secret, raw)

    assert adapter.verify({"X-Hub-Signature-256": signature}, raw, secret, url="")
    assert not adapter.verify(
        {"X-Hub-Signature-256": "sha256=bad"}, raw, secret, url=""
    )

    inbound = adapter.normalize({}, raw)
    assert inbound.provider == "meta_whatsapp"
    assert inbound.external_contact == "15551234567"
    assert inbound.body_md == "Bonjour"
    assert inbound.provider_message_id == "wamid.HBgLMTU1NTEyMzQ1Njc="


def test_meta_whatsapp_rejects_non_object_payload() -> None:
    adapter = MetaWhatsAppAdapter()

    try:
        adapter.normalize({}, b"[]")
    except ValueError as exc:
        assert str(exc) == "meta whatsapp payload must be an object"
    else:
        raise AssertionError("expected ValueError")


def test_postmark_verifies_signature_and_normalizes_inbound_email() -> None:
    adapter = PostmarkInboundAdapter()
    secret = "postmark-secret"
    raw = json.dumps(
        {
            "MessageID": "pm-message-1",
            "FromFull": {"Email": "worker@example.com"},
            "To": "chat@example.crew.day",
            "Subject": "Help",
            "TextBody": "Can someone approve this?",
        }
    ).encode("utf-8")
    signature = hmac_hex(secret, raw)

    assert adapter.verify({"X-Postmark-Signature": signature}, raw, secret, url="")
    assert not adapter.verify({"X-Postmark-Signature": "bad"}, raw, secret, url="")

    inbound = adapter.normalize({}, raw)
    assert inbound.provider == "postmark"
    assert inbound.external_contact == "worker@example.com"
    assert inbound.body_md == "Can someone approve this?"
    assert inbound.provider_message_id == "pm-message-1"


def test_postmark_rejects_non_object_payload() -> None:
    adapter = PostmarkInboundAdapter()

    try:
        adapter.normalize({}, b"[]")
    except ValueError as exc:
        assert str(exc) == "postmark payload must be an object"
    else:
        raise AssertionError("expected ValueError")
