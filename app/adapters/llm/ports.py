"""LLM ports.

Defines the seam domain code uses to talk to a language-model
provider. Concrete v1 implementation is ``OpenRouterClient`` (see
``docs/specs/11-llm-and-agents.md``). Capability routing (which model
to pick for which task, which workspace budget to charge) is a
domain-layer concern; this protocol stays transport-agnostic.

Optional capabilities (e.g. OCR, streaming) are part of the same
protocol; adapters that do not implement a capability raise
:class:`LLMCapabilityMissing` with the capability name. Callers either
feature-detect beforehand (by asking the router) or handle the
exception.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

__all__ = [
    "ChatMessage",
    "LLMCapabilityMissing",
    "LLMClient",
    "LLMResponse",
    "LLMUsage",
]


class LLMCapabilityMissing(Exception):
    """Raised by adapters that do not implement an optional capability.

    The string argument is the capability name (e.g. ``"ocr"``,
    ``"stream_chat"``). Callers can feature-detect by catching this
    exception or by asking the router up front.
    """

    def __init__(self, capability: str) -> None:
        super().__init__(f"LLM capability not supported by this client: {capability}")
        self.capability = capability


class ChatMessage(TypedDict):
    """A single role-tagged chat turn."""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """Token accounting returned alongside every completion."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A non-streaming completion result."""

    text: str
    usage: LLMUsage
    model_id: str
    finish_reason: str


class LLMClient(Protocol):
    """Language-model client.

    ``model_id`` is always provided by the caller — model selection is
    a domain-level concern, not an adapter concern.
    """

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Single-shot text completion."""
        ...

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Multi-turn chat completion."""
        ...

    def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
        """Extract text from an image.

        Optional capability; adapters without vision raise
        :class:`LLMCapabilityMissing` with ``"ocr"``.
        """
        ...

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Iterator[str]:
        """Stream chat tokens as they arrive.

        Optional capability; adapters without streaming raise
        :class:`LLMCapabilityMissing` with ``"stream_chat"``.
        """
        ...
