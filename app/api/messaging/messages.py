"""Chat-message HTTP router — ``/chat/channels/{id}/messages``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.db.messaging.repositories import (
    SqlAlchemyChatChannelRepository,
    SqlAlchemyChatMessageRepository,
)
from app.adapters.storage.ports import Storage
from app.api.deps import current_workspace_context, db_session
from app.api.pagination import DEFAULT_LIMIT, LimitQuery, decode_cursor, paginate
from app.domain.messaging.channels import (
    ChatChannelInvalid,
    ChatChannelNotFound,
    ChatChannelPermissionDenied,
)
from app.domain.messaging.messages import (
    ChatMessageAttachmentMissing,
    ChatMessageCursor,
    ChatMessageInvalid,
    ChatMessageService,
    ChatMessageView,
)
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext

__all__ = [
    "ChatMessageListResponse",
    "ChatMessageResponse",
    "ChatMessageSendRequest",
    "build_messages_router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_BlobHash = Annotated[
    str,
    Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    ),
]


class ChatMessageAttachmentResponse(BaseModel):
    blob_hash: str


class ChatMessageResponse(BaseModel):
    id: str
    workspace_id: str
    channel_id: str
    author_user_id: str | None
    author_label: str
    body_md: str
    attachments: list[ChatMessageAttachmentResponse]
    created_at: datetime

    @classmethod
    def from_view(cls, view: ChatMessageView) -> ChatMessageResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            channel_id=view.channel_id,
            author_user_id=view.author_user_id,
            author_label=view.author_label,
            body_md=view.body_md,
            attachments=[
                ChatMessageAttachmentResponse(blob_hash=item["blob_hash"])
                for item in view.attachments
            ],
            created_at=view.created_at,
        )


class ChatMessageListResponse(BaseModel):
    data: list[ChatMessageResponse]
    next_cursor: str | None = None
    has_more: bool = False


class ChatMessageSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body_md: str = Field(default="", max_length=20_000)
    attachments: list[_BlobHash] = Field(default_factory=list, max_length=10)


def _encode_cursor(view: ChatMessageView) -> str:
    payload = {"created_at": view.created_at.isoformat(), "id": view.id}
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_cursor(value: str | None) -> ChatMessageCursor | None:
    if value is None:
        return None
    try:
        raw = decode_cursor(value)
        payload = json.loads(raw) if raw is not None else None
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        created_at_raw = payload.get("created_at")
        message_id = payload.get("id")
        if not isinstance(created_at_raw, str) or not isinstance(message_id, str):
            raise ValueError("cursor is missing created_at or id")
        created_at = datetime.fromisoformat(created_at_raw)
        if created_at.tzinfo is None or created_at.utcoffset() != timedelta(0):
            raise ValueError("cursor created_at must be timezone-aware UTC")
        return ChatMessageCursor(created_at=created_at.astimezone(UTC), id=message_id)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_cursor"},
        ) from exc


def _storage_for_send(request: Request, attachments: list[str]) -> Storage | None:
    if not attachments:
        return None
    storage: Storage | None = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "storage_unavailable"},
        )
    return storage


def _http_for_message_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ChatChannelNotFound):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "chat_channel_not_found"},
        )
    if isinstance(exc, ChatMessageAttachmentMissing):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "attachment_not_found",
                "blob_hash": exc.blob_hash,
            },
        )
    if isinstance(exc, (ChatChannelPermissionDenied,)):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "permission_denied", "message": str(exc)},
        )
    if isinstance(exc, (ChatChannelInvalid, ChatMessageInvalid)):
        return HTTPException(
            status_code=422,
            detail={"error": "chat_message_invalid", "message": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def build_messages_router(*, event_bus: EventBus | None = None) -> APIRouter:
    router = APIRouter(
        prefix="/chat/channels/{channel_id}/messages",
        tags=["messaging", "chat"],
    )

    @router.post(
        "",
        response_model=ChatMessageResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="messaging.chat_messages.send",
        summary="Send a chat message",
    )
    def send_message(
        channel_id: str,
        body: ChatMessageSendRequest,
        request: Request,
        ctx: _Ctx,
        session: _Db,
    ) -> ChatMessageResponse:
        service = ChatMessageService(
            ctx,
            storage=_storage_for_send(request, body.attachments),
            event_bus=event_bus,
        )
        try:
            view = service.send(
                SqlAlchemyChatMessageRepository(session),
                SqlAlchemyChatChannelRepository(session),
                channel_id,
                body.body_md,
                attachments=body.attachments,
            )
        except (
            ChatChannelInvalid,
            ChatChannelNotFound,
            ChatChannelPermissionDenied,
            ChatMessageAttachmentMissing,
            ChatMessageInvalid,
        ) as exc:
            raise _http_for_message_error(exc) from exc
        return ChatMessageResponse.from_view(view)

    @router.get(
        "",
        response_model=ChatMessageListResponse,
        operation_id="messaging.chat_messages.list",
        summary="List chat messages in a channel",
    )
    def list_messages(
        channel_id: str,
        ctx: _Ctx,
        session: _Db,
        before: str | None = None,
        limit: LimitQuery = DEFAULT_LIMIT,
    ) -> ChatMessageListResponse:
        service = ChatMessageService(ctx, event_bus=event_bus)
        try:
            views = service.list(
                SqlAlchemyChatMessageRepository(session),
                SqlAlchemyChatChannelRepository(session),
                channel_id,
                before=_decode_cursor(before),
                limit=limit + 1,
            )
        except (
            ChatChannelInvalid,
            ChatChannelNotFound,
            ChatChannelPermissionDenied,
            ChatMessageInvalid,
        ) as exc:
            raise _http_for_message_error(exc) from exc
        page = paginate(views, limit=limit, key_getter=_encode_cursor)
        return ChatMessageListResponse(
            data=[ChatMessageResponse.from_view(view) for view in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    return router
