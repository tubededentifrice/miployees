"""Public surface of the places context.

Re-exports the property domain service so callers in other bounded
contexts (stays, tasks, API handlers) import from here rather than
reaching directly into :mod:`app.domain.places`. Keeps the domain
module free to restructure its internals.

See ``docs/specs/04-properties-and-stays.md`` §"Property" and
``docs/specs/01-architecture.md`` §"Contexts & boundaries".
"""

from __future__ import annotations

from app.domain.places.property_service import (
    AddressCountryMismatch,
    AddressPayload,
    PropertyCreate,
    PropertyKind,
    PropertyNotFound,
    PropertyUpdate,
    PropertyView,
    create_property,
    get_property,
    list_properties,
    soft_delete_property,
    update_property,
)

__all__ = [
    "AddressCountryMismatch",
    "AddressPayload",
    "PropertyCreate",
    "PropertyKind",
    "PropertyNotFound",
    "PropertyUpdate",
    "PropertyView",
    "create_property",
    "get_property",
    "list_properties",
    "soft_delete_property",
    "update_property",
]
