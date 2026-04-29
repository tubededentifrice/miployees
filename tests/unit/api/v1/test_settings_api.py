"""HTTP tests for the workspace settings surface."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.api.v1.settings import build_settings_router
from app.tenancy import WorkspaceContext
from tests.factories.identity import bootstrap_user, bootstrap_workspace
from tests.unit.api.v1.identity.conftest import build_client, ctx_for

pytest_plugins = ("tests.unit.api.v1.identity.conftest",)


def _seed(factory: sessionmaker[Session]) -> tuple[WorkspaceContext, str]:
    with factory() as session:
        owner = bootstrap_user(
            session,
            email="settings-owner@example.com",
            display_name="Settings Owner",
        )
        workspace = bootstrap_workspace(
            session,
            slug="settings",
            name="Settings House",
            owner_user_id=owner.id,
        )
        workspace.default_timezone = "Europe/Paris"
        workspace.default_locale = "fr-FR"
        workspace.default_currency = "EUR"
        workspace.settings_json = {
            "evidence.policy": "require",
            "workspace.default_country": "FR",
        }
        session.commit()
        ctx = ctx_for(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            actor_id=owner.id,
            grant_role="manager",
            actor_was_owner_member=True,
        )
        return ctx, workspace.id


def _client(ctx: WorkspaceContext, factory: sessionmaker[Session]) -> TestClient:
    return build_client([("", build_settings_router())], factory, ctx)


def test_settings_read_merges_workspace_values_with_catalog_defaults(
    factory: sessionmaker[Session],
) -> None:
    ctx, _workspace_id = _seed(factory)
    client = _client(ctx, factory)

    response = client.get("/settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"] == {
        "name": "Settings House",
        "timezone": "Europe/Paris",
        "currency": "EUR",
        "country": "FR",
        "default_locale": "fr-FR",
    }
    assert body["defaults"]["evidence.policy"] == "require"
    assert body["defaults"]["tasks.checklist_required"] is False
    assert body["policy"]["approvals"]["always_gated"]


def test_settings_patch_updates_known_keys_and_audits(
    factory: sessionmaker[Session],
) -> None:
    ctx, workspace_id = _seed(factory)
    client = _client(ctx, factory)

    response = client.patch(
        "/settings",
        json={
            "evidence.policy": "forbid",
            "tasks.checklist_required": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["defaults"]["evidence.policy"] == "forbid"
    assert body["defaults"]["tasks.checklist_required"] is True

    with factory() as session:
        audit = session.query(AuditLog).filter_by(
            workspace_id=workspace_id,
            action="workspace.settings_updated",
        ).one()
        assert audit.diff["after"] == {
            "evidence.policy": "forbid",
            "tasks.checklist_required": True,
        }


def test_settings_patch_rejects_unknown_keys(factory: sessionmaker[Session]) -> None:
    ctx, _workspace_id = _seed(factory)
    client = _client(ctx, factory)

    response = client.patch("/settings", json={"unknown.key": True})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unknown_setting"
