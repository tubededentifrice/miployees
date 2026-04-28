"""Inventory HTTP router."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.authz.dep import Permission
from app.services.inventory import item_service
from app.services.inventory.item_service import (
    InventoryItemCreate,
    InventoryItemUpdate,
    InventoryItemView,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "InventoryItemCreateRequest",
    "InventoryItemListResponse",
    "InventoryItemResponse",
    "InventoryItemUpdateRequest",
    "build_inventory_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]

_MAX_TEXT = 20_000
_MAX_SHORT = 500


class InventoryItemCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_SHORT)
    sku: str | None = Field(default=None, max_length=_MAX_SHORT)
    unit: str = Field(min_length=1, max_length=_MAX_SHORT)
    reorder_point: Decimal | None = None
    reorder_target: Decimal | None = None
    vendor: str | None = Field(default=None, max_length=_MAX_SHORT)
    vendor_url: str | None = Field(default=None, max_length=2_000)
    unit_cost_cents: int | None = Field(default=None, ge=0)
    barcode_ean13: str | None = Field(default=None, max_length=_MAX_SHORT)
    tags: list[str] = Field(default_factory=list)
    notes_md: str | None = Field(default=None, max_length=_MAX_TEXT)

    def to_service(self) -> InventoryItemCreate:
        return InventoryItemCreate(
            name=self.name,
            sku=self.sku,
            unit=self.unit,
            reorder_point=self.reorder_point,
            reorder_target=self.reorder_target,
            vendor=self.vendor,
            vendor_url=self.vendor_url,
            unit_cost_cents=self.unit_cost_cents,
            barcode_ean13=self.barcode_ean13,
            tags=tuple(self.tags),
            notes_md=self.notes_md,
        )


class InventoryItemUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=_MAX_SHORT)
    sku: str | None = Field(default=None, max_length=_MAX_SHORT)
    unit: str | None = Field(default=None, min_length=1, max_length=_MAX_SHORT)
    reorder_point: Decimal | None = None
    reorder_target: Decimal | None = None
    vendor: str | None = Field(default=None, max_length=_MAX_SHORT)
    vendor_url: str | None = Field(default=None, max_length=2_000)
    unit_cost_cents: int | None = Field(default=None, ge=0)
    barcode_ean13: str | None = Field(default=None, max_length=_MAX_SHORT)
    tags: list[str] = Field(default_factory=list)
    notes_md: str | None = Field(default=None, max_length=_MAX_TEXT)

    def to_service(self) -> InventoryItemUpdate:
        return InventoryItemUpdate(
            fields_set=frozenset(self.model_fields_set),
            name=self.name,
            sku=self.sku,
            unit=self.unit,
            reorder_point=self.reorder_point,
            reorder_target=self.reorder_target,
            vendor=self.vendor,
            vendor_url=self.vendor_url,
            unit_cost_cents=self.unit_cost_cents,
            barcode_ean13=self.barcode_ean13,
            tags=tuple(self.tags),
            notes_md=self.notes_md,
        )


class InventoryItemResponse(BaseModel):
    id: str
    workspace_id: str
    property_id: str
    name: str
    sku: str | None
    unit: str
    on_hand: Decimal
    reorder_point: Decimal | None
    reorder_target: Decimal | None
    vendor: str | None
    vendor_url: str | None
    unit_cost_cents: int | None
    barcode_ean13: str | None
    tags: list[str]
    notes_md: str | None
    created_at: str
    updated_at: str | None
    deleted_at: str | None

    @field_serializer("on_hand", "reorder_point", "reorder_target")
    def _decimal_as_number(self, value: Decimal | None) -> int | float | None:
        if value is None:
            return None
        if value == value.to_integral_value():
            return int(value)
        return float(value)

    @classmethod
    def from_view(cls, view: InventoryItemView) -> InventoryItemResponse:
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            property_id=view.property_id,
            name=view.name,
            sku=view.sku,
            unit=view.unit,
            on_hand=view.on_hand,
            reorder_point=view.reorder_point,
            reorder_target=view.reorder_target,
            vendor=view.vendor,
            vendor_url=view.vendor_url,
            unit_cost_cents=view.unit_cost_cents,
            barcode_ean13=view.barcode_ean13,
            tags=list(view.tags),
            notes_md=view.notes_md,
            created_at=view.created_at.isoformat(),
            updated_at=view.updated_at.isoformat() if view.updated_at else None,
            deleted_at=view.deleted_at.isoformat() if view.deleted_at else None,
        )


class InventoryItemListResponse(BaseModel):
    data: list[InventoryItemResponse]


def _http_for_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "inventory_item_not_found"},
    )


def _http_for_property_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "property_not_found"},
    )


def _http_for_conflict(exc: item_service.InventoryItemConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "inventory_item_conflict", "field": exc.field},
    )


def _http_for_validation(
    exc: item_service.InventoryItemValidationError,
) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": exc.error, "field": exc.field},
    )


def build_inventory_router() -> APIRouter:
    api = APIRouter(tags=["inventory"])
    view_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    edit_gate = Depends(Permission("scope.edit_settings", scope_kind="workspace"))

    @api.get(
        "/properties/{property_id}/items",
        dependencies=[view_gate],
    )
    def list_items(
        property_id: str,
        ctx: _Ctx,
        session: _Db,
        barcode: Annotated[str | None, Query(max_length=_MAX_SHORT)] = None,
    ) -> InventoryItemListResponse | InventoryItemResponse:
        try:
            if barcode is not None:
                return InventoryItemResponse.from_view(
                    item_service.get_by_barcode(
                        session,
                        ctx,
                        property_id=property_id,
                        barcode_ean13=barcode,
                    )
                )
            views = item_service.list(session, ctx, property_id=property_id)
        except item_service.InventoryPropertyNotFound as exc:
            raise _http_for_property_not_found() from exc
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc
        return InventoryItemListResponse(
            data=[InventoryItemResponse.from_view(view) for view in views]
        )

    @api.get(
        "/properties/{property_id}/items/by_sku/{sku}",
        dependencies=[view_gate],
    )
    def get_item_by_sku(
        property_id: str,
        sku: str,
        ctx: _Ctx,
        session: _Db,
    ) -> InventoryItemResponse:
        try:
            return InventoryItemResponse.from_view(
                item_service.get_by_sku(session, ctx, property_id=property_id, sku=sku)
            )
        except item_service.InventoryPropertyNotFound as exc:
            raise _http_for_property_not_found() from exc
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc

    @api.get(
        "/properties/{property_id}/items/by_barcode/{barcode_ean13}",
        dependencies=[view_gate],
    )
    def get_item_by_barcode(
        property_id: str,
        barcode_ean13: str,
        ctx: _Ctx,
        session: _Db,
    ) -> InventoryItemResponse:
        try:
            return InventoryItemResponse.from_view(
                item_service.get_by_barcode(
                    session,
                    ctx,
                    property_id=property_id,
                    barcode_ean13=barcode_ean13,
                )
            )
        except item_service.InventoryPropertyNotFound as exc:
            raise _http_for_property_not_found() from exc
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc

    @api.post(
        "/properties/{property_id}/items",
        status_code=status.HTTP_201_CREATED,
        dependencies=[edit_gate],
    )
    def create_item(
        property_id: str,
        body: InventoryItemCreateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> InventoryItemResponse:
        try:
            view = item_service.create(
                session,
                ctx,
                property_id=property_id,
                body=body.to_service(),
            )
        except item_service.InventoryPropertyNotFound as exc:
            raise _http_for_property_not_found() from exc
        except item_service.InventoryItemConflict as exc:
            raise _http_for_conflict(exc) from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc
        return InventoryItemResponse.from_view(view)

    @api.patch(
        "/properties/{property_id}/items/{item_id}",
        dependencies=[edit_gate],
    )
    def update_item(
        property_id: str,
        item_id: str,
        body: InventoryItemUpdateRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> InventoryItemResponse:
        try:
            view = item_service.update(
                session,
                ctx,
                property_id=property_id,
                item_id=item_id,
                body=body.to_service(),
            )
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemConflict as exc:
            raise _http_for_conflict(exc) from exc
        except item_service.InventoryItemValidationError as exc:
            raise _http_for_validation(exc) from exc
        return InventoryItemResponse.from_view(view)

    @api.delete(
        "/properties/{property_id}/items/{item_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[edit_gate],
    )
    def archive_item(
        property_id: str,
        item_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        try:
            item_service.archive(
                session,
                ctx,
                property_id=property_id,
                item_id=item_id,
            )
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post(
        "/properties/{property_id}/items/{item_id}/restore",
        dependencies=[edit_gate],
    )
    def restore_item(
        property_id: str,
        item_id: str,
        ctx: _Ctx,
        session: _Db,
    ) -> InventoryItemResponse:
        try:
            view = item_service.restore(
                session,
                ctx,
                property_id=property_id,
                item_id=item_id,
            )
        except item_service.InventoryItemNotFound as exc:
            raise _http_for_not_found() from exc
        except item_service.InventoryItemConflict as exc:
            raise _http_for_conflict(exc) from exc
        return InventoryItemResponse.from_view(view)

    return api


router = build_inventory_router()
