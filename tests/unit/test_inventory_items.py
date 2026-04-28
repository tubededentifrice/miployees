"""Unit tests for inventory item CRUD service."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.inventory.models import Item
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.services.inventory import item_service
from app.services.inventory.item_service import InventoryItemCreate, InventoryItemUpdate
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def engine_inventory_items() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine_inventory_items: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine_inventory_items, expire_on_commit=False)
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _seed_scope(session: Session, clock: FrozenClock) -> tuple[WorkspaceContext, str]:
    owner = bootstrap_user(
        session,
        email=f"owner-{new_ulid().lower()}@example.com",
        display_name="Owner",
        clock=clock,
    )
    ws = bootstrap_workspace(
        session,
        slug=f"inv-{new_ulid()[-8:].lower()}",
        name="Inventory",
        owner_user_id=owner.id,
        clock=clock,
    )
    with tenant_agnostic():
        prop = Property(
            id=new_ulid(clock=clock),
            address="1 Stock Way",
            timezone="UTC",
            tags_json=[],
            created_at=_PINNED,
        )
        session.add(prop)
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=prop.id,
                workspace_id=ws.id,
                label="Main",
                membership_role="owner_workspace",
                status="active",
                created_at=_PINNED,
            )
        )
        session.flush()

    ctx = build_workspace_context(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=owner.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
    )
    return ctx, prop.id


def test_create_rejects_active_sku_collision(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item_service.create(
        session,
        ctx,
        property_id=property_id,
        body=InventoryItemCreate(name="Soap", sku="SOAP", unit="bottle"),
        clock=clock,
    )

    with pytest.raises(item_service.InventoryItemConflict) as raised:
        item_service.create(
            session,
            ctx,
            property_id=property_id,
            body=InventoryItemCreate(name="Other soap", sku="SOAP", unit="case"),
            clock=clock,
        )

    assert raised.value.field == "sku"


def test_create_rejects_active_barcode_collision(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    item_service.create(
        session,
        ctx,
        property_id=property_id,
        body=InventoryItemCreate(
            name="Towels",
            sku="TOWELS",
            unit="set",
            barcode_ean13="1234567890123",
        ),
        clock=clock,
    )

    with pytest.raises(item_service.InventoryItemConflict) as raised:
        item_service.create(
            session,
            ctx,
            property_id=property_id,
            body=InventoryItemCreate(
                name="Bath towels",
                sku="BATH-TOWELS",
                unit="set",
                barcode_ean13="1234567890123",
            ),
            clock=clock,
        )

    assert raised.value.field == "barcode_ean13"


def test_archive_then_create_same_sku_is_allowed(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    first = item_service.create(
        session,
        ctx,
        property_id=property_id,
        body=InventoryItemCreate(name="Toilet paper", sku="TP", unit="roll"),
        clock=clock,
    )
    item_service.archive(
        session,
        ctx,
        property_id=property_id,
        item_id=first.id,
        clock=clock,
    )

    second = item_service.create(
        session,
        ctx,
        property_id=property_id,
        body=InventoryItemCreate(name="Toilet paper new", sku="TP", unit="roll"),
        clock=clock,
    )

    assert second.id != first.id
    active = item_service.list(session, ctx, property_id=property_id)
    assert [item.id for item in active] == [second.id]


def test_unit_is_free_text_per_current_spec(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)

    view = item_service.create(
        session,
        ctx,
        property_id=property_id,
        body=InventoryItemCreate(name="Coffee pods", sku="COFFEE", unit="sleeve/10"),
        clock=clock,
    )

    assert view.unit == "sleeve/10"


def test_reorder_quantities_reject_more_than_four_decimal_places(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)

    with pytest.raises(item_service.InventoryItemValidationError) as raised:
        item_service.create(
            session,
            ctx,
            property_id=property_id,
            body=InventoryItemCreate(
                name="Coffee pods",
                sku="COFFEE",
                unit="sleeve",
                reorder_point=Decimal("1.23456"),
            ),
            clock=clock,
        )

    assert raised.value.field == "reorder_point"
    assert raised.value.error == "quantity_precision"


def test_get_by_barcode_returns_only_active_rows(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    view = item_service.create(
        session,
        ctx,
        property_id=property_id,
        body=InventoryItemCreate(
            name="Window wash",
            sku="WINDOW",
            unit="L",
            barcode_ean13="3017620422003",
            reorder_point=Decimal("1.5"),
        ),
        clock=clock,
    )

    found = item_service.get_by_barcode(
        session,
        ctx,
        property_id=property_id,
        barcode_ean13="3017620422003",
    )
    assert found.id == view.id

    item_service.archive(
        session,
        ctx,
        property_id=property_id,
        item_id=view.id,
        clock=clock,
    )
    with pytest.raises(item_service.InventoryItemNotFound):
        item_service.get_by_barcode(
            session,
            ctx,
            property_id=property_id,
            barcode_ean13="3017620422003",
        )


def test_restore_detects_active_sku_collision(
    session: Session, clock: FrozenClock
) -> None:
    ctx, property_id = _seed_scope(session, clock)
    archived = item_service.create(
        session,
        ctx,
        property_id=property_id,
        body=InventoryItemCreate(name="Bleach", sku="BLEACH", unit="bottle"),
        clock=clock,
    )
    item_service.archive(
        session,
        ctx,
        property_id=property_id,
        item_id=archived.id,
        clock=clock,
    )
    item_service.create(
        session,
        ctx,
        property_id=property_id,
        body=InventoryItemCreate(name="Bleach v2", sku="BLEACH", unit="bottle"),
        clock=clock,
    )

    with pytest.raises(item_service.InventoryItemConflict):
        item_service.restore(
            session,
            ctx,
            property_id=property_id,
            item_id=archived.id,
            clock=clock,
        )


def test_update_writes_audit_row(session: Session, clock: FrozenClock) -> None:
    ctx, property_id = _seed_scope(session, clock)
    view = item_service.create(
        session,
        ctx,
        property_id=property_id,
        body=InventoryItemCreate(name="Soap", sku="SOAP", unit="bottle"),
        clock=clock,
    )

    item_service.update(
        session,
        ctx,
        property_id=property_id,
        item_id=view.id,
        body=InventoryItemUpdate(
            fields_set=frozenset({"name", "tags"}),
            name="Dish soap",
            tags=("cleaning", " kitchen "),
        ),
        clock=clock,
    )

    audit_actions = session.scalars(
        select(AuditLog.action).where(AuditLog.entity_id == view.id)
    ).all()
    assert audit_actions == ["inventory_item.created", "inventory_item.updated"]

    row = session.get(Item, view.id)
    assert row is not None
    assert row.tags_json == ["cleaning", "kitchen"]
