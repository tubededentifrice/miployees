"""Instructions context router scaffold.

Owns KB CRUD, revisions, and scope resolution (spec §01 "Context
map", §12 "Instructions"). Routes land in cd-xkfe; this file is
the reserved seat under ``/w/<slug>/api/v1/instructions``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["instructions"])

__all__ = ["router"]
