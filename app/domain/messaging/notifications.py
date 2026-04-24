"""NotificationService — multi-channel fanout for in-app notifications.

One public entry point: :meth:`NotificationService.notify`. Given a
recipient + :class:`NotificationKind` + free-form payload, the service:

1. Persists a :class:`~app.adapters.db.messaging.models.Notification`
   row via the caller's open :class:`~sqlalchemy.orm.Session`.
2. Publishes a :class:`~app.events.types.NotificationCreated` SSE
   event so the recipient's tabs can invalidate their unread-count
   + notification-list caches immediately.
3. Renders the matching email template (subject + body_md) and hands
   it to the injected :class:`~app.adapters.mail.ports.Mailer` — skipped
   when the recipient has an opt-out row for ``(workspace_id, user_id,
   category)`` where ``category`` is the kind's string value.
4. Renders the matching push template and enqueues a push job via
   the injected ``push_enqueue`` callable — skipped when the recipient
   has zero :class:`~app.adapters.db.messaging.models.PushToken` rows
   in this workspace.
5. Writes one :mod:`app.audit` row per *attempted* channel
   (``messaging.notification.dispatched``, ``channel=<inbox|sse|
   email|push>``, ``action`` carrying the kind + recipient).

**Transaction boundary.** The service writes to the caller's open
session (``session.add`` + one ``flush`` to realise the row id for the
event payload) but **never commits**. The caller's Unit of Work owns
transaction boundaries per §01 "Key runtime invariants" #3. The SSE
publish fires inside the UoW, so a handler that raises rolls the
whole notification back — audit row, notification row, email queue
entry if you are wiring ``push_enqueue`` to the outbox.

**Template resolution.** Templates live under
:mod:`app.domain.messaging.templates` as ``<kind>.<channel>.j2`` files
(no locale) or ``<kind>.<locale>.<channel>.j2`` (with locale). The
resolver tries the locale-specific file first, then falls back to the
locale-free default. A missing **kind** template (no default file) is
a loud failure — the service raises :class:`TemplateNotFound` rather
than silently skipping. A missing **locale** variant falls back to
English defaults: the spec (§10 "Locale-aware template resolution")
ships English defaults in v1 and uses the fallback chain as the
feature-flag equivalent for future localised copy.

**Channel selection.** Every kind writes the inbox row + publishes
the SSE event. Email and push only fire when a template exists for
the channel. A kind whose push template is absent silently skips
push (the audit row for push is still written if the recipient has
at least one active push token — the skip is "no template" not "no
tokens", and the auditor makes that visible).

**Opt-out semantics.** The email opt-out check compares
``EmailOptOut.category == kind.value``. A catch-all wildcard
(``category='*'``) also suppresses — a workspace admin toggling
"silence all emails" for a user does not need to list every kind.
Required categories (magic link, payslip issued, expense decision,
issue reported, agent approval pending per §10) are handled by
their sending services choosing **not** to call this service for
those flows — the spec explicitly documents that mapping. See
``docs/specs/10-messaging-notifications.md`` §"Email" →
§"``email_opt_out``".

Public surface:

* :class:`NotificationKind` — string-backed enum mirroring
  :data:`~app.adapters.db.messaging.models._NOTIFICATION_KIND_VALUES`.
* :class:`NotificationService` — the ergonomics wrapper the caller
  instantiates with the ambient context + deps.
* :class:`TemplateNotFound` — raised when the kind's default template
  does not exist on disk.
* :data:`TEMPLATE_ROOT` — absolute path to the template directory.
  Exposed for tests that want to point a Jinja :class:`FileSystemLoader`
  at the same tree.

See ``docs/specs/10-messaging-notifications.md`` §"Channels", §"Email
template system", §"In-app messaging", §"Agent-message delivery".

**Shape: dataclass service, not module functions.** Sibling domain
contexts (:mod:`app.domain.llm.router`, :mod:`app.domain.time.shifts`,
:mod:`app.domain.tasks.oneoff`) expose module-level functions that
take ``(session, ctx, *, ...)`` per call. :class:`NotificationService`
is a deliberate departure: fanout needs seven injected collaborators
(session, ctx, mailer, clock, bus, push_enqueue, templates) and they
are all consulted inside one logical operation. Threading every one
through ``notify()`` and every private helper would push the call
site to a wall of positional arguments; bundling them once on a
:func:`dataclasses.dataclass(frozen=True, slots=True)` container keeps
the hot signature narrow (``notify(recipient_user_id, kind, payload)``)
without giving up the per-helper access to the adapters. The frozen
slot-dataclass carries the same ergonomics as a
:class:`~typing.NamedTuple`, compiles to ``__slots__``, and — unlike a
class with ``__init__`` — refuses mutation, so a handler cannot rewrite
``service.mailer`` mid-call. Future services with the same shape should
follow this pattern; services with two or three dependencies should
stay module-level per the prevailing convention.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from jinja2 import TemplateNotFound as _JinjaTemplateNotFound
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.messaging.models import (
    _NOTIFICATION_KIND_VALUES,
    EmailOptOut,
    Notification,
    PushToken,
)
from app.adapters.mail.ports import Mailer
from app.audit import write_audit
from app.events import NotificationCreated
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "TEMPLATE_ROOT",
    "NotificationKind",
    "NotificationService",
    "PushEnqueue",
    "TemplateLoader",
    "TemplateNotFound",
]


# ---------------------------------------------------------------------------
# Notification kind enum
# ---------------------------------------------------------------------------


class NotificationKind(enum.StrEnum):
    """String-valued enum mirroring the DB CHECK constraint.

    Each value is the on-disk template-name segment (``task_assigned``
    → ``task_assigned.subject.j2``) and the
    :class:`~app.adapters.db.messaging.models.EmailOptOut.category`
    value the pre-send probe consults. Keeping one source of truth
    per kind avoids a ``kind → category`` mapping drifting between
    the inbox row and the opt-out table.

    Widening the enum is a coordinated change: extend this enum,
    extend :data:`~app.adapters.db.messaging.models._NOTIFICATION_KIND_VALUES`
    (or widen the CHECK via an additive migration), and add the
    templates. The ``__post_init__`` assertion below refuses to import
    if the two enumerations drift.
    """

    TASK_ASSIGNED = "task_assigned"
    TASK_OVERDUE = "task_overdue"
    EXPENSE_APPROVED = "expense_approved"
    EXPENSE_REJECTED = "expense_rejected"
    EXPENSE_SUBMITTED = "expense_submitted"
    APPROVAL_NEEDED = "approval_needed"
    APPROVAL_DECIDED = "approval_decided"
    ISSUE_REPORTED = "issue_reported"
    ISSUE_RESOLVED = "issue_resolved"
    COMMENT_MENTION = "comment_mention"
    PAYSLIP_ISSUED = "payslip_issued"
    STAY_UPCOMING = "stay_upcoming"
    ANOMALY_DETECTED = "anomaly_detected"
    AGENT_MESSAGE = "agent_message"


# Import-time invariant: the enum and the DB CHECK must stay aligned.
# If a migration widens the CHECK, the enum must follow (or vice
# versa); either way a notify() call with a kind the CHECK rejects
# would fail at insert time. Fail at import rather than at first
# call so the drift is caught in CI and not in production.
_ENUM_VALUES = frozenset(k.value for k in NotificationKind)
_DB_VALUES = frozenset(_NOTIFICATION_KIND_VALUES)
if _ENUM_VALUES != _DB_VALUES:
    # ``assert`` would be stripped under ``-O``; a plain RuntimeError
    # survives.
    raise RuntimeError(
        "NotificationKind enum and DB CHECK drift detected: "
        f"enum-only={_ENUM_VALUES - _DB_VALUES}, "
        f"db-only={_DB_VALUES - _ENUM_VALUES}."
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TemplateNotFound(LookupError):
    """The kind has no default (English) template for the requested channel.

    Raised LOUDLY rather than silently skipped. A notify() call whose
    kind's template file is absent is almost always a caller bug: the
    caller added a new :class:`NotificationKind` value without
    shipping the templates, or renamed the kind without renaming the
    files. Silent skip would make that bug invisible — the recipient
    would miss every email for the new kind and nobody would notice
    until a support ticket lands.

    Carries ``kind`` + ``channel`` (and ``locale`` for diagnostics)
    so the error message points the operator directly at the missing
    file(s).
    """

    def __init__(
        self,
        *,
        kind: str,
        channel: str,
        locale: str | None = None,
    ) -> None:
        self.kind = kind
        self.channel = channel
        self.locale = locale
        locale_hint = f" (locale={locale!r})" if locale else ""
        super().__init__(
            f"No template found for kind={kind!r} channel={channel!r}"
            f"{locale_hint}; expected "
            f"{TEMPLATE_ROOT}/{kind}.{channel}.j2 to exist."
        )


# ---------------------------------------------------------------------------
# Template loader protocol + default implementation
# ---------------------------------------------------------------------------


# Absolute path to the Jinja2 template directory. Exposed so tests
# can point their own FileSystemLoader at the same tree (a stand-alone
# constant beats reaching into the class for the path — and it
# documents the on-disk contract).
TEMPLATE_ROOT: Path = Path(__file__).resolve().parent / "templates"


class TemplateLoader(Protocol):
    """Render a template for (``kind``, ``locale``, ``channel``).

    Implementations resolve the locale fallback internally:

    * Try ``<kind>.<locale>.<channel>.j2`` first.
    * Fall back to ``<kind>.<channel>.j2`` (English default).
    * Raise :class:`TemplateNotFound` if the default is also absent.

    The contract is "find + render in one call" because Jinja's
    resolver is already set up to do exactly that — splitting into
    ``find`` + ``render`` would double the I/O without buying
    anything.

    ``context`` is the Jinja context dict. Extra keys the template
    does not use are ignored; missing keys referenced by the template
    raise ``UndefinedError`` (Jinja's :class:`StrictUndefined`
    default in :class:`Jinja2TemplateLoader`).
    """

    def render(
        self,
        *,
        kind: str,
        locale: str | None,
        channel: str,
        context: Mapping[str, Any],
    ) -> str:
        """Return the rendered template body."""
        ...


@dataclass(frozen=True, slots=True)
class Jinja2TemplateLoader:
    """Default :class:`TemplateLoader` backed by a Jinja2 environment.

    ``env`` is a bare Jinja :class:`~jinja2.Environment` with:

    * :class:`StrictUndefined` — a template referencing a missing
      key raises at render time instead of silently emitting the
      empty string; the service turns the error into a clear
      exception the caller sees.
    * :func:`select_autoescape` — HTML escaping on ``.html`` / ``.j2``
      files defensively. The v1 templates are Markdown (``body_md``)
      and plaintext (subject + push), so autoescape is a no-op on
      them; wiring it here keeps the door open for the MJML / HTML
      variants the spec calls out without a second env later.

    The loader caches compiled templates via Jinja's built-in cache;
    no process-level invalidation needed for v1 (templates live in
    the wheel, not on a hot-reload path).
    """

    env: Environment

    @classmethod
    def default(cls) -> Jinja2TemplateLoader:
        """Return a loader wired to :data:`TEMPLATE_ROOT`.

        The production path. Tests that point at a temp directory
        construct the environment themselves.
        """
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_ROOT)),
            autoescape=select_autoescape(["html", "j2"]),
            undefined=StrictUndefined,
            # ``trim_blocks`` + ``lstrip_blocks`` keep block-tag
            # whitespace honest in Markdown bodies — without them the
            # ``{% if %}`` chain in :file:`task_assigned.body_md.j2`
            # would leak blank lines into the rendered output.
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )
        return cls(env=env)

    def render(
        self,
        *,
        kind: str,
        locale: str | None,
        channel: str,
        context: Mapping[str, Any],
    ) -> str:
        """Resolve + render ``<kind>[.<locale>].<channel>.j2``.

        Tries the locale-specific file first, then the locale-free
        default. Raises :class:`TemplateNotFound` when the default
        is also missing — a missing *locale* variant silently falls
        back, a missing *default* is a loud error per the
        module docstring.
        """
        # Candidate file names, in resolution order. A locale like
        # ``fr-CA`` is normalised below to try both the full tag and
        # the language-only prefix — matches the spec's "``{key}_
        # {locale}`` then ``{key}_{language}``" pattern from §10
        # "Locale-aware template resolution".
        candidates: list[str] = []
        if locale:
            candidates.append(f"{kind}.{locale}.{channel}.j2")
            # Strip region for BCP-47 tags like ``fr-CA`` → ``fr``.
            language = locale.split("-", 1)[0]
            if language != locale:
                candidates.append(f"{kind}.{language}.{channel}.j2")
        candidates.append(f"{kind}.{channel}.j2")

        for name in candidates:
            try:
                template = self.env.get_template(name)
            except _JinjaTemplateNotFound:
                continue
            return template.render(**context)

        raise TemplateNotFound(kind=kind, channel=channel, locale=locale)


# ---------------------------------------------------------------------------
# Push-enqueue callable signature
# ---------------------------------------------------------------------------


# Callable seam for web-push dispatch. v1 does not ship a concrete
# worker (the native-app project is future work per §10
# "Agent-message delivery" v1 scope note) so the caller injects the
# function that knows how to enqueue; a test-only implementation
# records into an in-memory list. Signature matches what the
# eventual :func:`app.worker.push.enqueue_push` will expose so the
# swap is a drop-in.
#
# ``user_id`` is the recipient. ``kind`` is the notification kind's
# string value (used for telemetry on the push adapter). ``body`` is
# the rendered push copy — the short envelope per §10 "Agent-message
# delivery" tier 2 (server never ships full message body in push).
# ``payload`` is the structured context the template consumed,
# forwarded so the push adapter can build a deep-link URL without
# re-resolving the entity.
PushEnqueue = Callable[
    [str, str, str, Mapping[str, Any]],
    None,
]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


# Channel strings. Kept as module-level constants so the audit
# writer's ``action`` field and the template loader's ``channel``
# argument cannot drift (a typo in one would silently route past
# the other).
_CHANNEL_INBOX = "inbox"
_CHANNEL_SSE = "sse"
_CHANNEL_EMAIL = "email"
_CHANNEL_PUSH = "push"

# Wildcard opt-out category — a single row with ``category = '*'``
# suppresses email for every kind for that user. Kept as a
# module-level constant so the probe body is self-documenting.
_OPT_OUT_WILDCARD = "*"


@dataclass(frozen=True, slots=True)
class NotificationService:
    """Fan out a notification to inbox + SSE + email + push.

    Construct once per request (or reuse across a Unit of Work) with
    the ambient :class:`WorkspaceContext` + injected adapters:

    * ``session`` — open SQLAlchemy session for the caller's UoW.
    * ``ctx`` — resolved tenancy + actor context.
    * ``mailer`` — :class:`~app.adapters.mail.ports.Mailer` for the
      email channel.
    * ``clock`` — time source for the notification's ``created_at``
      and the audit row's timestamp.
    * ``bus`` — event bus to publish :class:`NotificationCreated`
      on. Defaults to the production singleton; tests inject a
      fresh :class:`EventBus`.
    * ``push_enqueue`` — callable that takes the rendered push body
      and hands it to the web-push worker. v1 does not ship a
      concrete worker (see §10 "v1 scope note"); the caller wires
      either a no-op or an in-memory queue until the native-app
      project lands.
    * ``templates`` — :class:`TemplateLoader` implementation.
      Defaults to :meth:`Jinja2TemplateLoader.default`.

    The envelope-from address is **not** a per-service override: the
    :class:`~app.adapters.mail.ports.Mailer` protocol owns the
    ``From:`` header (the SMTP adapter fills it from ``MAIL_FROM``).
    A service-layer ``from_email`` field would be dead weight — the
    port has no channel to receive it. If a deployment needs
    per-kind From addresses, the right surface is a second Mailer
    instance (one per envelope) wired into a second service, not a
    field here that the underlying protocol ignores.
    """

    session: Session
    ctx: WorkspaceContext
    mailer: Mailer
    # ``default_factory`` rather than a plain default so mutable-looking
    # singletons (``SystemClock()``, the module-level ``bus``) are
    # resolved at construction time — matches the RUF009 guidance and
    # keeps test doubles injected per-instance without aliasing.
    clock: Clock = field(default_factory=SystemClock)
    bus: EventBus = field(default_factory=lambda: default_event_bus)
    push_enqueue: PushEnqueue | None = None
    templates: TemplateLoader | None = None

    # ---- Public entry point -----------------------------------------

    def notify(
        self,
        *,
        recipient_user_id: str,
        kind: NotificationKind,
        payload: Mapping[str, Any],
    ) -> str:
        """Persist + fan out one notification. Returns the row id.

        Dispatch order — inbox, SSE, email, push — is fixed so the
        caller reads a deterministic sequence of audit rows.

        The inbox row always lands (no skip). The SSE event always
        publishes (no skip). Email is skipped when an opt-out row
        matches. Push is skipped when the recipient has zero active
        push tokens in the workspace. Every attempted channel writes
        one audit row; skipped channels write a row with
        ``action='messaging.notification.skipped'`` so the ledger
        remains honest.

        Raises:

        * :class:`TemplateNotFound` — missing default template file
          for email or push (no silent skip on authoring bugs).
        * :class:`~app.adapters.mail.ports.MailDeliveryError` — the
          mailer refused the send. The caller's UoW decides whether
          to roll back or continue; this service propagates the
          error without swallowing it.
        """
        # ---- Step 1: recipient lookup (locale + display name) -----
        recipient = self._load_recipient(recipient_user_id)

        # Template context: the caller's payload plus a few
        # convenience keys the templates commonly reference. We copy
        # to a plain dict so a Jinja template mutating the mapping
        # (it cannot, but defence-in-depth) doesn't scribble on the
        # caller's input.
        context: dict[str, Any] = dict(payload)
        context.setdefault("recipient_display_name", recipient.display_name)
        context.setdefault("recipient_user_id", recipient.id)
        context.setdefault("workspace_id", self.ctx.workspace_id)
        locale = recipient.locale  # may be None → English defaults

        templates = self.templates or Jinja2TemplateLoader.default()

        # ---- Step 2: render the subject / body / push up front ----
        # Rendering before the DB write keeps a template bug from
        # half-persisting (we'd rather fail fast with a
        # ``TemplateNotFound`` than land a row with an empty subject).
        # Subject + body are required (the inbox row stores them);
        # push is optional (skipped when the template is absent).
        subject = self._render_required(
            templates,
            kind=kind.value,
            locale=locale,
            channel="subject",
            context=context,
        ).strip()
        body_md = self._render_required(
            templates,
            kind=kind.value,
            locale=locale,
            channel="body_md",
            context=context,
        )
        push_body = self._render_optional(
            templates,
            kind=kind.value,
            locale=locale,
            channel="push",
            context=context,
        )
        if push_body is not None:
            push_body = push_body.strip()

        # ---- Step 3: persist the inbox row ------------------------
        now = self.clock.now()
        notification_id = new_ulid()
        row = Notification(
            id=notification_id,
            workspace_id=self.ctx.workspace_id,
            recipient_user_id=recipient.id,
            kind=kind.value,
            subject=subject,
            body_md=body_md,
            read_at=None,
            created_at=now,
            payload_json=dict(payload),
        )
        self.session.add(row)
        # Flush so the row id is realised + the FK is validated
        # BEFORE we publish the SSE event. A flush that fails here
        # (FK violation, CHECK rejection) rolls the caller's UoW
        # without having announced a ghost notification to the
        # client.
        self.session.flush()

        self._audit(
            entity_id=notification_id,
            channel=_CHANNEL_INBOX,
            kind=kind,
            recipient_user_id=recipient.id,
            action="messaging.notification.dispatched",
        )

        # ---- Step 4: SSE event ------------------------------------
        # Published BEFORE email / push so the web client's unread
        # badge updates the instant the row is visible; the outbound
        # channels race the browser's cache invalidation.
        self.bus.publish(
            NotificationCreated(
                workspace_id=self.ctx.workspace_id,
                actor_id=self.ctx.actor_id,
                correlation_id=self.ctx.audit_correlation_id,
                occurred_at=now,
                notification_id=notification_id,
                kind=kind.value,
                actor_user_id=recipient.id,
            )
        )
        self._audit(
            entity_id=notification_id,
            channel=_CHANNEL_SSE,
            kind=kind,
            recipient_user_id=recipient.id,
            action="messaging.notification.dispatched",
        )

        # ---- Step 5: email ---------------------------------------
        if self._email_opted_out(recipient.id, kind):
            self._audit(
                entity_id=notification_id,
                channel=_CHANNEL_EMAIL,
                kind=kind,
                recipient_user_id=recipient.id,
                action="messaging.notification.skipped",
                diff={"reason": "email_opt_out"},
            )
        elif not recipient.email:
            # Recipient has no on-file email (shouldn't happen once
            # identity guarantees it, but the column is nullable at
            # the schema level today — belt-and-braces). Record as
            # a skip so support can trace why a notification didn't
            # reach its inbox.
            self._audit(
                entity_id=notification_id,
                channel=_CHANNEL_EMAIL,
                kind=kind,
                recipient_user_id=recipient.id,
                action="messaging.notification.skipped",
                diff={"reason": "no_email_on_file"},
            )
        else:
            self.mailer.send(
                to=(recipient.email,),
                subject=subject,
                body_text=body_md,
                reply_to=None,
                headers={
                    "X-CrewDay-Notification-Id": notification_id,
                    "X-CrewDay-Notification-Kind": kind.value,
                },
            )
            self._audit(
                entity_id=notification_id,
                channel=_CHANNEL_EMAIL,
                kind=kind,
                recipient_user_id=recipient.id,
                action="messaging.notification.dispatched",
            )

        # ---- Step 6: push -----------------------------------------
        # Skipped (silently, with an audit row) when:
        #   * recipient has no active push tokens; OR
        #   * the kind has no push template.
        # The template-absent branch is recorded separately from the
        # no-tokens branch so the ledger can tell the operator which
        # side of the skip tripped.
        has_tokens = self._has_push_tokens(recipient.id)
        if not has_tokens:
            self._audit(
                entity_id=notification_id,
                channel=_CHANNEL_PUSH,
                kind=kind,
                recipient_user_id=recipient.id,
                action="messaging.notification.skipped",
                diff={"reason": "no_active_push_tokens"},
            )
        elif push_body is None:
            self._audit(
                entity_id=notification_id,
                channel=_CHANNEL_PUSH,
                kind=kind,
                recipient_user_id=recipient.id,
                action="messaging.notification.skipped",
                diff={"reason": "no_push_template"},
            )
        elif self.push_enqueue is None:
            # Service was constructed without a push worker seam.
            # This is a deployment-configuration miss, not a per-
            # notification skip — record it distinctly so ops can
            # notice on day one.
            self._audit(
                entity_id=notification_id,
                channel=_CHANNEL_PUSH,
                kind=kind,
                recipient_user_id=recipient.id,
                action="messaging.notification.skipped",
                diff={"reason": "push_enqueue_not_configured"},
            )
        else:
            self.push_enqueue(
                recipient.id,
                kind.value,
                push_body,
                dict(payload),
            )
            self._audit(
                entity_id=notification_id,
                channel=_CHANNEL_PUSH,
                kind=kind,
                recipient_user_id=recipient.id,
                action="messaging.notification.dispatched",
            )

        return notification_id

    # ---- Helpers ----------------------------------------------------

    def _load_recipient(self, recipient_user_id: str) -> User:
        """Return the :class:`User` row or raise :class:`LookupError`.

        The ``user`` table is NOT workspace-scoped, so the tenant
        filter does not apply here — every workspace shares the
        identity graph per §05. We still scope the ``email_opt_out`` /
        ``push_token`` checks to ``ctx.workspace_id`` below.
        """
        stmt = select(User).where(User.id == recipient_user_id)
        user = self.session.execute(stmt).scalar_one_or_none()
        if user is None:
            raise LookupError(f"recipient_user_id={recipient_user_id!r} not found")
        return user

    def _render_required(
        self,
        templates: TemplateLoader,
        *,
        kind: str,
        locale: str | None,
        channel: str,
        context: Mapping[str, Any],
    ) -> str:
        """Render a channel that MUST have a default template."""
        return templates.render(
            kind=kind,
            locale=locale,
            channel=channel,
            context=context,
        )

    def _render_optional(
        self,
        templates: TemplateLoader,
        *,
        kind: str,
        locale: str | None,
        channel: str,
        context: Mapping[str, Any],
    ) -> str | None:
        """Render ``channel`` if a default exists; ``None`` otherwise.

        Unlike :meth:`_render_required`, a missing default is NOT a
        loud failure — the push channel is spec'd as optional (§10
        v1 scope: push ships when the native-app project lights up).
        Every other missing-template path should go through
        :meth:`_render_required`.
        """
        try:
            return templates.render(
                kind=kind,
                locale=locale,
                channel=channel,
                context=context,
            )
        except TemplateNotFound:
            return None

    def _email_opted_out(
        self,
        recipient_user_id: str,
        kind: NotificationKind,
    ) -> bool:
        """Return ``True`` if the recipient has opted out of ``kind`` or ``*``.

        A row matching the exact kind wins over the wildcard; both
        are treated as "opted out". The query reads one row at most
        — the unique index ``uq_email_opt_out_user_category``
        guarantees it.
        """
        stmt = select(EmailOptOut.id).where(
            EmailOptOut.workspace_id == self.ctx.workspace_id,
            EmailOptOut.user_id == recipient_user_id,
            EmailOptOut.category.in_((kind.value, _OPT_OUT_WILDCARD)),
        )
        return self.session.execute(stmt).first() is not None

    def _has_push_tokens(self, recipient_user_id: str) -> bool:
        """Return ``True`` when the user has at least one token.

        v1 treats every present row as "active" — a freshness sweep
        (§10 "Agent-message delivery" tier 2's 60-day window) lands
        with the native-app project. Until then a stale token is
        still a token the worker will attempt; the worker surfaces
        delivery failures on its own path.
        """
        stmt = (
            select(PushToken.id)
            .where(
                PushToken.workspace_id == self.ctx.workspace_id,
                PushToken.user_id == recipient_user_id,
            )
            .limit(1)
        )
        return self.session.execute(stmt).first() is not None

    def _audit(
        self,
        *,
        entity_id: str,
        channel: str,
        kind: NotificationKind,
        recipient_user_id: str,
        action: str,
        diff: dict[str, Any] | None = None,
    ) -> None:
        """Append one audit row scoped to the fanout attempt.

        ``entity_kind='notification'`` matches the
        :class:`Notification` row's logical name; ``entity_id`` is
        the notification id so every fanout row for a given
        notification shares an audit thread. ``channel`` / ``kind`` /
        ``recipient_user_id`` land on the ``diff`` JSON so a support
        query can slice the ledger without walking row text.
        """
        payload: dict[str, Any] = {
            "channel": channel,
            "kind": kind.value,
            "recipient_user_id": recipient_user_id,
        }
        if diff is not None:
            payload.update(diff)
        write_audit(
            self.session,
            self.ctx,
            entity_kind="notification",
            entity_id=entity_id,
            action=action,
            diff=payload,
            clock=self.clock,
        )
