"""tasks — templates, schedules, occurrences, checklist items, evidence, comments.

All seven tables in this package are workspace-scoped: each row
carries a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A bare
read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

Unlike the places package — where ``property`` intentionally stays
tenant-agnostic because a single villa may belong to several
workspaces — every tasks row is born inside exactly one workspace's
operations, so scoping is unambiguous.

The v1 slice lands the minimum columns needed by cd-chd's acceptance
criteria. Richer §02 / §06 columns (``paused_at``, ``active_from`` /
``active_until``, denormalised ``last_generated_for``, the tighter
state machine with ``scheduled`` / ``cancelled`` / ``overdue``,
``checklist_snapshot_json``, ``photo_evidence`` as an enum, etc.)
land with follow-up tasks without breaking this migration's public
write contract.

See ``docs/specs/02-domain-model.md`` §"task_template", §"schedule",
§"occurrence", §"checklist_item", §"evidence", §"comment", and
``docs/specs/06-tasks-and-scheduling.md``.
"""

from __future__ import annotations

from app.adapters.db.tasks.models import (
    ChecklistItem,
    ChecklistTemplateItem,
    Comment,
    Evidence,
    NlTaskPreview,
    Occurrence,
    Schedule,
    TaskTemplate,
)
from app.tenancy.registry import register

for _table in (
    "task_template",
    "checklist_template_item",
    "schedule",
    "nl_task_preview",
    "occurrence",
    "checklist_item",
    "evidence",
    "comment",
):
    register(_table)

__all__ = [
    "ChecklistItem",
    "ChecklistTemplateItem",
    "Comment",
    "Evidence",
    "NlTaskPreview",
    "Occurrence",
    "Schedule",
    "TaskTemplate",
]
