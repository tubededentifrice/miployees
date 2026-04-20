"""Expenses context router scaffold.

Owns expense claims, receipts, and approvals (spec §01 "Context
map", §12 "Time, payroll, expenses"). Routes land in cd-t6y2;
this file is the reserved seat under ``/w/<slug>/api/v1/expenses``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["expenses"])

__all__ = ["router"]
