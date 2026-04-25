// crewday — JSON API types: inventory items and movements.

export type InventoryMovementReason = "restock" | "consume" | "adjust" | "waste" | "transfer_in" | "transfer_out" | "audit_correction";

export interface InventoryMovement {
  id: string;
  item_id: string;
  delta: number;
  reason: InventoryMovementReason;
  // v1 collapses manager|employee|agent|system to user|agent|system (§02).
  actor_kind: "user" | "agent" | "system";
  actor_id: string;
  note: string | null;
  occurred_at: string;
}

export interface InventoryItem {
  id: string;
  property_id: string;
  name: string;
  sku: string;
  on_hand: number;
  par: number;
  unit: string;
  area: string;
}

// Per-template inventory hook — see §06 task templates / §08 inventory.
// `item_ref` is a soft string ref (sku or id, depending on caller); the
// task generator resolves it at materialise time.
export interface InventoryEffect {
  item_ref: string;
  kind: "consume" | "produce";
  qty: number;
}
