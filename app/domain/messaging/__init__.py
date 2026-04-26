"""Messaging context — digests, notifications, outbound email, chat gateway.

Public surface:

* :class:`~app.domain.messaging.notifications.NotificationService` —
  multi-channel fanout (inbox, SSE, email, push) for a single
  notification. Covers the §10 "Channels" contract.
* :class:`~app.domain.messaging.notifications.NotificationKind` —
  enum of valid notification kinds, mirroring the DB CHECK.
* :class:`~app.domain.messaging.notifications.TemplateNotFound` —
  loud error when a kind's default template is missing.
* :class:`~app.domain.messaging.ports.PushTokenRepository` —
  repository port for the web-push subscription seam (cd-74pb);
  see :mod:`app.domain.messaging.push_tokens` for the consumer.
* :class:`~app.domain.messaging.ports.PushTokenRow` — immutable
  row projection returned by the repo above.

See docs/specs/10-messaging-notifications.md and
docs/specs/23-chat-gateway.md.
"""

from app.domain.messaging.notifications import (
    TEMPLATE_ROOT,
    Jinja2TemplateLoader,
    NotificationKind,
    NotificationService,
    PushEnqueue,
    TemplateLoader,
    TemplateNotFound,
)
from app.domain.messaging.ports import (
    PushTokenRepository,
    PushTokenRow,
)

__all__ = [
    "TEMPLATE_ROOT",
    "Jinja2TemplateLoader",
    "NotificationKind",
    "NotificationService",
    "PushEnqueue",
    "PushTokenRepository",
    "PushTokenRow",
    "TemplateLoader",
    "TemplateNotFound",
]
