"""Billing context router scaffold.

Owns organizations, rate cards, work orders, quotes, vendor
invoices, and the client portal surface (spec §01 "Context map",
§12 "Clients, work orders, invoices", §22). Routes land in
cd-eb14; this file is the reserved seat under
``/w/<slug>/api/v1/billing``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["billing"])

__all__ = ["router"]
