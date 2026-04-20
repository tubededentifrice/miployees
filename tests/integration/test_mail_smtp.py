"""End-to-end test for :class:`app.adapters.mail.smtp.SMTPMailer`.

Spins up a :class:`testcontainers.core.container.DockerContainer` with
the ``axllent/mailpit`` image — no auth, no TLS, just an SMTP listener
on ``:1025`` and an HTTP API on ``:8025``. We send one message through
the real :class:`SMTPMailer`, then poll Mailpit's
``/api/v1/messages`` endpoint and assert the delivered envelope
matches what we sent.

The test skips cleanly when Docker isn't reachable so dev hosts
without a daemon can still run the rest of the integration suite
(same pattern as ``test_schema_parity.py``).

See ``docs/specs/10-messaging-notifications.md`` and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from app.adapters.mail.smtp import SMTPMailer

pytestmark = pytest.mark.integration

_MAILPIT_IMAGE = "axllent/mailpit:latest"
_SMTP_PORT = 1025
_HTTP_PORT = 8025


def _fetch_messages(api_url: str) -> list[dict[str, Any]]:
    """Return the ``messages`` array from Mailpit's ``/api/v1/messages``.

    Mailpit's JSON shape — ``{"total": N, "messages": [...]}`` — is
    documented and stable; the runtime ``isinstance`` guard keeps mypy
    honest without pretending we know more than we do about
    third-party JSON.
    """
    with urllib.request.urlopen(f"{api_url}/api/v1/messages", timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Mailpit returned non-object payload: {payload!r}")
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        raise AssertionError(f"Mailpit 'messages' is not a list: {messages!r}")
    return [item for item in messages if isinstance(item, dict)]


def _fetch_headers(api_url: str, internal_id: str) -> dict[str, list[str]]:
    """Return Mailpit's per-message header map (``/api/v1/message/{id}/headers``).

    Mailpit normalises header keys to ``Canonical-Case`` and yields
    each header's values as a list of strings — exactly the shape the
    caller wants for ``Reply-To`` / ``X-…`` assertions.
    """
    with urllib.request.urlopen(
        f"{api_url}/api/v1/message/{internal_id}/headers", timeout=5
    ) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Mailpit headers payload is not a dict: {payload!r}")
    out: dict[str, list[str]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        str_values = [v for v in value if isinstance(v, str)]
        out[key] = str_values
    return out


def _wait_for_message(
    api_url: str,
    *,
    message_id: str,
    deadline_s: float = 10.0,
) -> dict[str, Any]:
    """Poll Mailpit until the envelope with ``message_id`` arrives, then return it.

    We key on each stored envelope's top-level ``MessageID`` field
    (the angle-brackets-stripped form Mailpit surfaces in its list
    endpoint) so the assertion is tied to *this* test's send — a
    future caller adding a second send in the same module doesn't
    accidentally assert against the wrong envelope.
    """
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        items = _fetch_messages(api_url)
        for item in items:
            if item.get("MessageID") == message_id:
                return item
        time.sleep(0.2)
    raise AssertionError(
        f"Mailpit at {api_url} never received the expected message "
        f"({message_id!r}) within {deadline_s}s"
    )


@pytest.fixture(scope="module")
def mailpit_container() -> Iterator[tuple[str, int, str]]:
    """Spin up Mailpit, yield ``(smtp_host, smtp_port, http_api_url)``.

    Uses the generic :class:`DockerContainer` rather than a
    community-maintained image-specific wrapper — Mailpit's shape
    (single binary, two ports, zero config) doesn't justify a bespoke
    class. Bind guard: Mailpit listens on ``0.0.0.0`` inside the
    container, but Docker only publishes on ``127.0.0.1`` by default
    when the host isn't told otherwise. ``testcontainers`` follows
    that default, so there's no way for the exposed ports to reach
    the public interface from the host side.

    Skips when Docker isn't reachable (no daemon, no perms, image
    pull failed) — matches the skip pattern in
    ``tests/integration/test_schema_parity.py``.
    """
    try:
        from testcontainers.core.container import DockerContainer
    except ImportError as exc:  # pragma: no cover - dep is in dev group
        pytest.skip(f"testcontainers not installed: {exc}")

    container = DockerContainer(_MAILPIT_IMAGE).with_exposed_ports(
        _SMTP_PORT, _HTTP_PORT
    )
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Docker/Mailpit container unavailable: {exc}")

    try:
        host = container.get_container_host_ip()
        smtp_port = int(container.get_exposed_port(_SMTP_PORT))
        http_port = int(container.get_exposed_port(_HTTP_PORT))
        api_url = f"http://{host}:{http_port}"

        # Wait for the HTTP API to answer — the container is up well
        # before Mailpit finishes binding its listeners, and urlopen
        # against a not-yet-open port throws :class:`ConnectionRefusedError`.
        _wait_for_http(api_url, deadline_s=15.0)

        yield host, smtp_port, api_url
    finally:
        container.stop()


def _wait_for_http(base_url: str, *, deadline_s: float) -> None:
    """Poll Mailpit's ``/livez`` until it returns 2xx or we time out."""
    end = time.monotonic() + deadline_s
    last_exc: BaseException | None = None
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(f"{base_url}/livez", timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"Mailpit HTTP API at {base_url} never came up in {deadline_s}s "
        f"(last error: {last_exc!r})"
    )


def test_send_delivers_envelope_to_mailpit(
    mailpit_container: tuple[str, int, str],
) -> None:
    """One :meth:`SMTPMailer.send` → one stored message with matching fields.

    Covers the full live-SMTP path end-to-end:

    * TCP + SMTP handshake succeeds against a real server.
    * ``EmailMessage`` serialises into bytes Mailpit's SMTP stack
      accepts (multipart/alternative included).
    * The envelope fields we set (From, To, Subject, Message-ID,
      Reply-To) and any custom headers (e.g. ``X-Crewday-Template``)
      round-trip through SMTP and land on the stored copy.
    * The returned message id matches the stored ``MessageID``.

    Note on ``Return-Path``: Mailpit rewrites that header to the SMTP
    envelope sender at ingest, so the bounce token we put on the
    outgoing message cannot be observed here. Round-tripping the
    bounce token belongs in an adapter-level test that inspects the
    serialised :class:`EmailMessage`, not in this sink-level check.
    """
    host, smtp_port, api_url = mailpit_container

    mailer = SMTPMailer(
        host=host,
        port=smtp_port,
        from_addr="crew.day <noreply@example.com>",
        user=None,
        password=None,
        use_tls=False,  # Mailpit has no TLS listener
        timeout=10,
    )

    returned_id = mailer.send(
        to=["alice@example.com"],
        subject="Hello from crew.day",
        body_text="Plain-text body for text-only clients.",
        body_html="<p>HTML body with <strong>markup</strong>.</p>",
        reply_to="ops@example.com",
        headers={"X-Crewday-Template": "test-message"},
    )
    assert returned_id
    assert returned_id.endswith("@example.com")

    stored = _wait_for_message(api_url, message_id=returned_id)

    # Mailpit's list endpoint exposes the envelope fields directly:
    # ``MessageID`` (no angle brackets), ``From``/``To`` as
    # ``{"Name": ..., "Address": ...}`` records, and ``Subject`` as a
    # plain string.
    assert stored["MessageID"] == returned_id
    assert stored["Subject"] == "Hello from crew.day"
    assert stored["From"]["Address"] == "noreply@example.com"
    assert stored["From"]["Name"] == "crew.day"
    to_records = stored["To"]
    assert isinstance(to_records, list) and len(to_records) == 1
    assert to_records[0]["Address"] == "alice@example.com"

    # Custom headers and Reply-To live on the per-message headers
    # endpoint. Mailpit canonicalises header names to ``Canonical-Case``
    # — note ``Message-Id`` (not the raw ``Message-ID`` we sent).
    internal_id = stored["ID"]
    assert isinstance(internal_id, str) and internal_id
    headers = _fetch_headers(api_url, internal_id)
    assert headers["Subject"] == ["Hello from crew.day"]
    assert headers["From"] == ["crew.day <noreply@example.com>"]
    assert headers["To"] == ["alice@example.com"]
    assert headers["Reply-To"] == ["ops@example.com"]
    assert headers["X-Crewday-Template"] == ["test-message"]
    assert headers["Message-Id"] == [f"<{returned_id}>"]

    # MIME: multipart/alternative with both halves. Mailpit exposes
    # the rendered text/html bodies on the message-detail endpoint.
    content_type = headers["Content-Type"][0]
    assert content_type.startswith("multipart/alternative")
    with urllib.request.urlopen(
        f"{api_url}/api/v1/message/{internal_id}", timeout=5
    ) as resp:
        detail = json.loads(resp.read().decode("utf-8"))
    assert "Plain-text body" in detail["Text"]
    assert "HTML body with" in detail["HTML"]
