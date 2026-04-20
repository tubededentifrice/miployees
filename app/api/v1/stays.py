"""Stays context router scaffold.

Owns iCal feeds, reservations, stay task bundles, and guest
welcome tokens (spec §01 "Context map", §12 "Properties / areas /
stays"). Routes land in cd-0510; this file is the reserved seat
under ``/w/<slug>/api/v1/stays``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["stays"])

__all__ = ["router"]
