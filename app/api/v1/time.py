"""Time context router scaffold.

Owns shifts, leaves, and geofence settings (spec §01 "Context
map", §12 "Time, payroll, expenses"). Routes land in cd-gdit;
this file is the reserved seat under ``/w/<slug>/api/v1/time``.

Module name shadows the stdlib ``time`` module locally — this is
a relative-import-only context module under ``app.api.v1`` so no
import collision is possible.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["time"])

__all__ = ["router"]
