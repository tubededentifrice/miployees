"""End-to-end integration test for the notification fanout (cd-y1ge).

Exercises the :class:`~app.domain.messaging.notifications.NotificationService`
against the real migrated DB (SQLite by default; Postgres when
``CREWDAY_TEST_DB=postgres``) with:

* a real session bound to the integration fixtures' savepoint-wrapped
  connection,
* a real :class:`~app.events.bus.EventBus` subscriber attached,
* the in-memory :class:`~tests._fakes.mailer.InMemoryMailer` and a
  fake push queue to observe the outbound channels without hitting
  external services.

The test proves:

* The ``notification`` row + all four audit rows land in the caller's
  UoW (survive commit-at-savepoint, roll back at test end).
* The SSE subscriber sees exactly one
  :class:`~app.events.types.NotificationCreated` per notify() call,
  carrying the correct ``notification_id`` + ``actor_user_id``.
* The DB schema round-trip preserves the inbox payload verbatim
  (``payload_json`` round-trips through the JSON column).
* Email + push are both invoked with the rendered copy.

See ``docs/specs/10-messaging-notifications.md`` §"Channels" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.messaging.models import (
    EmailOptOut,
    Notification,
    PushToken,
)
from app.domain.messaging.notifications import (
    NotificationKind,
    NotificationService,
)
from app.events import NotificationCreated
from app.events.bus import EventBus
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests._fakes.mailer import InMemoryMailer
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# Messaging tables are workspace-scoped; a sibling unit test's autouse
# fixture (``tests/unit/test_tenancy_orm_filter.py``) can wipe the
# process-wide registry, so we re-register defensively. Mirrors the
# pattern in ``tests/integration/test_db_messaging.py``.
_MESSAGING_TABLES: tuple[str, ...] = (
    "notification",
    "push_token",
    "digest_record",
    "chat_channel",
    "chat_message",
    "email_opt_out",
    "email_delivery",
)


@pytest.fixture(autouse=True)
def _ensure_messaging_registered() -> None:
    for table in _MESSAGING_TABLES:
        registry.register(table)


# ---------------------------------------------------------------------------
# Fake push queue
# ---------------------------------------------------------------------------


@dataclass
class PushCall:
    user_id: str
    kind: str
    body: str
    payload: dict[str, Any]


class FakePushQueue:
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fanout_env(
    db_session: Session,
) -> Iterator[tuple[WorkspaceContext, str, FrozenClock]]:
    """Seed a workspace + recipient + actor on ``db_session``."""
    clock = FrozenClock(_PINNED)
    actor = bootstrap_user(
        db_session,
        email=f"actor+{new_ulid()}@example.com",
        display_name="Actor",
        clock=clock,
    )
    recipient = bootstrap_user(
        db_session,
        email=f"recipient+{new_ulid()}@example.com",
        display_name="Recipient",
        clock=clock,
    )
    workspace = bootstrap_workspace(
        db_session,
        slug=f"fanout-{new_ulid()[:10].lower()}",
        name="Fanout WS",
        owner_user_id=actor.id,
        clock=clock,
    )
    db_session.flush()
    ctx = WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    yield ctx, recipient.id, clock


# ---------------------------------------------------------------------------
# End-to-end fanout
# ---------------------------------------------------------------------------


class TestFanoutEndToEnd:
    def test_notify_persists_row_and_fires_sse_subscriber(
        self,
        db_session: Session,
        fanout_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Happy path: one inbox row, one SSE event, one email, one push."""
        ctx, recipient_id, clock = fanout_env

        # Register an active push token so the push branch fires.
        db_session.add(
            PushToken(
                id=new_ulid(),
                workspace_id=ctx.workspace_id,
                user_id=recipient_id,
                endpoint="https://example.invalid/push/sub-1",
                p256dh="p256dh-placeholder",
                auth="auth-placeholder",
                user_agent="Chrome on Pixel 9",
                created_at=_PINNED,
                last_used_at=None,
            )
        )
        db_session.flush()

        # Fresh bus so the subscriber we attach is the only one
        # observing this publish — the default bus can carry other
        # handlers installed by sibling integration tests.
        isolated_bus = EventBus()
        captured: list[NotificationCreated] = []

        @isolated_bus.subscribe(NotificationCreated)
        def _capture(event: NotificationCreated) -> None:
            captured.append(event)

        mailer = InMemoryMailer()
        push = FakePushQueue()
        service = NotificationService(
            session=db_session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=isolated_bus,
            push_enqueue=push,
        )

        notification_id = service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={
                "task_title": "Turnover villa 3",
                "due_at": "2026-04-24T15:00:00Z",
            },
        )
        # Commit-at-savepoint so the row is visible to a fresh SELECT
        # (still rolled back at test teardown).
        db_session.commit()

        # Inbox row
        row = db_session.execute(
            select(Notification).where(Notification.id == notification_id)
        ).scalar_one()
        assert row.recipient_user_id == recipient_id
        assert row.kind == "task_assigned"
        assert "Turnover villa 3" in row.subject
        assert row.body_md is not None and "Turnover villa 3" in row.body_md
        # JSON round-trip: payload_json preserves the caller's dict.
        assert row.payload_json["task_title"] == "Turnover villa 3"
        assert row.payload_json["due_at"] == "2026-04-24T15:00:00Z"
        assert row.read_at is None

        # SSE subscriber
        assert len(captured) == 1
        event = captured[0]
        assert event.notification_id == notification_id
        assert event.kind == "task_assigned"
        assert event.actor_user_id == recipient_id
        assert event.workspace_id == ctx.workspace_id

        # Email
        assert len(mailer.sent) == 1
        assert mailer.sent[0].to[0].endswith("@example.com")
        assert "Turnover villa 3" in mailer.sent[0].subject

        # Push
        assert len(push.calls) == 1
        assert push.calls[0].user_id == recipient_id
        assert push.calls[0].kind == "task_assigned"

        # Audit: four rows, one per channel, all ``dispatched``.
        audit_rows = (
            db_session.execute(
                select(AuditLog).where(AuditLog.entity_id == notification_id)
            )
            .scalars()
            .all()
        )
        assert len(audit_rows) == 4
        channels = {row.diff["channel"] for row in audit_rows}
        assert channels == {"inbox", "sse", "email", "push"}
        actions = {row.action for row in audit_rows}
        assert actions == {"messaging.notification.dispatched"}

    def test_email_opt_out_row_suppresses_email(
        self,
        db_session: Session,
        fanout_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """An ``email_opt_out`` row matching (workspace, user, kind)
        skips the email path end-to-end."""
        ctx, recipient_id, clock = fanout_env

        db_session.add(
            EmailOptOut(
                id=new_ulid(),
                workspace_id=ctx.workspace_id,
                user_id=recipient_id,
                category="task_assigned",
                opted_out_at=_PINNED,
                source="profile",
            )
        )
        db_session.flush()

        mailer = InMemoryMailer()
        push = FakePushQueue()
        service = NotificationService(
            session=db_session,
            ctx=ctx,
            mailer=mailer,
            clock=clock,
            bus=EventBus(),  # no subscribers; publish is a no-op on fresh bus
            push_enqueue=push,
        )
        notification_id = service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        db_session.commit()

        # Inbox row still landed.
        assert (
            db_session.execute(
                select(Notification).where(Notification.id == notification_id)
            ).scalar_one()
            is not None
        )

        # Email path skipped.
        assert mailer.sent == []

        # The ``email`` audit row records the opt-out skip.
        email_row = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == notification_id,
                AuditLog.diff["channel"].as_string() == "email",
            )
        ).scalar_one()
        assert email_row.action == "messaging.notification.skipped"
        assert email_row.diff["reason"] == "email_opt_out"

    def test_no_push_tokens_skips_push_end_to_end(
        self,
        db_session: Session,
        fanout_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Zero ``push_token`` rows for the recipient → push skipped."""
        ctx, recipient_id, clock = fanout_env

        push = FakePushQueue()
        service = NotificationService(
            session=db_session,
            ctx=ctx,
            mailer=InMemoryMailer(),
            clock=clock,
            bus=EventBus(),
            push_enqueue=push,
        )
        notification_id = service.notify(
            recipient_user_id=recipient_id,
            kind=NotificationKind.TASK_ASSIGNED,
            payload={"task_title": "X"},
        )
        db_session.commit()

        assert push.calls == []

        push_row = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_id == notification_id,
                AuditLog.diff["channel"].as_string() == "push",
            )
        ).scalar_one()
        assert push_row.action == "messaging.notification.skipped"
        assert push_row.diff["reason"] == "no_active_push_tokens"
