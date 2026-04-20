"""LLM context router scaffold.

Owns agents, approvals, preferences, model assignments, usage,
budgets, and outbound webhooks (spec §01 "Context map", §12
"LLM and approvals", §11). Routes land in cd-6bcl; this file is
the reserved seat under ``/w/<slug>/api/v1/llm``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["llm"])

__all__ = ["router"]
