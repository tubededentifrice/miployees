"""Tasks context router scaffold.

Owns task templates, schedules, occurrences, completion, evidence,
comments, and approvals (spec §01 "Context map", §12 "Tasks /
templates / schedules"). Routes land in cd-sn26; this file is the
reserved seat under ``/w/<slug>/api/v1/tasks``.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["tasks"])

__all__ = ["router"]
