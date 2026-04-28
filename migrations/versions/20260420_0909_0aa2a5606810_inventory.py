"""inventory

Revision ID: 0aa2a5606810
Revises: 53e464485919
Create Date: 2026-04-20 09:09:00.000000

Creates the final §08 inventory schema directly: property-scoped
items with free-text units and ``numeric(14, 4)`` quantities, an
append-only movement ledger using the final reason taxonomy, a
property-wide stocktake session table, and reorder rules.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0aa2a5606810"
down_revision: str | Sequence[str] | None = "53e464485919"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REASON_VALUES: tuple[str, ...] = (
    "restock",
    "consume",
    "produce",
    "waste",
    "theft",
    "loss",
    "found",
    "returned_to_vendor",
    "transfer_in",
    "transfer_out",
    "audit_correction",
    "adjust",
)
_MOVEMENT_ACTOR_KIND_VALUES: tuple[str, ...] = ("user", "agent", "system")
_STOCKTAKE_ACTOR_KIND_VALUES: tuple[str, ...] = ("user", "agent")
_MOVEMENT_REASON_ENUM = sa.Enum(
    *_REASON_VALUES,
    name="inventory_movement_reason",
    native_enum=True,
    create_constraint=True,
)


def _in_clause(values: tuple[str, ...]) -> str:
    return "'" + "', '".join(values) + "'"


def _qty_column(name: str, *, nullable: bool) -> sa.Column[sa.Numeric]:
    return sa.Column(
        name,
        sa.Numeric(precision=14, scale=4, asdecimal=True),
        nullable=nullable,
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "inventory_item",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=True),
        sa.Column("sku", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("barcode", sa.String(), nullable=True),
        sa.Column("barcode_ean13", sa.String(), nullable=True),
        _qty_column("on_hand", nullable=False),
        _qty_column("reorder_point", nullable=True),
        _qty_column("reorder_target", nullable=True),
        sa.Column("vendor", sa.String(), nullable=True),
        sa.Column("vendor_url", sa.String(), nullable=True),
        sa.Column("unit_cost_cents", sa.Integer(), nullable=True),
        sa.Column("tags_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_inventory_item_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_inventory_item_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_item")),
    )
    op.create_index(
        "ix_inventory_item_workspace_property_deleted",
        "inventory_item",
        ["workspace_id", "property_id", "deleted_at"],
        unique=False,
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

    op.create_table(
        "inventory_stocktake",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor_kind", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("note_md", sa.String(), nullable=True),
        sa.CheckConstraint(
            f"actor_kind IN ({_in_clause(_STOCKTAKE_ACTOR_KIND_VALUES)})",
            name=op.f("ck_inventory_stocktake_actor_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["user.id"],
            name=op.f("fk_inventory_stocktake_actor_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["property.id"],
            name=op.f("fk_inventory_stocktake_property_id_property"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_inventory_stocktake_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_stocktake")),
    )
    op.create_index(
        "ix_inventory_stocktake_workspace_property_started",
        "inventory_stocktake",
        ["workspace_id", "property_id", sa.text("started_at DESC")],
        unique=False,
    )

    op.create_table(
        "inventory_movement",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        _qty_column("delta", nullable=False),
        sa.Column("reason", _MOVEMENT_REASON_ENUM, nullable=False),
        sa.Column("source_task_id", sa.String(), nullable=True),
        sa.Column("source_stocktake_id", sa.String(), nullable=True),
        sa.Column("actor_kind", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.CheckConstraint(
            f"actor_kind IN ({_in_clause(_MOVEMENT_ACTOR_KIND_VALUES)})",
            name=op.f("ck_inventory_movement_actor_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["user.id"],
            name=op.f("fk_inventory_movement_actor_id_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["inventory_item.id"],
            name=op.f("fk_inventory_movement_item_id_inventory_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_stocktake_id"],
            ["inventory_stocktake.id"],
            name=op.f("fk_inventory_movement_source_stocktake_id_inventory_stocktake"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_task_id"],
            ["occurrence.id"],
            name=op.f("fk_inventory_movement_source_task_id_occurrence"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_inventory_movement_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_movement")),
    )
    op.create_index(
        "ix_inventory_movement_workspace_item_at",
        "inventory_movement",
        ["workspace_id", "item_id", "at"],
        unique=False,
    )

    op.create_table(
        "inventory_reorder_rule",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        _qty_column("reorder_at", nullable=False),
        _qty_column("reorder_qty", nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "reorder_at >= 0",
            name=op.f("ck_inventory_reorder_rule_reorder_at_nonneg"),
        ),
        sa.CheckConstraint(
            "reorder_qty > 0",
            name=op.f("ck_inventory_reorder_rule_reorder_qty_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["inventory_item.id"],
            name=op.f("fk_inventory_reorder_rule_item_id_inventory_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_inventory_reorder_rule_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_reorder_rule")),
        sa.UniqueConstraint(
            "workspace_id",
            "item_id",
            name="uq_inventory_reorder_rule_workspace_item",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("inventory_reorder_rule")
    op.drop_index(
        "ix_inventory_movement_workspace_item_at",
        table_name="inventory_movement",
    )
    op.drop_table("inventory_movement")
    if op.get_bind().dialect.name == "postgresql":
        _MOVEMENT_REASON_ENUM.drop(op.get_bind(), checkfirst=True)
    op.drop_index(
        "ix_inventory_stocktake_workspace_property_started",
        table_name="inventory_stocktake",
    )
    op.drop_table("inventory_stocktake")
    op.drop_index(
        "uq_inventory_item_workspace_property_barcode_active",
        table_name="inventory_item",
    )
    op.drop_index(
        "uq_inventory_item_workspace_property_sku_active",
        table_name="inventory_item",
    )
    op.drop_index(
        "ix_inventory_item_workspace_property_deleted",
        table_name="inventory_item",
    )
    op.drop_table("inventory_item")
