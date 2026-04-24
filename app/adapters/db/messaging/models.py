"""SQLAlchemy models for messaging + notifications (cd-pjm + cd-aqwt).

Defines the ``Notification`` / ``PushToken`` / ``DigestRecord`` /
``ChatChannel`` / ``ChatMessage`` / ``EmailOptOut`` / ``EmailDelivery``
mapped classes.

The cd-pjm v1 slice landed the notification / push / digest / chat
substrate; cd-aqwt extends the package with the per-user per-category
unsubscribe ledger (``email_opt_out``) and the per-send delivery
tracker (``email_delivery``). Together these cover the §10 fanout
opt-out probe, the §12 ``/me/push-tokens`` surface, the §23 chat
gateway, the daily / weekly digest worker, and the provider
bounce-reply correlation path. The richer surfaces (full
``chat_thread`` model with ``agent_dispatch_state`` machine,
WhatsApp-specific ``chat_channel_binding`` + link-challenge rows)
land with follow-ups without breaking these migrations' public
write contract.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. FK hygiene mirrors
the rest of the app; see the package docstring for the per-row
``ondelete`` rules.

See ``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/10-messaging-notifications.md`` (consumer contract
incl. §"email_opt_out" and §"Delivery tracking"),
``docs/specs/23-chat-gateway.md`` (gateway-inbound semantics).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = [
    "ChatChannel",
    "ChatMessage",
    "DigestRecord",
    "EmailDelivery",
    "EmailOptOut",
    "Notification",
    "PushToken",
]


# Allowed ``notification.kind`` values. Matches the §10 "Emails the
# system sends" catalogue plus the in-app fanout targets; the
# domain service narrows the string to a :class:`Literal` on read.
# New kinds land with a follow-up (enum widening is additive and
# portable: SQLite rewrites the CHECK body via ``batch_alter_table``,
# PG accepts the new body directly).
_NOTIFICATION_KIND_VALUES: tuple[str, ...] = (
    "task_assigned",
    "task_overdue",
    "expense_approved",
    "expense_rejected",
    "expense_submitted",
    "approval_needed",
    "approval_decided",
    "issue_reported",
    "issue_resolved",
    "comment_mention",
    "payslip_issued",
    "stay_upcoming",
    "anomaly_detected",
    "agent_message",
)

# Allowed ``digest_record.kind`` values. Matches the §10 "Daily digests"
# cadence options; weekly rollups land with a later follow-up but the
# column accepts the value now so the worker does not need a migration
# to start writing them.
_DIGEST_KIND_VALUES: tuple[str, ...] = ("daily", "weekly")

# Allowed ``chat_channel.kind`` values per cd-pjm scope. ``staff`` and
# ``manager`` are the two in-app conversation surfaces (§14
# ``.desk__agent`` sidebar for managers, ``/chat`` page for workers);
# ``chat_gateway`` is the §23 inbound surface that lands external
# traffic (WhatsApp, SMS, email) in the shared substrate. A richer
# enum (``web_owner_sidebar`` / ``web_worker_chat`` / etc.) is
# tracked in §23 and lands with the gateway service follow-up.
_CHAT_CHANNEL_KIND_VALUES: tuple[str, ...] = (
    "staff",
    "manager",
    "chat_gateway",
)

# Allowed ``chat_channel.source`` values — which transport surfaced
# the channel. ``app`` is the default for in-app channels; the other
# three surface the §23 gateway adapters. The CHECK clamps the v1
# enum so a typo in a service-layer caller fails fast at INSERT
# time; §23's ``offapp_*`` taxonomy maps onto this simpler set at
# the domain boundary.
_CHAT_CHANNEL_SOURCE_VALUES: tuple[str, ...] = (
    "app",
    "whatsapp",
    "sms",
    "email",
)

# Allowed ``email_opt_out.source`` values per §10. ``unsubscribe_link``
# covers the signed one-click link embedded in every opt-outable email;
# ``profile`` covers the owner / manager / worker toggling the
# preference on their own ``/me`` page; ``admin`` covers a staff-side
# override (a support escalation turning the category off on a user's
# behalf). CHECK clamps the enum so a service-layer typo fails at
# INSERT time.
_EMAIL_OPT_OUT_SOURCE_VALUES: tuple[str, ...] = (
    "unsubscribe_link",
    "profile",
    "admin",
)

# Allowed ``email_delivery.delivery_state`` values per §10.
# ``queued`` is the initial state at row-insert time (worker picks
# it up for dispatch); ``sent`` is post-SMTP ``250 OK``; ``delivered``
# lands when the provider posts back a positive event; ``bounced``
# when the mailbox rejects; ``failed`` when the adapter exhausts the
# retry budget. A new state lands via an additive migration — enum
# widening is portable (CHECK body rewrite via
# ``batch_alter_table`` on SQLite, direct ALTER on PG).
_EMAIL_DELIVERY_STATE_VALUES: tuple[str, ...] = (
    "queued",
    "sent",
    "delivered",
    "bounced",
    "failed",
)


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Kept as a tiny helper so the four CHECK constraints below stay
    readable; matches the convention used by every sibling module
    (``tasks``, ``instructions``, ``places``, ``payroll``, …).
    """
    return "'" + "', '".join(values) + "'"


class Notification(Base):
    """In-app notification row — the unread-fanout primary source.

    One row per delivered notification. Rendered in the §14 bell menu
    and consumed by the read-state endpoints (``PATCH
    /notifications/{id}/read``, bulk mark-read). ``payload_json``
    carries the free-form template context the renderer uses to build
    subject + body from locale-specific strings — stored rather than
    re-derived so the notification survives the underlying row's
    evolution (a task whose title changes still reads back the
    original subject). ``read_at`` is NULL on unread rows — the
    hot-path index ``(workspace_id, recipient_user_id, read_at)``
    keeps the bell menu's "unread count" cheap.

    FK hygiene:

    * ``workspace_id`` CASCADE — sweeping a workspace sweeps its
      notifications.
    * ``recipient_user_id`` CASCADE — a user's notifications do not
      outlive the user.
    """

    __tablename__ = "notification"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The ``kind`` enum drives renderer + icon selection in the bell
    # menu. See ``_NOTIFICATION_KIND_VALUES`` for the v1 taxonomy;
    # new kinds extend the enum via an additive migration.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # Human-visible subject line. Rendered server-side so the bell
    # menu stays backend-agnostic (no client-side templating).
    subject: Mapped[str] = mapped_column(String, nullable=False)
    # Markdown body — optional. Short notifications (a task became
    # overdue, an expense was approved) often render from ``subject``
    # alone; the richer in-app card uses ``body_md`` when set.
    body_md: Mapped[str | None] = mapped_column(String, nullable=True)
    # Read marker. ``NULL`` on unread rows — the hot-path index
    # ``(workspace_id, recipient_user_id, read_at)`` keeps the
    # "unread count" cheap because NULL stays sortable as a leading
    # value on SQLite and PG.
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Free-form render payload — the locale-specific context the
    # renderer consults (task title, property name, amount, actor).
    # The outer ``Any`` is scoped to SQLAlchemy's JSON column type —
    # callers writing a typed payload should use a TypedDict locally
    # and coerce into this column. Empty mapping by default.
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_NOTIFICATION_KIND_VALUES)})",
            name="kind",
        ),
        # Per-acceptance: "unread notifications for this user". The
        # leading ``workspace_id`` lets the tenant filter ride the
        # same B-tree; ``recipient_user_id`` carries the equality
        # filter; ``read_at`` carries the "NULL = unread" predicate.
        Index(
            "ix_notification_workspace_recipient_read",
            "workspace_id",
            "recipient_user_id",
            "read_at",
        ),
    )


class PushToken(Base):
    """Web-push subscription registered by a logged-in user.

    One row per ``(user_id, endpoint)`` pair — the browser's push
    subscription is identified by ``endpoint`` (the service-worker
    URL), with ``p256dh`` + ``auth`` carrying the VAPID encryption
    keys. Registered from the user's ``/me`` surface; the §10 agent-
    delivery worker walks active rows (``last_used_at`` within the
    60-day freshness window per §10 "Agent-message delivery" tier 2).

    ``user_agent`` is the browser's ``User-Agent`` snapshot at
    registration time — used on ``/me`` to render "Chrome on
    Pixel 9" alongside an unlink button; stored as text so future
    UA formats do not force a schema change. The field is PII-
    adjacent and never logged outside the audit trail.

    FK hygiene:

    * ``workspace_id`` CASCADE — sweeping a workspace sweeps its
      push tokens (matches the §10 fanout semantics: a revoked
      workspace should stop waking the device).
    * ``user_id`` CASCADE — a user's tokens do not outlive the
      user.

    §02 §"user_push_token" defines a richer identity-scoped shape
    (one row per install, delivering for every workspace the user
    belongs to). The cd-pjm slice lands the workspace-scoped
    variant first to match the tenant-filter convention the rest
    of messaging follows; a follow-up promotes the table to the
    identity scope once the native-app project lights up and the
    §12 ``/me/push-tokens`` surface stops returning ``501
    push_unavailable``.
    """

    __tablename__ = "push_token"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Service-worker URL — the Web-Push subscription's identifier.
    # Long, opaque, per-browser-install; never displayed to the user.
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    # VAPID-style encryption keys supplied by the browser's
    # ``PushSubscription``. Both are raw base64url-encoded public
    # material — not credentials per se, but treated as PII and
    # never logged.
    p256dh: Mapped[str] = mapped_column(String, nullable=False)
    auth: Mapped[str] = mapped_column(String, nullable=False)
    # ``User-Agent`` snapshot at registration. Rendered on ``/me``
    # alongside an unlink action; see class docstring.
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Bumped on every successful push delivery. Drives the 60-day
    # freshness check in the §10 delivery worker; stale tokens are
    # skipped silently (and the row eventually purged by a sweep).
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # "All active tokens for this user in this workspace" — the
        # fanout hot path. Leading ``workspace_id`` lets the tenant
        # filter ride the same B-tree.
        Index("ix_push_token_workspace_user", "workspace_id", "user_id"),
    )


class DigestRecord(Base):
    """Daily / weekly email-digest send ledger.

    One row per digest actually emitted to a recipient. The §10
    digest worker writes this row **after** the SMTP send returns
    ``250 OK`` so a crash mid-fanout does not create a phantom
    "already sent" marker; the ``(workspace_id, recipient_user_id,
    period_start, kind)`` composite is the natural idempotency key
    the worker consults before emitting (a duplicate run for the
    same ``period_start`` skips).

    ``body_md`` snapshots the rendered markdown body so a support
    query ("did Maria receive yesterday's digest?") can replay the
    exact content without re-deriving it from live data (which
    would drift: a stay might have been amended, a task completed
    after the digest was cut).

    FK hygiene matches :class:`Notification` — workspace + recipient
    both CASCADE.
    """

    __tablename__ = "digest_record"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    recipient_user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Digest window. ``period_start`` / ``period_end`` are UTC instants
    # — the digest worker resolves "07:00 local time" against the
    # recipient's timezone before computing these bounds. ``DateTime
    # (timezone=True)`` keeps the round-trip portable (Postgres
    # ``TIMESTAMP WITH TIME ZONE``, SQLite ISO-8601 UTC text).
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # ``daily`` | ``weekly``. See ``_DIGEST_KIND_VALUES`` for the v1
    # cadence options.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # Rendered markdown snapshot of the digest body at send time. Used
    # for support / replay — never mutated after insert.
    body_md: Mapped[str] = mapped_column(String, nullable=False)
    # Stamped when the SMTP send returned ``250 OK`` (or the adapter
    # equivalent). A NULL ``sent_at`` after an insert is a data bug
    # the domain layer guards against; the column stays nullable so
    # the row can be inserted atomically alongside the retry-later
    # paths a later worker surfaces.
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_DIGEST_KIND_VALUES)})",
            name="kind",
        ),
        CheckConstraint(
            "period_end > period_start",
            name="period_end_after_start",
        ),
        # "Did we already send today's digest to this recipient?" —
        # the idempotency check the worker runs before emitting.
        Index(
            "ix_digest_record_workspace_recipient_period",
            "workspace_id",
            "recipient_user_id",
            "period_start",
        ),
    )


class ChatChannel(Base):
    """A conversation thread.

    In-app channels (``kind = 'staff' | 'manager'``) are created at
    membership time; gateway channels (``kind = 'chat_gateway'``) are
    created on the first inbound from an external sender whose
    address matches an active binding. ``external_ref`` is the
    channel-native identifier the gateway writes for inbound-
    channel rows (the Meta ``waId`` for WhatsApp, Telegram
    ``chat_id``, email ``Message-ID``) — it is the primary lookup
    key for the adapter when it needs to route a subsequent inbound
    back to the same channel. ``NULL`` on in-app channels where
    there is no external counterpart.

    ``title`` is the display label (``"Villa Cap Ferrat — team"``,
    ``"WhatsApp: +33 6 …"``). Nullable so a fresh gateway channel
    can be created before the display metadata has been backfilled.

    FK hygiene: ``workspace_id`` CASCADE — sweeping a workspace
    sweeps its channels.
    """

    __tablename__ = "chat_channel"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``staff`` | ``manager`` | ``chat_gateway``. See
    # ``_CHAT_CHANNEL_KIND_VALUES`` for the v1 taxonomy.
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # Transport that surfaced the channel. Always ``app`` for in-app
    # channels; one of ``whatsapp`` / ``sms`` / ``email`` for gateway
    # channels.
    source: Mapped[str] = mapped_column(String, nullable=False)
    # Channel-native identifier for gateway-inbound channels. See
    # class docstring. Plain :class:`str` soft-ref — no FK because
    # the targets are opaque per-provider strings (a WhatsApp
    # ``waId``, a Telegram ``chat_id``) rather than rows we own.
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # Display label. Nullable for freshly-minted gateway channels
    # pending backfill.
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_CHAT_CHANNEL_KIND_VALUES)})",
            name="kind",
        ),
        CheckConstraint(
            f"source IN ({_in_clause(_CHAT_CHANNEL_SOURCE_VALUES)})",
            name="source",
        ),
        # "Channels in this workspace" — the list surface's hot path.
        Index("ix_chat_channel_workspace", "workspace_id"),
        # "Look up by external ref" — the gateway-inbound router
        # consults this when deciding whether an inbound from a known
        # provider id already has a channel. Partial ``IS NOT NULL``
        # would be cheaper but is not portably expressible across
        # SQLite + PG at the Alembic layer via plain
        # :class:`~sqlalchemy.Index` arguments without per-dialect
        # ``_where`` kwargs; the unrestricted index is sufficient for
        # v1 volumes.
        Index(
            "ix_chat_channel_workspace_external_ref",
            "workspace_id",
            "external_ref",
        ),
    )


class ChatMessage(Base):
    """A single turn in a :class:`ChatChannel`.

    Rows land in one of two shapes:

    * **Authored** — ``author_user_id`` is set to the speaker's
      :class:`User.id`. Authored rows are the in-app web surfaces'
      inputs (a manager typing in ``.desk__agent``, a worker
      replying on ``/chat``) and the agent's own outbound turns
      (``author_label = 'agent'`` + ``author_user_id`` pointing at
      the delegated actor per §11).
    * **Gateway-inbound** — ``author_user_id`` is ``NULL``; the
      external sender has no :class:`User` row. The binding's
      display label flows into ``author_label`` so the in-app
      rendering surfaces ``"WhatsApp: Maria"`` without joining
      back through the binding table.

    ``dispatched_to_agent_at`` tracks the §23 async dispatch handoff
    for gateway-inbound rows. ``NULL`` on in-app rows (those turns
    run synchronously inside ``POST /api/v1/agent/*`` per §11) and
    on outbound rows. The v1 slice lands the timestamp only; the
    fuller state machine (``pending | dispatching | dispatched |
    failed``) per §23 lands with the gateway-service follow-up.

    FK hygiene:

    * ``workspace_id`` CASCADE — sweeping a workspace sweeps its
      chat history.
    * ``channel_id`` CASCADE — deleting a channel sweeps its
      messages; messages are not independently useful.
    * ``author_user_id`` SET NULL — a user delete must not nuke
      thread history (audit trail survives). Nullable on the
      schema side so gateway-inbound rows land with no author.
    """

    __tablename__ = "chat_message"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("chat_channel.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Display label for the speaker — ``"Maria"`` for a user-authored
    # row, ``"agent"`` for an agent turn, ``"WhatsApp: Maria"`` for a
    # gateway-inbound row. Denormalised so the list view does not
    # need to join back through ``user`` / the binding table for
    # every row.
    author_label: Mapped[str] = mapped_column(String, nullable=False)
    # Markdown body. The §23 translation path stores both the
    # original and the translated copy on later fields (not in this
    # slice); ``body_md`` is the displayed-to-recipient form.
    body_md: Mapped[str] = mapped_column(String, nullable=False)
    # List of ``{blob_hash, filename, …}`` payloads — matches the
    # ``comment.attachments_json`` shape on :mod:`app.adapters.db.
    # tasks`. The outer ``Any`` is scoped to SQLAlchemy's JSON
    # column type — callers writing a typed payload should use a
    # TypedDict locally and coerce into this column.
    attachments_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Set when the gateway dispatcher handed the row to the agent
    # runtime. ``NULL`` on in-app rows (synchronous per §11) and on
    # outbound rows; populated on gateway-inbound rows once the
    # dispatcher CASes them out of ``pending``. See class docstring
    # for the fuller state-machine follow-up.
    dispatched_to_agent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # Per-acceptance: "messages in this channel, newest / oldest
        # first" is the scrollback's hot path. ``channel_id`` first
        # because the common query pins the channel; ``created_at``
        # second carries the time ordering.
        Index(
            "ix_chat_message_channel_created",
            "channel_id",
            "created_at",
        ),
        # Per-acceptance: "messages in this workspace for this
        # channel" — the tenant filter rides the leading
        # ``workspace_id`` and the scoped cross-channel queries
        # (manager's "unread across every chat") ride the composite.
        Index(
            "ix_chat_message_workspace_channel",
            "workspace_id",
            "channel_id",
        ),
    )


class EmailOptOut(Base):
    """Per-user per-category unsubscribe marker.

    One row per ``(workspace_id, user_id, category)`` triple — the
    §10 delivery worker consults this table before emitting any
    opt-outable email. ``category`` lines up with the ``template_key``
    family on :class:`EmailDelivery`; the taxonomy is maintained in
    §10 (e.g. ``task_reminder``, ``daily_digest``,
    ``holiday_schedule_impact``, ``invoice_reminder``). Required
    categories (magic link, payslip issued, expense decision, issue
    reported, agent approval pending) are never suppressed even if a
    row exists — the row is kept for audit but ignored for those
    templates at the domain layer.

    ``source`` pins how the row was created so audit can trace the
    opt-out back to the action: ``unsubscribe_link`` (signed footer
    link), ``profile`` (owner/manager/worker toggling on ``/me``),
    ``admin`` (support override on the user's behalf).

    FK hygiene: both ``workspace_id`` and ``user_id`` CASCADE —
    sweeping a workspace or a user sweeps their opt-outs. An
    archived user is a distinct concern handled in the domain layer.
    """

    __tablename__ = "email_opt_out"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Category key — matches :class:`EmailDelivery.template_key`
    # family. Plain ``String`` (no length) per the sibling-table
    # convention across this package + ``tasks`` / ``instructions`` /
    # ``places`` / ``payroll`` — new keys land without a migration
    # (the column is a plain string rather than an enum).
    category: Mapped[str] = mapped_column(String, nullable=False)
    opted_out_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # ``unsubscribe_link`` | ``profile`` | ``admin`` per §10. See
    # ``_EMAIL_OPT_OUT_SOURCE_VALUES`` for the v1 enum body.
    source: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"source IN ({_in_clause(_EMAIL_OPT_OUT_SOURCE_VALUES)})",
            name="source",
        ),
        # One row per user+category within a workspace — the worker's
        # pre-send probe consults this triple, and a duplicate row
        # would be a data bug (two "opted out at" timestamps for the
        # same preference). The unique index also powers the probe
        # query cheaply.
        Index(
            "uq_email_opt_out_user_category",
            "workspace_id",
            "user_id",
            "category",
            unique=True,
        ),
        # Per-user lookup: "what has this user opted out of?" —
        # surfaces on ``/me`` and in audit. The composite also carries
        # the tenant filter on its leading ``workspace_id``.
        Index(
            "ix_email_opt_out_workspace_user",
            "workspace_id",
            "user_id",
        ),
    )


class EmailDelivery(Base):
    """Per-message delivery ledger — the bounce / retry / audit source.

    One row per email the worker queues; ``delivery_state`` walks
    ``queued → sent → delivered`` on the happy path and
    ``queued → sent → bounced`` / ``queued → failed`` on the error
    paths. ``first_error`` snapshots the adapter's first failure
    message so a support query can answer "why did this bounce?"
    without replaying the SMTP log; ``retry_count`` drives the §10
    worker's exponential back-off. ``provider_message_id`` is the
    SMTP / ESP-issued id the bounce-reply correlator keys on when
    an inbound bounce notification arrives (see §23 — matched via
    ``(workspace_id, provider_message_id)``).

    ``to_person_id`` is a **soft reference** — a plain :class:`str`
    with no FK. §10 reminders can target a ``client_user`` that has
    not yet been materialised into the :class:`User` table when the
    email is queued (invoice reminder to a biller's client, stay-
    upcoming email to an external guest). The row must insert even
    when the id points at a row that doesn't exist yet; the domain
    layer resolves the pointer at render time.

    ``context_snapshot_json`` freezes the renderer context at queue
    time so a replay / audit reads the exact subject + body the
    recipient saw, even if the underlying task / stay / expense
    evolves afterwards — matches the :class:`Notification.payload_
    json` / :class:`ChatMessage.attachments_json` convention.

    FK hygiene: ``workspace_id`` CASCADE — sweeping a workspace
    sweeps its delivery ledger. No user FK: see ``to_person_id``.
    """

    __tablename__ = "email_delivery"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Soft reference — no FK. See class docstring for the client_user
    # rationale (recipient may not yet be in the ``user`` table when
    # the email is queued).
    to_person_id: Mapped[str] = mapped_column(String, nullable=False)
    # Snapshot of the destination address at send time. Stored so a
    # later profile-edit doesn't retcon the audit trail: the row
    # answers "who did we actually send this to?" verbatim. RFC 5321
    # caps addr-spec at 320 chars (64 local + '@' + 255 domain); we
    # store as plain ``String`` per the sibling-table convention
    # (validation lives in the domain layer, not the schema).
    to_email_at_send: Mapped[str] = mapped_column(String, nullable=False)
    # Renderer template key — lines up with
    # :class:`EmailOptOut.category` for the opt-out probe and with
    # the §10 "Emails the system sends" catalogue.
    template_key: Mapped[str] = mapped_column(String, nullable=False)
    # Free-form render context at queue time. The outer ``Any`` is
    # scoped to SQLAlchemy's JSON column type — callers writing a
    # typed payload should use a TypedDict locally and coerce into
    # this column. Empty mapping default matches
    # :class:`Notification.payload_json`.
    context_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # Set when the SMTP / ESP dispatch returned success. ``NULL`` on
    # queued rows and on rows the worker bounced off before the
    # first dispatch attempt.
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Provider-issued id for bounce-reply correlation. ``NULL`` until
    # the first dispatch attempt lands; then carries the ESP's
    # message id (conventionally the RFC 5322 Message-ID header).
    provider_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Walks ``queued → sent → delivered`` on success,
    # ``queued → sent → bounced`` / ``queued → failed`` on error.
    # See ``_EMAIL_DELIVERY_STATE_VALUES``.
    delivery_state: Mapped[str] = mapped_column(String, nullable=False)
    # First adapter-level error string, if any. Stored on first
    # failure and not overwritten on subsequent retries so support can
    # answer "what went wrong initially?". ``Text`` rather than
    # ``String`` because SMTP / ESP error bodies can be long and
    # multi-line (matches sibling ``issue.description_md`` usage).
    first_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bumped on each retry attempt. Drives the §10 worker's back-off
    # schedule; the worker gives up and transitions to ``failed``
    # once the budget is exhausted.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Optional reply-tracking token — when set, an inbound bounce
    # notification that carries this opaque string links back to
    # the originating ledger row. Separate from
    # ``provider_message_id`` because some adapters surface only one
    # or the other (ESP ``Message-ID`` vs custom VERP token).
    inbound_linkage: Mapped[str | None] = mapped_column(String, nullable=True)
    # Row-create timestamp — set when the worker queues the email.
    # NOT NULL and never updated after insert.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"delivery_state IN ({_in_clause(_EMAIL_DELIVERY_STATE_VALUES)})",
            name="delivery_state",
        ),
        # Audit hot path: "every email sent to this person, newest
        # first". Tenant filter rides the leading ``workspace_id``;
        # ``to_person_id`` carries the equality filter;
        # ``sent_at`` carries the ordering (and stays NULL-safe on
        # both backends as a trailing key).
        Index(
            "ix_email_delivery_workspace_person_sent",
            "workspace_id",
            "to_person_id",
            "sent_at",
        ),
        # Bounce-reply correlator: "find the ledger row this ESP
        # bounce belongs to". A partial ``IS NOT NULL`` would be
        # cheaper but is not portably expressible across SQLite + PG
        # at the Alembic layer without per-dialect ``_where`` kwargs;
        # the unrestricted index is sufficient for v1 volumes.
        Index(
            "ix_email_delivery_workspace_provider_msgid",
            "workspace_id",
            "provider_message_id",
        ),
        # Retry-scheduler hot path: "pending rows oldest first". The
        # worker orders by ``sent_at`` within a state bucket (queued
        # / failed) to emit the longest-waiting row first.
        Index(
            "ix_email_delivery_workspace_state_sent",
            "workspace_id",
            "delivery_state",
            "sent_at",
        ),
    )
