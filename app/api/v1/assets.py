"""Assets context router scaffold.

Owns asset types, assets, asset actions, and asset documents
(spec §01 "Context map", §12 "Assets / documents"). Routes land
in cd-qm5b; this file is the reserved seat under
``/w/<slug>/api/v1/assets``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["assets"])

__all__ = ["router"]
