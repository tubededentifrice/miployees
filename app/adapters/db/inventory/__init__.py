"""inventory - item catalog, movement ledger, stocktakes, reorder rules.

All inventory tables in this package are workspace-scoped: each row
carries a ``workspace_id`` column and is registered in
:mod:`app.tenancy.registry` so the ORM tenant filter auto-injects a
``workspace_id`` predicate on every SELECT / UPDATE / DELETE. A
bare read without a :class:`~app.tenancy.WorkspaceContext` raises
:class:`~app.tenancy.orm_filter.TenantFilterMissing`.

``item_id`` FKs on ``inventory_movement`` and
``inventory_reorder_rule`` cascade on delete. ``source_task_id`` and
``source_stocktake_id`` on :class:`Movement` are nullable FKs to the
task row and stocktake session that caused a ledger entry.

Units are free text. Movement reasons use the final §08 taxonomy:
``restock | consume | produce | waste | theft | loss | found |
returned_to_vendor | transfer_in | transfer_out | audit_correction |
adjust``.

See ``docs/specs/02-domain-model.md`` §"inventory_item",
§"inventory_movement", and ``docs/specs/08-inventory.md``.
"""

from __future__ import annotations

from app.adapters.db.inventory.models import Item, Movement, ReorderRule, Stocktake
from app.tenancy.registry import register

for _table in (
    "inventory_item",
    "inventory_movement",
    "inventory_stocktake",
    "inventory_reorder_rule",
):
    register(_table)

__all__ = ["Item", "Movement", "ReorderRule", "Stocktake"]
