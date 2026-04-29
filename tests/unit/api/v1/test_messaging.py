"""Focused unit checks for the messaging router surface."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.messaging import build_messaging_router


def test_messaging_router_declares_notifications_and_push_management_routes() -> None:
    operations = {
        route.operation_id
        for route in build_messaging_router().routes
        if hasattr(route, "operation_id")
    }

    assert {
        "messaging.notifications.list",
        "messaging.notifications.get",
        "messaging.notifications.update",
        "messaging.notifications.mark_read",
        "messaging.push_tokens.list",
        "messaging.push_tokens.register_native_unavailable",
        "messaging.push_tokens.delete",
        "messaging.register_push_subscription",
        "messaging.unregister_push_subscription",
    }.issubset(operations)


def test_native_push_registration_requires_workspace_context() -> None:
    app = FastAPI()
    app.include_router(build_messaging_router())
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/notifications/push/tokens",
        json={"platform": "ios", "token": "native-token"},
    )

    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "not_authenticated"
