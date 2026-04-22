# 08 — Inventory

Inventory is a lightweight SKU catalog **per property** with an
append-only ledger of movements. It is not an accounting system; it is
a practical way to know when the toilet paper is about to run out at
Apt 3B.

**All quantities are decimal, not integer.** A pool service consumes
`0.3` of a window-washer bottle; a laundry cycle uses `0.05 kg` of
detergent. The model and API always carry fractional quantities; the
UI formats them locale-aware.

### Precision contract

Inventory is *practical, not accounting* (§00), but the precision
story still has to be nailed down so sums don't drift across the
Postgres → Python → JSON → JS path. Rules:

- **Storage.** Postgres columns are `numeric(14, 4)`; SQLite uses
  `NUMERIC` affinity (stored as text for safety). Fields affected:
  `inventory_item.on_hand`, `.reorder_point`, `.reorder_target`,
  `inventory_movement.delta`, effect `qty`, stocktake line
  `observed_on_hand`.
- **Server-side arithmetic.** Python uses `decimal.Decimal` for
  every aggregation (sums, rate reports, reorder decisions). No
  `float` in the service layer; the Alembic migrations map the
  columns to `sqlalchemy.Numeric(14, 4, asdecimal=True)`. Sums are
  exact.
- **Wire format.** JSON carries the value as a number (e.g.
  `"qty": 0.3`, not `"qty": "0.3"`). Third-party consumers who
  care about exactness are expected to reparse via their own
  decimal library; the human-facing surfaces (web, CLI tables,
  digest emails) format on display.
- **Rounding rule.** Writes are rounded **half-even** to 4 decimal
  places. Observed values beyond 4 decimals are rejected with 422
  `error = "quantity_precision"`.
- **Display.** The UI renders up to 3 decimal places with trailing
  zeros trimmed (`0.3` not `0.300`; `2` not `2.000`). Locale
  formatting follows §18.
- **Heterogeneous units.** There is no universal minor unit across
  the catalog (litres of chemicals, kg of detergent, `each` of
  coffee pods, `stère` of firewood). The 4-decimal contract is
  per-item and unit-agnostic; conversions between units are not
  performed.

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
| delta                  | decimal | positive = stock in (restock / produce / found / transfer_in), negative = stock out (consume / waste / theft / loss / returned_to_vendor / transfer_out); fractional allowed |
| reason                 | enum    | `restock \| consume \| produce \| waste \| theft \| loss \| found \| returned_to_vendor \| transfer_in \| transfer_out \| audit_correction \| adjust` |
| source_task_id         | ULID FK?| task that caused the movement (consume or produce) |
| source_stocktake_id    | ULID FK?| stocktake session, when this row was written by a reconciliation pass |
| actor_kind             | enum    | `user \| agent \| system`          |
| actor_id               | ULID FK?| references `users.id`; null when actor_kind = system |
| note                   | text?   | optional free-text context ("found behind the washing machine", "kid spilled the jar") |

### `inventory_snapshot` (optional)

A rolled-up snapshot of `on_hand` by `(item_id, day)`, produced by a
daily worker job. Gives history graphs without scanning the ledger.

### `inventory_stocktake`

A property-wide reconciliation session: one row per stocktake, with
one `inventory_movement` child per item whose observed count
differed from the cached `on_hand`. Ties a batch of reconciliation
movements together under a single audit handle so the daily digest
can say "Elodie stocktook Villa Sud yesterday; 4 items adjusted"
instead of surfacing four isolated deltas.

| field                  | type    | notes                              |
|------------------------|---------|------------------------------------|
| id                     | ULID PK |                                    |
| workspace_id           | ULID FK |                                    |
| property_id            | ULID FK |                                    |
| started_at             | tstz    |                                    |
| completed_at           | tstz?   | null while the walk is in progress |
| actor_kind             | enum    | `user \| agent`                    |
| actor_id               | ULID FK | who ran the stocktake              |
| note_md                | text?   | optional session-level note        |

Individual movements generated by the session carry
`source_stocktake_id` and `reason = audit_correction` (or a more
specific reason when the stocktake operator picks one per line —
e.g. `theft`, `waste`, `found`; see "Reason taxonomy" below).

## Reason taxonomy

The `inventory_movement.reason` enum is the fixed vocabulary for
*why* stock changed. Reports group by it; the reconcile and adjust
UIs pick from it. Every movement also carries an optional free-text
`note` for the story ("box was damp", "guest broke the kettle").

| reason                | delta sign | typical source                       |
|-----------------------|------------|--------------------------------------|
| `restock`             | positive   | purchase from a vendor (manual or restock-task completion) |
| `produce`             | positive   | task-driven production (laundry produces clean sheets; sheet change produces dirty sheets) |
| `found`               | positive   | item turned up during a stocktake or tidy-up |
| `transfer_in`         | positive   | incoming leg of an inter-property transfer |
| `consume`             | negative   | task-driven consumption (turnover consumes TP and window-washer) |
| `waste`               | negative   | spoilage, breakage, past-expiry disposal |
| `theft`               | negative   | known or suspected theft             |
| `loss`                | negative   | missing, cause unknown (shrinkage)   |
| `returned_to_vendor`  | negative   | returned a faulty or excess item to the supplier |
| `transfer_out`        | negative   | outgoing leg of an inter-property transfer |
| `audit_correction`    | either     | stocktake delta with no named cause — "we counted 12, book said 14, reason unknown" |
| `adjust`              | either     | generic manual correction (deprecated authoring label; kept for back-compat in imports) |

`produce`, `theft`, `loss`, `found`, and `returned_to_vendor` are
new in this revision alongside task-driven production. Historical
rows written with `reason = adjust` remain readable; new writes
prefer `audit_correction` (stocktake without a cause) or the
specific reason.

## Inventory effects on task completion

Task templates (§06) and asset actions (§21) declare
`inventory_effects_json`, a list of `{item_ref, kind, qty}`
entries describing what the task **uses** and what it **produces**.
Each entry:

| field     | type       | notes                                       |
|-----------|------------|---------------------------------------------|
| item_ref  | text       | `sku` during authoring; resolved to `item_id` at task materialisation against the task's property. For `property_scope = any`, the SKU is looked up in the task's resolved property at generation time; unresolved SKUs are skipped with a `task.inventory_ref_missing` audit event. |
| kind      | enum       | `consume \| produce`                        |
| qty       | decimal    | strictly positive; the delta direction comes from `kind` |

Example (linen change bundling consume + produce):

```json
[
  {"item_ref": "LINEN-Q-CLEAN",  "kind": "consume", "qty": 1.0},
  {"item_ref": "LINEN-Q-DIRTY",  "kind": "produce", "qty": 1.0},
  {"item_ref": "WINDOW-WASHER",  "kind": "consume", "qty": 0.3}
]
```

On task completion, the server applies the list **atomically** in
one transaction:

- If the resolved setting `inventory.apply_on_task = true` for the
  completing user, one `inventory_movement` is inserted per entry
  with `source_task_id` set. `consume` writes a negative delta and
  `reason = consume`; `produce` writes a positive delta and
  `reason = produce`.
- If a `consume` would take `on_hand` below zero, the movement
  **still applies** (the item ends the transaction with a negative
  `on_hand`). The item is flagged in both the owner/manager's daily
  digest and an `inventory.stock_drift` event. Rationale: staff
  know more than the model; counts can be wrong, and blocking task
  completion on a bookkeeping disagreement punishes the wrong
  person. Owners or managers reconcile by recording a restock or
  running the adjust flow.
- `produce` never warns on `on_hand` — positive deltas can only
  increase stock.

This is a soft coupling: completing the task never fails because an
effect disagrees with reality.

**Setting cascade (renamed).** The single key
`inventory.apply_on_task` (replaces the pre-revision
`inventory.consume_on_task`) gates both consumption and production.
Operators migrating from the old key are auto-mapped on read:
legacy `inventory.consume_on_task` resolves to the same value until
the key is removed from storage. There is no separate
`inventory.produce_on_task` — agencies either let tasks touch
inventory or they don't.

**Asset action effects.** Asset actions (§21) carry the same
`inventory_effects_json` list. When an action is activated as a
recurring schedule, the list is copied to the generated task
template's `inventory_effects_json`. Effects then flow through the
task-completion mechanism above — no separate inventory path is
needed.

**Completion UI preview.** The task detail screen (worker PWA and
manager view) shows a "Will use / Will produce" panel driven by
`inventory_effects_json`:

- "Uses" lists each `consume` entry with `qty + unit` and a soft
  warning chip when the projected `on_hand - qty < 0`
  ("⚠ 1.5 left — task needs 2").
- "Produces" lists each `produce` entry with `qty + unit` and the
  target item name.
- The panel is visible pre-completion; after completion it is
  collapsed into "Used / Produced" with the actual movement ids
  linked to the ledger drawer.

## Reorder logic

Periodic worker job `check_reorder_points` (hourly):

- For each item at `on_hand ≤ reorder_point`, ensure there is exactly
  one **open** restock task.
- A restock task is a task generated from a per-property "restock"
  template (default name: "Restock {item}"), role-assigned to whichever
  work_role the owner or manager has set for restocks at that property
  (default `property_manager`).
- When the restock task is completed, the completion UI prompts the
  completing user to enter the actual restocked quantity, creating
  a single positive-delta `inventory_movement` with
  `reason = restock` and `source_task_id` set.

## Adjustments and reconciliation

Two paths, both append-only, both audit-logged. Neither ever
edits or deletes an existing `inventory_movement` — the ledger is
the full history.

### Per-item adjust

Quick correction for a single item. Triggered from the inventory
drawer (§14) or `POST /api/v1/inventory/{item_id}/adjust`. The
caller supplies:

- `observed_on_hand` (decimal, required) — the real count.
- `reason` (enum, required) — pick from the full taxonomy
  (`theft | loss | found | waste | returned_to_vendor |
  audit_correction | …`).
- `note` (text, optional) — free-form context.

The server computes `delta = observed_on_hand - on_hand` and
writes one `inventory_movement` with the supplied `reason`,
`note`, and the actor's identity. A zero-delta adjust is rejected
with 422 `error = "nothing_to_adjust"`.

Permissions: owner/manager by default, plus any user carrying the
`inventory.adjust` action (§05).

### Stocktake (property-wide reconciliation)

A walk-every-item session rooted at a property. Used quarterly,
post-renovation, when onboarding a new manager, or whenever the
numbers feel off. The flow:

1. Opener posts `POST /api/v1/properties/{pid}/stocktakes`
   (returns an `inventory_stocktake` row with `completed_at =
   null`).
2. The UI lists every active `inventory_item` at the property with
   an editable "observed" cell next to the cached `on_hand`. The
   user walks the property counting; observed values are saved as
   draft lines on the session (`PATCH
   /api/v1/stocktakes/{sid}/lines/{item_id}`).
3. When ready, the opener posts `POST
   /api/v1/stocktakes/{sid}/commit`. The server writes one
   `inventory_movement` per non-zero delta line inside a single
   transaction, each row carrying `source_stocktake_id = sid` and
   the user-selected `reason` per line (defaults to
   `audit_correction`). `completed_at` is set.
4. The daily digest surfaces the session summary: "Elodie
   stocktook Villa Sud — 4 items adjusted (3 `audit_correction`,
   1 `theft` on bath towels)".

An open stocktake does not block concurrent task completions;
task-driven `consume`/`produce` movements written mid-session are
respected when the session commits (the final delta is
`observed_on_hand - on_hand_at_commit_time`, not `on_hand_at_open`).
If the opener abandons a session (no commit within 24 h), the
`inventory_stocktake` row is auto-marked `completed_at` with a
note "abandoned — no movements written" and no ledger rows are
produced.

Permissions: owner/manager by default, plus any user carrying the
`inventory.stocktake` action (§05).

### Per-item movement history

Every inventory row is backed by its full ledger via `GET
/api/v1/inventory/{item_id}/movements`. The manager UI exposes
this in a right-hand drawer opened from the inventory table row:
paginated list of movements with `occurred_at`, `reason`, `delta`,
actor, `note`, and chips for `source_task_id` / `source_stocktake_id`.
The drawer also hosts the per-item **Adjust** and **Restock**
actions, so opening a row is the single entry point for "what
happened to this item, and fix it".

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

- **Low stock** — items at or below reorder point (excludes items
  with `produce`-only movement history — they are task outputs,
  not restockable supplies).
- **30-day burn rate** — average daily consumption (`reason =
  consume`), by item, by property.
- **30-day production rate** — average daily production (`reason =
  produce`), by item, by property. Useful for sizing the laundry
  loop: if Villa Sud produces 14 dirty sheet-sets/week, the
  laundry schedule needs matching capacity.
- **Top consumed** — items ranked by total consumption in the window.
- **Top produced** — items ranked by total production in the window.
- **Shrinkage** — sum of `theft + loss` deltas per item per
  property over the window; paired with `audit_correction`
  magnitude for a "unexplained variance" line. The digest
  highlights any item whose shrinkage exceeds the operator-set
  `inventory.shrinkage_alert_pct` of rolling consumption (default
  10%).
- **Vendor spend** — grouped by `vendor`, summing unit_cost × qty of
  restocks (excludes `transfer_in` and `produce`, which didn't cost
  anything).
- **Stocktake activity** — list of `inventory_stocktake` sessions
  with per-session delta totals, grouped by property and actor.

Exposed as REST endpoints and as CSV export (§12).

## Out of scope (v1)

- Lot tracking, expiration dates, serial numbers.
- Multi-warehouse accounting.
- Automated reordering via Amazon/Instacart APIs — we surface a
  pre-filled `vendor_url`, humans click it.
