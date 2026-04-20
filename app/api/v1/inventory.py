"""Inventory context router scaffold.

Owns items, movements, reorder rules, and consumption hooks
(spec §01 "Context map", §12 "Inventory"). Routes land in cd-t9ur;
this file is the reserved seat under ``/w/<slug>/api/v1/inventory``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["inventory"])

__all__ = ["router"]
