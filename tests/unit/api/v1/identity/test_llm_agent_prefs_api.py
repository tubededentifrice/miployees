"""HTTP round-trip tests for LLM agent preference routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.llm.models import BudgetLedger
from app.api.v1.llm import router as llm_router
from app.tenancy import WorkspaceContext
from app.util.ulid import new_ulid
from tests.unit.api.v1.identity.conftest import build_client


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", llm_router)], factory, ctx)


def test_visible_llm_routes_have_cli_and_agent_annotations() -> None:
    app = FastAPI()
    app.include_router(llm_router)

    operations = {
        (method.upper(), path): operation
        for path, methods in app.openapi()["paths"].items()
        for method, operation in methods.items()
    }

    visible_routes = {
        ("GET", "/agent_preferences/workspace"),
        ("PUT", "/agent_preferences/workspace"),
        ("GET", "/agent_preferences/me"),
        ("PUT", "/agent_preferences/me"),
        ("GET", "/me/agent_approval_mode"),
        ("PUT", "/me/agent_approval_mode"),
        ("GET", "/workspace/usage"),
    }
    for key in visible_routes:
        assert operations[key]["x-cli"]["summary"]

    mutating_routes = {
        ("PUT", "/agent_preferences/workspace"),
        ("PUT", "/agent_preferences/me"),
        ("PUT", "/me/agent_approval_mode"),
    }
    for key in mutating_routes:
        assert operations[key]["x-cli"]["mutates"] is True
        assert "x-agent-confirm" in operations[key]


def test_workspace_agent_preferences_round_trip_via_api(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/workspace/agent_prefs",
        json={
            "body_md": "Keep owner replies formal.",
            "blocked_actions": ["tasks.cancel"],
            "default_approval_mode": "strict",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_kind"] == "workspace"
    assert body["scope_id"] == workspace_id
    assert body["body_md"] == "Keep owner replies formal."
    assert body["blocked_actions"] == ["tasks.cancel"]
    assert body["default_approval_mode"] == "strict"

    readback = client.get("/workspace/agent_prefs")
    assert readback.status_code == 200
    assert readback.json() == body


def test_workspace_agent_preferences_round_trip_via_spec_path(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/agent_preferences/workspace",
        json={
            "body_md": "Prefer terse task summaries.",
            "blocked_actions": ["payroll.issue"],
            "default_approval_mode": "auto",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_kind"] == "workspace"
    assert body["scope_id"] == workspace_id
    assert body["body_md"] == "Prefer terse task summaries."

    readback = client.get("/agent_preferences/workspace")
    assert readback.status_code == 200
    assert readback.json() == body


def test_self_agent_preferences_round_trip_via_api(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/users/me/agent_prefs",
        json={"body_md": "One paragraph maximum."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_kind"] == "user"
    assert body["scope_id"] == ctx.actor_id
    assert body["body_md"] == "One paragraph maximum."

    readback = client.get("/users/me/agent_prefs")
    assert readback.status_code == 200
    assert readback.json() == body


def test_self_agent_preferences_round_trip_via_spec_path(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/agent_preferences/me",
        json={"body_md": "Ask before moving calendar events."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope_kind"] == "user"
    assert body["scope_id"] == ctx.actor_id
    assert body["body_md"] == "Ask before moving calendar events."

    readback = client.get("/agent_preferences/me")
    assert readback.status_code == 200
    assert readback.json() == body


def test_agent_preferences_reject_secret_like_body(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    response = client.put(
        "/workspace/agent_prefs",
        json={"body_md": "wifi password: swordfish"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "preference_contains_secret"


def test_my_agent_approval_mode_round_trip(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, _workspace_id = owner_ctx
    client = _client(ctx, factory)

    initial = client.get("/me/agent_approval_mode")
    assert initial.status_code == 200
    assert initial.json() == {"mode": "strict"}

    response = client.put("/me/agent_approval_mode", json={"mode": "auto"})
    assert response.status_code == 200, response.text
    assert response.json() == {"mode": "auto"}

    readback = client.get("/me/agent_approval_mode")
    assert readback.status_code == 200
    assert readback.json() == {"mode": "auto"}


def test_workspace_usage_reads_budget_ledger(
    owner_ctx: tuple[WorkspaceContext, sessionmaker[Session], str],
) -> None:
    ctx, factory, workspace_id = owner_ctx
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    with factory() as s:
        s.add(
            BudgetLedger(
                id=new_ulid(),
                workspace_id=workspace_id,
                period_start=now - timedelta(days=30),
                period_end=now,
                spent_cents=320,
                cap_cents=1000,
                updated_at=now,
            )
        )
        s.commit()
    client = _client(ctx, factory)

    response = client.get("/workspace/usage")

    assert response.status_code == 200
    assert response.json() == {
        "percent": 32,
        "paused": False,
        "window_label": "Rolling 30 days",
    }
