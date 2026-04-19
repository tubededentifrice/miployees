"""Adapter-port contract tests.

Each adapter protocol is exercised via a minimal in-memory stub. The
goal is two-fold:

1. Prove the protocol is *satisfiable* — a plain Python object with
   the right methods can be used everywhere the protocol is required.
2. Lock the surface — adding or renaming a method on a port breaks
   these tests, forcing a conscious update.

Protocols here are **not** ``runtime_checkable``, so we cannot use
``isinstance`` for structural compatibility. Instead each test passes
the stub into a function typed against the protocol; mypy validates
the structural match, and the test asserts the behaviour at runtime.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import IO, Any

import pytest
from sqlalchemy import Executable, Result, ScalarResult

from app.adapters.db.ports import DbSession, UnitOfWork
from app.adapters.llm.ports import (
    ChatMessage,
    LLMCapabilityMissing,
    LLMClient,
    LLMResponse,
    LLMUsage,
)
from app.adapters.mail.ports import Mailer
from app.adapters.storage.ports import Blob, BlobNotFound, Storage
from app.util.clock import Clock as UtilClock
from app.util.clock import FrozenClock

# ---------------------------------------------------------------------------
# DbSession / UnitOfWork
# ---------------------------------------------------------------------------


@dataclass
class _FakeSession:
    """In-memory DbSession stub: records calls, no SQL involved."""

    added: list[object] = field(default_factory=list)
    committed: int = 0
    rolled_back: int = 0
    flushed: int = 0

    def execute(
        self,
        statement: Executable,
        params: Mapping[str, Any] | None = None,
    ) -> Result[Any]:
        raise NotImplementedError("fake session does not execute SQL")

    def scalar(self, statement: Executable) -> Any:
        return None

    def scalars(self, statement: Executable) -> ScalarResult[Any]:
        raise NotImplementedError("fake session does not execute SQL")

    def get(self, entity: type[Any], ident: Any) -> Any | None:
        return None

    def add(self, instance: object) -> None:
        self.added.append(instance)

    def flush(self) -> None:
        self.flushed += 1

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


class _FakeUnitOfWork:
    """Context-manager stub yielding a :class:`_FakeSession`."""

    def __init__(self) -> None:
        self.session = _FakeSession()
        self.exited_with: type[BaseException] | None = None

    def __enter__(self) -> DbSession:
        return self.session

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        self.exited_with = exc_type
        return None


def _use_session(session: DbSession) -> None:
    """Type-bound helper: mypy rejects a non-DbSession argument here."""
    session.add(object())
    session.flush()
    session.commit()


def test_fake_session_satisfies_dbsession_protocol() -> None:
    session = _FakeSession()
    _use_session(session)
    assert session.flushed == 1
    assert session.committed == 1
    assert len(session.added) == 1


def test_fake_session_rollback_records() -> None:
    session = _FakeSession()
    session.rollback()
    assert session.rolled_back == 1


def test_unit_of_work_yields_dbsession() -> None:
    uow: UnitOfWork = _FakeUnitOfWork()
    with uow as session:
        _use_session(session)
    # The context manager saw a clean exit (no exception propagated).
    assert isinstance(uow, _FakeUnitOfWork)
    assert uow.exited_with is None


def test_unit_of_work_exit_sees_exception_type() -> None:
    uow = _FakeUnitOfWork()
    with pytest.raises(RuntimeError, match="boom"), uow:
        raise RuntimeError("boom")
    assert uow.exited_with is RuntimeError


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class _InMemoryStorage:
    """Dict-backed Storage: enough to exercise every port method."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._content_types: dict[str, str | None] = {}

    def put(
        self,
        content_hash: str,
        data: IO[bytes],
        *,
        content_type: str | None = None,
    ) -> Blob:
        payload = data.read()
        self._blobs[content_hash] = payload
        self._content_types[content_hash] = content_type
        return Blob(
            content_hash=content_hash,
            size_bytes=len(payload),
            content_type=content_type,
            created_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
        )

    def get(self, content_hash: str) -> IO[bytes]:
        if content_hash not in self._blobs:
            raise BlobNotFound(content_hash)
        return io.BytesIO(self._blobs[content_hash])

    def exists(self, content_hash: str) -> bool:
        return content_hash in self._blobs

    def sign_url(self, content_hash: str, *, ttl_seconds: int) -> str:
        return f"memory://{content_hash}?ttl={ttl_seconds}"

    def delete(self, content_hash: str) -> None:
        self._blobs.pop(content_hash, None)
        self._content_types.pop(content_hash, None)


def _round_trip(storage: Storage, content_hash: str, payload: bytes) -> bytes:
    """Typed helper that only uses the protocol surface."""
    storage.put(content_hash, io.BytesIO(payload), content_type="text/plain")
    return storage.get(content_hash).read()


def test_storage_put_get_round_trip() -> None:
    storage = _InMemoryStorage()
    back = _round_trip(storage, "abc", b"hello")
    assert back == b"hello"


def test_storage_put_returns_blob_metadata() -> None:
    storage = _InMemoryStorage()
    blob = storage.put("abc", io.BytesIO(b"hello world"), content_type="text/plain")
    assert blob.content_hash == "abc"
    assert blob.size_bytes == len(b"hello world")
    assert blob.content_type == "text/plain"
    assert blob.created_at.tzinfo is not None


def test_storage_exists_reflects_put() -> None:
    storage = _InMemoryStorage()
    assert storage.exists("abc") is False
    storage.put("abc", io.BytesIO(b"x"))
    assert storage.exists("abc") is True


def test_storage_get_raises_blob_not_found_on_missing() -> None:
    storage = _InMemoryStorage()
    with pytest.raises(BlobNotFound):
        storage.get("missing")


def test_storage_sign_url_embeds_ttl() -> None:
    storage = _InMemoryStorage()
    url = storage.sign_url("abc", ttl_seconds=60)
    assert "abc" in url
    assert "ttl=60" in url


def test_storage_delete_is_idempotent() -> None:
    storage = _InMemoryStorage()
    storage.put("abc", io.BytesIO(b"x"))
    storage.delete("abc")
    storage.delete("abc")  # second call must not raise
    assert storage.exists("abc") is False


# ---------------------------------------------------------------------------
# Mailer
# ---------------------------------------------------------------------------


@dataclass
class _SentMessage:
    to: tuple[str, ...]
    subject: str
    body_text: str
    body_html: str | None
    headers: dict[str, str]
    reply_to: str | None


class _InMemoryMailer:
    """Mailer stub that records every send into a list."""

    def __init__(self) -> None:
        self.sent: list[_SentMessage] = []

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        headers: Mapping[str, str] | None = None,
        reply_to: str | None = None,
    ) -> str:
        self.sent.append(
            _SentMessage(
                to=tuple(to),
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                headers=dict(headers or {}),
                reply_to=reply_to,
            )
        )
        return f"msg-{len(self.sent)}"


def _send_hello(mailer: Mailer) -> str:
    return mailer.send(
        to=["alice@example.com"],
        subject="Hello",
        body_text="Hi there",
    )


def test_mailer_send_returns_message_id() -> None:
    mailer = _InMemoryMailer()
    mid = _send_hello(mailer)
    assert mid == "msg-1"
    assert len(mailer.sent) == 1


def test_mailer_optional_kwargs_default_to_none() -> None:
    mailer = _InMemoryMailer()
    _send_hello(mailer)
    recorded = mailer.sent[0]
    assert recorded.body_html is None
    assert recorded.reply_to is None
    assert recorded.headers == {}


def test_mailer_carries_explicit_headers_and_reply_to() -> None:
    mailer = _InMemoryMailer()
    mailer.send(
        to=["alice@example.com", "bob@example.com"],
        subject="Hi",
        body_text="text",
        body_html="<p>text</p>",
        headers={"X-Crewday": "yes"},
        reply_to="ops@example.com",
    )
    recorded = mailer.sent[0]
    assert recorded.to == ("alice@example.com", "bob@example.com")
    assert recorded.body_html == "<p>text</p>"
    assert recorded.headers == {"X-Crewday": "yes"}
    assert recorded.reply_to == "ops@example.com"


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------


class _EchoLLMClient:
    """Deterministic LLMClient stub.

    Echoes back the last user message, reports trivial usage, and
    refuses OCR to exercise :class:`LLMCapabilityMissing`.
    """

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        return LLMResponse(
            text=prompt,
            usage=LLMUsage(
                prompt_tokens=len(prompt),
                completion_tokens=len(prompt),
                total_tokens=2 * len(prompt),
            ),
            model_id=model_id,
            finish_reason="stop",
        )

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        last = messages[-1]["content"] if messages else ""
        return LLMResponse(
            text=last,
            usage=LLMUsage(
                prompt_tokens=len(last),
                completion_tokens=len(last),
                total_tokens=2 * len(last),
            ),
            model_id=model_id,
            finish_reason="stop",
        )

    def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
        raise LLMCapabilityMissing("ocr")

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Iterator[str]:
        last = messages[-1]["content"] if messages else ""
        yield from last.split()


def _ask(client: LLMClient) -> LLMResponse:
    return client.complete(model_id="google/gemma-3-12b", prompt="ping")


def test_llm_complete_returns_response() -> None:
    client = _EchoLLMClient()
    resp = _ask(client)
    assert resp.text == "ping"
    assert resp.model_id == "google/gemma-3-12b"
    assert resp.finish_reason == "stop"
    expected_total = resp.usage.prompt_tokens + resp.usage.completion_tokens
    assert resp.usage.total_tokens == expected_total


def test_llm_chat_echoes_last_message() -> None:
    client: LLMClient = _EchoLLMClient()
    resp = client.chat(
        model_id="google/gemma-3-12b",
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello"},
        ],
    )
    assert resp.text == "hello"


def test_llm_stream_chat_yields_tokens() -> None:
    client: LLMClient = _EchoLLMClient()
    tokens = list(
        client.stream_chat(
            model_id="google/gemma-3-12b",
            messages=[{"role": "user", "content": "one two three"}],
        )
    )
    assert tokens == ["one", "two", "three"]


def test_llm_ocr_raises_capability_missing_on_unsupported_client() -> None:
    client: LLMClient = _EchoLLMClient()
    with pytest.raises(LLMCapabilityMissing) as excinfo:
        client.ocr(model_id="google/gemma-3-12b", image_bytes=b"png")
    assert excinfo.value.capability == "ocr"


# ---------------------------------------------------------------------------
# Clock re-export
# ---------------------------------------------------------------------------


def test_clock_port_reexports_util_clock() -> None:
    from app.adapters.clock.ports import Clock as AdapterClock

    # Re-export MUST be the *same* object — not a subclass, not a copy.
    assert AdapterClock is UtilClock


def test_clock_port_satisfied_by_frozen_clock() -> None:
    from app.adapters.clock.ports import Clock as AdapterClock

    clock: AdapterClock = FrozenClock(datetime(2026, 4, 19, tzinfo=UTC))
    assert clock.now() == datetime(2026, 4, 19, tzinfo=UTC)
