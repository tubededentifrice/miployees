"""HTTP tests for inbound chat gateway webhooks."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.chat_gateway._hmac import hmac_base64
from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.messaging.models import (
    ChatChannel,
    ChatGatewayBinding,
    ChatMessage,
)
from app.adapters.db.session import UnitOfWorkImpl, make_engine
from app.adapters.db.workspace.models import Workspace
from app.api.chat_gateway import ChatGatewayProviderConfig, build_chat_gateway_router
from app.api.deps import db_session
from app.events.bus import EventBus
from app.events.types import ChatMessageReceived
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def _load_all_models() -> None:
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
def api_engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def factory(api_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=api_engine, expire_on_commit=False, class_=Session)


def _workspace(s: Session) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=f"ws-{workspace_id[-6:].lower()}",
            name="Gateway Test",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _build_app(
    factory: sessionmaker[Session],
    *,
    workspace_id: str,
    secret: str,
    event_bus: EventBus,
) -> FastAPI:
    app = FastAPI()
    app.include_router(
        build_chat_gateway_router(
            providers=[
                ChatGatewayProviderConfig(
                    provider="twilio",
                    secret=secret,
                    workspace_id=workspace_id,
                )
            ],
            event_bus=event_bus,
        )
    )

    def _override_db() -> Iterator[Session]:
        uow = UnitOfWorkImpl(session_factory=factory)
        with uow as s:
            assert isinstance(s, Session)
            yield s

    app.dependency_overrides[db_session] = _override_db
    return app


def _twilio_body(message_sid: str = "SMaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") -> bytes:
    return urlencode(
        {
            "AccountSid": "AC123",
            "From": "+15551234567",
            "To": "+15557654321",
            "Body": "Need help with room 4",
            "MessageSid": message_sid,
        }
    ).encode("utf-8")


def _twilio_signature(raw: bytes, *, secret: str, url: str) -> str:
    params = dict(parse_qsl(raw.decode("utf-8"), keep_blank_values=True))
    signed = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    return hmac_base64(secret, signed.encode("utf-8"), digest="sha1")


def test_twilio_webhook_rejects_bad_signature_and_writes_no_row(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _workspace(s)
        s.commit()
    client = TestClient(
        _build_app(
            factory,
            workspace_id=workspace_id,
            secret="twilio-secret",
            event_bus=EventBus(),
        ),
        raise_server_exceptions=False,
    )

    resp = client.post(
        "/webhooks/chat/twilio",
        content=_twilio_body(),
        headers={"X-Twilio-Signature": "bad"},
    )

    assert resp.status_code == 401
    with factory() as s:
        assert s.scalars(select(ChatMessage)).all() == []
        assert s.scalars(select(ChatGatewayBinding)).all() == []


def test_twilio_webhook_auto_creates_channel_binding_message_and_is_idempotent(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        workspace_id = _workspace(s)
        s.commit()
    event_bus = EventBus()
    events: list[ChatMessageReceived] = []

    @event_bus.subscribe(ChatMessageReceived)
    def _append(event: ChatMessageReceived) -> None:
        events.append(event)

    client = TestClient(
        _build_app(
            factory,
            workspace_id=workspace_id,
            secret="twilio-secret",
            event_bus=event_bus,
        ),
        raise_server_exceptions=False,
    )
    raw = _twilio_body()
    signature = _twilio_signature(
        raw,
        secret="twilio-secret",
        url="http://testserver/webhooks/chat/twilio",
    )

    first = client.post(
        "/webhooks/chat/twilio",
        content=raw,
        headers={"X-Twilio-Signature": signature},
    )
    second = client.post(
        "/webhooks/chat/twilio",
        content=raw,
        headers={"X-Twilio-Signature": signature},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert second.json()["message_id"] == first.json()["message_id"]
    with factory() as s:
        channels = s.scalars(select(ChatChannel)).all()
        bindings = s.scalars(select(ChatGatewayBinding)).all()
        messages = s.scalars(select(ChatMessage)).all()
        assert len(channels) == 1
        assert channels[0].kind == "chat_gateway"
        assert channels[0].source == "sms"
        assert len(bindings) == 1
        assert bindings[0].external_contact == "+15551234567"
        assert len(messages) == 1
        assert messages[0].body_md == "Need help with room 4"
        assert messages[0].source == "twilio"
        assert messages[0].provider_message_id == "SMaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert (
            "chat_gateway.message.received" in s.scalars(select(AuditLog.action)).all()
        )
    assert [event.message_id for event in events] == [first.json()["message_id"]]
