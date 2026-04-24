"""Messaging context — digests, notifications, outbound email, chat gateway.

Public surface:

* :class:`~app.domain.messaging.notifications.NotificationService` —
  multi-channel fanout (inbox, SSE, email, push) for a single
  notification. Covers the §10 "Channels" contract.
* :class:`~app.domain.messaging.notifications.NotificationKind` —
  enum of valid notification kinds, mirroring the DB CHECK.
* :class:`~app.domain.messaging.notifications.TemplateNotFound` —
  loud error when a kind's default template is missing.

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

__all__ = [
    "TEMPLATE_ROOT",
    "Jinja2TemplateLoader",
    "NotificationKind",
    "NotificationService",
    "PushEnqueue",
    "TemplateLoader",
    "TemplateNotFound",
]
