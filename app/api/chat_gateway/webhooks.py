"""Inbound chat gateway webhook routes."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.chat_gateway import UnsupportedProvider, get_adapter
from app.adapters.db.messaging.repositories import SqlAlchemyChatGatewayRepository
from app.api.deps import db_session
from app.config import Settings, get_settings
from app.domain.messaging.gateway import ChatGatewayService
from app.events.bus import EventBus
from app.events.bus import bus as default_event_bus
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid

__all__ = [
    "ChatGatewayProviderConfig",
    "ChatGatewayResponse",
    "build_chat_gateway_router",
    "router",
]


_Db = Annotated[Session, Depends(db_session)]


@dataclass(frozen=True, slots=True)
class ChatGatewayProviderConfig:
    """Deployment config for one inbound chat provider."""

    provider: str
    secret: str
    workspace_id: str
    workspace_slug: str = "webhooks"
    actor_id: str = "system:chat_gateway"


class ChatGatewayResponse(BaseModel):
    """Fast-ack response for provider webhook deliveries."""

    ok: bool
    message_id: str
    binding_id: str
    channel_id: str
    duplicate: bool


def build_chat_gateway_router(
    *,
    providers: Iterable[ChatGatewayProviderConfig] | None = None,
    event_bus: EventBus | None = None,
    settings: Settings | None = None,
) -> APIRouter:
    """Build the deployment-scoped inbound webhook router."""

    config_by_provider = _provider_config_map(providers, settings=settings)
    bus = event_bus if event_bus is not None else default_event_bus
    r = APIRouter(prefix="/webhooks/chat", tags=["chat_gateway"])

    @r.post(
        "/{provider}",
        response_model=ChatGatewayResponse,
        operation_id="chat_gateway.webhook.receive",
        summary="Receive a signed inbound chat provider webhook",
    )
    async def receive_webhook(
        provider: str,
        request: Request,
        session: _Db,
    ) -> ChatGatewayResponse:
        try:
            adapter = get_adapter(provider)
        except UnsupportedProvider as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "chat_gateway_provider_not_found"},
            ) from exc
        config = config_by_provider.get(adapter.provider)
        if config is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "chat_gateway_provider_not_configured"},
            )
        raw_body = await request.body()
        if not adapter.verify(
            request.headers,
            raw_body,
            config.secret,
            url=str(request.url),
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "chat_gateway_signature_invalid"},
            )
        try:
            inbound = adapter.normalize(request.headers, raw_body)
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "chat_gateway_payload_invalid", "message": str(exc)},
            ) from exc

        ctx = WorkspaceContext(
            workspace_id=config.workspace_id,
            workspace_slug=config.workspace_slug,
            actor_id=config.actor_id,
            actor_kind="system",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id=request.headers.get("X-Request-Id", new_ulid()),
            principal_kind="system",
        )
        result = ChatGatewayService(ctx, event_bus=bus).receive(
            SqlAlchemyChatGatewayRepository(session),
            inbound,
            channel_source=adapter.channel_source,
        )
        return ChatGatewayResponse(
            ok=True,
            message_id=result.message_id,
            binding_id=result.binding_id,
            channel_id=result.channel_id,
            duplicate=result.duplicate,
        )

    return r


def _provider_config_map(
    providers: Iterable[ChatGatewayProviderConfig] | None,
    *,
    settings: Settings | None,
) -> dict[str, ChatGatewayProviderConfig]:
    source = providers if providers is not None else _providers_from_settings(settings)
    configs: dict[str, ChatGatewayProviderConfig] = {}
    for config in source:
        if not config.secret.strip() or not config.workspace_id.strip():
            continue
        try:
            adapter = get_adapter(config.provider)
        except UnsupportedProvider:
            continue
        configs[adapter.provider] = ChatGatewayProviderConfig(
            provider=adapter.provider,
            secret=config.secret,
            workspace_id=config.workspace_id,
            workspace_slug=config.workspace_slug,
            actor_id=config.actor_id,
        )
    return configs


def _providers_from_settings(
    settings: Settings | None,
) -> Iterator[ChatGatewayProviderConfig]:
    cfg = settings if settings is not None else get_settings()
    workspace_id = cfg.chat_gateway_workspace_id
    if workspace_id is None:
        return
    for provider, secret in (
        ("twilio", cfg.chat_gateway_twilio_secret),
        ("meta_whatsapp", cfg.chat_gateway_meta_whatsapp_secret),
        ("postmark", cfg.chat_gateway_postmark_secret),
    ):
        if secret is None:
            continue
        yield ChatGatewayProviderConfig(
            provider=provider,
            secret=secret.get_secret_value(),
            workspace_id=workspace_id,
        )


router: APIRouter = build_chat_gateway_router(providers=())
