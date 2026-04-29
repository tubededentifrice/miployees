"""SA-backed repositories implementing :mod:`app.domain.messaging.ports`.

The concrete class here adapts SQLAlchemy ``Session`` work to the
Protocol surface :mod:`app.domain.messaging.push_tokens` consumes
(cd-74pb):

* :class:`SqlAlchemyPushTokenRepository` — wraps the ``push_token``
  table and the per-workspace VAPID setting on
  ``workspace.settings_json``.

Reaches into both :mod:`app.adapters.db.messaging.models` (for
``push_token`` rows) and :mod:`app.adapters.db.workspace.models` (for
the ``Workspace.settings_json`` lookup that backs
:func:`~app.domain.messaging.push_tokens.get_vapid_public_key`).
Adapter-to-adapter imports are allowed by the import-linter — only
``app.domain → app.adapters`` is forbidden.

The repo carries an open ``Session`` and never commits or flushes
beyond what the underlying statements require — the caller's UoW
owns the transaction boundary (§01 "Key runtime invariants" #3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatChannelMember,
    ChatGatewayBinding,
    ChatMessage,
    PushToken,
)
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.domain.messaging.ports import (
    ChatChannelRepository,
    ChatChannelRow,
    ChatGatewayBindingRow,
    ChatGatewayRepository,
    ChatMessageRepository,
    ChatMessageRow,
    PushTokenRepository,
    PushTokenRow,
)
from app.tenancy import tenant_agnostic

__all__ = [
    "SqlAlchemyChatChannelRepository",
    "SqlAlchemyChatGatewayRepository",
    "SqlAlchemyChatMessageRepository",
    "SqlAlchemyPushTokenRepository",
]


def _to_row(row: PushToken) -> PushTokenRow:
    """Project an ORM ``PushToken`` into the seam-level row.

    Field-by-field copy — :class:`PushTokenRow` is frozen so the
    domain never mutates the ORM-managed instance through a shared
    reference.
    """
    return PushTokenRow(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        endpoint=row.endpoint,
        p256dh=row.p256dh,
        auth=row.auth,
        user_agent=row.user_agent,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
    )


def _to_channel_row(row: ChatChannel) -> ChatChannelRow:
    return ChatChannelRow(
        id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        source=row.source,
        external_ref=row.external_ref,
        title=row.title,
        created_at=_as_utc(row.created_at),
        archived_at=_as_utc(row.archived_at) if row.archived_at is not None else None,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_message_row(row: ChatMessage) -> ChatMessageRow:
    return ChatMessageRow(
        id=row.id,
        workspace_id=row.workspace_id,
        channel_id=row.channel_id,
        author_user_id=row.author_user_id,
        author_label=row.author_label,
        body_md=row.body_md,
        attachments_json=[
            {"blob_hash": str(item["blob_hash"])}
            for item in row.attachments_json
            if isinstance(item, dict) and "blob_hash" in item
        ],
        dispatched_to_agent_at=(
            _as_utc(row.dispatched_to_agent_at)
            if row.dispatched_to_agent_at is not None
            else None
        ),
        created_at=_as_utc(row.created_at),
    )


def _to_gateway_binding_row(row: ChatGatewayBinding) -> ChatGatewayBindingRow:
    return ChatGatewayBindingRow(
        id=row.id,
        workspace_id=row.workspace_id,
        provider=row.provider,
        external_contact=row.external_contact,
        channel_id=row.channel_id,
        display_label=row.display_label,
        provider_metadata_json=dict(row.provider_metadata_json),
        created_at=_as_utc(row.created_at),
        last_message_at=(
            _as_utc(row.last_message_at) if row.last_message_at is not None else None
        ),
    )


class SqlAlchemyChatChannelRepository(ChatChannelRepository):
    """SA-backed concretion of :class:`ChatChannelRepository`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def insert(
        self,
        *,
        channel_id: str,
        workspace_id: str,
        kind: str,
        source: str,
        external_ref: str | None,
        title: str | None,
        created_at: datetime,
    ) -> ChatChannelRow:
        row = ChatChannel(
            id=channel_id,
            workspace_id=workspace_id,
            kind=kind,
            source=source,
            external_ref=external_ref,
            title=title,
            created_at=created_at,
            archived_at=None,
        )
        self._session.add(row)
        self._session.flush()
        return _to_channel_row(row)

    def list(
        self,
        *,
        workspace_id: str,
        kinds: Sequence[str],
        include_archived: bool,
        after_id: str | None,
        limit: int,
    ) -> Sequence[ChatChannelRow]:
        stmt = (
            select(ChatChannel)
            .where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.kind.in_(tuple(kinds)),
            )
            .order_by(ChatChannel.id.asc())
            .limit(limit)
        )
        if after_id is not None:
            stmt = stmt.where(ChatChannel.id > after_id)
        if not include_archived:
            stmt = stmt.where(ChatChannel.archived_at.is_(None))
        rows = self._session.scalars(stmt).all()
        return [_to_channel_row(row) for row in rows]

    def get(
        self,
        *,
        workspace_id: str,
        channel_id: str,
    ) -> ChatChannelRow | None:
        row = self._session.scalars(
            select(ChatChannel).where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.id == channel_id,
            )
        ).one_or_none()
        return _to_channel_row(row) if row is not None else None

    def rename(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        title: str | None,
    ) -> ChatChannelRow:
        row = self._load(workspace_id=workspace_id, channel_id=channel_id)
        row.title = title
        self._session.flush()
        return _to_channel_row(row)

    def archive(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        archived_at: datetime,
    ) -> ChatChannelRow:
        row = self._load(workspace_id=workspace_id, channel_id=channel_id)
        if row.archived_at is None:
            row.archived_at = archived_at
            self._session.flush()
        return _to_channel_row(row)

    def add_member(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        user_id: str,
        added_at: datetime,
    ) -> None:
        existing = self._session.get(ChatChannelMember, (channel_id, user_id))
        if existing is not None:
            return
        self._session.add(
            ChatChannelMember(
                channel_id=channel_id,
                user_id=user_id,
                workspace_id=workspace_id,
                added_at=added_at,
            )
        )
        self._session.flush()

    def is_workspace_member(self, *, workspace_id: str, user_id: str) -> bool:
        return (
            self._session.scalars(
                select(UserWorkspace.user_id)
                .where(
                    UserWorkspace.workspace_id == workspace_id,
                    UserWorkspace.user_id == user_id,
                )
                .limit(1)
            ).first()
            is not None
        )

    def remove_member(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        user_id: str,
    ) -> None:
        row = self._session.get(ChatChannelMember, (channel_id, user_id))
        if row is None or row.workspace_id != workspace_id:
            return
        self._session.delete(row)
        self._session.flush()

    def _load(self, *, workspace_id: str, channel_id: str) -> ChatChannel:
        return self._session.scalars(
            select(ChatChannel).where(
                ChatChannel.workspace_id == workspace_id,
                ChatChannel.id == channel_id,
            )
        ).one()


class SqlAlchemyChatMessageRepository(ChatMessageRepository):
    """SA-backed concretion of :class:`ChatMessageRepository`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def display_label_for_user(self, *, workspace_id: str, user_id: str) -> str:
        row = self._session.scalars(
            select(User)
            .join(UserWorkspace, UserWorkspace.user_id == User.id)
            .where(
                User.id == user_id,
                UserWorkspace.workspace_id == workspace_id,
            )
        ).one()
        return row.display_name or row.email

    def insert(
        self,
        *,
        message_id: str,
        workspace_id: str,
        channel_id: str,
        author_user_id: str | None,
        author_label: str,
        body_md: str,
        attachments_json: list[dict[str, str]],
        created_at: datetime,
    ) -> ChatMessageRow:
        row = ChatMessage(
            id=message_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            author_user_id=author_user_id,
            author_label=author_label,
            body_md=body_md,
            attachments_json=attachments_json,
            dispatched_to_agent_at=None,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _to_message_row(row)

    def list_for_channel(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        before_created_at: datetime | None,
        before_id: str | None,
        limit: int,
    ) -> Sequence[ChatMessageRow]:
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.workspace_id == workspace_id,
                ChatMessage.channel_id == channel_id,
            )
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit)
        )
        if before_created_at is not None and before_id is not None:
            stmt = stmt.where(
                or_(
                    ChatMessage.created_at < before_created_at,
                    (
                        (ChatMessage.created_at == before_created_at)
                        & (ChatMessage.id < before_id)
                    ),
                )
            )
        rows = self._session.scalars(stmt).all()
        return [_to_message_row(row) for row in rows]


class SqlAlchemyChatGatewayRepository(ChatGatewayRepository):
    """SA-backed concretion for inbound chat gateway persistence."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    def find_binding(
        self, *, provider: str, external_contact: str
    ) -> ChatGatewayBindingRow | None:
        with tenant_agnostic():
            # justification: provider webhooks are bare-host ingress; the
            # binding row itself carries the workspace selected by config.
            row = self._session.scalars(
                select(ChatGatewayBinding).where(
                    ChatGatewayBinding.provider == provider,
                    ChatGatewayBinding.external_contact == external_contact,
                )
            ).one_or_none()
        return _to_gateway_binding_row(row) if row is not None else None

    def insert_binding_with_channel(
        self,
        *,
        binding_id: str,
        channel_id: str,
        workspace_id: str,
        provider: str,
        external_contact: str,
        channel_source: str,
        display_label: str,
        provider_metadata_json: dict[str, object],
        created_at: datetime,
    ) -> ChatGatewayBindingRow:
        channel = ChatChannel(
            id=channel_id,
            workspace_id=workspace_id,
            kind="chat_gateway",
            source=channel_source,
            external_ref=f"{provider}:{external_contact}",
            title=display_label,
            created_at=created_at,
            archived_at=None,
        )
        binding = ChatGatewayBinding(
            id=binding_id,
            workspace_id=workspace_id,
            provider=provider,
            external_contact=external_contact,
            channel_id=channel_id,
            display_label=display_label,
            provider_metadata_json=dict(provider_metadata_json),
            created_at=created_at,
            last_message_at=None,
        )
        self._session.add_all([channel, binding])
        self._session.flush()
        return _to_gateway_binding_row(binding)

    def touch_binding(
        self, *, binding_id: str, last_message_at: datetime
    ) -> ChatGatewayBindingRow:
        with tenant_agnostic():
            # justification: provider webhooks resolve tenant from the binding,
            # not an authenticated workspace route.
            row = self._session.get(ChatGatewayBinding, binding_id)
            if row is None:
                raise LookupError(f"chat_gateway_binding {binding_id!r} not found")
            row.last_message_at = last_message_at
            self._session.flush()
        return _to_gateway_binding_row(row)

    def find_message_by_provider_id(
        self, *, source: str, provider_message_id: str
    ) -> ChatMessageRow | None:
        with tenant_agnostic():
            # justification: replay defeat must work before a tenant context is
            # bound; source/provider_message_id is globally unique.
            row = self._session.scalars(
                select(ChatMessage).where(
                    ChatMessage.source == source,
                    ChatMessage.provider_message_id == provider_message_id,
                )
            ).one_or_none()
        return _to_message_row(row) if row is not None else None

    def insert_inbound_message(
        self,
        *,
        message_id: str,
        workspace_id: str,
        channel_id: str,
        gateway_binding_id: str,
        source: str,
        provider_message_id: str,
        author_label: str,
        body_md: str,
        created_at: datetime,
    ) -> ChatMessageRow:
        row = ChatMessage(
            id=message_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            author_user_id=None,
            author_label=author_label,
            body_md=body_md,
            attachments_json=[],
            source=source,
            provider_message_id=provider_message_id,
            gateway_binding_id=gateway_binding_id,
            dispatched_to_agent_at=None,
            created_at=created_at,
        )
        self._session.add(row)
        self._session.flush()
        return _to_message_row(row)


class SqlAlchemyPushTokenRepository(PushTokenRepository):
    """SA-backed concretion of :class:`PushTokenRepository`.

    Wraps an open :class:`~sqlalchemy.orm.Session` and never commits
    or flushes outside what the underlying statements require — the
    caller's UoW owns the transaction boundary (§01 "Key runtime
    invariants" #3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def session(self) -> Session:
        return self._session

    # -- Reads -----------------------------------------------------------

    def find_by_user_endpoint(
        self, *, workspace_id: str, user_id: str, endpoint: str
    ) -> PushTokenRow | None:
        row = self._session.scalars(
            select(PushToken).where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
                PushToken.endpoint == endpoint,
            )
        ).one_or_none()
        return _to_row(row) if row is not None else None

    def list_for_user(
        self, *, workspace_id: str, user_id: str
    ) -> Sequence[PushTokenRow]:
        rows = self._session.scalars(
            select(PushToken)
            .where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
            )
            .order_by(PushToken.created_at.asc(), PushToken.id.asc())
        ).all()
        return [_to_row(row) for row in rows]

    def get_workspace_vapid_public_key(
        self, *, workspace_id: str, settings_key: str
    ) -> str | None:
        # ``settings_json`` is a flat dict — see
        # :class:`~app.adapters.db.workspace.models.Workspace` docstring.
        # We collapse "row missing", "settings not a dict", "key absent"
        # and "value not a non-empty string" into a single ``None``
        # return because they're operationally identical for the
        # caller (operator must provision the keypair). The defensive
        # ``isinstance`` mirrors the recovery-helper pattern in
        # ``app/auth/recovery.py``.
        payload = self._session.scalars(
            select(Workspace.settings_json).where(Workspace.id == workspace_id)
        ).one_or_none()
        if payload is None or not isinstance(payload, dict):
            return None
        value = payload.get(settings_key)
        if not isinstance(value, str) or not value:
            return None
        return value

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        token_id: str,
        workspace_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str | None,
        created_at: datetime,
    ) -> PushTokenRow:
        row = PushToken(
            id=token_id,
            workspace_id=workspace_id,
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            user_agent=user_agent,
            created_at=created_at,
            last_used_at=None,
        )
        self._session.add(row)
        self._session.flush()
        return _to_row(row)

    def update_keys(
        self,
        *,
        workspace_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str | None = None,
        auth: str | None = None,
        user_agent: str | None = None,
    ) -> PushTokenRow:
        # Pre-existing service contract: caller has just confirmed
        # the row exists via :meth:`find_by_user_endpoint`. Use the
        # same SELECT shape so the caller's UoW reuses the identity-
        # map entry rather than spawning a second instance for the
        # same primary key.
        row = self._session.scalars(
            select(PushToken).where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
                PushToken.endpoint == endpoint,
            )
        ).one()

        # Mirror the prior service-layer change-detection so a benign
        # refresh (browser re-running its service worker against the
        # same row, with identical keys + UA) never marks the row
        # dirty. Keeps SQLAlchemy from issuing an UPDATE — which in
        # turn keeps the caller's "no audit row on benign refresh"
        # invariant intact.
        changed = False
        if p256dh is not None and row.p256dh != p256dh:
            row.p256dh = p256dh
            changed = True
        if auth is not None and row.auth != auth:
            row.auth = auth
            changed = True
        # ``user_agent`` follows the existing service rule of "only
        # refresh when the caller actually provided one" — a curl
        # caller passes ``None`` and we keep the prior snapshot.
        if user_agent is not None and row.user_agent != user_agent:
            row.user_agent = user_agent
            changed = True
        if changed:
            self._session.flush()
        return _to_row(row)

    def delete(self, *, workspace_id: str, user_id: str, endpoint: str) -> None:
        row = self._session.scalars(
            select(PushToken).where(
                PushToken.workspace_id == workspace_id,
                PushToken.user_id == user_id,
                PushToken.endpoint == endpoint,
            )
        ).one_or_none()
        if row is None:
            # Idempotent: deleting a missing row is a no-op. The
            # caller's audit row still records the intent on a
            # successful prior find.
            return
        self._session.delete(row)
        self._session.flush()
