"""Sync HTTP client used by every codegen command and override.

:class:`CrewdayClient` wraps :class:`httpx.Client` (sync — Click is sync,
the future async pivot can wrap this surface) and centralises five
behaviours every CLI command needs:

* **Auth** — adds ``Authorization: Bearer <token>`` from the active
  profile (§13 "Auth & profiles").
* **Workspace addressing** — adds ``X-Workspace: <slug>`` for verbs that
  target a workspace (§01 "Workspace addressing", §13 "Global flags").
* **Retries** — three attempts on transient transport / 5xx-gateway
  errors with exponential backoff + jitter, *only* when the verb is
  idempotent or the caller supplied an explicit ``Idempotency-Key``
  (§13 "Retries", §12 "Idempotency"). A blind retry on POST without
  an idempotency key would risk double-creating a row.
* **Streaming** — :meth:`CrewdayClient.download` honours ``Range`` for
  resume, so a partial download (large evidence file, payslip PDF)
  picks up where it left off.
* **Pagination** — :meth:`CrewdayClient.iterate` walks the cursor
  envelope from §12 "Pagination" until ``has_more=False``.

Errors are raised as :class:`ApiError` (a :class:`CrewdayError` subclass
so :func:`crewday._main.handle_errors` already maps it to the right
spec §13 "Exit codes" slot). The error carries the canonical ``code``
(parsed from the problem+json ``type`` URI), the human ``message``, and
the structured ``details`` payload.

See ``docs/specs/13-cli.md`` §"HTTP client", §"Retries", §"Streaming",
§"Pagination" and ``docs/specs/12-rest-api.md`` §"Auth", §"Pagination",
§"Errors", §"Idempotency".
"""

from __future__ import annotations

import logging
import pathlib
import random
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from types import TracebackType
from typing import Any, Final

import httpx

from crewday._main import (
    ConfigError,
    CrewdayError,
    ExitCode,
    ServerError,
)

__all__ = [
    "ApiError",
    "CrewdayClient",
]


# Spec §12 "Pagination": ``limit`` default 50, max 500. We default to 50
# on iterate(); callers can override per-call.
_DEFAULT_PAGE_LIMIT: Final[int] = 50

# Spec §13 "Retries": three attempts on connect / 5xx-gateway / read
# timeout. Base 250 ms, factor 2, jitter 0..0.5x (so a 1 s sleep can be
# up to 1.5 s).
_MAX_ATTEMPTS: Final[int] = 3
_BACKOFF_BASE_SECONDS: Final[float] = 0.25
_BACKOFF_FACTOR: Final[float] = 2.0
_BACKOFF_JITTER: Final[float] = 0.5

# HTTP statuses we consider transient (gateway-class). 429 is *not* on
# this list deliberately — the spec models 429 as a hard
# :class:`RateLimited` exit (§13 "Exit codes" slot 4); we let the caller
# decide whether to back off on the response's ``Retry-After`` rather
# than blindly burning the retry budget.
_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({502, 503, 504})

# Verbs that are idempotent by HTTP semantics; safe to retry without an
# explicit ``Idempotency-Key``.
_IDEMPOTENT_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

# Transport-level exceptions we treat as transient. ``ConnectError`` and
# ``ConnectTimeout`` are subclasses of one another in httpx's hierarchy so
# the connect family is covered by ``ConnectError`` alone; ``ReadTimeout``
# / ``WriteTimeout`` / ``PoolTimeout`` cover the timeout flavours; and
# ``RemoteProtocolError`` covers the case where a load balancer or
# upstream cleanly tears the TCP connection down mid-response (very
# common on idle keep-alive sockets). Anything outside this tuple is a
# genuine local bug (e.g. ``LocalProtocolError``, ``UnsupportedProtocol``)
# and should propagate as an unhandled exception so the operator sees it.
#
# Typed against ``Exception`` (not ``BaseException``) so the ``except``
# clause narrows to the concrete httpx hierarchy and ``last_exception``
# stays a plain ``Exception | None``.
_TRANSIENT_TRANSPORT_EXCEPTIONS: Final[tuple[type[Exception], ...]] = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

# Canonical ``type`` URI prefix used by the server's problem+json
# envelope (mirrors ``app.domain.errors.CANONICAL_TYPE_BASE``). The CLI
# can't import that constant directly — the import-linter contract
# forbids ``crewday`` reaching into ``app.domain`` — so we duplicate the
# literal here. A drift between this and the server is caught by the
# CLI's contract tests against the real OpenAPI surface (cd-uky5).
_CANONICAL_TYPE_BASE: Final[str] = "https://crewday.dev/errors/"

# ``code`` value when the server response cannot be parsed as
# problem+json. Stable so callers can branch on it without scraping
# message text.
_FALLBACK_ERROR_CODE: Final[str] = "http_error"

# Approval-pipeline error code (§11). Returned with status 409; the CLI
# maps it to ExitCode.APPROVAL_PENDING (3) so an agent's automation can
# distinguish "needs human approval" from a regular conflict.
_APPROVAL_REQUIRED_CODE: Final[str] = "approval_required"

_log = logging.getLogger("crewday.client")


def _resolve_user_agent(default: str) -> str:
    """Build the ``User-Agent`` value for outbound requests.

    Reads the installed package version via :mod:`importlib.metadata`;
    falls back to the caller-supplied ``default`` when the package isn't
    installed (editable / virtual checkout). Mirrors
    :func:`crewday._main._resolve_version`'s pattern so the CLI version
    in ``--version`` and ``User-Agent`` stay in sync.
    """
    try:
        return f"crewday-cli/{_pkg_version('crewday')}"
    except PackageNotFoundError:
        return default


def _exit_code_for_status(status: int, code: str) -> int:
    """Map an HTTP status + canonical code onto a §13 exit-code slot.

    The mapping is intentionally narrow: 401/403 stay on the generic
    client-error slot (1), 429 maps to the rate-limit slot (4), 5xx maps
    to the server-error slot (2), and the special-case 409
    ``approval_required`` (§11 "Approval pipeline") maps to slot 3 so an
    agent can branch on it. Anything else falls through to the generic
    client-error slot, matching the :class:`CrewdayError` default.
    """
    if status == 429:
        return ExitCode.RATE_LIMITED
    if status >= 500:
        return ExitCode.SERVER_ERROR
    if status == 409 and code == _APPROVAL_REQUIRED_CODE:
        return ExitCode.APPROVAL_PENDING
    return ExitCode.CLIENT_ERROR


class ApiError(CrewdayError):
    """Raised on a non-2xx response after retries are exhausted.

    Carries the structured error envelope from §12 "Errors":

    * ``status`` — HTTP status as returned by the server.
    * ``code`` — canonical short name (last segment of the
      ``type`` URI), e.g. ``"validation"``, ``"not_found"``,
      ``"idempotency_conflict"``. Falls back to ``"http_error"`` when
      the body is not parseable as problem+json.
    * ``message`` — human-readable detail; mirrors ``detail`` (or
      ``title`` when ``detail`` is absent) from the envelope.
    * ``details`` — the full structured payload (``errors[]``, any
      extra fields). ``None`` when the body could not be parsed.

    The instance's ``exit_code`` is set per :func:`_exit_code_for_status`
    so :func:`crewday._main.handle_errors` (Click's own exit-code
    plumbing) routes the failure to the right §13 slot.
    """

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        # Click's :class:`ClickException.__init__` takes a single
        # message string; we keep that contract so ``--verbose`` traces
        # render the way Click expects.
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = dict(details) if details is not None else None
        # Override the class-level exit_code with a per-instance value
        # derived from the response status. Click reads ``self.exit_code``
        # so an instance attribute wins over the class attribute.
        self.exit_code = _exit_code_for_status(status, code)


def _parse_error_body(
    response: httpx.Response,
) -> tuple[str, str, dict[str, Any] | None]:
    """Extract ``(code, message, details)`` from a non-2xx response.

    Spec §12 "Errors" emits problem+json with ``type``, ``title``,
    ``detail``, ``errors[]``. We derive ``code`` from the trailing slug
    of the ``type`` URI (``https://crewday.dev/errors/<code>``); the
    server pins this prefix and a clean parse is the common case. When
    the body isn't JSON or doesn't carry the expected keys we fall back
    to ``code="http_error"`` and trim the body to 200 chars so a binary
    payload doesn't flood stderr.
    """
    raw_text = response.text
    try:
        body: Any = response.json()
    except (ValueError, UnicodeDecodeError):
        return _FALLBACK_ERROR_CODE, (raw_text or response.reason_phrase)[:200], None

    if not isinstance(body, dict):
        return _FALLBACK_ERROR_CODE, raw_text[:200], None

    type_uri = body.get("type")
    if isinstance(type_uri, str) and type_uri.startswith(_CANONICAL_TYPE_BASE):
        code = type_uri[len(_CANONICAL_TYPE_BASE) :] or _FALLBACK_ERROR_CODE
    else:
        # Server emitted a non-canonical envelope (older service, custom
        # error). Use the literal ``error`` field if present, else fall
        # back to the generic code so callers can still branch.
        error_field = body.get("error")
        code = (
            error_field
            if isinstance(error_field, str) and error_field
            else _FALLBACK_ERROR_CODE
        )

    detail = body.get("detail")
    title = body.get("title")
    explicit_message = body.get("message")
    if isinstance(detail, str) and detail:
        message = detail
    elif isinstance(explicit_message, str) and explicit_message:
        message = explicit_message
    elif isinstance(title, str) and title:
        message = title
    else:
        message = response.reason_phrase or f"HTTP {response.status_code}"

    return code, message, body


def _should_retry(
    *,
    method: str,
    has_idempotency_key: bool,
    exception: BaseException | None,
    response: httpx.Response | None,
) -> bool:
    """Decide whether the request should be re-attempted.

    Two gates: the *eligibility* gate (HTTP method or idempotency
    header) and the *signal* gate (transient transport error or 5xx
    gateway). Both must pass.

    DELETE without an idempotency key is *never* retried: a transient
    success-after-failure could double-delete a sibling row that the
    server creates between attempts (§13 "Retries").
    """
    method_upper = method.upper()
    if method_upper not in _IDEMPOTENT_METHODS and not has_idempotency_key:
        return False
    if exception is not None:
        return isinstance(exception, _TRANSIENT_TRANSPORT_EXCEPTIONS)
    if response is not None:
        return response.status_code in _RETRYABLE_STATUSES
    return False


class CrewdayClient(AbstractContextManager["CrewdayClient"]):
    """Sync HTTP client wrapping :class:`httpx.Client`.

    Constructed once per ``crewday`` invocation from the active profile
    (base URL + token) and the global ``--workspace`` slug. Every
    request inherits the auth + workspace + user-agent headers; per-call
    overrides go through ``json``, ``params``, and ``idempotency_key``.

    Tests inject :class:`httpx.MockTransport` via the ``transport``
    parameter to skip the real network — see ``cli/tests/test_client.py``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        workspace: str | None = None,
        user_agent: str = "crewday-cli",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        rng: random.Random | None = None,
        sleep: object = time.sleep,
    ) -> None:
        if not base_url:
            # Profile resolution should reject empty base URLs; this is
            # a defence-in-depth guard so a misconfigured profile doesn't
            # silently send requests to the wrong host.
            raise ConfigError("base_url is required for CrewdayClient")
        self._base_url = base_url
        self._token = token
        self._workspace = workspace
        self._user_agent = _resolve_user_agent(user_agent)
        # ``rng`` is injectable so tests can pin jitter; default RNG is
        # the module random because retries don't need cryptographic
        # randomness — just enough variance to avoid thundering-herd
        # alignment across many CLI invocations.
        self._rng = rng if rng is not None else random.Random()
        # ``sleep`` is parameterised on a callable so tests can stub it
        # to a no-op without touching ``time``. Typed as ``object`` and
        # narrowed at the call site — mypy strict refuses
        # ``Callable[[float], None]`` defaulted to ``time.sleep`` because
        # ``time.sleep`` is overloaded.
        self._sleep = sleep

        headers: dict[str, str] = {"User-Agent": self._user_agent}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if workspace:
            headers["X-Workspace"] = workspace

        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    # -- lifecycle ---------------------------------------------------

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> CrewdayClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- core request loop ------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        params: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        stream: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send a single request with retry + structured error mapping.

        Retries are governed by :func:`_should_retry`; a successful 2xx
        response is returned verbatim. Non-2xx after the retry budget
        raises :class:`ApiError`. ``stream=True`` returns an active
        streaming response — the caller is responsible for closing it
        (and the body); :meth:`download` handles that path.

        ``extra_headers`` is the seam for per-call additions (e.g.
        ``Range`` from :meth:`download`). Auth / workspace / user-agent
        headers are inherited from the constructor and cannot be
        overridden here — that's intentional, profile resolution is the
        sole source of truth.
        """
        method_upper = method.upper()

        # Build the per-call header set. ``Idempotency-Key`` is
        # forwarded verbatim (§12 "Idempotency": opaque ASCII, server
        # validates length).
        headers: dict[str, str] = dict(extra_headers) if extra_headers else {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        # Each iteration of the loop either ``return``s a 2xx response,
        # raises (transport budget exhausted or non-retriable HTTP
        # error), or ``continue``s into the next attempt. The body is
        # therefore total — there is no fall-through past the loop, and
        # the final attempt always lands on a raising branch because
        # ``attempt < _MAX_ATTEMPTS`` is False.
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            is_final_attempt = attempt >= _MAX_ATTEMPTS
            try:
                if stream:
                    # ``send`` + ``stream=True`` keeps the response open
                    # so the caller can iterate ``iter_bytes``. The
                    # caller (or :meth:`download`) is responsible for
                    # closing it.
                    request_obj = self._client.build_request(
                        method_upper,
                        path,
                        json=json,
                        params=params,
                        headers=headers or None,
                    )
                    response = self._client.send(request_obj, stream=True)
                else:
                    response = self._client.request(
                        method_upper,
                        path,
                        json=json,
                        params=params,
                        headers=headers or None,
                    )
            except _TRANSIENT_TRANSPORT_EXCEPTIONS as exc:
                self._log_attempt(
                    method=method_upper,
                    path=path,
                    attempt=attempt,
                    status=None,
                    correlation_id=None,
                    exception=exc,
                )
                if not is_final_attempt and _should_retry(
                    method=method_upper,
                    has_idempotency_key=idempotency_key is not None,
                    exception=exc,
                    response=None,
                ):
                    self._sleep_backoff(attempt)
                    continue
                # Out of retries (or the verb is not retryable). Raise a
                # transport-level :class:`ServerError` so the CLI exits
                # on the server-error slot (§13 "Exit codes" 2).
                raise ServerError(
                    f"transport error after {attempt} attempt(s): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            # Spec §12 "Errors": the server echoes inbound
            # ``X-Correlation-Id`` and ``X-Request-Id`` verbatim; when the
            # CLI didn't send a correlation id the server still generates
            # one and surfaces it via ``X-Correlation-Id-Echo`` (so a
            # proxy chain can tie request → response back together). Walk
            # all three so debug logs always carry the trace anchor.
            correlation_id = (
                response.headers.get("X-Correlation-Id")
                or response.headers.get("X-Correlation-Id-Echo")
                or response.headers.get("X-Request-Id")
            )
            self._log_attempt(
                method=method_upper,
                path=path,
                attempt=attempt,
                status=response.status_code,
                correlation_id=correlation_id,
                exception=None,
            )

            if 200 <= response.status_code < 300:
                return response

            # Streaming + non-2xx: drain the body so we can parse the
            # error envelope. The transport otherwise leaves the
            # connection mid-flight.
            if stream:
                response.read()

            if not is_final_attempt and _should_retry(
                method=method_upper,
                has_idempotency_key=idempotency_key is not None,
                exception=None,
                response=response,
            ):
                # Close the response before retrying so we don't leak
                # the underlying connection.
                response.close()
                self._sleep_backoff(attempt)
                continue

            # Non-2xx, no more retries: parse the envelope and raise.
            code, message, details = _parse_error_body(response)
            response.close()
            raise ApiError(
                status=response.status_code,
                code=code,
                message=message,
                details=details,
            )

        # The for-loop body ``return``s, ``raise``s, or ``continue``s on
        # every iteration; falling through means a logic bug somewhere
        # above. Surface it as a server error rather than letting the
        # function silently return ``None``.
        raise ServerError(  # pragma: no cover — defensive guard.
            "request loop exited without producing a response (logic bug)"
        )

    # -- verb shortcuts ----------------------------------------------

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json: object | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        return self.request("POST", path, json=json, idempotency_key=idempotency_key)

    def patch(
        self,
        path: str,
        *,
        json: object | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        return self.request("PATCH", path, json=json, idempotency_key=idempotency_key)

    def delete(
        self,
        path: str,
        *,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        return self.request("DELETE", path, idempotency_key=idempotency_key)

    # -- pagination --------------------------------------------------

    def iterate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield each row from a cursor-paginated list endpoint.

        Walks the §12 "Pagination" envelope ``{data, next_cursor,
        has_more}``. Older endpoints that return a bare JSON list are
        supported as a single-page degenerate case — a server-side
        migration to the envelope is non-breaking under this iterator.
        """
        next_params: dict[str, Any] = dict(params) if params else {}
        next_params.setdefault("limit", _DEFAULT_PAGE_LIMIT)

        while True:
            response = self.get(path, params=next_params)
            # Server contract violations on a 2xx response (non-JSON body,
            # wrong envelope shape) are *server* faults: surface them as
            # ``ServerError`` so the §13 exit code lands on slot 2
            # (server) rather than slot 1 (client). Raising
            # ``ApiError(status=200)`` would also work but is misleading
            # — ApiError represents non-2xx by construction.
            try:
                payload: Any = response.json()
            except ValueError as exc:
                raise ServerError(
                    f"non-JSON response on paginated GET {path}"
                ) from exc

            if isinstance(payload, list):
                # Bare list: single-page degenerate case. Yield each row
                # and stop — there's no cursor to follow.
                for row in payload:
                    if isinstance(row, dict):
                        yield row
                return

            if not isinstance(payload, dict):
                raise ServerError(
                    f"unexpected pagination payload on GET {path}: "
                    f"expected list or {{data, next_cursor, has_more}}"
                )

            data = payload.get("data", [])
            if not isinstance(data, list):
                raise ServerError(
                    f"pagination 'data' must be a list on GET {path}"
                )
            for row in data:
                if isinstance(row, dict):
                    yield row

            has_more = bool(payload.get("has_more", False))
            next_cursor = payload.get("next_cursor")
            if not has_more or not next_cursor:
                return
            next_params["cursor"] = next_cursor

    # -- streaming ---------------------------------------------------

    def download(
        self,
        path: str,
        dest: pathlib.Path,
        *,
        chunk_size: int = 64 * 1024,
    ) -> int:
        """Stream the response body to ``dest`` with Range resume.

        If ``dest`` already exists, sends ``Range: bytes=<size>-`` and
        appends the response body. The server may answer:

        * ``206 Partial Content`` — append; total returned includes the
          pre-existing prefix.
        * ``200 OK`` — full body (server ignored Range, e.g. resource
          changed). Truncate ``dest`` and write fresh.

        Any other status (including 416 Range Not Satisfiable) raises
        :class:`ApiError`. Any 2xx other than 200/206 — for example a
        bare 204 No Content — is also rejected: silently truncating the
        existing partial would discard a download we already paid for,
        and the spec only documents 200/206 as the streaming success
        shapes (§12 ``files.blob``).
        """
        existing_size = dest.stat().st_size if dest.exists() else 0
        extra_headers: dict[str, str] | None = None
        if existing_size > 0:
            extra_headers = {"Range": f"bytes={existing_size}-"}

        response = self.request(
            "GET",
            path,
            stream=True,
            extra_headers=extra_headers,
        )

        # ``request`` returns 2xx only; anything else has already
        # raised. 200 vs 206 governs whether we append or truncate;
        # any other 2xx is unexpected for a streaming download.
        try:
            status = response.status_code
            if status not in (200, 206):
                raise ServerError(
                    f"unexpected {status} response on streaming GET {path}: "
                    "expected 200 OK or 206 Partial Content"
                )
            mode = "ab" if status == 206 and existing_size > 0 else "wb"
            written = 0
            with dest.open(mode) as fh:
                for chunk in response.iter_bytes(chunk_size):
                    if chunk:
                        fh.write(chunk)
                        written += len(chunk)
            if mode == "ab":
                return existing_size + written
            return written
        finally:
            response.close()

    # -- internal helpers --------------------------------------------

    def _sleep_backoff(self, attempt: int) -> None:
        """Sleep ``base * factor^(attempt-1) * (1 + uniform(0, jitter))``.

        Bounded multiplicatively so the worst-case sleep on a 3-attempt
        budget is roughly ``0.25 * 4 * 1.5 = 1.5 s`` between attempts 2
        and 3 — short enough to feel responsive, long enough to clear a
        gateway brown-out.
        """
        base_sleep = _BACKOFF_BASE_SECONDS * (_BACKOFF_FACTOR ** (attempt - 1))
        jitter = self._rng.uniform(0.0, _BACKOFF_JITTER)
        # mypy strict: ``self._sleep`` is typed as ``object`` so callers
        # can stub time.sleep without dragging its overloaded signature
        # in; narrow at the call site.
        sleep_fn = self._sleep
        if not callable(sleep_fn):
            raise TypeError("sleep callable was replaced with non-callable")
        sleep_fn(base_sleep * (1.0 + jitter))

    def _log_attempt(
        self,
        *,
        method: str,
        path: str,
        attempt: int,
        status: int | None,
        correlation_id: str | None,
        exception: BaseException | None,
    ) -> None:
        """Emit a DEBUG line per request attempt.

        Format keeps PII out by construction: only the method, path,
        attempt number, status, correlation id, and exception class
        name are included. Bodies and headers stay out of the log.
        """
        if exception is not None:
            _log.debug(
                "request attempt=%d method=%s path=%s exception=%s",
                attempt,
                method,
                path,
                type(exception).__name__,
            )
            return
        _log.debug(
            "request attempt=%d method=%s path=%s status=%s correlation_id=%s",
            attempt,
            method,
            path,
            status,
            correlation_id or "-",
        )


