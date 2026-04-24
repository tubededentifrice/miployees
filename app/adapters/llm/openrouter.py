"""OpenRouter implementation of the :class:`~app.adapters.llm.ports.LLMClient` port.

Transport choice: **synchronous** :class:`httpx.Client`. The
:class:`LLMClient` port is sync (§01 "Adapters"), matching the
:class:`~app.adapters.mail.smtp.SMTPMailer` convention. Wrapping one
HTTP client in the async layer would add an event-loop dependency
the rest of the adapter surface doesn't need; the worker that actually
runs LLM calls is a thread-pool executor (§10 worker loop), and a
sync client is the natural fit.

Error taxonomy (adapter-local, not in the port):

* :class:`LlmRateLimited` — the provider returned ``429`` on every
  attempt in the retry budget. Domain code can surface this to the
  user as "model cooled down; try another assignment" (§11 fallback
  chain).
* :class:`LlmTransportError` — non-429 transport-layer failure
  (``5xx`` after retries, connect refused, timeout, malformed body).
  The caller can retry at a higher layer or flip to the next
  ``llm_provider_model`` rung.
* :class:`LlmProviderError` — the request itself is bad (``4xx``
  other than ``429``). Retrying without editing the payload is
  guaranteed to hit the same wall, so we raise immediately.

Retry policy mirrors the SMTP adapter: exponential backoff ``(0.5,
1.0, 2.0)`` seconds across ``max_retries`` attempts. Retries cover
``408`` (Request Timeout), ``429`` (rate limit), and ``5xx``. Any
other ``4xx`` is treated as permanent — retrying without editing the
payload is guaranteed to hit the same wall.

Trust boundary: ``base_url`` is operator-controlled configuration,
not attacker input. The adapter does not validate it against an
allow-list; if the §15 fetch-guard lands later and the base URL
becomes tenant-reachable, that changes. See ``docs/specs/11``
§"Providers" and ``docs/specs/15-privacy-and-pii.md``.

API-key handling: :meth:`SecretStr.get_secret_value` is invoked
**once per request**, exclusively inside the ``Authorization`` header
builder. The raw string never lands in exception messages, structured
logs, or the :class:`LLMResponse` payload.

Outbound-payload redaction: every request body is funnelled through
:func:`app.util.redact.redact` (scope ``"llm"``) before the JSON hits
the wire. Callers can hand in a workspace-scoped
:class:`~app.util.redact.ConsentSet` via the ``consents`` kwarg on
:meth:`complete` / :meth:`chat` / :meth:`ocr` / :meth:`stream_chat`;
``None`` falls back to the redact-everything default. See
``docs/specs/11-llm-and-agents.md`` §"Redaction layer" and
``docs/specs/15-security-privacy.md`` §"Logging and redaction".

See ``docs/specs/11-llm-and-agents.md`` §"Providers", §"Model router"
and ``docs/specs/01-architecture.md`` §"Adapters/llm".
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Final, TypedDict, cast

import httpx
from pydantic import SecretStr

from app.adapters.llm.ports import ChatMessage, LLMResponse, LLMUsage
from app.util.clock import Clock, SystemClock
from app.util.redact import ConsentSet, redact

__all__ = [
    "LlmProviderError",
    "LlmRateLimited",
    "LlmTransportError",
    "OpenRouterClient",
]

_log = logging.getLogger(__name__)

# Backoff schedule (seconds). Index is the zero-based retry number; a
# three-attempt budget sleeps 0.5s then 1.0s between the three calls
# for a worst-case wall clock ~1.5s before giving up. Values beyond
# the schedule reuse the last entry.
_BACKOFF_SCHEDULE: Final[tuple[float, ...]] = (0.5, 1.0, 2.0)

# OpenRouter's published attribution headers. ``HTTP-Referer`` is the
# landing page users would see if the provider surfaces the caller,
# and ``X-Title`` is the app name. Both are advertised as optional
# but OpenRouter uses them for their public "apps using us" page and
# for abuse-triage signals — keeping them honest is cheaper than
# shaving four bytes off every call.
_ATTRIBUTION_REFERER: Final[str] = "https://crew.day"
_ATTRIBUTION_TITLE: Final[str] = "crewday"

_DEFAULT_BASE_URL: Final[str] = "https://openrouter.ai/api/v1"

# SSE prefixes emitted by OpenRouter's streaming endpoint. Every data
# frame is ``data: {...}`` on its own line with a blank-line separator
# between frames; the stream terminates with ``data: [DONE]``.
_SSE_DATA_PREFIX: Final[str] = "data: "
_SSE_DONE_SENTINEL: Final[str] = "[DONE]"

# OCR defaults. The spec points vision-capable assignments at
# ``google/gemma-3-27b-it`` (§11 catalog), which accepts a JPEG via
# data-URL; we default the MIME to ``image/jpeg`` because receipts
# from the upload pipeline are normalised to JPEG (§21 assets), but
# callers can pass another type through if they already know the
# source format.
_DEFAULT_OCR_PROMPT: Final[str] = (
    "Extract every piece of visible text from this image verbatim. "
    "Preserve line breaks; do not summarise."
)
_DEFAULT_OCR_MIME: Final[str] = "image/jpeg"


# ---------------------------------------------------------------------------
# Typed JSON shapes
# ---------------------------------------------------------------------------
#
# ``TypedDict``s covering the subset of OpenRouter's OpenAI-compatible
# schema this adapter actually reads. Kept ``total=False`` on
# ``_Choice`` / ``_Delta`` because streaming frames omit ``message``
# and non-streaming frames omit ``delta``; mypy then forces an
# ``isinstance``/``in`` check before access.


class _Message(TypedDict):
    role: str
    content: str


class _Delta(TypedDict, total=False):
    role: str
    content: str


class _Choice(TypedDict, total=False):
    index: int
    message: _Message
    delta: _Delta
    finish_reason: str | None


class _Usage(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class _ChatCompletion(TypedDict, total=False):
    id: str
    model: str
    choices: list[_Choice]
    usage: _Usage


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LlmRateLimited(RuntimeError):
    """Raised after the retry budget is exhausted on ``429`` responses."""


class LlmTransportError(RuntimeError):
    """Raised for transport-level failures (5xx after retries, timeouts)."""


class LlmProviderError(RuntimeError):
    """Raised when the provider rejects the request with a non-retryable ``4xx``."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OpenRouterClient:
    """Concrete :class:`~app.adapters.llm.ports.LLMClient` over OpenRouter.

    Constructed once per process (or per test) and reused. All four
    protocol methods share the same ``/chat/completions`` endpoint;
    streaming flips ``"stream": true`` and consumes SSE frames.

    The ``http`` and ``sleep`` arguments exist for tests — production
    wiring passes neither. ``clock`` defaults to :class:`SystemClock`
    so the adapter can report per-call latency without each call site
    having to hand one in.
    """

    def __init__(
        self,
        api_key: SecretStr,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 3,
        http: httpx.Client | None = None,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        self._api_key = api_key
        # ``rstrip('/')`` so callers can pass either
        # ``https://openrouter.ai/api/v1`` or the same URL with a
        # trailing slash without us emitting ``//chat/completions``.
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._clock = clock or SystemClock()
        self._sleep = sleep
        # ``http`` is provided by tests (preloaded with
        # :class:`httpx.MockTransport`); in production we build a
        # fresh client so the timeout and defaults live on the wire.
        self._http = http or httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------
    # Public LLMClient surface
    # ------------------------------------------------------------------

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        """Single-shot text completion. See :class:`LLMClient.complete`.

        ``consents`` is the workspace-scoped consent set that lets
        specific PII fields pass through the §15 redaction seam. An
        omitted or ``None`` value defaults to :meth:`ConsentSet.none`
        — the redact-everything posture that every call site is
        safe to start from.
        """
        messages: list[ChatMessage] = [{"role": "user", "content": prompt}]
        return self._chat_completion(
            model_id=model_id,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            consents=consents,
        )

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> LLMResponse:
        """Multi-turn chat. See :class:`LLMClient.chat`.

        See :meth:`complete` for the ``consents`` argument semantics.
        """
        return self._chat_completion(
            model_id=model_id,
            messages=list(messages),
            max_tokens=max_tokens,
            temperature=temperature,
            consents=consents,
        )

    def ocr(
        self,
        *,
        model_id: str,
        image_bytes: bytes,
        consents: ConsentSet | None = None,
    ) -> str:
        """Vision extract. See :class:`LLMClient.ocr`.

        Encodes ``image_bytes`` as a base64 ``data:`` URL and posts it
        through ``chat/completions`` as a multimodal user message —
        the shape OpenRouter documents for vision requests on models
        that carry the ``vision`` capability tag.

        See :meth:`complete` for the ``consents`` argument semantics.
        The base64 image bytes pass through unchanged — the §15
        redactor carves multimodal ``{"type": "image_url", ...}``
        blocks out of the free-text sweep so the opaque data URL
        survives, while every other key in the surrounding message
        (text blocks, role, ...) still runs through the regular
        rules. See :func:`app.util.redact._redact_image_url_block`.
        """
        if not image_bytes:
            raise ValueError("ocr requires non-empty image_bytes")
        data_url = _build_data_url(image_bytes, mime_type=_DEFAULT_OCR_MIME)
        payload_messages: list[_WireMessage] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _DEFAULT_OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        body = _build_request_body(
            model_id=model_id,
            messages=payload_messages,
            max_tokens=1024,
            temperature=0.0,
            stream=False,
        )
        response = self._post_with_retry(_redact_body(body, consents))
        parsed = _parse_completion(response)
        return parsed.text

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        consents: ConsentSet | None = None,
    ) -> Iterator[str]:
        """Stream chat tokens. See :class:`LLMClient.stream_chat`.

        Yields each ``choices[0].delta.content`` chunk as it arrives;
        frames without ``content`` (e.g. the leading ``role`` frame)
        are silently skipped. A mid-stream ``429`` raises
        :class:`LlmRateLimited` — the server-sent stream can surface
        rate-limit errors after the initial 200 response, so we check
        the status code before iterating lines.

        See :meth:`complete` for the ``consents`` argument semantics.
        """
        wire_messages: list[_WireMessage] = [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]
        body = _build_request_body(
            model_id=model_id,
            messages=wire_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )
        return self._stream_request(_redact_body(body, consents))

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _chat_completion(
        self,
        *,
        model_id: str,
        messages: list[ChatMessage],
        max_tokens: int,
        temperature: float,
        consents: ConsentSet | None,
    ) -> LLMResponse:
        wire_messages: list[_WireMessage] = [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]
        body = _build_request_body(
            model_id=model_id,
            messages=wire_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
        )
        response = self._post_with_retry(_redact_body(body, consents))
        return _parse_completion(response)

    def _post_with_retry(self, body: Mapping[str, object]) -> _ChatCompletion:
        """POST to ``/chat/completions`` with retry on 429 / 5xx.

        Returns the parsed JSON body on 2xx. On final failure, raises
        one of :class:`LlmRateLimited`, :class:`LlmProviderError`,
        :class:`LlmTransportError` depending on the terminal reason.
        """
        url = f"{self._base_url}/chat/completions"
        headers = self._build_headers()

        last_status: int | None = None
        last_transport_exc: Exception | None = None

        for attempt in range(self._max_retries):
            started = self._clock.now()
            try:
                response = self._http.post(url, headers=headers, json=dict(body))
            except httpx.TimeoutException as exc:
                last_transport_exc = exc
                _log.warning(
                    "openrouter request timed out (attempt %d/%d)",
                    attempt + 1,
                    self._max_retries,
                )
                if attempt + 1 >= self._max_retries:
                    raise LlmTransportError(
                        f"openrouter request timed out after "
                        f"{self._max_retries} attempt(s)"
                    ) from exc
                self._sleep(_backoff_seconds(attempt))
                continue
            except httpx.HTTPError as exc:
                last_transport_exc = exc
                _log.warning(
                    "openrouter transport error (attempt %d/%d): %s",
                    attempt + 1,
                    self._max_retries,
                    type(exc).__name__,
                )
                if attempt + 1 >= self._max_retries:
                    raise LlmTransportError(
                        f"openrouter transport failed after "
                        f"{self._max_retries} attempt(s): {type(exc).__name__}"
                    ) from exc
                self._sleep(_backoff_seconds(attempt))
                continue

            elapsed_ms = _elapsed_ms(started, self._clock.now())
            last_status = response.status_code

            if 200 <= response.status_code < 300:
                _log.debug(
                    "openrouter ok status=%d latency_ms=%d",
                    response.status_code,
                    elapsed_ms,
                )
                return _decode_json(response)

            if response.status_code == 429:
                _log.warning(
                    "openrouter rate limited (attempt %d/%d, latency_ms=%d)",
                    attempt + 1,
                    self._max_retries,
                    elapsed_ms,
                )
                if attempt + 1 >= self._max_retries:
                    raise LlmRateLimited(
                        f"openrouter rate limited after {self._max_retries} attempt(s)"
                    )
                self._sleep(_backoff_seconds(attempt))
                continue

            if response.status_code == 408 or 500 <= response.status_code < 600:
                # 408 (Request Timeout) is a transient server-side
                # signal that the previous request took too long; it
                # belongs in the same retry bucket as 5xx and 429.
                _log.warning(
                    "openrouter transient status=%d (attempt %d/%d, latency_ms=%d)",
                    response.status_code,
                    attempt + 1,
                    self._max_retries,
                    elapsed_ms,
                )
                if attempt + 1 >= self._max_retries:
                    raise LlmTransportError(
                        f"openrouter returned {response.status_code} after "
                        f"{self._max_retries} attempt(s)"
                    )
                self._sleep(_backoff_seconds(attempt))
                continue

            # 4xx other than 408 / 429 → permanent: raise immediately.
            raise LlmProviderError(
                f"openrouter rejected request: {response.status_code} "
                f"{_safe_error_detail(response)}"
            )

        # Loop exit only reachable on the transient path; the last
        # iteration's raise should have fired. Keep an explicit
        # fallback so mypy sees the function always returns or raises.
        if last_transport_exc is not None:
            raise LlmTransportError(
                f"openrouter transport failed: {type(last_transport_exc).__name__}"
            ) from last_transport_exc
        raise LlmTransportError(f"openrouter request failed with status {last_status}")

    def _stream_request(self, body: Mapping[str, object]) -> Iterator[str]:
        """Open a streaming POST and yield content chunks.

        ``httpx`` returns a context-managed ``Response`` for streaming;
        we consume lines eagerly and close the context before
        returning so the caller doesn't have to manage the socket.
        Errors mid-stream surface as :class:`LlmRateLimited` (on 429
        before the body started) or :class:`LlmTransportError` (other
        transport faults).

        The read timeout is deliberately left to the underlying
        :class:`httpx.Client`'s default for the stream call — the
        60s budget that bounds non-streaming requests can trip
        legitimate long generations (digest summaries, chat
        completions past the default ``max_tokens``). Connect / write
        / pool timeouts still apply; the caller controls stream
        lifetime by closing the iterator.
        """
        url = f"{self._base_url}/chat/completions"
        headers = self._build_headers()

        # Disable the read timeout for streaming only. Keep connect /
        # write / pool bounds from the underlying client so a stalled
        # socket setup still fails fast.
        stream_timeout = httpx.Timeout(
            connect=self._timeout,
            write=self._timeout,
            pool=self._timeout,
            read=None,
        )

        try:
            with self._http.stream(
                "POST",
                url,
                headers=headers,
                json=dict(body),
                timeout=stream_timeout,
            ) as response:
                if response.status_code == 429:
                    raise LlmRateLimited("openrouter rate limited mid-stream")
                if 400 <= response.status_code < 500:
                    # Drain once so the error body (if any) is available
                    # to the exception message.
                    response.read()
                    raise LlmProviderError(
                        f"openrouter rejected stream request: "
                        f"{response.status_code} {_safe_error_detail(response)}"
                    )
                if response.status_code >= 500:
                    response.read()
                    raise LlmTransportError(
                        f"openrouter stream failed: {response.status_code}"
                    )

                for line in response.iter_lines():
                    chunk = _parse_sse_line(line)
                    if chunk is None:
                        continue
                    if chunk == "__done__":
                        return
                    yield chunk
        except httpx.TimeoutException as exc:
            raise LlmTransportError("openrouter stream timed out") from exc
        except httpx.HTTPError as exc:
            raise LlmTransportError(
                f"openrouter stream transport failed: {type(exc).__name__}"
            ) from exc

    def _build_headers(self) -> dict[str, str]:
        """Return the per-request header map.

        ``get_secret_value`` is called exactly here — nowhere else in
        the module — so the raw key touches memory once per call,
        inside the string we're about to hand to :mod:`httpx`.
        """
        return {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "HTTP-Referer": _ATTRIBUTION_REFERER,
            "X-Title": _ATTRIBUTION_TITLE,
        }


# ---------------------------------------------------------------------------
# Wire-shape builders
# ---------------------------------------------------------------------------


# Multimodal content blocks (used by :meth:`OpenRouterClient.ocr`).
class _TextBlock(TypedDict):
    type: str
    text: str


class _ImageUrlRef(TypedDict):
    url: str


class _ImageBlock(TypedDict):
    type: str
    image_url: _ImageUrlRef


# Messages on the wire can carry either plain-string content (the
# usual case) or a list of blocks (multimodal). Using a loose ``object``
# here keeps the builder signature simple; the concrete shape is
# enforced at the call sites above.
class _WireMessage(TypedDict):
    role: str
    content: object


def _build_request_body(
    *,
    model_id: str,
    messages: Sequence[_WireMessage],
    max_tokens: int,
    temperature: float,
    stream: bool,
) -> dict[str, object]:
    """Assemble the JSON body for ``/chat/completions``.

    Extracted so :meth:`complete`, :meth:`chat`, :meth:`ocr`, and
    :meth:`stream_chat` all share one serialisation path — there's
    exactly one place where "what does OpenRouter expect on the wire"
    is answered.
    """
    body: dict[str, object] = {
        "model": model_id,
        "messages": list(messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if stream:
        body["stream"] = True
    return body


def _build_data_url(image_bytes: bytes, *, mime_type: str) -> str:
    """Return a ``data:<mime>;base64,<payload>`` URL for vision requests."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _redact_body(
    body: Mapping[str, object], consents: ConsentSet | None
) -> dict[str, object]:
    """Run the outbound body through the §15 redaction seam.

    Called once per outbound request — the final step before the
    JSON bytes hit the wire. Passing ``consents=None`` falls back to
    :meth:`ConsentSet.none`, i.e. redact everything. The function
    returns a deep copy so the caller's original body (which lives
    on the call frame) is untouched.

    See ``docs/specs/15-security-privacy.md`` §"Logging and redaction"
    and ``docs/specs/11-llm-and-agents.md`` §"Redaction layer" for
    the exact rule set.
    """
    effective = consents if consents is not None else ConsentSet.none()
    redacted = redact(dict(body), scope="llm", consents=effective)
    if not isinstance(redacted, dict):  # pragma: no cover - defensive
        raise TypeError("redact() must preserve dict shape on outbound body")
    return redacted


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _decode_json(response: httpx.Response) -> _ChatCompletion:
    """Decode ``response.json()`` into the typed ``_ChatCompletion`` shape.

    Raises :class:`LlmTransportError` on unparseable bodies — a 200
    with junk payload is still a provider failure we cannot recover
    from, and we'd rather the caller see it as transport than as a
    silent ``None`` somewhere deeper.
    """
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise LlmTransportError("openrouter returned non-JSON body") from exc
    if not isinstance(payload, dict):
        raise LlmTransportError("openrouter returned non-object JSON body")
    return cast(_ChatCompletion, payload)


def _parse_completion(payload: _ChatCompletion) -> LLMResponse:
    """Build an :class:`LLMResponse` from a non-streaming completion.

    Raises :class:`LlmTransportError` when the shape diverges from
    OpenAI-compatible expectations (no choices, missing content).
    """
    choices = payload.get("choices") or []
    if not choices:
        raise LlmTransportError("openrouter response contained no choices")

    first = choices[0]
    message = first.get("message")
    if message is None:
        raise LlmTransportError("openrouter response choice lacked a message")
    text = message.get("content", "")

    finish_reason_raw = first.get("finish_reason")
    finish_reason = finish_reason_raw if finish_reason_raw is not None else "stop"

    usage_raw = payload.get("usage") or _Usage()
    usage = LLMUsage(
        prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
        total_tokens=int(usage_raw.get("total_tokens", 0) or 0),
    )

    # Prefer the model echoed back by the provider; some routes rewrite
    # the requested id (e.g. ``:free`` suffix is stripped server-side).
    model_id = payload.get("model", "") or ""

    return LLMResponse(
        text=text,
        usage=usage,
        model_id=model_id,
        finish_reason=finish_reason,
    )


def _parse_sse_line(line: str) -> str | None:
    """Decode one SSE line into either a chunk, ``__done__``, or ``None``.

    Returns:

    * ``None`` for blank lines / frames without ``delta.content``.
    * ``"__done__"`` (sentinel string) on ``data: [DONE]``. The caller
      checks identity against this exact string and then returns.
    * A non-empty string otherwise: the chunk's ``delta.content``.
    """
    if not line or not line.startswith(_SSE_DATA_PREFIX):
        return None
    payload_str = line[len(_SSE_DATA_PREFIX) :].strip()
    if not payload_str:
        return None
    if payload_str == _SSE_DONE_SENTINEL:
        return "__done__"
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        # A malformed frame is logged and skipped — OpenRouter has
        # been observed to emit occasional keep-alive comments that
        # decode as data lines on buggy proxies. Rather than tear the
        # whole stream down we drop the frame.
        _log.warning("openrouter emitted non-JSON SSE frame; skipping")
        return None
    if not isinstance(payload, dict):
        return None

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    if not isinstance(content, str) or not content:
        return None
    return content


def _safe_error_detail(response: httpx.Response) -> str:
    """Return a short, log-safe summary of an error response body.

    Never includes the API key — the key is only ever in the request
    headers we set, and ``httpx`` does not echo request headers back
    in ``Response`` objects.
    """
    try:
        body = response.json()
    except json.JSONDecodeError:
        return response.text[:200]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:200]
        if isinstance(error, str):
            return error[:200]
    return response.text[:200]


def _backoff_seconds(attempt_idx: int) -> float:
    """Look up the sleep duration for retry ``attempt_idx``."""
    if attempt_idx < len(_BACKOFF_SCHEDULE):
        return _BACKOFF_SCHEDULE[attempt_idx]
    return _BACKOFF_SCHEDULE[-1]


def _elapsed_ms(started: object, ended: object) -> int:
    """Return milliseconds between two :class:`~datetime.datetime` instants.

    ``object`` types keep this helper free of a ``datetime`` import at
    the signature level — the callers always pass aware UTC datetimes
    because :class:`~app.util.clock.Clock` guarantees it.
    """
    from datetime import datetime as _dt

    if not isinstance(started, _dt) or not isinstance(ended, _dt):
        return 0
    delta = ended - started
    return int(delta.total_seconds() * 1000)
