"""Outbound redaction coverage for :class:`OpenRouterClient`.

Every request leaving the adapter must pass through the §15
redaction seam before the JSON body hits the wire. These tests
stand in front of an :class:`httpx.MockTransport` so the adapter
never opens a real socket; we capture the request and assert the
body the provider would have received is already PII-free.

Consent (``scope="llm"`` with a non-empty :class:`ConsentSet`) is
exercised here because the consent loader (workspace-scoped
``agent_preferences.upstream_pii_consent``) does not exist yet — the
only way to prove the plumbing passes a consent set through the
adapter is to hand one in at the call site.

See ``docs/specs/11-llm-and-agents.md`` §"Redaction layer",
``docs/specs/15-security-privacy.md`` §"Logging and redaction".
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import cast

import httpx
from pydantic import SecretStr

from app.adapters.llm.openrouter import OpenRouterClient
from app.adapters.llm.ports import ChatMessage
from app.util.clock import FrozenClock
from app.util.redact import ConsentSet

_API_KEY = SecretStr("sk-or-test-0000")
_MODEL = "google/gemma-3-27b-it"

_FAKE_COMPLETION: dict[str, object] = {
    "id": "gen-test-redaction",
    "model": _MODEL,
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class _RecordingHandler:
    """Capture every :class:`httpx.Request` and return a scripted 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=_FAKE_COMPLETION)


def _make_client(handler: _RecordingHandler) -> OpenRouterClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return OpenRouterClient(
        _API_KEY,
        max_retries=1,
        http=http,
        clock=FrozenClock(datetime(2026, 4, 20, 9, 0, tzinfo=UTC)),
        sleep=lambda _s: None,
    )


def _body(request: httpx.Request) -> dict[str, object]:
    return cast(dict[str, object], json.loads(request.content.decode("utf-8")))


class TestChatBodyIsRedacted:
    def test_email_in_user_message_is_scrubbed(self) -> None:
        handler = _RecordingHandler()
        client = _make_client(handler)

        messages: list[ChatMessage] = [
            {"role": "user", "content": "email me back at jean@example.com"},
        ]
        client.chat(model_id=_MODEL, messages=messages)

        assert len(handler.requests) == 1
        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        user_content = cast(str, wire_msgs[0]["content"])
        assert "jean@example.com" not in user_content
        assert "<redacted:email>" in user_content

    def test_iban_and_pan_in_prompt_are_scrubbed(self) -> None:
        handler = _RecordingHandler()
        client = _make_client(handler)

        prompt = (
            "process refund for IBAN FR1420041010050500013M02606 "
            "charged on card 4242424242424242"
        )
        client.complete(model_id=_MODEL, prompt=prompt)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "FR1420041010050500013M02606" not in content
        assert "4242424242424242" not in content
        assert "<redacted:iban>" in content
        assert "<redacted:pan>" in content

    def test_system_and_assistant_turns_are_scrubbed(self) -> None:
        handler = _RecordingHandler()
        client = _make_client(handler)

        messages: list[ChatMessage] = [
            {"role": "system", "content": "contact ops at ops@example.com"},
            {"role": "user", "content": "thanks, mine is jean@example.com"},
            {"role": "assistant", "content": "noted, calling +33612345678 now"},
        ]
        client.chat(model_id=_MODEL, messages=messages)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        all_contents = " | ".join(cast(str, m["content"]) for m in wire_msgs)
        assert "ops@example.com" not in all_contents
        assert "jean@example.com" not in all_contents
        assert "+33612345678" not in all_contents


class TestConsentPassThrough:
    def test_consent_allows_legal_name_through(self) -> None:
        handler = _RecordingHandler()
        client = _make_client(handler)

        # A real caller would pass a `ChatMessage` with the plain
        # content. Consent flags operate at the mapping-key level,
        # so the pass-through kicks in when the LLM payload is a
        # dict with a matching key name. This test demonstrates the
        # consent plumbing: a `legal_name` key under
        # `messages[0]["content"]` would be scrubbed without the
        # consent flag.
        #
        # We exercise the path end-to-end by building a chat turn
        # whose content happens to be the target name. Without
        # consent, a plain name like "Jean Dupont" has no PII
        # shape anyway — the more expressive assertion lives in
        # the unit tests where the mapping keys are under our
        # control; here we just prove the adapter threads consents
        # into the seam.
        messages: list[ChatMessage] = [
            {"role": "user", "content": "Remember Jean Dupont."},
        ]
        consents = ConsentSet(fields=frozenset({"legal_name"}))
        client.chat(model_id=_MODEL, messages=messages, consents=consents)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "Jean Dupont" in content

    def test_consent_does_not_override_sensitive_key(self) -> None:
        """A consent flag for ``iban`` must not leak an IBAN value."""
        handler = _RecordingHandler()
        client = _make_client(handler)

        prompt = "account IBAN FR1420041010050500013M02606 for ops"
        consents = ConsentSet(fields=frozenset({"iban"}))
        client.complete(model_id=_MODEL, prompt=prompt, consents=consents)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "FR1420041010050500013M02606" not in content
        assert "<redacted:iban>" in content


class TestStreamChatRedaction:
    def test_stream_body_is_redacted(self) -> None:
        handler = _RecordingHandler()

        # Streaming handler: return an SSE body that the iterator
        # will decode. Empty ``[DONE]``-only stream is enough — we
        # care about the outbound request body, not the response.
        def stream_handler(request: httpx.Request) -> httpx.Response:
            handler.requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"data: [DONE]\n\n",
            )

        transport = httpx.MockTransport(stream_handler)
        http = httpx.Client(transport=transport)
        client = OpenRouterClient(
            _API_KEY,
            max_retries=1,
            http=http,
            clock=FrozenClock(datetime(2026, 4, 20, 9, 0, tzinfo=UTC)),
            sleep=lambda _s: None,
        )

        list(
            client.stream_chat(
                model_id=_MODEL,
                messages=[{"role": "user", "content": "email jean@example.com"}],
            )
        )

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content = cast(str, wire_msgs[0]["content"])
        assert "jean@example.com" not in content
        assert "<redacted:email>" in content


class TestOcrRedaction:
    def test_ocr_text_block_retains_static_prompt(self) -> None:
        """The static OCR prompt text survives the scrub.

        The free-text prompt contains no PII shapes, so no regex hits
        it. This fixes the invariant so a future rewrite of the prompt
        that accidentally includes PII-shaped strings is caught.
        """
        handler = _RecordingHandler()
        client = _make_client(handler)

        image_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 400 + b"\xff\xd9"
        client.ocr(model_id=_MODEL, image_bytes=image_bytes)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content_blocks = cast(list[dict[str, object]], wire_msgs[0]["content"])
        text_block = content_blocks[0]
        text = cast(str, text_block["text"])
        # The default OCR prompt mentions no emails / phones / credentials.
        assert "Extract every piece of visible text" in text
        assert "<redacted:" not in text

    def test_ocr_image_data_url_bytes_survive_intact(self) -> None:
        """The base64 image payload passes through the redactor unchanged.

        Multimodal ``{"type": "image_url", ...}`` blocks are carved
        out of the free-text regex sweep — scrubbing a base64 blob
        as a ``<redacted:credential>`` would silently break every
        vision call. The sibling ``type`` and ``text`` blocks still
        run through the regular rules so a PII-shaped prompt next to
        the image is still caught.
        """
        handler = _RecordingHandler()
        client = _make_client(handler)

        image_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 400 + b"\xff\xd9"
        client.ocr(model_id=_MODEL, image_bytes=image_bytes)

        body = _body(handler.requests[0])
        wire_msgs = cast(list[dict[str, object]], body["messages"])
        content_blocks = cast(list[dict[str, object]], wire_msgs[0]["content"])
        image_block = content_blocks[1]
        assert image_block["type"] == "image_url"
        image_url = cast(dict[str, object], image_block["image_url"])
        url = cast(str, image_url["url"])
        expected_payload = base64.b64encode(image_bytes).decode("ascii")
        assert url == f"data:image/jpeg;base64,{expected_payload}"
        assert "<redacted:credential>" not in url
