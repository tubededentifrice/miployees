"""Mail ports.

Defines the outbound-email seam. Concrete adapters (SMTP in v1,
Resend/SES later) live under ``app/adapters/mail/`` and satisfy this
protocol.

See ``docs/specs/01-architecture.md`` §"Adapters".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

__all__ = ["MailDeliveryError", "Mailer"]


class MailDeliveryError(Exception):
    """Raised when the underlying transport rejects or fails to deliver."""


class Mailer(Protocol):
    """Outbound email sender."""

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
        """Send one message and return the provider-assigned message id.

        Raises :class:`MailDeliveryError` if the transport refuses or
        fails the send.
        """
        ...
