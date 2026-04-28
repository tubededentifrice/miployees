"""inventory_items_cd_jkwr

Revision ID: a5c7e9b1d3f5
Revises: f4a6b8c0d2e4
Create Date: 2026-04-28 16:00:00.000000

Extends the early inventory slice for property-scoped item CRUD:

* adds ``property_id``, soft-delete timestamps, vendor/cost/note/tag
  fields, reorder target, and the spec-named ``barcode_ean13`` column;
* relaxes ``sku`` to nullable and removes the old unit CHECK because
  §08 now makes ``inventory_item.unit`` free text;
* replaces workspace-wide SKU uniqueness with active-row partial unique
  indexes scoped to ``(workspace_id, property_id)`` for both SKU and
  barcode.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5c7e9b1d3f5"
down_revision: str | Sequence[str] | None = "f4a6b8c0d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("inventory_item", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_inventory_item_unit"), type_="check")
        batch_op.drop_constraint("uq_inventory_item_workspace_sku", type_="unique")
        batch_op.alter_column("sku", existing_type=sa.String(), nullable=True)

        batch_op.add_column(sa.Column("property_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_inventory_item_property_id_property",
            "property",
            ["property_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.add_column(sa.Column("barcode_ean13", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "reorder_target", sa.Numeric(precision=18, scale=4), nullable=True
            )
        )
        batch_op.add_column(sa.Column("vendor", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("vendor_url", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("unit_cost_cents", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("tags_json", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(sa.Column("notes_md", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            "ix_inventory_item_workspace_property_deleted",
            ["workspace_id", "property_id", "deleted_at"],
            unique=False,
        )

    op.execute(
        "UPDATE inventory_item SET barcode_ean13 = barcode "
        "WHERE barcode_ean13 IS NULL AND barcode IS NOT NULL"
    )

    op.create_index(
        "uq_inventory_item_workspace_property_sku_active",
        "inventory_item",
        ["workspace_id", "property_id", "sku"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL AND sku IS NOT NULL"),
        postgresql_where=sa.text("deleted_at IS NULL AND sku IS NOT NULL"),
    )
    op.create_index(
        "uq_inventory_item_workspace_property_barcode_active",
        "inventory_item",
        ["workspace_id", "property_id", "barcode_ean13"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL AND barcode_ean13 IS NOT NULL"),
        postgresql_where=sa.text("deleted_at IS NULL AND barcode_ean13 IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    _normalize_skus_for_downgrade()
    _normalize_units_for_downgrade()
    op.drop_index(
        "uq_inventory_item_workspace_property_barcode_active",
        table_name="inventory_item",
    )
    op.drop_index(
        "uq_inventory_item_workspace_property_sku_active",
        table_name="inventory_item",
    )

    with op.batch_alter_table("inventory_item", schema=None) as batch_op:
        batch_op.drop_index("ix_inventory_item_workspace_property_deleted")
        batch_op.drop_column("deleted_at")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("notes_md")
        batch_op.drop_column("tags_json")
        batch_op.drop_column("unit_cost_cents")
        batch_op.drop_column("vendor_url")
        batch_op.drop_column("vendor")
        batch_op.drop_column("reorder_target")
        batch_op.drop_column("barcode_ean13")
        batch_op.drop_constraint(
            "fk_inventory_item_property_id_property", type_="foreignkey"
        )
        batch_op.drop_column("property_id")
        batch_op.alter_column("sku", existing_type=sa.String(), nullable=False)
        batch_op.create_check_constraint(
            "unit",
            "unit IN ('ea', 'l', 'kg', 'm', 'pkg', 'box', 'other')",
        )
        batch_op.create_unique_constraint(
            "uq_inventory_item_workspace_sku",
            ["workspace_id", "sku"],
        )


def _normalize_skus_for_downgrade() -> None:
    """Fit property-scoped active SKUs back into the legacy workspace key."""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, workspace_id, sku FROM inventory_item "
            "ORDER BY workspace_id, sku, id"
        )
    ).mappings()
    used_by_workspace: dict[str, set[str]] = {}
    for row in rows:
        item_id = str(row["id"])
        workspace_id = str(row["workspace_id"])
        original = row["sku"]
        sku = str(original) if original else f"legacy-{item_id}"
        used = used_by_workspace.setdefault(workspace_id, set())
        candidate = sku
        suffix = item_id[-8:]
        index = 2
        while candidate in used:
            candidate = f"{sku}-{suffix}"
            if candidate in used:
                candidate = f"{sku}-{suffix}-{index}"
                index += 1
        used.add(candidate)
        if candidate != original:
            bind.execute(
                sa.text("UPDATE inventory_item SET sku = :sku WHERE id = :id"),
                {"sku": candidate, "id": item_id},
            )


def _normalize_units_for_downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE inventory_item SET unit = 'other' "
            "WHERE unit NOT IN ('ea', 'l', 'kg', 'm', 'pkg', 'box', 'other')"
        )
    )
