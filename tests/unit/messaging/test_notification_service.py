"""Unit tests for :mod:`app.domain.messaging.notifications` (cd-y1ge).

Exercises the service surface against an in-memory SQLite engine built
via ``Base.metadata.create_all()`` — no alembic, no tenant filter, just
the ORM round-trip + the template loader + the pure-Python fanout
logic.

Covers:

* Happy-path fanout: inbox row persists, SSE event fires, email sent,
  push enqueued, one audit row per channel.
* Email opt-out: matching ``email_opt_out`` row → email skipped,
  audit row records the reason. Wildcard category (``'*'``) also
  suppresses.
* Push: zero active tokens → skipped; template missing → skipped;
  push_enqueue not configured → skipped with a distinct audit reason.
* ``TemplateNotFound`` raised LOUDLY when the kind's default template
  does not exist.
* Locale fallback: ``fr`` template used when recipient's locale is
  ``fr``; falls back to the locale-free default when a locale-specific
  template is missing.
* Recipient not on file: :class:`LookupError` raised.
* Enum ↔ DB CHECK parity: the module-level import guard refuses to
  import with drift.
* SSE event name matches the ``notification.created`` contract.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import (
    EmailOptOut,
    Notification,
    PushToken,
)
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.messaging.notifications import (
    TEMPLATE_ROOT,
    Jinja2TemplateLoader,
    NotificationKind,
    NotificationService,
    TemplateNotFound,
)
from app.events import NotificationCreated, bus, get_event_type
from app.events.bus import EventBus
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema built from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture(autouse=True)
def reset_bus() -> Iterator[None]:
    """Drop every subscription between tests so captures don't bleed."""
    yield
    bus._reset_for_tests()


# ---------------------------------------------------------------------------
# Push-queue fake
# ---------------------------------------------------------------------------


@dataclass
class PushCall:
    user_id: str
    kind: str
    body: str
    payload: dict[str, Any]


class FakePushQueue:
    """In-memory recorder for the ``push_enqueue`` callable seam."""

    def __init__(self) -> None:
        self.calls: list[PushCall] = []

    def __call__(
        self,
        user_id: str,
        kind: str,
        body: str,
        payload: Mapping[str, Any],
    ) -> None:
        self.calls.append(
            PushCall(
                user_id=user_id,
                kind=kind,
                body=body,
                payload=dict(payload),
            )
        )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _bootstrap_workspace(s: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(
    s: Session,
    *,
    email: str,
    display_name: str,
    locale: str | None = None,
) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=display_name,
            locale=locale,
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _ctx(*, workspace_id: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="ws",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def _add_push_token(s: Session, *, workspace_id: str, user_id: str) -> None:
    s.add(
        PushToken(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint=f"https://example.invalid/push/{new_ulid()}",
            p256dh="p256dh-placeholder",
            auth="auth-placeholder",
            user_agent=None,
            created_at=_PINNED,
            last_used_at=None,
        )
    )
    s.flush()


def _add_email_opt_out(
    s: Session,
    *,
    workspace_id: str,
    user_id: str,
    category: str,
) -> None:
    s.add(
        EmailOptOut(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            category=category,
            opted_out_at=_PINNED,
            source="profile",
        )
    )
    s.flush()


@pytest.fixture
def base_env(
    session: Session,
) -> tuple[WorkspaceContext, str, FrozenClock]:
    """Workspace + recipient user + FrozenClock the tests share."""
    ws_id = _bootstrap_workspace(session, slug="notify-env")
    recipient_id = _bootstrap_user(
        session,
        email="recipient@example.com",
        display_name="Recipient",
    )
    actor_id = _bootstrap_user(
        session,
        email="actor@example.com",
        display_name="Actor",
    )
    session.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=actor_id)
    return ctx, recipient_id, FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Enum ↔ DB parity guard
# ---------------------------------------------------------------------------


class TestEnumParity:
    def test_enum_values_match_db_check(self) -> None:
        """The import-time guard in notifications.py refuses to import
        when the enum and the DB CHECK drift. This test just verifies
        the current state is consistent — a drifted state would have
        made the module import itself fail.
        """
        from app.adapters.db.messaging.models import _NOTIFICATION_KIND_VALUES

        assert frozenset(k.value for k in NotificationKind) == frozenset(
            _NOTIFICATION_KIND_VALUES
        )


# ---------------------------------------------------------------------------
# Event registration
# ---------------------------------------------------------------------------


class TestNotificationCreatedEvent:
    def test_registered_under_expected_name(self) -> None:
        assert get_event_type("notification.created") is NotificationCreated

    def test_is_user_scoped(self) -> None:
        assert NotificationCreated.user_scoped is True

    def test_carries_actor_user_id_field(self) -> None:
        # Required by the ``user_scoped=True`` registry contract.
        assert "actor_user_id" in NotificationCreated.model_fields


# ---------------------------------------------------------------------------
# TemplateLoader — locale fallback + loud failure
# ---------------------------------------------------------------------------


class TestTemplateLoader:
    def test_renders_default_locale(self) -> None:
        loader = Jinja2TemplateLoader.default()
        out = loader.render(
            kind="task_assigned",
            locale=None,
            channel="subject",
            context={"task_title": "Clean room 3"},
        )
        assert "Clean room 3" in out

    def test_locale_specific_template_takes_precedence(self) -> None:
        """``task_assigned.fr.subject.j2`` exists and is used for ``fr``."""
        loader = Jinja2TemplateLoader.default()
        out = loader.render(
            kind="task_assigned",
            locale="fr",
            channel="subject",
            context={"task_title": "X"},
        )
        # French file uses the no-break colon; the presence of the
        # accented word proves the fr file was picked, not the default.
        assert "Tâche" in out

    def test_unknown_locale_falls_back_to_default(self) -> None:
        """A locale with no template variant falls through to English."""
        loader = Jinja2TemplateLoader.default()
        out = loader.render(
            kind="task_assigned",
            locale="xx",
            channel="subject",
            context={"task_title": "X"},
        )
        assert "Task assigned" in out

    def test_bcp47_region_falls_back_to_language(self, tmp_path: Path) -> None:
        """``fr-CA`` looks for ``fr-CA`` first, then ``fr``, then default."""
        env = Environment(
            loader=FileSystemLoader(str(tmp_path)),
            autoescape=select_autoescape(["html", "j2"]),
            undefined=StrictUndefined,
        )
        (tmp_path / "kind.fr.subject.j2").write_text("french\n")
        (tmp_path / "kind.subject.j2").write_text("english\n")
        loader = Jinja2TemplateLoader(env=env)
        out = loader.render(
            kind="kind",
            locale="fr-CA",
            channel="subject",
            context={},
        )
        assert out.strip() == "french"

    def test_missing_default_raises_template_not_found(self) -> None:
        loader = Jinja2TemplateLoader.default()
        with pytest.raises(TemplateNotFound) as excinfo:
            loader.render(
                kind="task_assigned",
                locale=None,
                channel="does_not_exist_channel",
                context={},
            )
        assert excinfo.value.kind == "task_assigned"
        assert excinfo.value.channel == "does_not_exist_channel"

    def test_template_not_found_is_lookup_error(self) -> None:
        assert issubclass(TemplateNotFound, LookupError)

    def test_strict_undefined_raises_on_missing_key(self, tmp_path: Path) -> None:
        """A template referencing a missing key raises, not silently
        emits an empty string."""
        from jinja2 import UndefinedError

        env = Environment(
            loader=FileSystemLoader(str(tmp_path)),
            autoescape=select_autoescape(["html", "j2"]),
            undefined=StrictUndefined,
        )
        (tmp_path / "k.subject.j2").write_text("{{ missing_key }}\n")
        loader = Jinja2TemplateLoader(env=env)
        with pytest.raises(UndefinedError):
            loader.render(
                kind="k",
                locale=None,
                channel="subject",
                context={},
            )

    def test_template_root_points_at_real_dir(self) -> None:
        assert TEMPLATE_ROOT.exists()
        assert TEMPLATE_ROOT.is_dir()
        # At least one of the kinds ships with a default subject file.
        assert (TEMPLATE_ROOT / "task_assigned.subject.j2").exists()


# ---------------------------------------------------------------------------
# NotificationService — happy path
# ---------------------------------------------------------------------------


class TestNotifyHappyPath:
    def test_fans_out_to_all_four_channels(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, recipient_id, clock = base_env
        _add_push_token(session, workspace_id=ctx.workspace_id, user_id=recipient_id)
        session.commit()

        mailer = InMemoryMailer()
        push = FakePushQueue()
        captured: list[NotificationCreated] = []

        @bus.subscribe(NotificationCreated)
        def _capture(event: NotificationCreated) -> None:
            captured.append(event)

        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=bus,
            push_enqueue=push,
        )

        notification_id = service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "Clean room 3"},
        )

        # Inbox row persisted exactly once.
        rows = session.execute(select(Notification)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == notification_id
        assert row.recipient_user_id == recipient_id
        assert row.kind == "task_assigned"
        assert "Clean room 3" in row.subject
        assert row.body_md is not None
        assert row.payload_json == {"task_title": "Clean room 3"}
        assert row.read_at is None

        # SSE event fired exactly once, addressed to the recipient.
        assert len(captured) == 1
        event = captured[0]
        assert event.notification_id == notification_id
        assert event.kind == "task_assigned"
        assert event.actor_user_id == recipient_id

        # Email sent exactly once, with the rendered subject/body.
        assert len(mailer.sent) == 1
        sent = mailer.sent[0]
        assert sent.to == ("recipient@example.com",)
        assert "Clean room 3" in sent.subject
        assert "Clean room 3" in sent.body_text
        assert sent.headers["X-CrewDay-Notification-Id"] == notification_id
        assert sent.headers["X-CrewDay-Notification-Kind"] == "task_assigned"

        # Push enqueued exactly once, with the short rendered copy.
        assert len(push.calls) == 1
        call = push.calls[0]
        assert call.user_id == recipient_id
        assert call.kind == "task_assigned"
        assert "Clean room 3" in call.body
        assert call.payload == {"task_title": "Clean room 3"}

    def test_audit_row_per_channel(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, recipient_id, clock = base_env
        _add_push_token(session, workspace_id=ctx.workspace_id, user_id=recipient_id)
        session.commit()

        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=bus,
            push_enqueue=FakePushQueue(),
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        session.flush()

        audit_rows = (
            session.execute(
                select(AuditLog).where(AuditLog.entity_kind == "notification")
            )
            .scalars()
            .all()
        )
        # Four attempted channels, four audit rows.
        assert len(audit_rows) == 4
        channels = {row.diff["channel"] for row in audit_rows}
        assert channels == {"inbox", "sse", "email", "push"}
        # All four should be "dispatched" (happy path, no skips).
        actions = {row.action for row in audit_rows}
        assert actions == {"messaging.notification.dispatched"}
        # Every row carries the recipient + kind denormalised so
        # support queries can slice without joining.
        for row in audit_rows:
            assert row.diff["recipient_user_id"] == recipient_id
            assert row.diff["kind"] == "task_assigned"

    def test_returns_notification_id_matching_row(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, recipient_id, clock = base_env
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=bus,
            push_enqueue=FakePushQueue(),
        )
        notification_id = service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        row_id = session.execute(select(Notification.id)).scalar_one()
        assert row_id == notification_id


# ---------------------------------------------------------------------------
# Email opt-out path
# ---------------------------------------------------------------------------


class TestEmailOptOut:
    def test_exact_category_match_skips_email(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, recipient_id, clock = base_env
        _add_email_opt_out(
            session,
            workspace_id=ctx.workspace_id,
            user_id=recipient_id,
            category="task_assigned",
        )
        session.commit()

        mailer = InMemoryMailer()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=bus,
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        session.flush()

        # Email NOT sent.
        assert mailer.sent == []

        # Inbox row still persisted (opt-out is email-only).
        assert session.execute(select(Notification)).scalars().all() != []

        # Audit row records the skip reason.
        email_rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "notification",
                    AuditLog.diff["channel"].as_string() == "email",
                )
            )
            .scalars()
            .all()
        )
        assert len(email_rows) == 1
        assert email_rows[0].action == "messaging.notification.skipped"
        assert email_rows[0].diff["reason"] == "email_opt_out"

    def test_wildcard_category_skips_every_kind(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """A single ``category='*'`` row suppresses email for every kind."""
        ctx, recipient_id, clock = base_env
        _add_email_opt_out(
            session,
            workspace_id=ctx.workspace_id,
            user_id=recipient_id,
            category="*",
        )
        session.commit()

        mailer = InMemoryMailer()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=bus,
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        assert mailer.sent == []

    def test_opt_out_in_another_workspace_does_not_suppress(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Opt-out is (workspace, user, category) — sibling workspaces
        do not share the suppression."""
        ctx, recipient_id, clock = base_env
        other_ws = _bootstrap_workspace(session, slug="other-ws")
        _add_email_opt_out(
            session,
            workspace_id=other_ws,
            user_id=recipient_id,
            category="task_assigned",
        )
        session.commit()

        mailer = InMemoryMailer()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=bus,
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        assert len(mailer.sent) == 1


# ---------------------------------------------------------------------------
# Push path
# ---------------------------------------------------------------------------


class TestPushPath:
    def test_no_tokens_skips_push(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, recipient_id, clock = base_env
        # No tokens inserted.
        push = FakePushQueue()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=bus,
            push_enqueue=push,
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        session.flush()
        assert push.calls == []

        # Audit row records the no-tokens skip.
        push_rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "notification",
                    AuditLog.diff["channel"].as_string() == "push",
                )
            )
            .scalars()
            .all()
        )
        assert len(push_rows) == 1
        assert push_rows[0].action == "messaging.notification.skipped"
        assert push_rows[0].diff["reason"] == "no_active_push_tokens"

    def test_no_push_template_skips_with_distinct_reason(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
        tmp_path: Path,
    ) -> None:
        """A kind whose push template is absent records a different
        skip reason from the no-tokens case."""
        ctx, recipient_id, clock = base_env
        _add_push_token(session, workspace_id=ctx.workspace_id, user_id=recipient_id)
        session.commit()

        # Point the loader at a directory that only carries subject +
        # body_md (no push), so the optional push template resolution
        # returns None.
        (tmp_path / "task_assigned.subject.j2").write_text("Task: {{ task_title }}\n")
        (tmp_path / "task_assigned.body_md.j2").write_text("Body: {{ task_title }}\n")
        env = Environment(
            loader=FileSystemLoader(str(tmp_path)),
            autoescape=select_autoescape(["html", "j2"]),
            undefined=StrictUndefined,
        )
        loader = Jinja2TemplateLoader(env=env)

        push = FakePushQueue()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=bus,
            push_enqueue=push,
            templates=loader,
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        session.flush()
        assert push.calls == []

        push_rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "notification",
                    AuditLog.diff["channel"].as_string() == "push",
                )
            )
            .scalars()
            .all()
        )
        assert len(push_rows) == 1
        assert push_rows[0].diff["reason"] == "no_push_template"

    def test_push_enqueue_missing_records_distinct_skip_reason(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """A service built without ``push_enqueue`` records a config skip
        reason — distinct from no-tokens / no-template so ops can tell
        the difference at a glance."""
        ctx, recipient_id, clock = base_env
        _add_push_token(session, workspace_id=ctx.workspace_id, user_id=recipient_id)
        session.commit()

        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=bus,
            # push_enqueue intentionally left as None.
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        session.flush()

        push_rows = (
            session.execute(
                select(AuditLog).where(
                    AuditLog.entity_kind == "notification",
                    AuditLog.diff["channel"].as_string() == "push",
                )
            )
            .scalars()
            .all()
        )
        assert len(push_rows) == 1
        assert push_rows[0].diff["reason"] == "push_enqueue_not_configured"

    def test_token_in_another_workspace_is_ignored(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """A token registered in a different workspace does NOT count
        toward the has-tokens check for this workspace."""
        ctx, recipient_id, clock = base_env
        other_ws = _bootstrap_workspace(session, slug="other-ws-push")
        _add_push_token(session, workspace_id=other_ws, user_id=recipient_id)
        session.commit()

        push = FakePushQueue()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=bus,
            push_enqueue=push,
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        assert push.calls == []


# ---------------------------------------------------------------------------
# Template-not-found loud failure
# ---------------------------------------------------------------------------


class TestTemplateNotFoundLoud:
    def test_missing_subject_raises(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
        tmp_path: Path,
    ) -> None:
        """Point the loader at an empty directory so even subject is
        missing — the service must raise :class:`TemplateNotFound`
        rather than insert a half-baked row."""
        ctx, recipient_id, clock = base_env
        env = Environment(
            loader=FileSystemLoader(str(tmp_path)),
            autoescape=select_autoescape(["html", "j2"]),
            undefined=StrictUndefined,
        )
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=bus,
            push_enqueue=FakePushQueue(),
            templates=Jinja2TemplateLoader(env=env),
        )
        with pytest.raises(TemplateNotFound) as excinfo:
            service.notify(
                recipient_user_id=recipient_id,
                kind=NotificationKind.TASK_ASSIGNED,
                payload={"task_title": "X"},
            )
        assert excinfo.value.kind == "task_assigned"
        assert excinfo.value.channel == "subject"
        # No inbox row persisted — we failed fast before the DB write.
        session.flush()
        assert session.execute(select(Notification)).scalars().all() == []


# ---------------------------------------------------------------------------
# Locale fallback
# ---------------------------------------------------------------------------


class TestLocaleFallback:
    def test_recipient_locale_selects_french_template(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _recipient_id, clock = base_env
        # Re-insert a recipient with a French locale.
        fr_recipient_id = _bootstrap_user(
            session,
            email="maria@example.com",
            display_name="Maria",
            locale="fr",
        )
        session.commit()

        mailer = InMemoryMailer()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=bus,
        )
        service.notify(
            recipient_user_id=fr_recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "Nettoyer la chambre 3"},
        )

        assert len(mailer.sent) == 1
        # The French template used 'Tâche assignée'.
        assert "Tâche" in mailer.sent[0].subject

    def test_unknown_locale_falls_back_to_default(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _recipient_id, clock = base_env
        xx_recipient_id = _bootstrap_user(
            session,
            email="xx@example.com",
            display_name="XX",
            locale="xx",  # no template variant on disk
        )
        session.commit()

        mailer = InMemoryMailer()
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=bus,
        )
        service.notify(
            recipient_user_id=xx_recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        # English default used — recipient still receives an email.
        assert len(mailer.sent) == 1
        assert "Task assigned" in mailer.sent[0].subject


# ---------------------------------------------------------------------------
# Misc edges
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_unknown_recipient_raises_lookup_error(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _recipient_id, clock = base_env
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=bus,
        )
        with pytest.raises(LookupError):
            service.notify(
                recipient_user_id="does-not-exist",
                kind=NotificationKind.TASK_ASSIGNED,
                payload={"task_title": "X"},
            )

    def test_payload_is_copied_not_aliased(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Mutating the caller's payload after notify() MUST NOT
        scribble on the persisted row."""
        ctx, recipient_id, clock = base_env
        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=bus,
        )
        payload: dict[str, Any] = {"task_title": "Original"}
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload=payload,
        )
        payload["task_title"] = "Tampered"

        row = session.execute(select(Notification)).scalar_one()
        assert row.payload_json == {"task_title": "Original"}

    def test_isolated_bus_receives_event_but_default_does_not(
        self,
        session: Session,
        base_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """A caller injecting a fresh :class:`EventBus` scopes the
        publish to their handlers only."""
        ctx, recipient_id, clock = base_env
        isolated = EventBus()

        captured_isolated: list[NotificationCreated] = []
        captured_default: list[NotificationCreated] = []

        @isolated.subscribe(NotificationCreated)
        def _on_isolated(event: NotificationCreated) -> None:
            captured_isolated.append(event)

        @bus.subscribe(NotificationCreated)
        def _on_default(event: NotificationCreated) -> None:
            captured_default.append(event)

        service = NotificationService(
            session=session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=isolated,
        )
        service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )

        assert len(captured_isolated) == 1
        assert captured_default == []
