"""Payroll context router scaffold.

Owns pay rules, periods, payslips, and CSV exports (spec §01
"Context map", §12 "Time, payroll, expenses"). Routes land in
cd-jsci; this file is the reserved seat under
``/w/<slug>/api/v1/payroll``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["payroll"])

__all__ = ["router"]
