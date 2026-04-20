"""Unit tests for :class:`app.adapters.mail.smtp.SMTPMailer`.

The mailer is exercised with a hand-rolled fake :class:`smtplib.SMTP`
so the tests never touch a real socket. Each scenario covers one
observable behaviour from §10 (message envelope, multipart layout, TLS
negotiation) or the retry taxonomy documented in the adapter's module
docstring.

See ``docs/specs/10-messaging-notifications.md`` §"Email" and
``docs/specs/17-testing-quality.md`` §"Unit".
"""

from __future__ import annotations

import smtplib
from collections.abc import Iterator
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

import pytest
from pydantic import SecretStr

from app.adapters.mail.ports import MailDeliveryError
from app.adapters.mail.smtp import SMTPMailer

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeSMTP:
    """Records every :class:`smtplib.SMTP` call a :class:`SMTPMailer` makes.

    Instances are produced by :class:`_FakeSMTPFactory` so the test can
    inspect the full call log per-connection. Any method whose return
    value is used (``ehlo``, ``login``, ``send_message``, ``quit``)
    returns a plausible 2xx-ish tuple; behaviour deviates via
    ``raise_on``.
    """

    host: str
    port: int
    timeout: int
    implicit_tls: bool = False
    calls: list[str] = field(default_factory=list)
    sent_messages: list[tuple[EmailMessage, str, list[str]]] = field(
        default_factory=list
    )
    raise_on: dict[str, BaseException] = field(default_factory=dict)

    def _maybe_raise(self, method: str) -> None:
        exc = self.raise_on.get(method)
        if exc is not None:
            raise exc

    def ehlo(self, name: str | None = None) -> tuple[int, bytes]:
        self.calls.append("ehlo")
        self._maybe_raise("ehlo")
        return (250, b"fake")

    def starttls(self, **_: Any) -> tuple[int, bytes]:
        self.calls.append("starttls")
        self._maybe_raise("starttls")
        return (220, b"Ready to start TLS")

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        self.calls.append("login")
        self._maybe_raise("login")
        return (235, b"2.7.0 Authentication successful")

    def send_message(
        self,
        msg: EmailMessage,
        from_addr: str | None = None,
        to_addrs: list[str] | None = None,
    ) -> dict[str, tuple[int, bytes]]:
        self.calls.append("send_message")
        self._maybe_raise("send_message")
        self.sent_messages.append((msg, from_addr or "", list(to_addrs or [])))
        return {}

    def quit(self) -> tuple[int, bytes]:
        self.calls.append("quit")
        self._maybe_raise("quit")
        return (221, b"Bye")


class _FakeSMTPFactory:
    """Callable stand-in for both :func:`smtplib.SMTP` and ``SMTP_SSL``.

    Exposes every instance it produced in ``self.connections``, and
    lets the test pre-queue per-connection behaviour via
    ``self.raise_on_connection[<index>]``.
    """

    def __init__(self, *, implicit_tls: bool = False) -> None:
        self._implicit_tls = implicit_tls
        self.connections: list[_FakeSMTP] = []
        self.raise_on_connection: list[dict[str, BaseException]] = []

    def __call__(self, host: str, port: int, *, timeout: int, **_: Any) -> _FakeSMTP:
        idx = len(self.connections)
        raise_on = (
            self.raise_on_connection[idx] if idx < len(self.raise_on_connection) else {}
        )
        smtp = _FakeSMTP(
            host=host,
            port=port,
            timeout=timeout,
            implicit_tls=self._implicit_tls,
            raise_on=dict(raise_on),
        )
        self.connections.append(smtp)
        return smtp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plain_factory() -> _FakeSMTPFactory:
    return _FakeSMTPFactory(implicit_tls=False)


@pytest.fixture
def ssl_factory() -> _FakeSMTPFactory:
    return _FakeSMTPFactory(implicit_tls=True)


@pytest.fixture
def sleeps() -> list[float]:
    """Captures every ``sleep(<s>)`` the mailer's retry loop issues."""
    return []


# Module-level default so the ``SecretStr`` isn't constructed at
# function definition time (ruff B008).
_DEFAULT_PASSWORD: SecretStr = SecretStr("hunter2")


def _make_mailer(
    plain_factory: _FakeSMTPFactory,
    ssl_factory: _FakeSMTPFactory,
    sleeps: list[float],
    *,
    port: int = 587,
    use_tls: bool = True,
    user: str | None = "noreply@example.com",
    password: SecretStr | None = _DEFAULT_PASSWORD,
    bounce_domain: str | None = None,
) -> SMTPMailer:
    return SMTPMailer(
        host="smtp.example.com",
        port=port,
        from_addr="crew.day <noreply@example.com>",
        user=user,
        password=password,
        use_tls=use_tls,
        timeout=10,
        bounce_domain=bounce_domain,
        sleep=sleeps.append,
        smtp_factory=plain_factory,
        smtp_ssl_factory=ssl_factory,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_send_runs_full_session_and_returns_message_id(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mid = mailer.send(
            to=["alice@example.com"],
            subject="Hello",
            body_text="Hi there",
        )

        # STARTTLS port (587) → plain factory used, SSL factory idle.
        assert len(plain_factory.connections) == 1
        assert ssl_factory.connections == []

        session = plain_factory.connections[0]
        assert session.calls == [
            "ehlo",
            "starttls",
            "ehlo",
            "login",
            "send_message",
            "quit",
        ]

        recorded_msg, from_addr, recipients = session.sent_messages[0]
        # Envelope sender passes through verbatim.
        assert from_addr == "crew.day <noreply@example.com>"
        assert recipients == ["alice@example.com"]
        # ``EmailMessage`` may quote the display-name half of the From
        # header on serialisation (RFC 5322 §3.4). We assert on the
        # parsed address rather than string equality.
        from email.utils import parseaddr as _parse

        _, parsed_from = _parse(recorded_msg["From"])
        assert parsed_from == "noreply@example.com"
        assert recorded_msg["To"] == "alice@example.com"
        assert recorded_msg["Subject"] == "Hello"
        assert recorded_msg["Message-ID"] == f"<{mid}>"
        return_path = recorded_msg["Return-Path"]
        assert return_path.startswith("<bounce+")
        assert return_path.endswith("@example.com>")
        assert mid.endswith("@example.com")

    def test_send_includes_reply_to_and_caller_headers(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mailer.send(
            to=["alice@example.com"],
            subject="Hello",
            body_text="Hi",
            reply_to="ops@example.com",
            headers={"X-Crewday": "yes"},
        )
        msg, _, _ = plain_factory.connections[0].sent_messages[0]
        assert msg["Reply-To"] == "ops@example.com"
        assert msg["X-Crewday"] == "yes"

    def test_html_body_produces_multipart_alternative(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mailer.send(
            to=["alice@example.com"],
            subject="Hello",
            body_text="Hi there",
            body_html="<p>Hi there</p>",
        )
        msg, _, _ = plain_factory.connections[0].sent_messages[0]
        assert msg.get_content_type() == "multipart/alternative"
        parts = list(msg.iter_parts())
        subtypes = [p.get_content_type() for p in parts]
        assert "text/plain" in subtypes
        assert "text/html" in subtypes

    def test_multiple_recipients_are_comma_joined(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mailer.send(
            to=["alice@example.com", "bob@example.com"],
            subject="Hi",
            body_text="text",
        )
        msg, _, recipients = plain_factory.connections[0].sent_messages[0]
        assert recipients == ["alice@example.com", "bob@example.com"]
        assert msg["To"] == "alice@example.com, bob@example.com"

    def test_unicode_subject_and_body_are_utf8_encoded(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        from email.header import decode_header, make_header

        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mailer.send(
            to=["maria@example.com"],
            subject="Café ☕ naïve 日本語",
            body_text="Hola Maria — ¿cómo estás? ☕",
        )
        msg, _, _ = plain_factory.connections[0].sent_messages[0]
        # Subject round-trips: the decoded header equals what we put in.
        assert msg["Subject"] == "Café ☕ naïve 日本語"
        # Rendered header uses RFC 2047 encoded-word form so naive
        # MTAs don't choke; decoding it back gives the original.
        rendered = bytes(msg)
        assert b"=?utf-8?" in rendered  # encoded-word wrapper present
        subject_header = msg["Subject"]
        decoded_subject = str(make_header(decode_header(subject_header)))
        assert decoded_subject == "Café ☕ naïve 日本語"
        # Body round-trip: UTF-8 content-transfer-encoded and visible
        # once the message is re-parsed.
        payload = msg.get_payload()
        assert "Café" not in "Hola Maria — ¿cómo estás? ☕"  # sanity
        assert "cómo" in payload
        assert "☕" in payload

    def test_no_reply_to_when_caller_omits_it(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mailer.send(
            to=["alice@example.com"],
            subject="Hi",
            body_text="text",
        )
        msg, _, _ = plain_factory.connections[0].sent_messages[0]
        assert msg["Reply-To"] is None


# ---------------------------------------------------------------------------
# TLS strategy by port
# ---------------------------------------------------------------------------


class TestTLSStrategy:
    def test_port_465_uses_implicit_tls_factory(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps, port=465)
        mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert ssl_factory.connections and not plain_factory.connections
        assert "starttls" not in ssl_factory.connections[0].calls

    def test_port_587_negotiates_starttls(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps, port=587)
        mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert "starttls" in plain_factory.connections[0].calls

    def test_port_25_skips_tls(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps, port=25)
        mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert "starttls" not in plain_factory.connections[0].calls

    def test_use_tls_false_disables_starttls_on_587(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(
            plain_factory, ssl_factory, sleeps, port=587, use_tls=False
        )
        mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert "starttls" not in plain_factory.connections[0].calls

    def test_no_login_when_credentials_unset(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(
            plain_factory,
            ssl_factory,
            sleeps,
            user=None,
            password=None,
        )
        mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert "login" not in plain_factory.connections[0].calls


# ---------------------------------------------------------------------------
# Retry taxonomy
# ---------------------------------------------------------------------------


class TestTransientRetry:
    def test_retries_on_server_disconnected_then_succeeds(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        plain_factory.raise_on_connection.append(
            {"send_message": smtplib.SMTPServerDisconnected("peer dropped")}
        )
        plain_factory.raise_on_connection.append({})
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)

        mid = mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert mid  # success returns the Message-ID

        # Two full sessions opened.
        assert len(plain_factory.connections) == 2
        # Exactly one sleep between retries — backoff schedule is
        # (base * 2**0) + jitter ∈ [0.5, 0.75).
        assert len(sleeps) == 1
        assert 0.5 <= sleeps[0] < 0.76

    def test_three_transient_failures_raise_mail_delivery_error(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        for _ in range(3):
            plain_factory.raise_on_connection.append(
                {"send_message": smtplib.SMTPConnectError(421, b"try later")}
            )
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)

        with pytest.raises(MailDeliveryError, match="after 3 attempts"):
            mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")

        assert len(plain_factory.connections) == 3
        # Two sleeps between three attempts, never a trailing sleep
        # after the final failure.
        assert len(sleeps) == 2

    def test_socket_timeout_is_transient(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        # ``socket.timeout`` is an alias for :class:`TimeoutError` on
        # Python 3.10+; use the builtin name to appease ruff UP041.
        plain_factory.raise_on_connection.append(
            {"send_message": TimeoutError("read timed out")}
        )
        plain_factory.raise_on_connection.append({})
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert len(plain_factory.connections) == 2

    def test_econnreset_is_transient(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        import errno as _errno

        plain_factory.raise_on_connection.append(
            {"send_message": OSError(_errno.ECONNRESET, "connection reset by peer")}
        )
        plain_factory.raise_on_connection.append({})
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert len(plain_factory.connections) == 2

    def test_smtp_4xx_response_is_transient(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        plain_factory.raise_on_connection.append(
            {"send_message": smtplib.SMTPDataError(451, b"mailbox busy")}
        )
        plain_factory.raise_on_connection.append({})
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert len(plain_factory.connections) == 2


class TestPermanentFailure:
    def test_recipients_refused_raises_immediately(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        plain_factory.raise_on_connection.append(
            {
                "send_message": smtplib.SMTPRecipientsRefused(
                    {"alice@example.com": (550, b"no such user")}
                )
            }
        )
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        with pytest.raises(MailDeliveryError, match="permanently"):
            mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        # No retry — exactly one session opened.
        assert len(plain_factory.connections) == 1
        assert sleeps == []

    def test_smtp_5xx_data_error_does_not_retry(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        plain_factory.raise_on_connection.append(
            {"send_message": smtplib.SMTPDataError(554, b"message refused")}
        )
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        with pytest.raises(MailDeliveryError, match="permanently"):
            mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert len(plain_factory.connections) == 1

    def test_sender_refused_is_permanent(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        plain_factory.raise_on_connection.append(
            {
                "send_message": smtplib.SMTPSenderRefused(
                    550, b"not allowed", "noreply@example.com"
                )
            }
        )
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        with pytest.raises(MailDeliveryError, match="permanently"):
            mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert len(plain_factory.connections) == 1


# ---------------------------------------------------------------------------
# Validation / construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_empty_from_addr_rejected(self) -> None:
        with pytest.raises(ValueError, match="from_addr"):
            SMTPMailer(host="h", port=587, from_addr="")

    def test_empty_recipient_list_rejected(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        with pytest.raises(ValueError, match="recipient"):
            mailer.send(to=[], subject="Hi", body_text="text")

    def test_reserved_header_rejected(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        with pytest.raises(ValueError, match="reserved"):
            mailer.send(
                to=["alice@example.com"],
                subject="Hi",
                body_text="text",
                headers={"Message-ID": "<spoofed@elsewhere>"},
            )

    def test_bounce_domain_override_wins_over_from_domain(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        mailer = _make_mailer(
            plain_factory,
            ssl_factory,
            sleeps,
            bounce_domain="bounces.example.net",
        )
        mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        msg, _, _ = plain_factory.connections[0].sent_messages[0]
        assert "@bounces.example.net" in msg["Return-Path"]
        assert "@bounces.example.net" in msg["Message-ID"]


# ---------------------------------------------------------------------------
# Quit resilience
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _seed_jitter(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin ``random.random`` so backoff-window assertions are tight."""
    import random as _random

    monkeypatch.setattr(_random, "random", lambda: 0.5)
    yield


class TestQuitIsBestEffort:
    def test_quit_disconnect_does_not_mask_success(
        self,
        plain_factory: _FakeSMTPFactory,
        ssl_factory: _FakeSMTPFactory,
        sleeps: list[float],
    ) -> None:
        plain_factory.raise_on_connection.append(
            {"quit": smtplib.SMTPServerDisconnected("peer closed")}
        )
        mailer = _make_mailer(plain_factory, ssl_factory, sleeps)
        mid = mailer.send(to=["alice@example.com"], subject="Hi", body_text="text")
        assert mid
        assert "send_message" in plain_factory.connections[0].calls
