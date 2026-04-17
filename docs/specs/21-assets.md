# 21 — Assets, actions & documents

Physical equipment and appliances tracked per property, with scheduled
maintenance actions and attached documents (manuals, warranties,
invoices). The goal is simple lifecycle management — know what you have,
keep it maintained, and understand what it costs.

## Asset types (catalog)

A catalog of equipment categories. The system ships with pre-seeded
rows (`workspace_id = NULL`); managers add workspace-custom types.

### `asset_types`

| field                  | type    | notes                                 |
|------------------------|---------|---------------------------------------|
| id                     | ULID PK |                                       |
| workspace_id           | ULID FK? | NULL = system-seeded                 |
| key                    | text    | unique per workspace (composite `(workspace_id, key)`) |
| name                   | text    | "Air conditioner", "Pool pump"        |
| category               | enum    | `climate | appliance | plumbing | pool | heating | outdoor | safety | security | vehicle | other` |
| icon_glyph             | text?   | optional icon identifier              |
| description_md         | text?   |                                       |
| default_lifespan_years | int?    | hint for replacement planning         |
| default_actions_json   | jsonb   | see shape below                       |
| created_at             | tstz    |                                       |
| updated_at             | tstz    |                                       |
| deleted_at             | tstz?   |                                       |

### `default_actions_json` shape

Each entry describes a maintenance action that should be seeded on
assets of this type:

```json
[
  {
    "key": "filter_clean",
    "label": "Clean / replace filter",
    "description_md": "Remove the filter, rinse or replace, and reinstall.",
    "suggested_rrule": "FREQ=MONTHLY",
    "interval_days": 30,
    "estimated_duration_minutes": 20,
    "suggested_checklist": [
      "Turn off unit",
      "Remove filter",
      "Clean or replace",
      "Reinstall and power on"
    ],
    "suggested_inventory": [
      {"sku": "filter_ac_standard", "qty": 1}
    ]
  }
]
```

All fields except `key` and `label` are optional.

### Pre-seeded catalog

| key                | name                | category   | default_lifespan_years | default actions (summary)                                                 |
|--------------------|---------------------|------------|------------------------|---------------------------------------------------------------------------|
| `air_conditioner`  | Air conditioner     | climate    | 12                     | filter_clean (30d), coil_wash (180d), refrigerant_check (365d)            |
| `oven_range`       | Oven / range        | appliance  | 15                     | deep_clean (90d), calibration_check (365d)                                |
| `refrigerator`     | Refrigerator        | appliance  | 14                     | coil_clean (180d), seal_inspect (180d), defrost (90d)                     |
| `dishwasher`       | Dishwasher          | appliance  | 10                     | filter_clean (30d), spray_arm_check (90d)                                 |
| `washing_machine`  | Washing machine     | appliance  | 10                     | drum_clean (30d), filter_clean (60d), hose_inspect (180d)                 |
| `dryer`            | Dryer               | appliance  | 12                     | lint_trap_clean (7d), vent_clean (90d)                                    |
| `water_heater`     | Water heater        | plumbing   | 12                     | anode_inspect (365d), flush_tank (180d), pressure_relief_test (365d)      |
| `boiler`           | Boiler              | heating    | 15                     | annual_service (365d), pressure_check (90d)                               |
| `pool_pump`        | Pool pump           | pool       | 8                      | basket_clean (7d), seal_inspect (180d), motor_service (365d)              |
| `pool_heater`      | Pool heater         | pool       | 10                     | descale (180d), annual_service (365d)                                     |
| `smoke_detector`   | Smoke detector      | safety     | 10                     | test_alarm (30d), battery_replace (365d)                                  |
| `fire_extinguisher` | Fire extinguisher  | safety     | 12                     | visual_inspect (30d), professional_service (365d)                         |
| `generator`        | Generator           | outdoor    | 20                     | oil_change (200h / 180d), load_test (90d), fuel_system_check (365d)       |
| `solar_panel`      | Solar panel         | outdoor    | 25                     | panel_clean (90d), inverter_check (365d), wiring_inspect (365d)           |
| `septic_tank`      | Septic tank         | plumbing   | 30                     | pump_out (1095d), inspection (365d)                                       |
| `irrigation`       | Irrigation system   | outdoor    | 15                     | head_inspect (90d), winterize (365d), spring_startup (365d)               |
| `alarm_system`     | Alarm system        | security   | 10                     | sensor_test (90d), battery_replace (365d), code_review (180d)             |
| `vehicle`          | Vehicle             | vehicle    | —                      | oil_change (180d), tire_inspect (90d), registration_renew (365d)          |

System-seeded rows are read-only in the UI; managers can duplicate
and customize. Workspace-custom types follow the same schema.

## Assets

An individual tracked item installed at a property.

### `assets`

| field                    | type    | notes                                 |
|--------------------------|---------|---------------------------------------|
| id                       | ULID PK |                                       |
| workspace_id             | ULID FK |                                       |
| property_id              | ULID FK |                                       |
| area_id                  | ULID FK? | optional; scopes to a room / zone    |
| asset_type_id            | ULID FK? | nullable for uncategorized items     |
| name                     | text    | "Living room AC", "Pool pump #2"      |
| make                     | text?   | manufacturer                          |
| model                    | text?   | model name/number                     |
| serial_number            | text?   |                                       |
| condition                | enum    | `new | good | fair | poor | needs_replacement` |
| status                   | enum    | `active | in_repair | decommissioned | disposed` |
| installed_on             | date?   |                                       |
| purchased_on             | date?   |                                       |
| purchase_price_cents     | int?    |                                       |
| purchase_currency        | text?   | ISO 4217                              |
| purchase_vendor          | text?   |                                       |
| warranty_expires_on      | date?   |                                       |
| expected_lifespan_years  | int?    | overrides type default                |
| estimated_replacement_on | date?   | derived or manually set               |
| cover_photo_file_id      | ULID FK? | references `file` (§02)             |
| qr_token                 | text    | unique 12-char token; see QR section  |
| guest_visible            | bool    | default `false`                       |
| guest_instructions_md    | text?   | shown on guest welcome page           |
| notes_md                 | text?   | internal staff notes                  |
| settings_override_json   | jsonb?  | settings cascade (§02)                |
| created_at               | tstz    |                                       |
| updated_at               | tstz    |                                       |
| deleted_at               | tstz?   |                                       |

`estimated_replacement_on` is derived as
`COALESCE(installed_on, purchased_on, created_at)::date + COALESCE(expected_lifespan_years, asset_type.default_lifespan_years) * interval '1 year'`
when not manually set. Manually set values take precedence.

## Asset actions

Maintenance operations tracked per asset. Each action defines a
recurring or one-off maintenance task that can be linked to the
scheduling system (§06).

### `asset_actions`

| field                      | type    | notes                                 |
|----------------------------|---------|---------------------------------------|
| id                         | ULID PK |                                       |
| workspace_id               | ULID FK |                                       |
| asset_id                   | ULID FK |                                       |
| key                        | text?   | slug from type defaults; null for ad-hoc actions |
| label                      | text    | "Clean filter", "Annual service"      |
| description_md             | text?   |                                       |
| task_template_id           | ULID FK? | linked task template (§06)           |
| schedule_id                | ULID FK? | linked schedule (§06)                |
| interval_days              | int?    | days between performances             |
| estimated_duration_minutes | int?    |                                       |
| inventory_consumption_json | jsonb?  | `[{"sku": "…", "qty": 1}]`; flows through task completion (§08) |
| last_performed_at          | tstz?   | derived/cached; updated on task completion |
| last_performed_task_id     | ULID FK? | the task that last performed this action |
| created_at                 | tstz    |                                       |
| updated_at                 | tstz    |                                       |
| deleted_at                 | tstz?   |                                       |

### Next due computation

```
next_due = COALESCE(last_performed_at, asset.installed_on, asset.created_at)
           + (interval_days * interval '1 day')
```

`next_due` is computed on read, not stored. The daily digest and
reporting queries use this expression to surface upcoming and overdue
maintenance.

## Asset documents

Files attached to an asset or a property — manuals, warranties,
invoices, certificates, insurance documents, etc.

### `asset_documents`

| field          | type    | notes                                 |
|----------------|---------|---------------------------------------|
| id             | ULID PK |                                       |
| workspace_id   | ULID FK |                                       |
| file_id        | ULID FK | references `file` (§02)               |
| asset_id       | ULID FK? |                                      |
| property_id    | ULID FK? |                                      |
| kind           | enum    | `manual | warranty | invoice | receipt | photo | certificate | contract | permit | insurance | other` |
| title          | text    |                                       |
| notes_md       | text?   |                                       |
| expires_on     | date?   | warranties, certificates, permits     |
| amount_cents   | int?    | for invoices/receipts — feeds TCO     |
| amount_currency | text?  | ISO 4217                              |
| created_at     | tstz    |                                       |
| updated_at     | tstz    |                                       |
| deleted_at     | tstz?   |                                       |

**CHECK constraint:** exactly one of `asset_id` / `property_id` is
non-null. A document belongs to either an asset or a property, never
both, never neither.

## Asset-action to task integration

The flow from asset type to completed maintenance:

1. **Seed from type.** When an asset is created with an `asset_type_id`,
   the system copies `default_actions_json` entries into
   `asset_action` rows for that asset. The manager can then edit,
   delete, or add actions.

2. **Manager activates recurring.** For each action the manager wants
   on a schedule, they link a `task_template` and a `schedule` (§06).
   The UI offers a one-click "Activate schedule" that creates both
   from the action's metadata (`label` as template name,
   `interval_days` as RRULE `FREQ=DAILY;INTERVAL=N`,
   `estimated_duration_minutes` carried over,
   `inventory_consumption_json` copied to the template).

3. **Schedule worker generates tasks.** The existing
   `generate_task_occurrences` worker (§06) materializes task rows.
   Each task carries `asset_id` and `asset_action_id` for traceability.

4. **Completion updates `last_performed_at`.** When a task with
   `asset_action_id` is completed (§06 "Completing a task"), the
   server updates `asset_action.last_performed_at = task.completed_at`
   and `asset_action.last_performed_task_id = task.id` in the same
   transaction.

5. **One-off maintenance.** A manager or agent can create a one-off
   task linked to an `asset_action_id` without a schedule. Completion
   updates `last_performed_at` the same way.

## QR codes

Every asset gets a unique QR token for quick identification via phone
scan.

### Token format

- 12 characters, Crockford base32 (uppercase, no ambiguous chars).
- Generated at asset creation; immutable thereafter.
- Unique across the entire database (not scoped to workspace).

### URL pattern

```
https://<host>/asset/scan/<qr_token>
```

### Scan endpoint behavior

1. Look up asset by `qr_token`.
2. If not found: 404 with a "This asset is not registered" page.
3. If found and caller has a valid session:
   - Employee: redirect to `/asset/<id>` (read-only detail with
     action history and the ability to log a one-off action or report
     an issue).
   - Manager: redirect to `/asset/<id>` (full detail with edit).
4. If found and caller has no session: redirect to `/login?next=
   /asset/<id>`.
5. If found and asset is soft-deleted: 410 Gone.

### Printing

The manager UI offers "Print QR" per asset and "Print all QR codes"
per property. Output is a PDF sheet of labels (asset name + QR +
property + area), suitable for a label printer or A4 cut-out sheet.

## TCO (total cost of ownership) reporting

TCO aggregates all costs associated with an asset over its lifetime.

### Cost components

- **Purchase price**: `asset.purchase_price_cents`.
- **Expense lines**: `expense_line` rows where `asset_id` matches
  (§09).
- **Document invoices**: `asset_document` rows with
  `kind IN ('invoice', 'receipt')` and `amount_cents` set.

### Annual cost formula

```
annual_cost = (purchase_price + sum(expense_lines) + sum(document_invoices))
              / max(1, years_since_purchase)
```

Where `years_since_purchase = (today - COALESCE(purchased_on, installed_on, created_at)) / 365.25`.

### Reporting endpoints

- **Per-asset TCO**: returned in the asset detail response.
- **Per-property TCO summary**: aggregated across all assets at a
  property, broken down by asset type category.
- **Replacement forecast**: assets approaching
  `estimated_replacement_on` within a configurable window.

## Settings cascade additions

Two new keys added to the settings catalog (§02):

| key                           | type | default | scope | spec |
|-------------------------------|------|---------|-------|------|
| `assets.warranty_alert_days`  | int  | `30`    | W/P   | §21  |
| `assets.show_guest_assets`    | bool | `false` | W/P/U | §21  |

- `assets.warranty_alert_days`: number of days before
  `warranty_expires_on` to surface a warranty-expiring alert in the
  manager digest and as a webhook event.
- `assets.show_guest_assets`: when `true`, assets with
  `guest_visible = true` appear on the guest welcome page. When
  `false`, guest assets are hidden regardless of the per-asset flag.

## Enums

Four new enums (added to the canonical list in §02):

- `asset_condition`: `new | good | fair | poor | needs_replacement`
- `asset_status`: `active | in_repair | decommissioned | disposed`
- `asset_document_kind`: `manual | warranty | invoice | receipt | photo | certificate | contract | permit | insurance | other`
- `asset_type_category`: `climate | appliance | plumbing | pool | heating | outdoor | safety | security | vehicle | other`

## Audit log actions

```
asset_type.create
asset_type.update
asset_type.delete

asset.create
asset.update
asset.condition_changed
asset.status_changed
asset.delete
asset.restore

asset_action.create
asset_action.update
asset_action.performed
asset_action.schedule_linked
asset_action.delete

asset_document.create
asset_document.update
asset_document.delete
```

`asset.condition_changed` and `asset.status_changed` are recorded in
addition to `asset.update` when those specific fields change, to allow
targeted webhook subscriptions and digest filtering.

## Webhook events

Added to the event catalog (§10):

```
asset.*               created, updated, condition_changed,
                      status_changed, deleted, restored
asset_action.*        created, updated, performed,
                      schedule_linked, deleted
asset_document.*      created, updated, deleted, expiring
```

`asset_document.expiring` fires when a document's `expires_on` is
within `assets.warranty_alert_days` of today. Evaluated by the daily
digest worker.

## REST API

### Asset types

```
GET    /asset_types                   # list; ?category=…&workspace_only=bool
POST   /asset_types                   # create workspace-custom type
GET    /asset_types/{id}
PATCH  /asset_types/{id}
DELETE /asset_types/{id}              # workspace-custom only; system types → 403
```

### Assets

```
GET    /assets                        # list; ?property_id=…&status=…&condition=…&asset_type_id=…&area_id=…&q=…
POST   /assets
GET    /assets/{id}                   # includes computed TCO, next_due per action
PATCH  /assets/{id}
DELETE /assets/{id}
PUT    /assets/{id}/restore

GET    /assets/{id}/actions           # actions for this asset
POST   /assets/{id}/actions
PATCH  /asset_actions/{id}
DELETE /asset_actions/{id}
POST   /asset_actions/{id}/activate   # create template + schedule from action metadata
POST   /asset_actions/{id}/perform    # log a one-off performance (creates + completes a task)

GET    /assets/{id}/documents         # documents for this asset
POST   /assets/{id}/documents         # multipart; file + metadata
```

### Asset documents (cross-entity)

```
GET    /documents                     # list; ?asset_id=…&property_id=…&kind=…&expires_before=…
GET    /documents/{id}
PATCH  /documents/{id}
DELETE /documents/{id}

GET    /properties/{id}/documents     # documents for this property
POST   /properties/{id}/documents     # multipart; file + metadata
```

### QR scan

```
GET    /asset/scan/{qr_token}         # redirect or error; see QR section
```

### Reports

```
GET    /assets/reports/tco?property_id=…           # per-property TCO summary
GET    /assets/reports/replacements?within_days=…   # upcoming replacements
GET    /assets/reports/maintenance_due              # overdue + upcoming actions
```

## CLI (examples)

```
crewday assets list --property prop_… --status active
crewday assets add "Pool pump #2" --property prop_… --type pool_pump \
                     --area pool --installed 2024-03-15
crewday assets qr-print --property prop_…
crewday asset-actions activate <action-id>
crewday documents list --property prop_… --kind warranty --expires-before 2026-06-01
crewday documents add --asset <id> --kind manual --title "AC user manual" --file ./manual.pdf
```

## Guest welcome page integration

When `assets.show_guest_assets` is `true` for a property (resolved
via the settings cascade), the guest welcome page (§04) includes an
**Equipment** section listing assets where `guest_visible = true`.
Each entry shows:

- Asset name.
- `guest_instructions_md` (rendered as markdown).
- Cover photo (if `cover_photo_file_id` is set).

The section is omitted entirely if no visible assets exist or the
setting is `false`.

## Out of scope (v1)

- IoT sensor integration (temperature, humidity, runtime hours).
- Automated vendor ordering for replacement parts.
- Depreciation accounting (straight-line, declining balance).
- Asset transfer between properties (handle via decommission + new
  asset at destination).
- Barcode/NFC scanning beyond QR (future extension of the scan
  endpoint).
