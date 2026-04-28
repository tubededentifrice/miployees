"""Integration tests for inventory item API routes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.api.deps import current_workspace_context
from app.api.deps import db_session as _db_session_dep
from app.api.v1.inventory import build_inventory_router
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def seeded(db_session: Session) -> tuple[WorkspaceContext, str]:
    tag = new_ulid()[-8:].lower()
    owner = bootstrap_user(
        db_session,
        email=f"owner-{tag}@example.com",
        display_name="Owner",
    )
    ws = bootstrap_workspace(
        db_session,
        slug=f"inv-{tag}",
        name="Inventory",
        owner_user_id=owner.id,
    )
    with tenant_agnostic():
        prop = Property(
            id=new_ulid(),
            address="1 Stock Way",
            timezone="UTC",
            tags_json=[],
            created_at=_PINNED,
        )
        db_session.add(prop)
        db_session.flush()
        db_session.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=ws.id,
                label="Main",
                membership_role="owner_workspace",
                status="active",
                created_at=_PINNED,
            )
        )
        db_session.flush()

    ctx = build_workspace_context(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    return ctx, prop.id


@pytest.fixture
def client(
    db_session: Session, seeded: tuple[WorkspaceContext, str]
) -> Iterator[TestClient]:
    ctx, _ = seeded
    app = FastAPI()
    app.include_router(build_inventory_router(), prefix="/api/v1/inventory")

    def _session() -> Iterator[Session]:
        yield db_session

    def _ctx() -> WorkspaceContext:
        return ctx

    app.dependency_overrides[_db_session_dep] = _session
    app.dependency_overrides[current_workspace_context] = _ctx

    with TestClient(app) as c:
        yield c


def _create(
    client: TestClient,
    property_id: str,
    *,
    name: str = "Soap",
    sku: str = "SOAP",
    barcode_ean13: str | None = None,
    unit: str = "bottle",
) -> dict[str, object]:
    payload: dict[str, object] = {"name": name, "sku": sku, "unit": unit}
    if barcode_ean13 is not None:
        payload["barcode_ean13"] = barcode_ean13
    response = client.post(
        f"/api/v1/inventory/properties/{property_id}/items", json=payload
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_create_duplicate_sku_returns_409(
    client: TestClient, seeded: tuple[WorkspaceContext, str]
) -> None:
    _, property_id = seeded
    _create(client, property_id, sku="SOAP")

    response = client.post(
        f"/api/v1/inventory/properties/{property_id}/items",
        json={"name": "Other soap", "sku": "SOAP", "unit": "case"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "error": "inventory_item_conflict",
        "field": "sku",
    }


def test_create_duplicate_barcode_returns_409(
    client: TestClient, seeded: tuple[WorkspaceContext, str]
) -> None:
    _, property_id = seeded
    _create(client, property_id, sku="SOAP", barcode_ean13="1234567890123")

    response = client.post(
        f"/api/v1/inventory/properties/{property_id}/items",
        json={
            "name": "Other soap",
            "sku": "SOAP-2",
            "unit": "case",
            "barcode_ean13": "1234567890123",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["field"] == "barcode_ean13"


def test_get_with_barcode_returns_single_active_item_or_404(
    client: TestClient, seeded: tuple[WorkspaceContext, str]
) -> None:
    _, property_id = seeded
    created = _create(
        client,
        property_id,
        name="Window wash",
        sku="WINDOW",
        barcode_ean13="3017620422003",
        unit="L/custom",
    )

    response = client.get(
        f"/api/v1/inventory/properties/{property_id}/items",
        params={"barcode": "3017620422003"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == created["id"]

    delete_response = client.delete(
        f"/api/v1/inventory/properties/{property_id}/items/{created['id']}"
    )
    assert delete_response.status_code == 204, delete_response.text

    response = client.get(
        f"/api/v1/inventory/properties/{property_id}/items",
        params={"barcode": "3017620422003"},
    )
    assert response.status_code == 404, response.text


def test_soft_delete_then_create_same_sku_allowed(
    client: TestClient, seeded: tuple[WorkspaceContext, str]
) -> None:
    _, property_id = seeded
    first = _create(client, property_id, sku="TP", unit="roll")

    for _ in range(2):
        response = client.delete(
            f"/api/v1/inventory/properties/{property_id}/items/{first['id']}"
        )
        assert response.status_code == 204, response.text

    second = _create(client, property_id, name="TP new", sku="TP", unit="roll")
    assert second["id"] != first["id"]

    response = client.get(f"/api/v1/inventory/properties/{property_id}/items")
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["data"]] == [second["id"]]


def test_patch_unknown_field_is_422(
    client: TestClient, seeded: tuple[WorkspaceContext, str]
) -> None:
    _, property_id = seeded
    created = _create(client, property_id)

    response = client.patch(
        f"/api/v1/inventory/properties/{property_id}/items/{created['id']}",
        json={"category": "cleaning"},
    )

    assert response.status_code == 422, response.text


def test_blank_required_field_is_422(
    client: TestClient, seeded: tuple[WorkspaceContext, str]
) -> None:
    _, property_id = seeded

    response = client.post(
        f"/api/v1/inventory/properties/{property_id}/items",
        json={"name": "   ", "sku": "BLANK", "unit": "each"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {"error": "blank", "field": "name"}


def test_quantity_precision_error_is_422(
    client: TestClient, seeded: tuple[WorkspaceContext, str]
) -> None:
    _, property_id = seeded

    response = client.post(
        f"/api/v1/inventory/properties/{property_id}/items",
        json={
            "name": "Coffee pods",
            "sku": "COFFEE",
            "unit": "sleeve",
            "reorder_point": 1.23456,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "error": "quantity_precision",
        "field": "reorder_point",
    }


def test_unit_free_text_is_accepted_and_mutations_audit(
    client: TestClient,
    seeded: tuple[WorkspaceContext, str],
    db_session: Session,
) -> None:
    _, property_id = seeded
    created = _create(client, property_id, sku="COFFEE", unit="sleeve/10")

    response = client.patch(
        f"/api/v1/inventory/properties/{property_id}/items/{created['id']}",
        json={"unit": "operator carton", "tags": ["kitchen"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["unit"] == "operator carton"
    actions = db_session.scalars(
        select(AuditLog.action).where(AuditLog.entity_id == created["id"])
    ).all()
    assert actions == ["inventory_item.created", "inventory_item.updated"]
