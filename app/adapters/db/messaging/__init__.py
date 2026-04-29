"""messaging — notification/push_token/digest_record/chat_*/email_*.

All eight tables in this package are workspace-scoped: each row carries
a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A bare
read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

The cd-pjm v1 slice landed the first five tables (notification,
push_token, digest_record, chat_channel, chat_message) — the
minimum-viable shape for the notification fanout (§10), the web-push
token registry (§12 ``/me/push-tokens`` — returns ``501
push_unavailable`` until the native app ships), the daily / weekly
email-digest ledger, and the chat-gateway substrate (§23). cd-7ej8
adds :class:`ChatChannelMember` for explicit per-channel membership
and soft channel archives via ``chat_channel.archived_at``. cd-aqwt
extends the package with :class:`EmailOptOut` (per-user per-category
unsubscribe marker the §10 worker consults pre-send) and
:class:`EmailDelivery` (per-send delivery ledger driving bounce-reply
correlation and retry scheduling). The richer surfaces — full
``chat_thread`` model, agent-dispatch state machine, WhatsApp
``chat_channel_binding`` rows — land with follow-ups without
breaking these migrations' public write contract.

FK hygiene mirrors the rest of the app:

* ``workspace_id`` → ``workspace.id`` with ``ondelete='CASCADE'`` on
  every row — sweeping a workspace sweeps its messaging history
  (§15 export worker snapshots first).
* ``recipient_user_id`` / ``user_id`` → ``user.id`` with
  ``ondelete='CASCADE'`` — a user's notifications / push tokens /
  digest ledger / opt-outs do not outlive the user. A revoked grant
  or archived user is a distinct concern handled in the domain
  layer.
* ``ChatMessage.author_user_id`` → ``user.id`` with
  ``ondelete='SET NULL'`` — gateway-inbound rows can have ``NULL``
  authors (the external sender has no user id), and a user delete
  must not nuke the thread history (audit trail survives).
* ``ChatMessage.channel_id`` → ``chat_channel.id`` with
  ``ondelete='CASCADE'`` — deleting a channel sweeps its messages;
  messages are not independently useful once the channel is gone.
* ``ChatChannelMember.channel_id`` → ``chat_channel.id`` with
  ``ondelete='CASCADE'`` — deleting or sweeping a channel removes its
  explicit membership rows.
* ``EmailDelivery.to_person_id`` has **no FK** — recipients can be
  client users not yet materialised in the ``user`` table (invoice
  reminders, stay-upcoming emails); the domain layer resolves the
  soft pointer at render time.

See ``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/10-messaging-notifications.md`` for the consumer
contract that drives the indexes (unread fanout, channel scrollback,
daily digests, opt-out probe, bounce correlation), and
``docs/specs/23-chat-gateway.md`` for the gateway-inbound semantics
(``external_ref``, ``dispatched_to_agent_at``, the channel / message
substrate shared across web + WhatsApp + Telegram).
"""

from __future__ import annotations

from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatGatewayBinding,
    ChatChannelMember,
    ChatMessage,
    DigestRecord,
    EmailDelivery,
    EmailOptOut,
    Notification,
    PushToken,
)
from app.tenancy.registry import register

for _table in (
    "notification",
    "push_token",
    "digest_record",
    "chat_channel",
    "chat_channel_member",
    "chat_gateway_binding",
    "chat_message",
    "email_opt_out",
    "email_delivery",
):
    register(_table)

__all__ = [
    "ChatChannel",
    "ChatGatewayBinding",
    "ChatChannelMember",
    "ChatMessage",
    "DigestRecord",
    "EmailDelivery",
    "EmailOptOut",
    "Notification",
    "PushToken",
]
