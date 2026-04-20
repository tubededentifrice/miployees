"""Messaging context router scaffold.

Owns notifications, push tokens, digests, chat channels, and
chat messages (spec §01 "Context map", §12 "Messaging", §23).
Routes land in cd-ykiq; this file is the reserved seat under
``/w/<slug>/api/v1/messaging``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["messaging"])

__all__ = ["router"]
