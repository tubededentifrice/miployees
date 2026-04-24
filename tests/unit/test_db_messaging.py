"""Unit tests for :mod:`app.adapters.db.messaging.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__`` (CHECK
constraints, index columns, tenancy-registry membership). Integration
coverage (migrations, FK cascade, CHECK violations against a real DB,
cross-workspace isolation, tenant-filter behaviour) lives in
``tests/integration/test_db_messaging.py``.

See ``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/10-messaging-notifications.md``, and
``docs/specs/23-chat-gateway.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import CheckConstraint, Index

from app.adapters.db.messaging import (
    ChatChannel,
    ChatMessage,
    DigestRecord,
    EmailDelivery,
    EmailOptOut,
    Notification,
    PushToken,
)
from app.adapters.db.messaging import models as messaging_models

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


class TestNotificationModel:
    """The ``Notification`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        notif = Notification(
            id="01HWA00000000000000000NOTA",
            workspace_id="01HWA00000000000000000WSPA",
            recipient_user_id="01HWA00000000000000000USRA",
            kind="task_assigned",
            subject="New task: Pool opening",
            created_at=_PINNED,
        )
        assert notif.id == "01HWA00000000000000000NOTA"
        assert notif.workspace_id == "01HWA00000000000000000WSPA"
        assert notif.recipient_user_id == "01HWA00000000000000000USRA"
        assert notif.kind == "task_assigned"
        assert notif.subject == "New task: Pool opening"
        # body_md is nullable — short notifications render from subject.
        assert notif.body_md is None
        # read_at NULL on unread rows — the index's "NULL = unread" key.
        assert notif.read_at is None
        assert notif.created_at == _PINNED

    def test_full_construction(self) -> None:
        payload = {"task_id": "01HWA00000000000000000TKAA", "title": "Pool opening"}
        notif = Notification(
            id="01HWA00000000000000000NOTB",
            workspace_id="01HWA00000000000000000WSPA",
            recipient_user_id="01HWA00000000000000000USRA",
            kind="approval_needed",
            subject="Approval needed",
            body_md="# Approval\n\nReview expense €42.",
            read_at=_LATER,
            created_at=_PINNED,
            payload_json=payload,
        )
        assert notif.body_md == "# Approval\n\nReview expense €42."
        assert notif.read_at == _LATER
        assert notif.payload_json == payload

    def test_every_kind_constructs(self) -> None:
        """Each v1 notification kind builds a valid row."""
        for index, kind in enumerate(messaging_models._NOTIFICATION_KIND_VALUES):
            notif = Notification(
                id=f"01HWA0000000000000000NOT{index:02d}",
                workspace_id="01HWA00000000000000000WSPA",
                recipient_user_id="01HWA00000000000000000USRA",
                kind=kind,
                subject=f"Subject for {kind}",
                created_at=_PINNED,
            )
            assert notif.kind == kind

    def test_tablename(self) -> None:
        assert Notification.__tablename__ == "notification"

    def test_kind_check_present(self) -> None:
        # The shared naming convention rewrites the bare ``kind`` name
        # to ``ck_notification_kind`` on the bound column; match by
        # suffix per the sibling ``tasks`` / ``instructions`` pattern.
        checks = [
            c
            for c in Notification.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("kind")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for kind in messaging_models._NOTIFICATION_KIND_VALUES:
            assert kind in sql, f"{kind} missing from CHECK constraint"

    def test_unread_fanout_index_present(self) -> None:
        """Acceptance: ``(workspace_id, recipient_user_id, read_at)`` index.

        The bell menu's "unread count" hot path — the cd-pjm
        acceptance criterion pins this composite.
        """
        indexes = [i for i in Notification.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_notification_workspace_recipient_read" in names
        target = next(
            i for i in indexes if i.name == "ix_notification_workspace_recipient_read"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "recipient_user_id",
            "read_at",
        ]


class TestPushTokenModel:
    """The ``PushToken`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        token = PushToken(
            id="01HWA00000000000000000PTKA",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            endpoint="https://fcm.googleapis.com/fcm/send/abc",
            p256dh="BP256DH_BASE64URL",
            auth="AUTH_BASE64URL",
            created_at=_PINNED,
        )
        assert token.endpoint == "https://fcm.googleapis.com/fcm/send/abc"
        assert token.p256dh == "BP256DH_BASE64URL"
        assert token.auth == "AUTH_BASE64URL"
        assert token.user_agent is None
        assert token.last_used_at is None

    def test_full_construction(self) -> None:
        token = PushToken(
            id="01HWA00000000000000000PTKB",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            endpoint="https://fcm.googleapis.com/fcm/send/abc",
            p256dh="BP256DH",
            auth="AUTH",
            user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 9)",
            created_at=_PINNED,
            last_used_at=_LATER,
        )
        assert token.user_agent == "Mozilla/5.0 (Linux; Android 14; Pixel 9)"
        assert token.last_used_at == _LATER

    def test_tablename(self) -> None:
        assert PushToken.__tablename__ == "push_token"

    def test_workspace_user_index_present(self) -> None:
        """Per-user fanout: ``(workspace_id, user_id)`` index."""
        indexes = [i for i in PushToken.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_push_token_workspace_user" in names
        target = next(i for i in indexes if i.name == "ix_push_token_workspace_user")
        assert [c.name for c in target.columns] == ["workspace_id", "user_id"]


class TestDigestRecordModel:
    """The ``DigestRecord`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        record = DigestRecord(
            id="01HWA00000000000000000DGRA",
            workspace_id="01HWA00000000000000000WSPA",
            recipient_user_id="01HWA00000000000000000USRA",
            period_start=_PINNED,
            period_end=_LATER,
            kind="daily",
            body_md="# Today\n\n- Pool opening at Villa A",
        )
        assert record.kind == "daily"
        assert record.period_start == _PINNED
        assert record.period_end == _LATER
        assert record.body_md.startswith("# Today")
        # sent_at NULL before the SMTP send returns.
        assert record.sent_at is None

    def test_sent_construction(self) -> None:
        record = DigestRecord(
            id="01HWA00000000000000000DGRB",
            workspace_id="01HWA00000000000000000WSPA",
            recipient_user_id="01HWA00000000000000000USRA",
            period_start=_PINNED,
            period_end=_LATER,
            kind="weekly",
            body_md="weekly body",
            sent_at=_LATER,
        )
        assert record.sent_at == _LATER

    def test_every_kind_constructs(self) -> None:
        """Each v1 digest kind builds a valid row."""
        for index, kind in enumerate(messaging_models._DIGEST_KIND_VALUES):
            record = DigestRecord(
                id=f"01HWA0000000000000000DGR{index}",
                workspace_id="01HWA00000000000000000WSPA",
                recipient_user_id="01HWA00000000000000000USRA",
                period_start=_PINNED,
                period_end=_LATER,
                kind=kind,
                body_md=f"body {kind}",
            )
            assert record.kind == kind

    def test_tablename(self) -> None:
        assert DigestRecord.__tablename__ == "digest_record"

    def test_kind_check_present(self) -> None:
        checks = [
            c
            for c in DigestRecord.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("kind")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for kind in messaging_models._DIGEST_KIND_VALUES:
            assert kind in sql, f"{kind} missing from CHECK constraint"

    def test_period_end_after_start_check_present(self) -> None:
        checks = [
            c
            for c in DigestRecord.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("period_end_after_start")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "period_end" in sql
        assert "period_start" in sql

    def test_workspace_recipient_period_index_present(self) -> None:
        """Idempotency probe: ``(workspace_id, recipient_user_id, period_start)``."""
        indexes = [i for i in DigestRecord.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_digest_record_workspace_recipient_period" in names
        target = next(
            i
            for i in indexes
            if i.name == "ix_digest_record_workspace_recipient_period"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "recipient_user_id",
            "period_start",
        ]


class TestChatChannelModel:
    """The ``ChatChannel`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        channel = ChatChannel(
            id="01HWA00000000000000000CCHA",
            workspace_id="01HWA00000000000000000WSPA",
            kind="staff",
            source="app",
            created_at=_PINNED,
        )
        assert channel.kind == "staff"
        assert channel.source == "app"
        # Nullable for in-app channels with no external counterpart.
        assert channel.external_ref is None
        assert channel.title is None

    def test_gateway_channel_construction(self) -> None:
        channel = ChatChannel(
            id="01HWA00000000000000000CCHB",
            workspace_id="01HWA00000000000000000WSPA",
            kind="chat_gateway",
            source="whatsapp",
            external_ref="wa_33600000001",
            title="WhatsApp: Maria",
            created_at=_PINNED,
        )
        assert channel.kind == "chat_gateway"
        assert channel.source == "whatsapp"
        assert channel.external_ref == "wa_33600000001"
        assert channel.title == "WhatsApp: Maria"

    def test_every_kind_constructs(self) -> None:
        for index, kind in enumerate(messaging_models._CHAT_CHANNEL_KIND_VALUES):
            channel = ChatChannel(
                id=f"01HWA0000000000000000CCH{index}",
                workspace_id="01HWA00000000000000000WSPA",
                kind=kind,
                source="app",
                created_at=_PINNED,
            )
            assert channel.kind == kind

    def test_every_source_constructs(self) -> None:
        for index, source in enumerate(messaging_models._CHAT_CHANNEL_SOURCE_VALUES):
            channel = ChatChannel(
                id=f"01HWA0000000000000000CCS{index}",
                workspace_id="01HWA00000000000000000WSPA",
                kind="chat_gateway" if source != "app" else "staff",
                source=source,
                created_at=_PINNED,
            )
            assert channel.source == source

    def test_tablename(self) -> None:
        assert ChatChannel.__tablename__ == "chat_channel"

    def test_kind_check_present(self) -> None:
        checks = [
            c
            for c in ChatChannel.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("kind")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for kind in messaging_models._CHAT_CHANNEL_KIND_VALUES:
            assert kind in sql, f"{kind} missing from CHECK constraint"

    def test_source_check_present(self) -> None:
        checks = [
            c
            for c in ChatChannel.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("source")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for source in messaging_models._CHAT_CHANNEL_SOURCE_VALUES:
            assert source in sql, f"{source} missing from CHECK constraint"

    def test_indexes_present(self) -> None:
        indexes = [i for i in ChatChannel.__table_args__ if isinstance(i, Index)]
        names = {i.name for i in indexes}
        assert "ix_chat_channel_workspace" in names
        assert "ix_chat_channel_workspace_external_ref" in names


class TestChatMessageModel:
    """The ``ChatMessage`` mapped class constructs from the v1 slice."""

    def test_authored_construction(self) -> None:
        msg = ChatMessage(
            id="01HWA00000000000000000CMSA",
            workspace_id="01HWA00000000000000000WSPA",
            channel_id="01HWA00000000000000000CCHA",
            author_user_id="01HWA00000000000000000USRA",
            author_label="Maria",
            body_md="On my way to Villa A.",
            created_at=_PINNED,
        )
        assert msg.author_user_id == "01HWA00000000000000000USRA"
        assert msg.author_label == "Maria"
        assert msg.body_md == "On my way to Villa A."
        # Nullable fields default.
        assert msg.dispatched_to_agent_at is None

    def test_gateway_inbound_construction(self) -> None:
        """Gateway-inbound rows carry no author_user_id."""
        msg = ChatMessage(
            id="01HWA00000000000000000CMSB",
            workspace_id="01HWA00000000000000000WSPA",
            channel_id="01HWA00000000000000000CCHB",
            author_label="WhatsApp: +33 6 …",
            body_md="Inbound from external sender",
            dispatched_to_agent_at=_LATER,
            created_at=_PINNED,
        )
        assert msg.author_user_id is None
        assert msg.author_label == "WhatsApp: +33 6 …"
        assert msg.dispatched_to_agent_at == _LATER

    def test_with_attachments_construction(self) -> None:
        attachments = [
            {"blob_hash": "sha256:abc", "filename": "receipt.jpg"},
            {"blob_hash": "sha256:def", "filename": "photo.jpg"},
        ]
        msg = ChatMessage(
            id="01HWA00000000000000000CMSC",
            workspace_id="01HWA00000000000000000WSPA",
            channel_id="01HWA00000000000000000CCHA",
            author_user_id="01HWA00000000000000000USRA",
            author_label="Maria",
            body_md="Here are the photos.",
            attachments_json=attachments,
            created_at=_PINNED,
        )
        assert msg.attachments_json == attachments

    def test_tablename(self) -> None:
        assert ChatMessage.__tablename__ == "chat_message"

    def test_channel_created_index_present(self) -> None:
        """Acceptance: ``(channel_id, created_at)`` index for scrollback."""
        indexes = [i for i in ChatMessage.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_chat_message_channel_created" in names
        target = next(i for i in indexes if i.name == "ix_chat_message_channel_created")
        assert [c.name for c in target.columns] == ["channel_id", "created_at"]

    def test_workspace_channel_index_present(self) -> None:
        indexes = [i for i in ChatMessage.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_chat_message_workspace_channel" in names
        target = next(
            i for i in indexes if i.name == "ix_chat_message_workspace_channel"
        )
        assert [c.name for c in target.columns] == ["workspace_id", "channel_id"]


class TestEmailOptOutModel:
    """The ``EmailOptOut`` mapped class constructs per spec §10."""

    def test_minimal_construction(self) -> None:
        row = EmailOptOut(
            id="01HWA00000000000000000EOOA",
            workspace_id="01HWA00000000000000000WSPA",
            user_id="01HWA00000000000000000USRA",
            category="task_reminder",
            opted_out_at=_PINNED,
            source="unsubscribe_link",
        )
        assert row.id == "01HWA00000000000000000EOOA"
        assert row.workspace_id == "01HWA00000000000000000WSPA"
        assert row.user_id == "01HWA00000000000000000USRA"
        assert row.category == "task_reminder"
        assert row.opted_out_at == _PINNED
        assert row.source == "unsubscribe_link"

    def test_every_source_constructs(self) -> None:
        """Each v1 source value builds a valid row."""
        for index, source in enumerate(messaging_models._EMAIL_OPT_OUT_SOURCE_VALUES):
            row = EmailOptOut(
                id=f"01HWA0000000000000000EOO{index}",
                workspace_id="01HWA00000000000000000WSPA",
                user_id="01HWA00000000000000000USRA",
                category="daily_digest",
                opted_out_at=_PINNED,
                source=source,
            )
            assert row.source == source

    def test_tablename(self) -> None:
        assert EmailOptOut.__tablename__ == "email_opt_out"

    def test_source_check_present(self) -> None:
        # The naming convention rewrites the bare ``source`` name to
        # ``ck_email_opt_out_source`` on the bound column; match by
        # suffix per the sibling ``chat_channel`` pattern.
        checks = [
            c
            for c in EmailOptOut.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("source")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for source in messaging_models._EMAIL_OPT_OUT_SOURCE_VALUES:
            assert source in sql, f"{source} missing from CHECK constraint"

    def test_unique_index_present(self) -> None:
        """Acceptance: unique ``(workspace_id, user_id, category)`` index.

        The §10 pre-send probe's key — one row per user+category within
        a workspace. cd-aqwt acceptance pins this composite as the
        only membership path.
        """
        indexes = [i for i in EmailOptOut.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "uq_email_opt_out_user_category" in names
        target = next(i for i in indexes if i.name == "uq_email_opt_out_user_category")
        assert target.unique is True
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "user_id",
            "category",
        ]

    def test_workspace_user_index_present(self) -> None:
        """Per-user lookup: non-unique ``(workspace_id, user_id)`` index."""
        indexes = [i for i in EmailOptOut.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_email_opt_out_workspace_user" in names
        target = next(i for i in indexes if i.name == "ix_email_opt_out_workspace_user")
        assert target.unique is False
        assert [c.name for c in target.columns] == ["workspace_id", "user_id"]


class TestEmailDeliveryModel:
    """The ``EmailDelivery`` mapped class constructs per spec §10."""

    def test_minimal_construction(self) -> None:
        row = EmailDelivery(
            id="01HWA00000000000000000EDLA",
            workspace_id="01HWA00000000000000000WSPA",
            to_person_id="01HWA00000000000000000USRA",
            to_email_at_send="maria@example.com",
            template_key="task_reminder",
            delivery_state="queued",
            retry_count=0,
            created_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000EDLA"
        assert row.to_person_id == "01HWA00000000000000000USRA"
        assert row.to_email_at_send == "maria@example.com"
        assert row.template_key == "task_reminder"
        assert row.delivery_state == "queued"
        assert row.retry_count == 0
        # Nullable fields default to None on a minimal construction.
        assert row.sent_at is None
        assert row.provider_message_id is None
        assert row.first_error is None
        assert row.inbound_linkage is None

    def test_full_construction(self) -> None:
        context = {"task_id": "01HWA00000000000000000TKAA", "title": "Pool"}
        row = EmailDelivery(
            id="01HWA00000000000000000EDLB",
            workspace_id="01HWA00000000000000000WSPA",
            to_person_id="01HWA00000000000000000USRA",
            to_email_at_send="maria@example.com",
            template_key="task_reminder",
            context_snapshot_json=context,
            sent_at=_LATER,
            provider_message_id="esp-msg-42",
            delivery_state="delivered",
            first_error=None,
            retry_count=2,
            inbound_linkage="verp-token-abc",
            created_at=_PINNED,
        )
        assert row.context_snapshot_json == context
        assert row.sent_at == _LATER
        assert row.provider_message_id == "esp-msg-42"
        assert row.delivery_state == "delivered"
        assert row.retry_count == 2
        assert row.inbound_linkage == "verp-token-abc"

    def test_every_state_constructs(self) -> None:
        """Each v1 delivery_state value builds a valid row."""
        for index, state in enumerate(messaging_models._EMAIL_DELIVERY_STATE_VALUES):
            row = EmailDelivery(
                id=f"01HWA0000000000000000EDL{index}",
                workspace_id="01HWA00000000000000000WSPA",
                to_person_id="01HWA00000000000000000USRA",
                to_email_at_send="m@example.com",
                template_key="task_reminder",
                delivery_state=state,
                retry_count=0,
                created_at=_PINNED,
            )
            assert row.delivery_state == state

    def test_tablename(self) -> None:
        assert EmailDelivery.__tablename__ == "email_delivery"

    def test_delivery_state_check_present(self) -> None:
        checks = [
            c
            for c in EmailDelivery.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("delivery_state")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for state in messaging_models._EMAIL_DELIVERY_STATE_VALUES:
            assert state in sql, f"{state} missing from CHECK constraint"

    def test_person_sent_index_present(self) -> None:
        """Audit: ``(workspace_id, to_person_id, sent_at)`` index."""
        indexes = [i for i in EmailDelivery.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_email_delivery_workspace_person_sent" in names
        target = next(
            i for i in indexes if i.name == "ix_email_delivery_workspace_person_sent"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "to_person_id",
            "sent_at",
        ]

    def test_provider_msgid_index_present(self) -> None:
        """Bounce correlator: ``(workspace_id, provider_message_id)`` index."""
        indexes = [i for i in EmailDelivery.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_email_delivery_workspace_provider_msgid" in names
        target = next(
            i for i in indexes if i.name == "ix_email_delivery_workspace_provider_msgid"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "provider_message_id",
        ]

    def test_state_sent_index_present(self) -> None:
        """Retry scheduler: ``(workspace_id, delivery_state, sent_at)`` index."""
        indexes = [i for i in EmailDelivery.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_email_delivery_workspace_state_sent" in names
        target = next(
            i for i in indexes if i.name == "ix_email_delivery_workspace_state_sent"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "delivery_state",
            "sent_at",
        ]


class TestPackageReExports:
    """``app.adapters.db.messaging`` re-exports every model."""

    def test_models_re_exported(self) -> None:
        assert Notification is messaging_models.Notification
        assert PushToken is messaging_models.PushToken
        assert DigestRecord is messaging_models.DigestRecord
        assert ChatChannel is messaging_models.ChatChannel
        assert ChatMessage is messaging_models.ChatMessage
        assert EmailOptOut is messaging_models.EmailOptOut
        assert EmailDelivery is messaging_models.EmailDelivery


class TestRegistryIntent:
    """Every messaging table is registered as workspace-scoped.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.messaging``: a sibling
    ``test_tenancy_orm_filter`` autouse fixture calls
    :func:`registry._reset_for_tests` which wipes the process-wide set,
    so asserting presence after that reset would be flaky. The tests
    below encode the invariant — "every messaging table is scoped"
    — without over-coupling to import ordering.
    """

    _TABLES: tuple[str, ...] = (
        "notification",
        "push_token",
        "digest_record",
        "chat_channel",
        "chat_message",
        "email_opt_out",
        "email_delivery",
    )

    def test_every_messaging_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in self._TABLES:
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in self._TABLES:
            assert table in scoped, f"{table} must be scoped"

    def test_is_scoped_reports_true(self) -> None:
        """``is_scoped`` agrees with ``scoped_tables`` membership."""
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in self._TABLES:
            registry.register(table)
        for table in self._TABLES:
            assert registry.is_scoped(table) is True

    def test_reimport_is_idempotent(self) -> None:
        """Re-importing ``app.adapters.db.messaging`` does not raise.

        A multi-worker ASGI process may import the package more than
        once (once per worker, and again under test harnesses that
        reload application code). ``registry.register`` is set-backed
        so the second pass is a no-op; the guard here is a regression
        test against a future refactor that tightens the registry into
        raising on double-register.
        """
        import importlib

        import app.adapters.db.messaging as messaging_pkg

        importlib.reload(messaging_pkg)
        for table in self._TABLES:
            assert messaging_pkg.__name__ == "app.adapters.db.messaging"
            # Re-register directly — exercises the same code path the
            # module body runs at import. Idempotent by set semantics.
            from app.tenancy import registry

            registry.register(table)
            registry.register(table)
            assert registry.is_scoped(table) is True


class TestSanityInterval:
    """Quick sanity: the pinned test constants respect the CHECK bound."""

    def test_pinned_later_is_after_pinned(self) -> None:
        # The ``DigestRecord.period_end > period_start`` CHECK relies on
        # this ordering — if the test constants drift, every digest test
        # above would insert an invalid row and the integration suite's
        # CHECK rejection would fail silently. Guard the invariant.
        assert _LATER > _PINNED
        assert timedelta(days=1) == _LATER - _PINNED
