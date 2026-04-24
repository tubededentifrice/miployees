"""Integration tests for :mod:`app.adapters.db.messaging` against a real DB.

Covers the post-migration schema shape (tables, FK targets, CHECK
constraints, indexes), the referential-integrity contract (workspace
CASCADE sweeps every row; user CASCADE on notification / push_token /
digest_record / email_opt_out; user SET NULL on chat_message author;
channel CASCADE on chat_message; no user FK on email_delivery —
soft reference), happy-path round-trip of every model, the unread-
fanout hot-path query, the chat-scrollback hot-path query, the
email opt-out pre-send probe, the email delivery state machine,
CHECK / FK / UNIQUE violations, cross-workspace isolation, and
tenant-filter behaviour (all seven tables scoped; SELECT without a
:class:`WorkspaceContext` raises :class:`TenantFilterMissing`).

The sibling ``tests/unit/test_db_messaging.py`` covers pure-Python
model construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/10-messaging-notifications.md`` (incl. §"email_opt_out"
and §"Delivery tracking"), and ``docs/specs/23-chat-gateway.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User
from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatMessage,
    DigestRecord,
    EmailDelivery,
    EmailOptOut,
    Notification,
    PushToken,
)
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = _PINNED + timedelta(hours=1)


_MESSAGING_TABLES: tuple[str, ...] = (
    "notification",
    "push_token",
    "digest_record",
    "chat_channel",
    "chat_message",
    "email_opt_out",
    "email_delivery",
)


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across tests. The top-level ``db_session`` fixture
    binds directly to a raw connection for SAVEPOINT isolation and
    therefore bypasses the filter; tests that need to observe
    :class:`TenantFilterMissing` use this factory explicitly.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_messaging_registered() -> None:
    """Re-register the messaging tables as workspace-scoped.

    ``app.adapters.db.messaging.__init__`` registers them at import
    time, but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite. Mirrors the pattern in
    ``tests/integration/test_db_instructions.py``.
    """
    for table in _MESSAGING_TABLES:
        registry.register(table)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace``."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLI",
    )


def _bootstrap(
    session: Session, *, email: str, display: str, slug: str, name: str
) -> tuple[Workspace, User]:
    """Seed a user + workspace pair for a test."""
    clock = FrozenClock(_PINNED)
    user = bootstrap_user(session, email=email, display_name=display, clock=clock)
    workspace = bootstrap_workspace(
        session, slug=slug, name=name, owner_user_id=user.id, clock=clock
    )
    return workspace, user


class TestMigrationShape:
    """The migrations land all seven tables with the correct keys + indexes."""

    def test_all_tables_exist(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        for table in _MESSAGING_TABLES:
            assert table in tables, f"{table} missing from schema"

    def test_notification_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("notification")}
        expected = {
            "id",
            "workspace_id",
            "recipient_user_id",
            "kind",
            "subject",
            "body_md",
            "read_at",
            "created_at",
            "payload_json",
        }
        assert set(cols) == expected
        for nullable in ("body_md", "read_at"):
            assert cols[nullable]["nullable"] is True
        for notnull in expected - {"body_md", "read_at"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_notification_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("notification")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("recipient_user_id",)]["referred_table"] == "user"
        assert fks[("recipient_user_id",)]["options"].get("ondelete") == "CASCADE"

    def test_notification_unread_fanout_index(self, engine: Engine) -> None:
        """Acceptance: ``(workspace_id, recipient_user_id, read_at)`` index."""
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("notification")}
        assert "ix_notification_workspace_recipient_read" in indexes
        assert indexes["ix_notification_workspace_recipient_read"]["column_names"] == [
            "workspace_id",
            "recipient_user_id",
            "read_at",
        ]

    def test_push_token_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("push_token")}
        expected = {
            "id",
            "workspace_id",
            "user_id",
            "endpoint",
            "p256dh",
            "auth",
            "user_agent",
            "created_at",
            "last_used_at",
        }
        assert set(cols) == expected
        for nullable in ("user_agent", "last_used_at"):
            assert cols[nullable]["nullable"] is True
        for notnull in expected - {"user_agent", "last_used_at"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_push_token_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("push_token")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("user_id",)]["referred_table"] == "user"
        assert fks[("user_id",)]["options"].get("ondelete") == "CASCADE"

    def test_push_token_workspace_user_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("push_token")}
        assert "ix_push_token_workspace_user" in indexes
        assert indexes["ix_push_token_workspace_user"]["column_names"] == [
            "workspace_id",
            "user_id",
        ]

    def test_digest_record_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("digest_record")}
        expected = {
            "id",
            "workspace_id",
            "recipient_user_id",
            "period_start",
            "period_end",
            "kind",
            "body_md",
            "sent_at",
        }
        assert set(cols) == expected
        assert cols["sent_at"]["nullable"] is True
        for notnull in expected - {"sent_at"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_digest_record_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("digest_record")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("recipient_user_id",)]["referred_table"] == "user"
        assert fks[("recipient_user_id",)]["options"].get("ondelete") == "CASCADE"

    def test_digest_record_index(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("digest_record")
        }
        assert "ix_digest_record_workspace_recipient_period" in indexes
        assert indexes["ix_digest_record_workspace_recipient_period"][
            "column_names"
        ] == ["workspace_id", "recipient_user_id", "period_start"]

    def test_chat_channel_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("chat_channel")}
        expected = {
            "id",
            "workspace_id",
            "kind",
            "source",
            "external_ref",
            "title",
            "created_at",
        }
        assert set(cols) == expected
        for nullable in ("external_ref", "title"):
            assert cols[nullable]["nullable"] is True
        for notnull in expected - {"external_ref", "title"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_chat_channel_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("chat_channel")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"

    def test_chat_channel_indexes(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("chat_channel")}
        assert "ix_chat_channel_workspace" in indexes
        assert "ix_chat_channel_workspace_external_ref" in indexes

    def test_chat_message_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("chat_message")}
        expected = {
            "id",
            "workspace_id",
            "channel_id",
            "author_user_id",
            "author_label",
            "body_md",
            "attachments_json",
            "dispatched_to_agent_at",
            "created_at",
        }
        assert set(cols) == expected
        for nullable in ("author_user_id", "dispatched_to_agent_at"):
            assert cols[nullable]["nullable"] is True
        for notnull in expected - {"author_user_id", "dispatched_to_agent_at"}:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_chat_message_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("chat_message")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("channel_id",)]["referred_table"] == "chat_channel"
        assert fks[("channel_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("author_user_id",)]["referred_table"] == "user"
        # SET NULL so thread history survives a user hard-delete.
        assert fks[("author_user_id",)]["options"].get("ondelete") == "SET NULL"

    def test_chat_message_channel_created_index(self, engine: Engine) -> None:
        """Acceptance: ``(channel_id, created_at)`` index for scrollback."""
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("chat_message")}
        assert "ix_chat_message_channel_created" in indexes
        assert indexes["ix_chat_message_channel_created"]["column_names"] == [
            "channel_id",
            "created_at",
        ]
        assert "ix_chat_message_workspace_channel" in indexes

    def test_email_opt_out_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("email_opt_out")}
        expected = {
            "id",
            "workspace_id",
            "user_id",
            "category",
            "opted_out_at",
            "source",
        }
        assert set(cols) == expected
        for notnull in expected:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_email_opt_out_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("email_opt_out")
        }
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        assert fks[("user_id",)]["referred_table"] == "user"
        assert fks[("user_id",)]["options"].get("ondelete") == "CASCADE"

    def test_email_opt_out_indexes(self, engine: Engine) -> None:
        """Acceptance: unique triple + non-unique workspace/user index."""
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("email_opt_out")
        }
        assert "uq_email_opt_out_user_category" in indexes
        uq = indexes["uq_email_opt_out_user_category"]
        assert uq["column_names"] == ["workspace_id", "user_id", "category"]
        # SQLite's inspector returns 1/0, PG returns True/False — coerce.
        assert bool(uq["unique"]) is True

        assert "ix_email_opt_out_workspace_user" in indexes
        ix = indexes["ix_email_opt_out_workspace_user"]
        assert ix["column_names"] == ["workspace_id", "user_id"]
        assert bool(ix["unique"]) is False

    def test_email_delivery_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("email_delivery")}
        expected = {
            "id",
            "workspace_id",
            "to_person_id",
            "to_email_at_send",
            "template_key",
            "context_snapshot_json",
            "sent_at",
            "provider_message_id",
            "delivery_state",
            "first_error",
            "retry_count",
            "inbound_linkage",
            "created_at",
        }
        assert set(cols) == expected
        nullable = {"sent_at", "provider_message_id", "first_error", "inbound_linkage"}
        for col in nullable:
            assert cols[col]["nullable"] is True, f"{col} must be NULLABLE"
        for notnull in expected - nullable:
            assert cols[notnull]["nullable"] is False, f"{notnull} must be NOT NULL"

    def test_email_delivery_fks(self, engine: Engine) -> None:
        """Only the workspace FK exists — ``to_person_id`` is a soft ref."""
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("email_delivery")
        }
        # workspace_id CASCADE — present.
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"
        # to_person_id is the soft ref — no FK per §10 (client_user
        # rows may not yet exist in ``user`` when the email queues).
        assert ("to_person_id",) not in fks

    def test_email_delivery_indexes(self, engine: Engine) -> None:
        """Acceptance: all three documented indexes exist."""
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("email_delivery")
        }
        assert "ix_email_delivery_workspace_person_sent" in indexes
        assert indexes["ix_email_delivery_workspace_person_sent"]["column_names"] == [
            "workspace_id",
            "to_person_id",
            "sent_at",
        ]

        assert "ix_email_delivery_workspace_provider_msgid" in indexes
        assert indexes["ix_email_delivery_workspace_provider_msgid"][
            "column_names"
        ] == ["workspace_id", "provider_message_id"]

        assert "ix_email_delivery_workspace_state_sent" in indexes
        assert indexes["ix_email_delivery_workspace_state_sent"]["column_names"] == [
            "workspace_id",
            "delivery_state",
            "sent_at",
        ]


class TestNotificationCrud:
    """Insert + select + update + delete round-trip on :class:`Notification`."""

    def test_round_trip_and_unread_fanout(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="notif-crud@example.com",
            display="NotifCrud",
            slug="notif-crud-ws",
            name="NotifCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            unread = Notification(
                id="01HWA00000000000000000NOTA",
                workspace_id=workspace.id,
                recipient_user_id=user.id,
                kind="task_assigned",
                subject="New task: Pool opening",
                body_md="Assigned to you at 09:00.",
                created_at=_PINNED,
                payload_json={"task_id": "01HWA00000000000000000TKAA"},
            )
            read_notif = Notification(
                id="01HWA00000000000000000NOTB",
                workspace_id=workspace.id,
                recipient_user_id=user.id,
                kind="expense_approved",
                subject="Expense approved",
                read_at=_LATER,
                created_at=_PINNED,
            )
            db_session.add_all([unread, read_notif])
            db_session.flush()

            # Unread fanout: the hot path the (workspace_id,
            # recipient_user_id, read_at) index serves.
            unread_rows = db_session.scalars(
                select(Notification)
                .where(Notification.workspace_id == workspace.id)
                .where(Notification.recipient_user_id == user.id)
                .where(Notification.read_at.is_(None))
            ).all()
            assert [r.id for r in unread_rows] == ["01HWA00000000000000000NOTA"]
            assert unread_rows[0].payload_json == {
                "task_id": "01HWA00000000000000000TKAA"
            }

            # Flip the unread row to read.
            unread_loaded = db_session.get(Notification, unread.id)
            assert unread_loaded is not None
            unread_loaded.read_at = _LATER
            db_session.flush()
            db_session.expire_all()

            reloaded = db_session.get(Notification, unread.id)
            assert reloaded is not None
            # SQLite's ``DateTime(timezone=True)`` round-trips the value
            # without tzinfo (ISO-8601 UTC text); PG preserves tzinfo.
            # Compare the naive UTC form so both backends agree.
            assert reloaded.read_at is not None
            assert reloaded.read_at.replace(tzinfo=UTC) == _LATER

            # After the flip: no unread rows for this user.
            still_unread = db_session.scalars(
                select(Notification)
                .where(Notification.workspace_id == workspace.id)
                .where(Notification.recipient_user_id == user.id)
                .where(Notification.read_at.is_(None))
            ).all()
            assert still_unread == []
        finally:
            reset_current(token)


class TestPushTokenCrud:
    """Insert + select + delete round-trip on :class:`PushToken`."""

    def test_round_trip_and_per_user_fanout(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="pt-crud@example.com",
            display="PtCrud",
            slug="pt-crud-ws",
            name="PtCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            token_a = PushToken(
                id="01HWA00000000000000000PTKA",
                workspace_id=workspace.id,
                user_id=user.id,
                endpoint="https://fcm.googleapis.com/fcm/send/A",
                p256dh="P256DH_A",
                auth="AUTH_A",
                user_agent="Chrome/126.0",
                created_at=_PINNED,
            )
            token_b = PushToken(
                id="01HWA00000000000000000PTKB",
                workspace_id=workspace.id,
                user_id=user.id,
                endpoint="https://fcm.googleapis.com/fcm/send/B",
                p256dh="P256DH_B",
                auth="AUTH_B",
                created_at=_PINNED,
                last_used_at=_LATER,
            )
            db_session.add_all([token_a, token_b])
            db_session.flush()

            rows = db_session.scalars(
                select(PushToken)
                .where(PushToken.workspace_id == workspace.id)
                .where(PushToken.user_id == user.id)
                .order_by(PushToken.id)
            ).all()
            assert [r.id for r in rows] == [
                "01HWA00000000000000000PTKA",
                "01HWA00000000000000000PTKB",
            ]
            # SQLite drops tzinfo on round-trip; compare naive UTC form.
            assert rows[1].last_used_at is not None
            assert rows[1].last_used_at.replace(tzinfo=UTC) == _LATER

            db_session.delete(token_a)
            db_session.flush()
            assert db_session.get(PushToken, token_a.id) is None
        finally:
            reset_current(token)


class TestDigestRecordCrud:
    """Insert + select round-trip on :class:`DigestRecord`."""

    def test_round_trip(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="dgr-crud@example.com",
            display="DgrCrud",
            slug="dgr-crud-ws",
            name="DgrCrudWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            record = DigestRecord(
                id="01HWA00000000000000000DGRA",
                workspace_id=workspace.id,
                recipient_user_id=user.id,
                period_start=_PINNED,
                period_end=_LATER,
                kind="daily",
                body_md="# Today\n\n- Pool opening at Villa A",
                sent_at=_LATER,
            )
            db_session.add(record)
            db_session.flush()

            loaded = db_session.get(DigestRecord, record.id)
            assert loaded is not None
            assert loaded.kind == "daily"
            # SQLite drops tzinfo on round-trip; compare naive UTC form.
            assert loaded.period_start.replace(tzinfo=UTC) == _PINNED
            assert loaded.period_end.replace(tzinfo=UTC) == _LATER
            assert loaded.sent_at is not None
            assert loaded.sent_at.replace(tzinfo=UTC) == _LATER
        finally:
            reset_current(token)


class TestChatChannelAndMessageCrud:
    """Insert + select round-trip on :class:`ChatChannel` + :class:`ChatMessage`."""

    def test_round_trip_and_scrollback(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="cchan@example.com",
            display="Cchan",
            slug="cchan-ws",
            name="CchanWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            channel = ChatChannel(
                id="01HWA00000000000000000CCHA",
                workspace_id=workspace.id,
                kind="staff",
                source="app",
                title="Villa A — team",
                created_at=_PINNED,
            )
            db_session.add(channel)
            db_session.flush()

            msg_a = ChatMessage(
                id="01HWA00000000000000000CMSA",
                workspace_id=workspace.id,
                channel_id=channel.id,
                author_user_id=user.id,
                author_label="Maria",
                body_md="On my way.",
                created_at=_PINNED,
            )
            msg_b = ChatMessage(
                id="01HWA00000000000000000CMSB",
                workspace_id=workspace.id,
                channel_id=channel.id,
                author_user_id=user.id,
                author_label="Maria",
                body_md="Arrived.",
                attachments_json=[{"blob_hash": "sha256:abc", "filename": "a.jpg"}],
                created_at=_LATER,
            )
            db_session.add_all([msg_a, msg_b])
            db_session.flush()

            # Scrollback: the (channel_id, created_at) index's query.
            rows = db_session.scalars(
                select(ChatMessage)
                .where(ChatMessage.channel_id == channel.id)
                .order_by(ChatMessage.created_at.asc())
            ).all()
            assert [r.id for r in rows] == [
                "01HWA00000000000000000CMSA",
                "01HWA00000000000000000CMSB",
            ]
            assert rows[1].attachments_json == [
                {"blob_hash": "sha256:abc", "filename": "a.jpg"}
            ]
        finally:
            reset_current(token)

    def test_gateway_inbound_no_author(self, db_session: Session) -> None:
        """Gateway-inbound rows land with ``author_user_id = NULL``."""
        workspace, user = _bootstrap(
            db_session,
            email="gw-inb@example.com",
            display="GwInb",
            slug="gw-inb-ws",
            name="GwInbWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            gw_channel = ChatChannel(
                id="01HWA00000000000000000GWCA",
                workspace_id=workspace.id,
                kind="chat_gateway",
                source="whatsapp",
                external_ref="wa_33600000001",
                title="WhatsApp: +33 6 …",
                created_at=_PINNED,
            )
            db_session.add(gw_channel)
            db_session.flush()

            inbound = ChatMessage(
                id="01HWA00000000000000000GWMA",
                workspace_id=workspace.id,
                channel_id=gw_channel.id,
                # author_user_id left NULL — external sender has no User.
                author_label="WhatsApp: +33 6 …",
                body_md="Need the Wi-Fi code.",
                dispatched_to_agent_at=_LATER,
                created_at=_PINNED,
            )
            db_session.add(inbound)
            db_session.flush()

            loaded = db_session.get(ChatMessage, inbound.id)
            assert loaded is not None
            assert loaded.author_user_id is None
            # SQLite drops tzinfo on round-trip; compare naive UTC form.
            assert loaded.dispatched_to_agent_at is not None
            assert loaded.dispatched_to_agent_at.replace(tzinfo=UTC) == _LATER
        finally:
            reset_current(token)


class TestCheckConstraints:
    """CHECK constraints reject values outside the v1 enums."""

    def test_bogus_notification_kind_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-nk@example.com",
            display="BogusNk",
            slug="bogus-nk-ws",
            name="BogusNkWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                Notification(
                    id="01HWA00000000000000000BOGN",
                    workspace_id=workspace.id,
                    recipient_user_id=user.id,
                    kind="task_volcano",  # not in the enum
                    subject="Bogus",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_digest_kind_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-dk@example.com",
            display="BogusDk",
            slug="bogus-dk-ws",
            name="BogusDkWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                DigestRecord(
                    id="01HWA00000000000000000BOGD",
                    workspace_id=workspace.id,
                    recipient_user_id=user.id,
                    period_start=_PINNED,
                    period_end=_LATER,
                    kind="hourly",  # not in the enum
                    body_md="nope",
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_inverted_digest_period_rejected(self, db_session: Session) -> None:
        """CHECK ``period_end > period_start`` rejects inverted windows."""
        workspace, user = _bootstrap(
            db_session,
            email="inv-dg@example.com",
            display="InvDg",
            slug="inv-dg-ws",
            name="InvDgWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                DigestRecord(
                    id="01HWA00000000000000000INVD",
                    workspace_id=workspace.id,
                    recipient_user_id=user.id,
                    # Inverted — end before start.
                    period_start=_LATER,
                    period_end=_PINNED,
                    kind="daily",
                    body_md="nope",
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_equal_digest_period_rejected(self, db_session: Session) -> None:
        """CHECK ``period_end > period_start`` rejects a zero-length window.

        Strict ``>`` (not ``>=``) means the boundary ``period_end =
        period_start`` is invalid too — a zero-length window is a data
        bug the worker must not be able to record (the §10 idempotency
        probe would otherwise match every future run at the same
        instant). Guards the strictness of the CHECK body so a later
        migration doesn't relax it to ``>=`` without somebody noticing.
        """
        workspace, user = _bootstrap(
            db_session,
            email="eq-dg@example.com",
            display="EqDg",
            slug="eq-dg-ws",
            name="EqDgWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                DigestRecord(
                    id="01HWA00000000000000000EQDR",
                    workspace_id=workspace.id,
                    recipient_user_id=user.id,
                    # Equal — zero-length window.
                    period_start=_PINNED,
                    period_end=_PINNED,
                    kind="daily",
                    body_md="nope",
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_chat_channel_kind_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-cck@example.com",
            display="BogusCck",
            slug="bogus-cck-ws",
            name="BogusCckWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ChatChannel(
                    id="01HWA00000000000000000BOGC",
                    workspace_id=workspace.id,
                    kind="broadcast",  # not in the enum
                    source="app",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_chat_channel_source_rejected(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="bogus-ccs@example.com",
            display="BogusCcs",
            slug="bogus-ccs-ws",
            name="BogusCcsWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                ChatChannel(
                    id="01HWA00000000000000000BOGS",
                    workspace_id=workspace.id,
                    kind="chat_gateway",
                    source="telegram",  # not in the v1 enum
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestCascadeOnChannelDelete:
    """Deleting a chat_channel sweeps every chat_message belonging to it."""

    def test_delete_channel_cascades_to_messages(self, db_session: Session) -> None:
        workspace, user = _bootstrap(
            db_session,
            email="cascade-chan@example.com",
            display="CascadeChan",
            slug="cascade-chan-ws",
            name="CascadeChanWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            channel = ChatChannel(
                id="01HWA00000000000000000CCHC",
                workspace_id=workspace.id,
                kind="staff",
                source="app",
                created_at=_PINNED,
            )
            db_session.add(channel)
            db_session.flush()

            msg_a = ChatMessage(
                id="01HWA00000000000000000CMCA",
                workspace_id=workspace.id,
                channel_id=channel.id,
                author_user_id=user.id,
                author_label="Maria",
                body_md="Msg A",
                created_at=_PINNED,
            )
            msg_b = ChatMessage(
                id="01HWA00000000000000000CMCB",
                workspace_id=workspace.id,
                channel_id=channel.id,
                author_user_id=user.id,
                author_label="Maria",
                body_md="Msg B",
                created_at=_LATER,
            )
            db_session.add_all([msg_a, msg_b])
            db_session.flush()

            msg_ids = (msg_a.id, msg_b.id)
            db_session.delete(channel)
            db_session.flush()
            # Expunge so the ORM identity-map doesn't try to refresh
            # the stale instances (they're swept at the DB level).
            db_session.expunge(msg_a)
            db_session.expunge(msg_b)
            survivors = db_session.scalars(
                select(ChatMessage).where(ChatMessage.id.in_(msg_ids))
            ).all()
            assert survivors == []
            assert db_session.get(ChatChannel, channel.id) is None
        finally:
            reset_current(token)


class TestChatMessageAuthorSetNull:
    """``ChatMessage.author_user_id`` FK uses SET NULL — history survives."""

    def test_deleting_author_nulls_author_user_id(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, owner = _bootstrap(
            db_session,
            email="author-snull@example.com",
            display="AuthorSnull",
            slug="author-snull-ws",
            name="AuthorSnullWS",
        )

        # Seed a second user who authors a message in the channel.
        clock = FrozenClock(_PINNED)
        author = bootstrap_user(
            db_session, email="author@example.com", display_name="Author", clock=clock
        )

        token = set_current(_ctx_for(workspace, owner.id))
        try:
            channel = ChatChannel(
                id="01HWA00000000000000000CCHN",
                workspace_id=workspace.id,
                kind="staff",
                source="app",
                created_at=_PINNED,
            )
            db_session.add(channel)
            db_session.flush()

            msg = ChatMessage(
                id="01HWA00000000000000000CMSN",
                workspace_id=workspace.id,
                channel_id=channel.id,
                author_user_id=author.id,
                author_label="Author",
                body_md="Hello",
                created_at=_PINNED,
            )
            db_session.add(msg)
            db_session.flush()
        finally:
            reset_current(token)

        # User delete is a platform-level op that predates the
        # ``WorkspaceContext`` — user is a cross-tenant row.
        with tenant_agnostic():
            db_session.delete(author)
            db_session.flush()

        token = set_current(_ctx_for(workspace, owner.id))
        try:
            db_session.expire_all()
            reloaded = db_session.get(ChatMessage, msg.id)
            assert reloaded is not None
            # SET NULL — the message row survives; author is NULL.
            assert reloaded.author_user_id is None
            assert reloaded.author_label == "Author"
            assert reloaded.body_md == "Hello"
        finally:
            reset_current(token)


class TestCrossWorkspaceIsolation:
    """A workspace's messaging rows do not leak to a sibling workspace."""

    def test_notification_scoped_per_workspace(self, db_session: Session) -> None:
        ws_a, user_a = _bootstrap(
            db_session,
            email="nxws-a@example.com",
            display="NxwsA",
            slug="nxws-a-ws",
            name="NxwsAWS",
        )
        ws_b, user_b = _bootstrap(
            db_session,
            email="nxws-b@example.com",
            display="NxwsB",
            slug="nxws-b-ws",
            name="NxwsBWS",
        )

        token = set_current(_ctx_for(ws_a, user_a.id))
        try:
            db_session.add(
                Notification(
                    id="01HWA00000000000000000NXA1",
                    workspace_id=ws_a.id,
                    recipient_user_id=user_a.id,
                    kind="task_assigned",
                    subject="A only",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        token = set_current(_ctx_for(ws_b, user_b.id))
        try:
            db_session.add(
                Notification(
                    id="01HWA00000000000000000NXB1",
                    workspace_id=ws_b.id,
                    recipient_user_id=user_b.id,
                    kind="task_assigned",
                    subject="B only",
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            b_rows = db_session.scalars(
                select(Notification).where(Notification.workspace_id == ws_b.id)
            ).all()
            assert {r.subject for r in b_rows} == {"B only"}

            a_rows = db_session.scalars(
                select(Notification).where(Notification.workspace_id == ws_a.id)
            ).all()
            assert {r.subject for r in a_rows} == {"A only"}
        finally:
            reset_current(token)


class TestCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps every messaging row belonging to it."""

    def test_delete_workspace_cascades(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="ws-cascade@example.com",
            display="WsCascade",
            slug="ws-cascade-ws",
            name="WsCascadeWS",
        )
        token = set_current(_ctx_for(workspace, user.id))
        try:
            channel = ChatChannel(
                id="01HWA00000000000000000CCHW",
                workspace_id=workspace.id,
                kind="staff",
                source="app",
                created_at=_PINNED,
            )
            db_session.add_all(
                [
                    Notification(
                        id="01HWA00000000000000000NOTW",
                        workspace_id=workspace.id,
                        recipient_user_id=user.id,
                        kind="task_assigned",
                        subject="ws-cascade",
                        created_at=_PINNED,
                    ),
                    PushToken(
                        id="01HWA00000000000000000PTKW",
                        workspace_id=workspace.id,
                        user_id=user.id,
                        endpoint="https://fcm.example.com/w",
                        p256dh="P",
                        auth="A",
                        created_at=_PINNED,
                    ),
                    DigestRecord(
                        id="01HWA00000000000000000DGRW",
                        workspace_id=workspace.id,
                        recipient_user_id=user.id,
                        period_start=_PINNED,
                        period_end=_LATER,
                        kind="daily",
                        body_md="ws-cascade body",
                    ),
                    channel,
                ]
            )
            db_session.flush()
            db_session.add(
                ChatMessage(
                    id="01HWA00000000000000000CMSW",
                    workspace_id=workspace.id,
                    channel_id=channel.id,
                    author_user_id=user.id,
                    author_label="Maria",
                    body_md="ws-cascade msg",
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        # Workspace delete predates the context.
        loaded_ws = db_session.get(Workspace, workspace.id)
        assert loaded_ws is not None
        with tenant_agnostic():
            db_session.delete(loaded_ws)
            db_session.flush()

        token = set_current(_ctx_for(workspace, user.id))
        try:
            for model in (Notification, PushToken, DigestRecord, ChatChannel):
                rows = db_session.scalars(
                    select(model).where(model.workspace_id == workspace.id)
                ).all()
                assert rows == [], f"{model.__tablename__} not swept"
            # chat_message has cascaded via chat_channel; check directly.
            msgs = db_session.scalars(
                select(ChatMessage).where(ChatMessage.workspace_id == workspace.id)
            ).all()
            assert msgs == []
        finally:
            reset_current(token)


class TestEmailOptOutCrud:
    """Insert + select + unique-index + check round-trip on :class:`EmailOptOut`."""

    def test_round_trip_and_per_user_lookup(self, db_session: Session) -> None:
        """CRUD happy path: insert, read back, mark another category."""
        workspace, user = _bootstrap(
            db_session,
            email="eoo-crud@example.com",
            display="EooCrud",
            slug="eoo-crud-ws",
            name="EooCrudWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row_a = EmailOptOut(
                id="01HWA00000000000000000EOOA",
                workspace_id=workspace.id,
                user_id=user.id,
                category="task_reminder",
                opted_out_at=_PINNED,
                source="unsubscribe_link",
            )
            row_b = EmailOptOut(
                id="01HWA00000000000000000EOOB",
                workspace_id=workspace.id,
                user_id=user.id,
                category="daily_digest",
                opted_out_at=_LATER,
                source="profile",
            )
            db_session.add_all([row_a, row_b])
            db_session.flush()

            # Per-user lookup — what has this user opted out of?
            rows = db_session.scalars(
                select(EmailOptOut)
                .where(EmailOptOut.workspace_id == workspace.id)
                .where(EmailOptOut.user_id == user.id)
                .order_by(EmailOptOut.category)
            ).all()
            assert [r.category for r in rows] == ["daily_digest", "task_reminder"]

            # SQLite drops tzinfo on round-trip; compare naive UTC form.
            reloaded = db_session.get(EmailOptOut, row_a.id)
            assert reloaded is not None
            assert reloaded.opted_out_at.replace(tzinfo=UTC) == _PINNED
            assert reloaded.source == "unsubscribe_link"

            # Pre-send probe: does a row exist for this triple?
            probe = db_session.scalars(
                select(EmailOptOut)
                .where(EmailOptOut.workspace_id == workspace.id)
                .where(EmailOptOut.user_id == user.id)
                .where(EmailOptOut.category == "task_reminder")
            ).all()
            assert len(probe) == 1
        finally:
            reset_current(ctx_token)

    def test_unique_triple_rejects_duplicate(self, db_session: Session) -> None:
        """Acceptance: unique ``(workspace_id, user_id, category)`` rejects dupe."""
        workspace, user = _bootstrap(
            db_session,
            email="eoo-uq@example.com",
            display="EooUq",
            slug="eoo-uq-ws",
            name="EooUqWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                EmailOptOut(
                    id="01HWA00000000000000000EOUA",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    category="task_reminder",
                    opted_out_at=_PINNED,
                    source="unsubscribe_link",
                )
            )
            db_session.flush()

            db_session.add(
                EmailOptOut(
                    id="01HWA00000000000000000EOUB",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    category="task_reminder",  # same triple — must reject
                    opted_out_at=_LATER,
                    source="profile",
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_different_category_allowed(self, db_session: Session) -> None:
        """A different ``category`` bypasses the unique — two rows coexist."""
        workspace, user = _bootstrap(
            db_session,
            email="eoo-diff@example.com",
            display="EooDiff",
            slug="eoo-diff-ws",
            name="EooDiffWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add_all(
                [
                    EmailOptOut(
                        id="01HWA00000000000000000EODA",
                        workspace_id=workspace.id,
                        user_id=user.id,
                        category="task_reminder",
                        opted_out_at=_PINNED,
                        source="unsubscribe_link",
                    ),
                    EmailOptOut(
                        id="01HWA00000000000000000EODB",
                        workspace_id=workspace.id,
                        user_id=user.id,
                        category="daily_digest",
                        opted_out_at=_PINNED,
                        source="unsubscribe_link",
                    ),
                ]
            )
            # Both rows land — different category is a new row.
            db_session.flush()
        finally:
            reset_current(ctx_token)


class TestEmailDeliveryCrud:
    """Insert + select + retry round-trip on :class:`EmailDelivery`."""

    def test_round_trip_queued_to_delivered(self, db_session: Session) -> None:
        """Walk a row from ``queued → sent → delivered`` with context snapshot."""
        workspace, user = _bootstrap(
            db_session,
            email="edl-crud@example.com",
            display="EdlCrud",
            slug="edl-crud-ws",
            name="EdlCrudWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            context = {"task_id": "01HWA00000000000000000TKAA", "title": "Pool"}
            row = EmailDelivery(
                id="01HWA00000000000000000EDLA",
                workspace_id=workspace.id,
                to_person_id=user.id,
                to_email_at_send="maria@example.com",
                template_key="task_reminder",
                context_snapshot_json=context,
                delivery_state="queued",
                retry_count=0,
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            # Worker dispatches the row — state → sent, sent_at populated.
            loaded = db_session.get(EmailDelivery, row.id)
            assert loaded is not None
            loaded.delivery_state = "sent"
            loaded.sent_at = _LATER
            loaded.provider_message_id = "esp-msg-42"
            db_session.flush()
            db_session.expire_all()

            # ESP webhook confirms delivery.
            reloaded = db_session.get(EmailDelivery, row.id)
            assert reloaded is not None
            reloaded.delivery_state = "delivered"
            db_session.flush()
            db_session.expire_all()

            final = db_session.get(EmailDelivery, row.id)
            assert final is not None
            assert final.delivery_state == "delivered"
            assert final.sent_at is not None
            assert final.sent_at.replace(tzinfo=UTC) == _LATER
            assert final.provider_message_id == "esp-msg-42"
            assert final.context_snapshot_json == context
        finally:
            reset_current(ctx_token)

    def test_soft_ref_accepts_nonexistent_person(self, db_session: Session) -> None:
        """``to_person_id`` is a soft reference — no FK means any ULID lands.

        §10 invoice reminders / stay-upcoming emails can target a
        ``client_user`` that is not yet a :class:`User` row when the
        email is queued. The column is plain :class:`String`; this
        test documents that no FK trips on an unknown id.
        """
        workspace, user = _bootstrap(
            db_session,
            email="edl-soft@example.com",
            display="EdlSoft",
            slug="edl-soft-ws",
            name="EdlSoftWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row = EmailDelivery(
                id="01HWA00000000000000000EDSA",
                workspace_id=workspace.id,
                # ULID for a client_user that isn't in ``user`` yet.
                to_person_id="01HWA0000000000000000GHOST",
                to_email_at_send="guest@example.com",
                template_key="stay_upcoming",
                delivery_state="queued",
                retry_count=0,
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            reloaded = db_session.get(EmailDelivery, row.id)
            assert reloaded is not None
            assert reloaded.to_person_id == "01HWA0000000000000000GHOST"
        finally:
            reset_current(ctx_token)

    def test_retry_path_records_first_error(self, db_session: Session) -> None:
        """Retry path: ``first_error`` is stamped once, ``retry_count`` bumps."""
        workspace, user = _bootstrap(
            db_session,
            email="edl-retry@example.com",
            display="EdlRetry",
            slug="edl-retry-ws",
            name="EdlRetryWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            row = EmailDelivery(
                id="01HWA00000000000000000EDRA",
                workspace_id=workspace.id,
                to_person_id=user.id,
                to_email_at_send="m@example.com",
                template_key="task_reminder",
                delivery_state="queued",
                first_error="smtp: 421 temporary failure",
                retry_count=1,
                created_at=_PINNED,
            )
            db_session.add(row)
            db_session.flush()

            # Second retry — worker bumps ``retry_count`` but leaves
            # ``first_error`` untouched (the original reason stays).
            loaded = db_session.get(EmailDelivery, row.id)
            assert loaded is not None
            loaded.retry_count = 2
            db_session.flush()
            db_session.expire_all()

            final = db_session.get(EmailDelivery, row.id)
            assert final is not None
            assert final.retry_count == 2
            assert final.first_error == "smtp: 421 temporary failure"
        finally:
            reset_current(ctx_token)


class TestEmailChecks:
    """CHECK constraints on :class:`EmailOptOut` / :class:`EmailDelivery`."""

    def test_bogus_opt_out_source_rejected(self, db_session: Session) -> None:
        """Acceptance: ``email_opt_out.source = 'other'`` rejected."""
        workspace, user = _bootstrap(
            db_session,
            email="bogus-eoos@example.com",
            display="BogusEoos",
            slug="bogus-eoos-ws",
            name="BogusEoosWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                EmailOptOut(
                    id="01HWA00000000000000000BEOS",
                    workspace_id=workspace.id,
                    user_id=user.id,
                    category="task_reminder",
                    opted_out_at=_PINNED,
                    source="other",  # not in the enum
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)

    def test_bogus_delivery_state_rejected(self, db_session: Session) -> None:
        """Acceptance: ``email_delivery.delivery_state = 'retrying'`` rejected."""
        workspace, user = _bootstrap(
            db_session,
            email="bogus-edls@example.com",
            display="BogusEdls",
            slug="bogus-edls-ws",
            name="BogusEdlsWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add(
                EmailDelivery(
                    id="01HWA00000000000000000BEDS",
                    workspace_id=workspace.id,
                    to_person_id=user.id,
                    to_email_at_send="m@example.com",
                    template_key="task_reminder",
                    delivery_state="retrying",  # not in the enum
                    retry_count=0,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(ctx_token)


class TestEmailCrossWorkspaceIsolation:
    """Email opt-out / delivery rows stay scoped to their workspace."""

    def test_opt_out_same_triple_different_workspace_allowed(
        self, db_session: Session
    ) -> None:
        """Two workspaces can each hold an opt-out for the same user+category.

        The unique index is scoped on ``(workspace_id, user_id,
        category)`` — the leading column is what lets two tenants
        coexist. Same user can appear in both workspaces via
        different ``role_grant`` rows (§03), so this is a real shape.
        """
        ws_a, user = _bootstrap(
            db_session,
            email="eoo-xws@example.com",
            display="EooXws",
            slug="eoo-xws-a",
            name="EooXwsA",
        )
        # Second workspace owned by the same user.
        clock = FrozenClock(_PINNED)
        ws_b = bootstrap_workspace(
            db_session,
            slug="eoo-xws-b",
            name="EooXwsB",
            owner_user_id=user.id,
            clock=clock,
        )

        token_a = set_current(_ctx_for(ws_a, user.id))
        try:
            db_session.add(
                EmailOptOut(
                    id="01HWA00000000000000000XOA1",
                    workspace_id=ws_a.id,
                    user_id=user.id,
                    category="task_reminder",
                    opted_out_at=_PINNED,
                    source="unsubscribe_link",
                )
            )
            db_session.flush()
        finally:
            reset_current(token_a)

        token_b = set_current(_ctx_for(ws_b, user.id))
        try:
            db_session.add(
                EmailOptOut(
                    id="01HWA00000000000000000XOB1",
                    workspace_id=ws_b.id,
                    user_id=user.id,
                    category="task_reminder",  # same category, different ws
                    opted_out_at=_PINNED,
                    source="unsubscribe_link",
                )
            )
            # Both rows land — the unique is per-workspace.
            db_session.flush()

            b_rows = db_session.scalars(
                select(EmailOptOut).where(EmailOptOut.workspace_id == ws_b.id)
            ).all()
            assert [r.category for r in b_rows] == ["task_reminder"]
        finally:
            reset_current(token_b)

    def test_delivery_scoped_per_workspace(self, db_session: Session) -> None:
        """``EmailDelivery`` rows do not leak across workspaces."""
        ws_a, user_a = _bootstrap(
            db_session,
            email="edl-xws-a@example.com",
            display="EdlXwsA",
            slug="edl-xws-a-ws",
            name="EdlXwsAWS",
        )
        ws_b, user_b = _bootstrap(
            db_session,
            email="edl-xws-b@example.com",
            display="EdlXwsB",
            slug="edl-xws-b-ws",
            name="EdlXwsBWS",
        )

        token_a = set_current(_ctx_for(ws_a, user_a.id))
        try:
            db_session.add(
                EmailDelivery(
                    id="01HWA00000000000000000XDA1",
                    workspace_id=ws_a.id,
                    to_person_id=user_a.id,
                    to_email_at_send="a@example.com",
                    template_key="task_reminder",
                    delivery_state="queued",
                    retry_count=0,
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        finally:
            reset_current(token_a)

        token_b = set_current(_ctx_for(ws_b, user_b.id))
        try:
            db_session.add(
                EmailDelivery(
                    id="01HWA00000000000000000XDB1",
                    workspace_id=ws_b.id,
                    to_person_id=user_b.id,
                    to_email_at_send="b@example.com",
                    template_key="task_reminder",
                    delivery_state="queued",
                    retry_count=0,
                    created_at=_PINNED,
                )
            )
            db_session.flush()

            b_rows = db_session.scalars(
                select(EmailDelivery).where(EmailDelivery.workspace_id == ws_b.id)
            ).all()
            assert {r.to_email_at_send for r in b_rows} == {"b@example.com"}

            a_rows = db_session.scalars(
                select(EmailDelivery).where(EmailDelivery.workspace_id == ws_a.id)
            ).all()
            assert {r.to_email_at_send for r in a_rows} == {"a@example.com"}
        finally:
            reset_current(token_b)


class TestEmailCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps ``email_opt_out`` + ``email_delivery`` rows."""

    def test_delete_workspace_cascades_email_rows(self, db_session: Session) -> None:
        from app.tenancy import tenant_agnostic

        workspace, user = _bootstrap(
            db_session,
            email="email-cascade@example.com",
            display="EmailCascade",
            slug="email-cascade-ws",
            name="EmailCascadeWS",
        )
        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            db_session.add_all(
                [
                    EmailOptOut(
                        id="01HWA00000000000000000ECEO",
                        workspace_id=workspace.id,
                        user_id=user.id,
                        category="task_reminder",
                        opted_out_at=_PINNED,
                        source="unsubscribe_link",
                    ),
                    EmailDelivery(
                        id="01HWA00000000000000000ECED",
                        workspace_id=workspace.id,
                        to_person_id=user.id,
                        to_email_at_send="m@example.com",
                        template_key="task_reminder",
                        delivery_state="queued",
                        retry_count=0,
                        created_at=_PINNED,
                    ),
                ]
            )
            db_session.flush()
        finally:
            reset_current(ctx_token)

        loaded_ws = db_session.get(Workspace, workspace.id)
        assert loaded_ws is not None
        with tenant_agnostic():
            db_session.delete(loaded_ws)
            db_session.flush()

        ctx_token = set_current(_ctx_for(workspace, user.id))
        try:
            for model in (EmailOptOut, EmailDelivery):
                rows = db_session.scalars(
                    select(model).where(model.workspace_id == workspace.id)
                ).all()
                assert rows == [], f"{model.__tablename__} not swept"
        finally:
            reset_current(ctx_token)


class TestEmailCascadeOnUserDelete:
    """Deleting a user sweeps ``email_opt_out`` rows (CASCADE).

    ``email_delivery`` has no user FK (``to_person_id`` is a soft
    reference) so rows survive the user delete — the ledger's audit
    trail is longer-lived than the user row. This mirrors the
    ``ChatMessage.author_user_id SET NULL`` rationale but enforced
    through "no FK at all" rather than a SET NULL rule.
    """

    def test_delete_user_cascades_opt_outs_but_not_deliveries(
        self, db_session: Session
    ) -> None:
        from app.tenancy import tenant_agnostic

        workspace, owner = _bootstrap(
            db_session,
            email="user-cascade-owner@example.com",
            display="UserCascOwner",
            slug="user-cascade-ws",
            name="UserCascWS",
        )
        clock = FrozenClock(_PINNED)
        target = bootstrap_user(
            db_session,
            email="user-cascade-target@example.com",
            display_name="Target",
            clock=clock,
        )

        ctx_token = set_current(_ctx_for(workspace, owner.id))
        try:
            db_session.add_all(
                [
                    EmailOptOut(
                        id="01HWA00000000000000000UCEO",
                        workspace_id=workspace.id,
                        user_id=target.id,
                        category="task_reminder",
                        opted_out_at=_PINNED,
                        source="profile",
                    ),
                    EmailDelivery(
                        id="01HWA00000000000000000UCED",
                        workspace_id=workspace.id,
                        to_person_id=target.id,
                        to_email_at_send="target@example.com",
                        template_key="task_reminder",
                        delivery_state="queued",
                        retry_count=0,
                        created_at=_PINNED,
                    ),
                ]
            )
            db_session.flush()
        finally:
            reset_current(ctx_token)

        with tenant_agnostic():
            db_session.delete(target)
            db_session.flush()

        ctx_token = set_current(_ctx_for(workspace, owner.id))
        try:
            db_session.expire_all()
            # Opt-out: CASCADE — row gone.
            opt_rows = db_session.scalars(
                select(EmailOptOut).where(EmailOptOut.workspace_id == workspace.id)
            ).all()
            assert opt_rows == []

            # Delivery: no FK on to_person_id — row survives with a
            # dangling ULID pointer (by design per §10).
            del_rows = db_session.scalars(
                select(EmailDelivery).where(EmailDelivery.workspace_id == workspace.id)
            ).all()
            assert len(del_rows) == 1
            assert del_rows[0].to_person_id == target.id
        finally:
            reset_current(ctx_token)


class TestTenantFilter:
    """All seven messaging tables are workspace-scoped under the filter."""

    @pytest.mark.parametrize(
        "model",
        [
            Notification,
            PushToken,
            DigestRecord,
            ChatChannel,
            ChatMessage,
            EmailOptOut,
            EmailDelivery,
        ],
    )
    def test_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[Notification]
        | type[PushToken]
        | type[DigestRecord]
        | type[ChatChannel]
        | type[ChatMessage]
        | type[EmailOptOut]
        | type[EmailDelivery],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__
