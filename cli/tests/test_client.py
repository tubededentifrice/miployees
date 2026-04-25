"""Unit tests for :mod:`crewday._client`.

Every scenario uses :class:`httpx.MockTransport` to route requests
through a per-test handler — no network, no fixtures, no
``pytest-httpx`` dependency. Retries are made deterministic by
injecting a seeded :class:`random.Random` and stubbing ``time.sleep``
to a no-op so the test suite stays fast.

Coverage maps to the §13 "HTTP client" / "Retries" / "Streaming" /
"Pagination" spec sections plus the §12 "Errors" / "Idempotency"
contracts.
"""

from __future__ import annotations

import json as json_lib
import logging
import pathlib
import random
from typing import Any

import httpx
import pytest
from crewday._client import ApiError, CrewdayClient
from crewday._main import ConfigError, ExitCode, ServerError


def _no_sleep(_seconds: float) -> None:
    """Drop-in replacement for :func:`time.sleep` used in retry paths.

    Tests never need real sleep; pinning to a no-op turns a 3-attempt
    backoff sequence from ~1.5 s of wall clock into microseconds.
    """
    return None


def _debug_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Collect rendered DEBUG records as plain strings.

    The correlation-id assertions all want the same shape — pulling it
    out keeps each test focused on the *what* (the id surfaces) rather
    than the boilerplate of filtering log records.
    """
    return [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]


def _make_client(
    handler: Any,
    *,
    token: str | None = "test-token",
    workspace: str | None = "smoke",
    base_url: str = "https://api.test.local",
) -> CrewdayClient:
    """Build a :class:`CrewdayClient` wired to a :class:`MockTransport`.

    The ``rng`` is seeded so jitter is reproducible across runs (not
    that the tests inspect it; deterministic seeding just means a
    flaky-test bisect can replay state). ``_no_sleep`` neutralises the
    retry backoff so no test pays for real wall time.
    """
    transport = httpx.MockTransport(handler)
    return CrewdayClient(
        base_url=base_url,
        token=token,
        workspace=workspace,
        transport=transport,
        rng=random.Random(0),
        sleep=_no_sleep,
    )


# ---------------------------------------------------------------------------
# Auth, workspace, user-agent headers
# ---------------------------------------------------------------------------


def test_bearer_token_header_when_set() -> None:
    """``Authorization: Bearer <token>`` is sent when the profile has a token."""
    captured: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler, token="abc123") as client:
        client.get("/api/v1/whoami")

    assert captured["headers"]["Authorization"] == "Bearer abc123"


def test_no_auth_header_when_token_unset() -> None:
    """``Authorization`` is absent when the active profile has no token.

    Anonymous endpoints (login, magic-link redeem) must work without
    leaking the previous user's token.
    """
    captured: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler, token=None, workspace=None) as client:
        client.get("/api/v1/auth/login")

    assert "authorization" not in {k.lower() for k in captured["headers"]}


def test_workspace_header_when_set() -> None:
    """``X-Workspace`` is sent when the global ``--workspace`` is set."""
    captured: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler, workspace="acme") as client:
        client.get("/w/acme/api/v1/tasks")

    assert captured["headers"]["X-Workspace"] == "acme"


def test_no_workspace_header_when_unset() -> None:
    """``X-Workspace`` is absent for host-level verbs (``auth``, ``admin``)."""
    captured: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler, workspace=None) as client:
        client.get("/api/v1/admin/health")

    assert "x-workspace" not in {k.lower() for k in captured["headers"]}


def test_user_agent_always_sent() -> None:
    """``User-Agent`` always includes ``crewday-cli`` plus a version
    suffix (``crewday-cli/<version>``). The suffix lets server-side
    log analytics group requests by client version when an old CLI
    starts misbehaving against a newer API."""
    captured: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler, token=None, workspace=None) as client:
        client.get("/api/v1/whoami")

    user_agent = captured["headers"]["User-Agent"]
    assert user_agent.startswith("crewday-cli"), user_agent
    # Either the installed-package shape ``crewday-cli/<version>`` or
    # the editable-checkout fallback (the literal ``crewday-cli``).
    # In both cases the prefix matches; we only require the slash form
    # when a version is resolvable so we don't pin to a specific
    # release.
    if "/" in user_agent:
        prefix, _, version = user_agent.partition("/")
        assert prefix == "crewday-cli"
        assert version, "User-Agent has '/' separator but no version segment"


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_get_retries_on_503_until_success() -> None:
    """Idempotent GET retries on 503 up to 3 attempts; 2 transient + 1 success."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(
                503,
                headers={"Content-Type": "application/problem+json"},
                content=b'{"type":"https://crewday.dev/errors/upstream_unavailable",'
                b'"title":"Gateway","detail":"upstream is down"}',
            )
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler) as client:
        response = client.get("/api/v1/tasks")

    assert len(attempts) == 3
    assert response.status_code == 200


def test_get_does_not_retry_on_401() -> None:
    """401 is non-transient; one attempt, ApiError(status=401) raised."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(
            401,
            headers={"Content-Type": "application/problem+json"},
            content=b'{"type":"https://crewday.dev/errors/unauthorized",'
            b'"title":"Unauthorized","detail":"missing bearer token"}',
        )

    with _make_client(handler) as client, pytest.raises(ApiError) as exc_info:
        client.get("/api/v1/whoami")

    assert len(attempts) == 1
    assert exc_info.value.status == 401
    assert exc_info.value.code == "unauthorized"


def test_post_without_idempotency_key_does_not_retry_on_502() -> None:
    """POST without an idempotency key is *not* retried — could double-create."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(
            502,
            headers={"Content-Type": "application/problem+json"},
            content=b'{"type":"https://crewday.dev/errors/upstream_unavailable",'
            b'"title":"Bad Gateway","detail":"upstream timeout"}',
        )

    with _make_client(handler) as client, pytest.raises(ApiError) as exc_info:
        client.post("/api/v1/tasks", json={"title": "foo"})

    assert len(attempts) == 1
    assert exc_info.value.status == 502


def test_post_with_idempotency_key_retries_on_503() -> None:
    """POST WITH ``Idempotency-Key`` retries on 503; 2 failures + 1 success."""
    attempts: list[int] = []
    sent_keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        sent_keys.append(request.headers.get("Idempotency-Key"))
        if len(attempts) < 3:
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(201, json={"id": "task_123"})

    with _make_client(handler) as client:
        response = client.post(
            "/api/v1/tasks",
            json={"title": "foo"},
            idempotency_key="ulid-key-1",
        )

    assert len(attempts) == 3
    # Idempotency-Key forwarded on every retry — the server uses it to
    # dedup the replay (§12 "Idempotency").
    assert sent_keys == ["ulid-key-1", "ulid-key-1", "ulid-key-1"]
    assert response.status_code == 201


def test_delete_never_retries_on_503() -> None:
    """DELETE without an idempotency key never retries: a transient
    success-after-failure could double-delete a sibling row created
    between attempts (§13 "Retries")."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, json={"detail": "down"})

    with _make_client(handler) as client, pytest.raises(ApiError):
        client.delete("/api/v1/tasks/abc")

    assert len(attempts) == 1


def test_delete_with_idempotency_key_retries() -> None:
    """DELETE *with* an explicit idempotency key may retry — the caller
    has signalled the server can dedupe the replay safely."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, json={"detail": "down"})
        return httpx.Response(204)

    with _make_client(handler) as client:
        response = client.delete("/api/v1/tasks/abc", idempotency_key="key-1")

    assert len(attempts) == 3
    assert response.status_code == 204


def test_get_retries_on_connect_error() -> None:
    """Transport-level :class:`ConnectError` is retried for idempotent verbs."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler) as client:
        response = client.get("/api/v1/tasks")

    assert len(attempts) == 3
    assert response.status_code == 200


def test_get_raises_server_error_when_connect_keeps_failing() -> None:
    """After 3 transport failures, raise :class:`ServerError` (§13 exit 2)."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("connection refused")

    with _make_client(handler) as client, pytest.raises(ServerError) as exc_info:
        client.get("/api/v1/tasks")

    assert len(attempts) == 3
    assert exc_info.value.exit_code == ExitCode.SERVER_ERROR


def test_get_retries_on_remote_protocol_error() -> None:
    """``RemoteProtocolError`` (server cleanly tore the keep-alive socket
    down) is treated as transient for idempotent verbs — keep-alive
    socket reuse against an Nginx/Pangolin upstream is the canonical
    case where this surfaces in production."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.RemoteProtocolError(
                "server disconnected without sending a response"
            )
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler) as client:
        response = client.get("/api/v1/tasks")

    assert len(attempts) == 3
    assert response.status_code == 200


def test_post_with_idempotency_key_retries_on_connect_error() -> None:
    """Symmetric to GET: a POST with an explicit ``Idempotency-Key`` is
    eligible for transport-level retry. The server uses the key to
    dedupe the replay safely (§12 "Idempotency")."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(201, json={"id": "task_42"})

    with _make_client(handler) as client:
        response = client.post(
            "/api/v1/tasks",
            json={"title": "foo"},
            idempotency_key="ulid-key-42",
        )

    assert len(attempts) == 3
    assert response.status_code == 201


# ---------------------------------------------------------------------------
# Correlation-id propagation
# ---------------------------------------------------------------------------


def test_correlation_id_logged_from_response_header(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``X-Correlation-Id`` header echoed by the server surfaces in
    the DEBUG request-attempt log so an operator can grep workspace
    logs by correlation id (§12 "Errors", §15 audit trail)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Correlation-Id": "01HK0AAAAAAAAAAAAAAAAAAAAA"},
            json={"ok": True},
        )

    caplog.set_level(logging.DEBUG, logger="crewday.client")
    with _make_client(handler) as client:
        client.get("/api/v1/whoami")

    debug_messages = _debug_messages(caplog)
    assert any("01HK0AAAAAAAAAAAAAAAAAAAAA" in msg for msg in debug_messages), (
        f"correlation id missing from debug log: {debug_messages!r}"
    )


def test_correlation_id_falls_back_to_x_correlation_id_echo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the CLI didn't send a correlation id, the server generates
    one and surfaces it via ``X-Correlation-Id-Echo`` (§12 "Errors").
    The CLI must log that variant too — otherwise a server-generated
    correlation id is silently dropped."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Correlation-Id-Echo": "server-generated-01HK0BBB"},
            json={"ok": True},
        )

    caplog.set_level(logging.DEBUG, logger="crewday.client")
    with _make_client(handler) as client:
        client.get("/api/v1/whoami")

    debug_messages = _debug_messages(caplog)
    assert any("server-generated-01HK0BBB" in msg for msg in debug_messages), (
        f"X-Correlation-Id-Echo missing from debug log: {debug_messages!r}"
    )


def test_correlation_id_falls_back_to_x_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Some proxies surface only ``X-Request-Id``; we accept that too
    (spec §12 echoes both names so either side of a proxy chain
    sees one)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Request-Id": "req-id-7"},
            json={"ok": True},
        )

    caplog.set_level(logging.DEBUG, logger="crewday.client")
    with _make_client(handler) as client:
        client.get("/api/v1/tasks")

    debug_messages = _debug_messages(caplog)
    assert any("req-id-7" in msg for msg in debug_messages), (
        f"X-Request-Id missing from debug log: {debug_messages!r}"
    )


# ---------------------------------------------------------------------------
# Error envelope mapping
# ---------------------------------------------------------------------------


def test_api_error_carries_code_message_details_from_problem_json() -> None:
    """``code`` derives from the canonical ``type`` URI; ``details`` is the body."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            headers={"Content-Type": "application/problem+json"},
            content=json_lib.dumps(
                {
                    "type": "https://crewday.dev/errors/validation",
                    "title": "Validation error",
                    "status": 422,
                    "detail": "property_id must be provided",
                    "errors": [
                        {
                            "loc": ["body", "property_id"],
                            "msg": "field required",
                            "type": "missing",
                        }
                    ],
                }
            ).encode("utf-8"),
        )

    with _make_client(handler) as client, pytest.raises(ApiError) as exc_info:
        client.post(
            "/api/v1/tasks",
            json={"title": "foo"},
            idempotency_key="k",
        )

    err = exc_info.value
    assert err.status == 422
    assert err.code == "validation"
    assert err.message == "property_id must be provided"
    assert err.details is not None
    assert err.details["errors"][0]["loc"] == ["body", "property_id"]
    # Validation errors map to the generic client-error slot (§13).
    assert err.exit_code == ExitCode.CLIENT_ERROR


def test_api_error_falls_back_on_non_json_body() -> None:
    """When the body isn't problem+json (e.g. HTML 502 from a proxy),
    fall back to ``code='http_error'`` with a trimmed message."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"Content-Type": "text/html"},
            content=b"<html><body>500 Internal Server Error</body></html>",
        )

    with _make_client(handler) as client, pytest.raises(ApiError) as exc_info:
        client.get("/api/v1/tasks")

    err = exc_info.value
    assert err.status == 500
    assert err.code == "http_error"
    assert err.details is None
    # 5xx → server-error slot regardless of body shape.
    assert err.exit_code == ExitCode.SERVER_ERROR


def test_api_error_429_maps_to_rate_limited_exit() -> None:
    """A 429 response routes to :data:`ExitCode.RATE_LIMITED`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Content-Type": "application/problem+json"},
            content=b'{"type":"https://crewday.dev/errors/rate_limited",'
            b'"title":"Rate limited","detail":"slow down"}',
        )

    with _make_client(handler) as client, pytest.raises(ApiError) as exc_info:
        client.get("/api/v1/tasks")

    assert exc_info.value.exit_code == ExitCode.RATE_LIMITED


def test_api_error_approval_required_maps_to_approval_pending_exit() -> None:
    """409 ``approval_required`` routes to :data:`ExitCode.APPROVAL_PENDING`.

    Distinguishes a "needs human approval" outcome from a regular 409
    conflict so agents can branch on the exit code (§11 "Approval pipeline").
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={"Content-Type": "application/problem+json"},
            content=b'{"type":"https://crewday.dev/errors/approval_required",'
            b'"title":"Approval required","detail":"manager must approve"}',
        )

    with _make_client(handler) as client, pytest.raises(ApiError) as exc_info:
        client.post(
            "/api/v1/expenses",
            json={"amount_minor": 5000},
            idempotency_key="k",
        )

    assert exc_info.value.exit_code == ExitCode.APPROVAL_PENDING


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_iterate_follows_cursors_through_pages() -> None:
    """Three pages, cursor advances each time, last has ``has_more=False``."""
    pages = [
        {"data": [{"id": "1"}, {"id": "2"}], "next_cursor": "c1", "has_more": True},
        {"data": [{"id": "3"}, {"id": "4"}], "next_cursor": "c2", "has_more": True},
        {"data": [{"id": "5"}], "next_cursor": None, "has_more": False},
    ]
    seen_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        seen_cursors.append(cursor)
        if cursor is None:
            return httpx.Response(200, json=pages[0])
        if cursor == "c1":
            return httpx.Response(200, json=pages[1])
        if cursor == "c2":
            return httpx.Response(200, json=pages[2])
        raise AssertionError(f"unexpected cursor {cursor!r}")

    with _make_client(handler) as client:
        rows = list(client.iterate("/api/v1/tasks"))

    assert [r["id"] for r in rows] == ["1", "2", "3", "4", "5"]
    # First request has no cursor; subsequent two carry the previous
    # ``next_cursor``.
    assert seen_cursors == [None, "c1", "c2"]


def test_iterate_stops_when_has_more_is_false_even_with_cursor() -> None:
    """``has_more=False`` is the terminal signal — a stale ``next_cursor``
    on the same response must not trigger another fetch."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={
                "data": [{"id": "1"}],
                "next_cursor": "ignored",
                "has_more": False,
            },
        )

    with _make_client(handler) as client:
        rows = list(client.iterate("/api/v1/tasks"))

    assert len(calls) == 1
    assert rows == [{"id": "1"}]


def test_iterate_handles_bare_list_response() -> None:
    """Older endpoints returning a bare JSON list iterate as a single page."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "1"}, {"id": "2"}])

    with _make_client(handler) as client:
        rows = list(client.iterate("/api/v1/legacy"))

    assert [r["id"] for r in rows] == ["1", "2"]


def test_iterate_passes_default_limit() -> None:
    """``iterate`` defaults to the §12 spec ``limit=50``."""
    captured: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params.get("limit"))
        return httpx.Response(200, json={"data": [], "has_more": False})

    with _make_client(handler) as client:
        list(client.iterate("/api/v1/tasks"))

    assert captured == ["50"]


def test_iterate_respects_caller_limit() -> None:
    """A caller-supplied ``limit`` overrides the default."""
    captured: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params.get("limit"))
        return httpx.Response(200, json={"data": [], "has_more": False})

    with _make_client(handler) as client:
        list(client.iterate("/api/v1/tasks", params={"limit": 200}))

    assert captured == ["200"]


def test_iterate_raises_server_error_on_non_json_body() -> None:
    """A 200 OK with a non-JSON body is a server contract violation —
    surface as :class:`ServerError` (§13 exit 2). Raising an
    ``ApiError(status=200)`` would be misleading: callers branch on
    ``status >= 400`` to decide whether the *server* misbehaved."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b"<html>not json</html>",
        )

    with _make_client(handler) as client, pytest.raises(ServerError) as exc_info:
        list(client.iterate("/api/v1/tasks"))

    assert exc_info.value.exit_code == ExitCode.SERVER_ERROR


def test_iterate_raises_server_error_on_unexpected_payload_shape() -> None:
    """A 200 with valid JSON but neither a list nor an envelope dict is a
    server contract violation; surface as :class:`ServerError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json="not-a-list-or-dict")

    with _make_client(handler) as client, pytest.raises(ServerError) as exc_info:
        list(client.iterate("/api/v1/tasks"))

    assert exc_info.value.exit_code == ExitCode.SERVER_ERROR


def test_iterate_raises_server_error_when_data_field_is_not_list() -> None:
    """The ``data`` field must be a list per §12 "Pagination"; surfacing
    a non-list ``data`` as a server-side violation prevents downstream
    code from iterating something the server promised was iterable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"oops": "this is a dict"}, "has_more": False},
        )

    with _make_client(handler) as client, pytest.raises(ServerError) as exc_info:
        list(client.iterate("/api/v1/tasks"))

    assert exc_info.value.exit_code == ExitCode.SERVER_ERROR


# ---------------------------------------------------------------------------
# Streaming + Range resume
# ---------------------------------------------------------------------------


def test_download_writes_full_body(tmp_path: pathlib.Path) -> None:
    """Happy path: stream a body to disk, return the byte count."""
    body = b"x" * 200_000

    def handler(request: httpx.Request) -> httpx.Response:
        assert "range" not in {k.lower() for k in request.headers}
        return httpx.Response(200, content=body)

    dest = tmp_path / "evidence.jpg"
    with _make_client(handler) as client:
        written = client.download("/api/v1/blobs/abc", dest)

    assert written == len(body)
    assert dest.read_bytes() == body


def test_download_resumes_with_range_when_dest_exists(tmp_path: pathlib.Path) -> None:
    """Existing partial → ``Range: bytes=N-`` → 206 → append; total == N+body."""
    prefix = b"AAAA"
    suffix = b"BBBB"
    captured_range: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_range.append(request.headers.get("Range"))
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 4-7/8"},
            content=suffix,
        )

    dest = tmp_path / "evidence.jpg"
    dest.write_bytes(prefix)

    with _make_client(handler) as client:
        total = client.download("/api/v1/blobs/abc", dest)

    assert captured_range == ["bytes=4-"]
    assert total == len(prefix) + len(suffix)
    assert dest.read_bytes() == prefix + suffix


def test_download_truncates_when_server_returns_200_after_partial(
    tmp_path: pathlib.Path,
) -> None:
    """Partial on disk + server replies 200 → truncate, write fresh body.

    The server may ignore Range when the underlying resource changed
    between the partial download and the resume request; the only safe
    response is to discard the on-disk prefix and start over.
    """
    prefix = b"OLD-OLD"
    fresh = b"BRAND-NEW-BODY"

    def handler(request: httpx.Request) -> httpx.Response:
        # Server ignored Range and returned the full body.
        return httpx.Response(200, content=fresh)

    dest = tmp_path / "evidence.jpg"
    dest.write_bytes(prefix)

    with _make_client(handler) as client:
        total = client.download("/api/v1/blobs/abc", dest)

    assert total == len(fresh)
    assert dest.read_bytes() == fresh


def test_download_raises_api_error_on_416(tmp_path: pathlib.Path) -> None:
    """Server replies 416 Range Not Satisfiable → :class:`ApiError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            416,
            headers={"Content-Type": "application/problem+json"},
            content=b'{"type":"https://crewday.dev/errors/http_416",'
            b'"title":"Range Not Satisfiable","detail":"out of range"}',
        )

    dest = tmp_path / "evidence.jpg"
    dest.write_bytes(b"existing")

    with _make_client(handler) as client, pytest.raises(ApiError) as exc_info:
        client.download("/api/v1/blobs/abc", dest)

    assert exc_info.value.status == 416


def test_download_rejects_unexpected_2xx_status(tmp_path: pathlib.Path) -> None:
    """A 204 (or any 2xx other than 200/206) on a streaming download is
    refused: silently truncating the existing partial would discard a
    download we already paid for, and §12 ``files.blob`` only documents
    200/206 as success shapes. Surface as :class:`ServerError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    dest = tmp_path / "evidence.jpg"
    prefix = b"PARTIAL-PREFIX"
    dest.write_bytes(prefix)

    with _make_client(handler) as client, pytest.raises(ServerError):
        client.download("/api/v1/blobs/abc", dest)

    # Existing partial must NOT have been truncated by the failed
    # download attempt.
    assert dest.read_bytes() == prefix


# ---------------------------------------------------------------------------
# Idempotency-Key forwarding
# ---------------------------------------------------------------------------


def test_idempotency_key_forwarded_on_post() -> None:
    """The caller-supplied key is sent verbatim as ``Idempotency-Key``."""
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(201, json={"id": "x"})

    with _make_client(handler) as client:
        client.post(
            "/api/v1/tasks",
            json={"title": "foo"},
            idempotency_key="ulid-abc-123",
        )

    assert captured["key"] == "ulid-abc-123"


def test_idempotency_key_absent_when_not_set() -> None:
    """No ``Idempotency-Key`` header when the caller didn't provide one."""
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={})

    with _make_client(handler) as client:
        client.get("/api/v1/tasks")

    assert captured["key"] is None


# ---------------------------------------------------------------------------
# Constructor guards
# ---------------------------------------------------------------------------


def test_empty_base_url_raises_config_error() -> None:
    """Empty base URL is a profile-resolution bug; surface via ConfigError."""
    with pytest.raises(ConfigError):
        CrewdayClient(base_url="", token=None)


def test_context_manager_closes_underlying_client() -> None:
    """``__exit__`` closes the underlying :class:`httpx.Client`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler) as client:
        underlying = client._client
        assert not underlying.is_closed
    assert underlying.is_closed
