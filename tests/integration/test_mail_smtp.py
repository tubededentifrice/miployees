"""End-to-end test for :class:`app.adapters.mail.smtp.SMTPMailer`.

Spins up a :class:`testcontainers.core.container.DockerContainer` with
the ``mailhog/mailhog`` image — no auth, no TLS, just an SMTP listener
on ``:1025`` and an HTTP API on ``:8025``. We send one message through
the real :class:`SMTPMailer`, then poll MailHog's
``/api/v2/messages`` endpoint and assert the delivered envelope
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

_MAILHOG_IMAGE = "mailhog/mailhog:latest"
_SMTP_PORT = 1025
_HTTP_PORT = 8025


def _fetch_messages(api_url: str) -> list[dict[str, Any]]:
    """Return the ``items`` array from MailHog's ``/api/v2/messages``.

    MailHog's JSON shape is documented and stable; the runtime
    ``isinstance`` guard keeps mypy honest without pretending we know
    more than we do about third-party JSON.
    """
    with urllib.request.urlopen(f"{api_url}/api/v2/messages", timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"MailHog returned non-object payload: {payload!r}")
    items = payload.get("items", [])
    if not isinstance(items, list):
        raise AssertionError(f"MailHog 'items' is not a list: {items!r}")
    return [item for item in items if isinstance(item, dict)]


def _wait_for_message(
    api_url: str,
    *,
    message_id: str | None = None,
    deadline_s: float = 10.0,
) -> dict[str, Any]:
    """Poll MailHog until the expected message arrives, then return it.

    When ``message_id`` is given, we search every stored envelope's
    ``Message-ID`` header for an exact match so the assertion is
    keyed to *this* test's send — a future caller adding a second
    send in the same module doesn't accidentally assert against the
    wrong envelope. Without ``message_id`` (e.g. a smoke case that
    only cares that *something* arrived) we fall back to the first
    stored message.
    """
    expected_header = f"<{message_id}>" if message_id is not None else None
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        items = _fetch_messages(api_url)
        if expected_header is None:
            if items:
                return items[0]
        else:
            for item in items:
                headers = item.get("Content", {}).get("Headers", {})
                stored_ids = headers.get("Message-ID", [])
                if expected_header in stored_ids:
                    return item
        time.sleep(0.2)
    raise AssertionError(
        f"MailHog at {api_url} never received the expected message "
        f"({message_id!r}) within {deadline_s}s"
    )


@pytest.fixture(scope="module")
def mailhog() -> Iterator[tuple[str, int, str]]:
    """Spin up MailHog, yield ``(smtp_host, smtp_port, http_api_url)``.

    Uses the generic :class:`DockerContainer` rather than a
    community-maintained image-specific wrapper — MailHog's shape
    (single binary, two ports, zero config) doesn't justify a bespoke
    class. Bind guard: MailHog listens on ``0.0.0.0`` inside the
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

    container = DockerContainer(_MAILHOG_IMAGE).with_exposed_ports(
        _SMTP_PORT, _HTTP_PORT
    )
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Docker/MailHog container unavailable: {exc}")

    try:
        host = container.get_container_host_ip()
        smtp_port = int(container.get_exposed_port(_SMTP_PORT))
        http_port = int(container.get_exposed_port(_HTTP_PORT))
        api_url = f"http://{host}:{http_port}"

        # Wait for the HTTP API to answer — the container is up well
        # before MailHog finishes binding its listeners, and urlopen
        # against a not-yet-open port throws :class:`ConnectionRefusedError`.
        _wait_for_http(api_url, deadline_s=15.0)

        yield host, smtp_port, api_url
    finally:
        container.stop()


def _wait_for_http(base_url: str, *, deadline_s: float) -> None:
    """Poll ``base_url/api/v2/messages`` until it returns 2xx or we time out."""
    end = time.monotonic() + deadline_s
    last_exc: BaseException | None = None
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(
                f"{base_url}/api/v2/messages", timeout=2
            ) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"MailHog HTTP API at {base_url} never came up in {deadline_s}s "
        f"(last error: {last_exc!r})"
    )


def test_send_delivers_envelope_to_mailhog(
    mailhog: tuple[str, int, str],
) -> None:
    """One :meth:`SMTPMailer.send` → one stored message with matching headers.

    Covers the full live-SMTP path end-to-end:

    * TCP + SMTP handshake succeeds against a real server.
    * ``EmailMessage`` serialises into bytes MailHog's SMTP stack
      accepts (multipart/alternative included).
    * The headers we set (From, To, Subject, Message-ID, Return-Path,
      Reply-To) round-trip through SMTP and land on the stored copy.
    * The returned message id matches the stored ``Message-ID``.
    """
    host, smtp_port, api_url = mailhog

    mailer = SMTPMailer(
        host=host,
        port=smtp_port,
        from_addr="crew.day <noreply@example.com>",
        user=None,
        password=None,
        use_tls=False,  # MailHog has no TLS listener
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

    # MailHog surfaces headers as lists-of-strings under ``Content.Headers``.
    headers = stored["Content"]["Headers"]
    assert headers["Subject"][0] == "Hello from crew.day"
    assert headers["From"][0].endswith("<noreply@example.com>")
    assert headers["To"][0] == "alice@example.com"
    assert headers["Reply-To"][0] == "ops@example.com"
    assert headers["X-Crewday-Template"][0] == "test-message"
    assert headers["Message-ID"][0] == f"<{returned_id}>"
    # Return-Path survives and carries the bounce token.
    return_path = headers["Return-Path"][0]
    assert return_path.startswith("<bounce+")
    assert return_path.endswith("@example.com>")

    # MIME: multipart/alternative with both halves.
    content_type = headers["Content-Type"][0]
    assert content_type.startswith("multipart/alternative")
    body = stored["Content"]["Body"]
    assert "Plain-text body" in body
    assert "HTML body with" in body
