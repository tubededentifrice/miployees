# 08 — Inventory

Inventory is a lightweight SKU catalog **per property** with an
append-only ledger of movements. It is not an accounting system; it is
a practical way to know when the toilet paper is about to run out at
Apt 3B.

## Model

### `inventory_item`

| field                  | type    | notes                              |
|------------------------|---------|------------------------------------|
| id                     | ULID PK |                                    |
| property_id            | ULID FK |                                    |
| name                   | text    | "Toilet paper (2-ply)"             |
| sku                    | text?   | free-form                          |
| unit                   | text    | `each`, `pack`, `kg`, `liter`, `roll` |
| on_hand                | decimal | cached; recomputed from movements  |
| reorder_point          | decimal | trigger level                      |
| reorder_target         | decimal | quantity to bring us to            |
| vendor                 | text?   | default supplier                   |
| vendor_url             | text?   | e.g. Amazon ASIN URL               |
| unit_cost_cents        | int?    | last known unit cost               |
| barcode_ean13          | text?   | for scanner UI                     |
| tags                   | text[]  | `cleaning`, `guest-amenity`, ...   |
| notes_md               | text?   |                                    |
| deleted_at             | tstz?   |                                    |

### `inventory_movement`

Append-only ledger.

| field                  | type    | notes                              |
|------------------------|---------|------------------------------------|
| id                     | ULID PK |                                    |
| item_id                | ULID FK |                                    |
| at                     | tstz    |                                    |
| delta                  | decimal | positive = restock, negative = consume / loss |
| reason                 | enum    | `restock | consume | adjust | waste | transfer_in | transfer_out | audit_correction` |
| source_task_id         | ULID FK?| task that caused consumption       |
| actor_kind / actor_id  |         |                                    |
| note                   | text?   |                                    |

### `inventory_snapshot` (optional)

A rolled-up snapshot of `on_hand` by `(item_id, day)`, produced by a
daily worker job. Gives history graphs without scanning the ledger.

## Consumption on task completion

Task templates carry `inventory_consumption_json`, a map of
`item_id → qty` (or `sku → qty` during authoring). On task completion:

- If `inventory.consume_on_task` capability is on for the completing
  employee, insert one `inventory_movement` with `reason = consume`,
  `source_task_id` set, and negative delta per entry.
- If consumption would take `on_hand` below 0: the movement **still
  applies** (the item ends the transaction with a negative `on_hand`).
  The item is flagged in both the manager's daily digest and an
  `inventory.stock_drift` event. Rationale: staff know more than the
  model; counts can be wrong, and blocking task completion on a
  bookkeeping disagreement punishes the wrong person. Managers
  reconcile by recording a restock or running the adjust flow.

This is a soft coupling: completing the task never fails because
consumption disagrees with reality.

## Reorder logic

Periodic worker job `check_reorder_points` (hourly):

- For each item at `on_hand ≤ reorder_point`, ensure there is exactly
  one **open** restock task.
- A restock task is a task generated from a per-property "restock"
  template (default name: "Restock {item}"), role-assigned to whichever
  role the manager has set for restocks at that property
  (default `property_manager`).
- When the restock task is completed, the completion UI prompts the
  employee/manager to enter the actual restocked quantity, creating
  a single positive-delta `inventory_movement` with
  `reason = restock` and `source_task_id` set.

## Adjustments

Inventory adjust flow (manager, or employee with
`inventory.adjust`): enter the observed on-hand value, the system
computes the delta, creates an `audit_correction` movement with the
reason and an optional note.

## Transfers between properties

A transfer is two movements — `transfer_out` at the source,
`transfer_in` at the destination — created atomically, sharing a
`transfer_correlation_id` in the `note` field (simplest; no separate
table for v1).

## Barcode scanning

PWA has a "Scan barcode" button that uses the browser's `BarcodeDetector`
API (Chromium, Android) with a polyfill fallback (ZXing-wasm). On
recognition, the item is looked up and the adjust/restock flow is
pre-filled.

## Reports

- **Low stock** — items at or below reorder point.
- **30-day burn rate** — average daily consumption, by item, by
  property.
- **Top consumed** — items ranked by total consumption in the window.
- **Vendor spend** — grouped by `vendor`, summing unit_cost × qty of
  restocks.

Exposed as REST endpoints and as CSV export (§12).

## Out of scope (v1)

- Lot tracking, expiration dates, serial numbers.
- Multi-warehouse accounting.
- Automated reordering via Amazon/Instacart APIs — we surface a
  pre-filled `vendor_url`, humans click it.
