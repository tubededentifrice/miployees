"""SMTP implementation of the :class:`~app.adapters.mail.ports.Mailer` port.

Transport choice: **stdlib** :mod:`smtplib`, not ``aiosmtplib``. The
:class:`Mailer` port is synchronous (§01 "Adapters"), outbound email is
a low-volume side-effect (digests, magic links, invoices — not a hot
path), and wrapping one stdlib class in the async layer would add a
dependency with no measurable latency gain. If the worker ever grows a
batch-send path where the per-message serialised RTT becomes the
bottleneck we can revisit; until then stdlib is ~40 LOC of wire.

Retry taxonomy (§10 spec "Retries if SMTP fails"):

* **Transient** — :class:`smtplib.SMTPServerDisconnected`,
  :class:`smtplib.SMTPConnectError`, :class:`smtplib.SMTPHeloError`,
  :class:`TimeoutError` (alias for ``socket.timeout`` on 3.10+),
  4xx :class:`smtplib.SMTPResponseException`, and :class:`OSError`
  with ``errno`` in
  ``{ECONNRESET, ETIMEDOUT, EPIPE, ECONNABORTED}``. Retried up to
  ``_MAX_ATTEMPTS`` times with exponential backoff + jitter.
* **Permanent** — :class:`smtplib.SMTPRecipientsRefused`,
  :class:`smtplib.SMTPDataError` with a ``5xx`` status,
  :class:`smtplib.SMTPSenderRefused`, and any other
  :class:`smtplib.SMTPResponseException` with a ``5xx`` status. Raised
  as :class:`~app.adapters.mail.ports.MailDeliveryError` immediately
  with no retry — the relay has told us the message will never land.

See ``docs/specs/10-messaging-notifications.md`` and
``docs/specs/01-architecture.md`` §"Adapters/mail".
"""

from __future__ import annotations

import contextlib
import errno
import logging
import random
import secrets
import smtplib
import time
from collections.abc import Callable, Mapping, Sequence
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Protocol, TypeVar

from pydantic import SecretStr

from app.adapters.mail.ports import MailDeliveryError
from app.util.ulid import new_ulid

__all__ = ["SMTPMailer"]

_log = logging.getLogger(__name__)

# Retry budget. Three attempts with (0.5s, 1s) + jitter between them —
# total worst-case wall clock ~1.5s before giving up. Transient SMTP
# glitches (TLS handshake flakes, mid-session drops) almost always
# resolve inside this window; anything longer than that looks like a
# real outage and belongs in the queue-level retry machinery, not here.
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_JITTER_SECONDS = 0.25

# Socket-level errors the relay may raise from inside an SMTP session
# before :mod:`smtplib` has a chance to classify them. These are all
# transient on any well-behaved network.
_TRANSIENT_ERRNOS: frozenset[int] = frozenset(
    {
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.EPIPE,
        errno.ECONNABORTED,
    }
)

# TLS strategy per port. ``465`` is the implicit-TLS SMTPS legacy port,
# ``587`` is STARTTLS-on-plaintext (MSA), ``25`` is MTA-to-MTA and
# historically ran cleartext. Anything else we treat like 587 unless
# the operator forced ``smtp_use_tls = False``.
_IMPLICIT_TLS_PORT = 465
_PLAIN_PORT = 25

_T = TypeVar("_T")


class _SMTPSession(Protocol):
    """Structural surface of :class:`smtplib.SMTP` the mailer actually uses.

    Declared here (instead of annotating factories as
    ``Callable[..., smtplib.SMTP]``) so test doubles can stand in
    without having to subclass the stdlib class. The production
    :class:`smtplib.SMTP` / :class:`smtplib.SMTP_SSL` satisfy this
    protocol by virtue of having the listed methods.
    """

    def ehlo(self, name: str = ...) -> object: ...
    def starttls(self) -> object: ...
    def login(self, user: str, password: str) -> object: ...
    def send_message(
        self,
        msg: EmailMessage,
        from_addr: str | None = ...,
        to_addrs: list[str] | None = ...,
    ) -> object: ...
    def quit(self) -> object: ...


class SMTPMailer:
    """Concrete :class:`~app.adapters.mail.ports.Mailer` over stdlib SMTP.

    Constructed once per process (or per test) and reused. Each
    :meth:`send` opens a fresh SMTP session — we deliberately do **not**
    pool connections: outbound volume is low, relays drop idle sessions
    quickly, and a per-send session keeps the failure surface tiny.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        from_addr: str,
        user: str | None = None,
        password: SecretStr | None = None,
        use_tls: bool = True,
        timeout: int = 10,
        bounce_domain: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        smtp_factory: Callable[..., _SMTPSession] | None = None,
        smtp_ssl_factory: Callable[..., _SMTPSession] | None = None,
    ) -> None:
        """Wire up the mailer.

        ``sleep`` and the two ``*_factory`` callables exist for tests —
        production wiring never passes them. We refuse to construct
        without ``from_addr`` because every downstream path (Message-ID,
        Return-Path, envelope) needs it; a missing From is a config
        error, not a runtime surprise.
        """
        if not from_addr:
            raise ValueError(
                "SMTPMailer requires a non-empty from_addr (set CREWDAY_SMTP_FROM)"
            )
        self._host = host
        self._port = port
        self._from_addr = from_addr
        self._user = user
        self._password = password
        self._use_tls = use_tls
        self._timeout = timeout
        self._bounce_domain = bounce_domain or _parse_domain(from_addr)
        self._sleep = sleep
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._smtp_ssl_factory = smtp_ssl_factory or smtplib.SMTP_SSL

        if self._port == _PLAIN_PORT:
            _log.warning(
                "SMTPMailer configured on port 25 without TLS; "
                "traffic will travel in cleartext. Use port 465 or 587 "
                "for any destination outside a trusted socket."
            )

    # ------------------------------------------------------------------
    # Public Mailer surface
    # ------------------------------------------------------------------

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
        """Send one message. See :class:`~app.adapters.mail.ports.Mailer`.

        Returns the ``Message-ID`` header value (sans angle brackets)
        as the provider-assigned id. SMTP itself never hands us a
        separate id — the Message-ID we mint is the one the recipient's
        MTA logs and the one a future bounce webhook will echo back.
        """
        if not to:
            raise ValueError("SMTPMailer.send requires at least one recipient")

        message, message_id = self._build_message(
            to=to,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            headers=headers,
            reply_to=reply_to,
        )
        self._retry_on_transient(lambda: self._dispatch(message, list(to)))
        return message_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_message(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body_text: str,
        body_html: str | None,
        headers: Mapping[str, str] | None,
        reply_to: str | None,
    ) -> tuple[EmailMessage, str]:
        """Construct the :class:`EmailMessage` and return ``(msg, message_id)``.

        Uses :class:`email.message.EmailMessage` — the modern
        (:pep:`3464`) API that handles UTF-8 subject / body encoding
        without the ``email.mime`` zoo. When ``body_html`` is provided
        we call :meth:`EmailMessage.add_alternative` to emit a proper
        ``multipart/alternative`` with plaintext first (so non-HTML
        readers see the text without scrolling past MIME delimiters).
        """
        message = EmailMessage()
        message["From"] = self._from_addr
        message["To"] = ", ".join(to)
        message["Subject"] = subject

        message_id = f"{new_ulid()}@{self._bounce_domain}"
        message["Message-ID"] = f"<{message_id}>"
        # Bounce token is a short random id per-message; when the
        # provider posts a DSN to our inbound webhook we look the
        # token up in ``email_delivery`` (§10 "Delivery tracking") to
        # correlate. ``token_urlsafe(9)`` → ~12 chars, plenty of
        # entropy to make collisions astronomical.
        bounce_token = secrets.token_urlsafe(9)
        message["Return-Path"] = f"<bounce+{bounce_token}@{self._bounce_domain}>"

        if reply_to is not None:
            message["Reply-To"] = reply_to

        for name, value in (headers or {}).items():
            # Caller-supplied headers can't overwrite the ones we
            # control — Message-ID / Return-Path are load-bearing for
            # bounce correlation, and From/To/Subject are the envelope.
            if name in {"From", "To", "Subject", "Message-ID", "Return-Path"}:
                raise ValueError(
                    f"header {name!r} is reserved by SMTPMailer; "
                    "remove it from the headers= argument"
                )
            message[name] = value

        message.set_content(body_text)
        if body_html is not None:
            message.add_alternative(body_html, subtype="html")

        return message, message_id

    def _dispatch(self, message: EmailMessage, recipients: list[str]) -> None:
        """Open a session, authenticate if needed, send, and quit.

        Separated from :meth:`send` so :meth:`_retry_on_transient` can
        re-invoke it without rebuilding the :class:`EmailMessage`.
        """
        smtp: _SMTPSession
        if self._port == _IMPLICIT_TLS_PORT and self._use_tls:
            smtp = self._smtp_ssl_factory(self._host, self._port, timeout=self._timeout)
        else:
            smtp = self._smtp_factory(self._host, self._port, timeout=self._timeout)

        try:
            smtp.ehlo()
            if (
                self._use_tls
                and self._port != _IMPLICIT_TLS_PORT
                and self._port != _PLAIN_PORT
            ):
                smtp.starttls()
                smtp.ehlo()

            if self._user and self._password is not None:
                smtp.login(self._user, self._password.get_secret_value())

            smtp.send_message(message, from_addr=self._from_addr, to_addrs=recipients)
        finally:
            # ``quit`` itself can raise :class:`SMTPServerDisconnected`
            # when the relay already dropped us. That's benign — we've
            # either already succeeded or we're on our way out via an
            # exception; either way the caller doesn't need to hear
            # about it, so we close without propagating.
            with contextlib.suppress(smtplib.SMTPException):
                smtp.quit()

    def _retry_on_transient(self, attempt: Callable[[], _T]) -> _T:
        """Run ``attempt`` up to ``_MAX_ATTEMPTS`` times on transient errors.

        Permanent SMTP errors raise :class:`MailDeliveryError`
        immediately. Transient failures sleep
        ``base * 2**n + jitter`` seconds (``n`` = retry index) before
        the next attempt, so three attempts wait at most ~0.5 + 1.0s
        plus up to 0.25s jitter each.
        """
        last_transient: Exception | None = None
        for attempt_idx in range(_MAX_ATTEMPTS):
            try:
                return attempt()
            except Exception as exc:
                if _is_permanent(exc):
                    raise MailDeliveryError(
                        f"SMTP rejected message permanently: {_describe(exc)}"
                    ) from exc
                if not _is_transient(exc):
                    raise
                last_transient = exc
                _log.warning(
                    "SMTP transient failure (attempt %d/%d): %s",
                    attempt_idx + 1,
                    _MAX_ATTEMPTS,
                    _describe(exc),
                )
                if attempt_idx + 1 >= _MAX_ATTEMPTS:
                    break
                delay = (
                    _BACKOFF_BASE_SECONDS * (2**attempt_idx)
                    + random.random() * _BACKOFF_JITTER_SECONDS
                )
                self._sleep(delay)

        assert last_transient is not None  # loop exit only on transient path
        raise MailDeliveryError(
            f"SMTP transport failed after {_MAX_ATTEMPTS} attempts: "
            f"{_describe(last_transient)}"
        ) from last_transient


def _parse_domain(from_addr: str) -> str:
    """Return the domain portion of an RFC 5322 address.

    ``parseaddr`` handles both ``"Name <a@b.com>"`` and ``"a@b.com"``;
    we fall back to the raw input after the ``@`` so a misformatted
    address still produces a non-empty bounce domain rather than
    exploding at construction.
    """
    _, addr = parseaddr(from_addr)
    if "@" in addr:
        return addr.rsplit("@", 1)[1] or "localhost"
    if "@" in from_addr:
        return from_addr.rsplit("@", 1)[1] or "localhost"
    return "localhost"


def _is_permanent(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` is a 5xx-class SMTP refusal.

    Permanent means the MTA told us this exact message won't deliver
    — retrying without editing the payload is guaranteed to hit the
    same wall. These raise :class:`MailDeliveryError` immediately.

    **Precedence quirk.** :meth:`_retry_on_transient` consults
    :func:`_is_permanent` *before* :func:`_is_transient`, so a 5xx
    response exception is classified as permanent even though it is
    also an :class:`smtplib.SMTPResponseException` (which the transient
    checker recognises on 4xx). Keep the order as-is: a 5xx is the
    relay's final word and retrying only wastes attempts.
    """
    if isinstance(exc, smtplib.SMTPRecipientsRefused | smtplib.SMTPSenderRefused):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        # 5xx → permanent, 4xx → transient (RFC 5321 §4.2.1).
        return 500 <= exc.smtp_code < 600
    return False


def _is_transient(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` is retry-safe (network glitch, 4xx)."""
    if isinstance(
        exc,
        smtplib.SMTPServerDisconnected
        | smtplib.SMTPConnectError
        | smtplib.SMTPHeloError,
    ):
        return True
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, TimeoutError):
        # ``socket.timeout`` is an alias for :class:`TimeoutError` on
        # Python 3.10+; catching the builtin covers both idioms.
        return True
    if isinstance(exc, OSError):
        return exc.errno in _TRANSIENT_ERRNOS
    return False


def _describe(exc: BaseException) -> str:
    """Render a log-safe single-line description of ``exc``.

    Never includes the SMTP password — we only ever log the exception
    class and its message, and :mod:`smtplib` does not put credentials
    in exception strings. The :class:`SecretStr` wrapper on the
    configured password keeps it out of ``repr`` paths regardless.
    """
    return f"{type(exc).__name__}: {exc}"
