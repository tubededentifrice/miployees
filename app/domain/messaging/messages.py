"""Chat-message send/list service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Final

from app.adapters.storage.ports import Storage
from app.audit import write_audit
from app.domain.messaging.channels import (
    ChatChannelService,
)
from app.domain.messaging.ports import (
    ChatChannelRepository,
    ChatMessageRepository,
    ChatMessageRow,
)
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.events.types import ChatMessageSent
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ChatMessageAttachmentMissing",
    "ChatMessageCursor",
    "ChatMessageInvalid",
    "ChatMessageService",
    "ChatMessageView",
    "sanitize_markdown",
]


_MAX_BODY_LEN = 20_000
_MAX_ATTACHMENTS = 10
_BLOB_HASH_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_AUTOLINK_RE: Final[re.Pattern[str]] = re.compile(r"<((?:https?://|mailto:)[^<>\s]+)>")


class ChatMessageInvalid(ValueError):
    """The requested message operation violates the service contract."""


class ChatMessageAttachmentMissing(LookupError):
    """One of the requested attachment blobs does not exist in storage."""

    def __init__(self, blob_hash: str) -> None:
        self.blob_hash = blob_hash
        super().__init__(f"attachment blob {blob_hash!r} was not found")


@dataclass(frozen=True, slots=True)
class ChatMessageCursor:
    created_at: datetime
    id: str


@dataclass(frozen=True, slots=True)
class ChatMessageView:
    id: str
    workspace_id: str
    channel_id: str
    author_user_id: str | None
    author_label: str
    body_md: str
    attachments: tuple[dict[str, str], ...]
    created_at: datetime


class _MarkdownHTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._chunks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.lower() in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._ignored_depth > 0:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self._chunks.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._ignored_depth == 0:
            self._chunks.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._ignored_depth == 0:
            self._chunks.append(f"&#{name};")

    def text(self) -> str:
        return "".join(self._chunks)


def sanitize_markdown(body_md: str) -> str:
    """Strip raw HTML while leaving Markdown syntax and autolinks intact."""

    protected: dict[str, str] = {}

    def _protect(match: re.Match[str]) -> str:
        token = f"CREWDAY_AUTOLINK_{len(protected)}"
        attempt = 0
        while token in body_md:
            attempt += 1
            token = f"CREWDAY_AUTOLINK_{len(protected)}_{attempt}"
        protected[token] = match.group(0)
        return token

    stripper = _MarkdownHTMLStripper()
    stripper.feed(_AUTOLINK_RE.sub(_protect, body_md))
    stripper.close()
    cleaned = unescape(stripper.text())
    for token, value in protected.items():
        cleaned = cleaned.replace(token, value)
    return cleaned.strip()


class ChatMessageService:
    """Workspace-scoped chat-message service."""

    def __init__(
        self,
        ctx: WorkspaceContext,
        *,
        storage: Storage | None = None,
        clock: Clock | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._ctx = ctx
        self._storage = storage
        self._clock = clock if clock is not None else SystemClock()
        self._event_bus = event_bus if event_bus is not None else default_event_bus

    def send(
        self,
        message_repo: ChatMessageRepository,
        channel_repo: ChatChannelRepository,
        channel_id: str,
        body_md: str,
        *,
        attachments: list[str] | tuple[str, ...] = (),
    ) -> ChatMessageView:
        if len(attachments) > _MAX_ATTACHMENTS:
            raise ChatMessageInvalid(
                f"messages support at most {_MAX_ATTACHMENTS} attachments"
            )
        sanitized = sanitize_markdown(body_md)
        if not sanitized and not attachments:
            raise ChatMessageInvalid("message body or attachment is required")
        if len(sanitized) > _MAX_BODY_LEN:
            raise ChatMessageInvalid(f"message body exceeds {_MAX_BODY_LEN} characters")

        channel_service = ChatChannelService(self._ctx, clock=self._clock)
        channel_service.assert_can_post_message(channel_repo, channel_id)
        channel = channel_service.get(channel_repo, channel_id, include_archived=False)

        if attachments and self._storage is None:
            raise ChatMessageInvalid("storage is required to send attachments")
        for blob_hash in attachments:
            if not blob_hash:
                raise ChatMessageInvalid("attachment blob_hash must be non-empty")
            if not _BLOB_HASH_RE.match(blob_hash):
                raise ChatMessageInvalid(
                    "attachment blob_hash must be a 64-character lowercase "
                    "sha256 hex digest"
                )
            assert self._storage is not None
            if not self._storage.exists(blob_hash):
                raise ChatMessageAttachmentMissing(blob_hash)

        now = self._clock.now()
        author_user_id = (
            self._ctx.actor_id if self._ctx.actor_kind != "system" else None
        )
        author_label = (
            message_repo.display_label_for_user(
                workspace_id=self._ctx.workspace_id,
                user_id=self._ctx.actor_id,
            )
            if author_user_id is not None
            else "system"
        )
        attachment_refs = [{"blob_hash": blob_hash} for blob_hash in attachments]
        row = message_repo.insert(
            message_id=new_ulid(),
            workspace_id=self._ctx.workspace_id,
            channel_id=channel_id,
            author_user_id=author_user_id,
            author_label=author_label,
            body_md=sanitized,
            attachments_json=attachment_refs,
            created_at=now,
        )
        view = _to_view(row)
        write_audit(
            message_repo.session,
            self._ctx,
            entity_kind="chat_message",
            entity_id=row.id,
            action="messaging.message.sent",
            diff={
                "channel_id": channel_id,
                "author_user_id": author_user_id,
                "attachments": [dict(item) for item in view.attachments],
            },
            clock=self._clock,
        )
        message_repo.session.flush()
        self._event_bus.publish(
            ChatMessageSent(
                workspace_id=self._ctx.workspace_id,
                actor_id=self._ctx.actor_id,
                correlation_id=self._ctx.audit_correlation_id,
                occurred_at=now,
                channel_id=channel_id,
                message_id=row.id,
                author_user_id=author_user_id,
                channel_kind=channel.kind,
            )
        )
        return view

    def list(
        self,
        message_repo: ChatMessageRepository,
        channel_repo: ChatChannelRepository,
        channel_id: str,
        *,
        before: ChatMessageCursor | None = None,
        limit: int = 50,
    ) -> list[ChatMessageView]:
        if limit < 1:
            raise ChatMessageInvalid("limit must be >= 1")
        ChatChannelService(self._ctx, clock=self._clock).get(
            channel_repo,
            channel_id,
            include_archived=False,
        )
        rows = message_repo.list_for_channel(
            workspace_id=self._ctx.workspace_id,
            channel_id=channel_id,
            before_created_at=before.created_at if before is not None else None,
            before_id=before.id if before is not None else None,
            limit=limit,
        )
        return [_to_view(row) for row in rows]


def _to_view(row: ChatMessageRow) -> ChatMessageView:
    return ChatMessageView(
        id=row.id,
        workspace_id=row.workspace_id,
        channel_id=row.channel_id,
        author_user_id=row.author_user_id,
        author_label=row.author_label,
        body_md=row.body_md,
        attachments=tuple(dict(item) for item in row.attachments_json),
        created_at=row.created_at,
    )
