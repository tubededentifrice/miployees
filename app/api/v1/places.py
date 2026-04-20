"""Places context router scaffold.

Owns properties, units, areas, and closures (spec §01 "Context
map", §12 "Properties / areas / stays"). Routes land in cd-75wp;
this file is the reserved seat under ``/w/<slug>/api/v1/places``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["places"])

__all__ = ["router"]
