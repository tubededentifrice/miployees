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
| icon_name              | text?   | Lucide icon name, PascalCase (e.g. `Snowflake`, `Refrigerator`). NULL → generic asset glyph. See §14 "Icons". |
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
| inventory_effects_json     | jsonb?  | `[{"item_ref": "…", "kind": "consume \| produce", "qty": 0.3}]`; flows through task completion (§08). Replaces the pre-revision consume-only `inventory_consumption_json`. |
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

**Blob download authorization.** Asset images, warranty PDFs, and
every other `asset_document` blob are served through the standard
`/uploads/<hash>` surface and inherit §15 "Blob download
authorization" wholesale — same 404-on-cross-workspace behaviour,
same short-lived signed URL for guest welcome pages (§"Guest
welcome page integration"), same audit trail. §21 does not
re-specify the rule; §15 is the source of truth.

## Document text extraction

Every uploaded `asset_document` triggers server-side text extraction
so the file becomes searchable through the knowledge-base index
(§02 "Full-text search ranking — knowledge base") and readable by
the agent through `search_kb` / `read_doc` (§11 "Agent knowledge
tools"). The extracted body is **not** authoritative content — the
binary on `file.storage_key` remains the canonical document — and
it is **not** shown to the user as a replacement for the original;
it powers search, agent grounding, and an optional "View extracted
text" disclosure for transparency.

### Pipeline

Always asynchronous. The upload `POST /assets/{id}/documents`
returns `201` as soon as the `file` and `asset_document` rows are
written, with `extraction_status = "pending"` echoed in the
response. A background worker `extract_document` picks the row up
within seconds.

The worker walks one extractor per MIME family, in this order, and
stops at the first that produces non-empty UTF-8 text:

| MIME family                                       | extractor                |
|---------------------------------------------------|--------------------------|
| `application/pdf` with a text layer                | `pypdf` → `pdfminer`     |
| `application/pdf` image-only (no text layer)       | `tesseract` → `llm_vision` (if assigned) |
| `image/jpeg`, `image/png`, `image/heic`, `image/webp` | `tesseract` → `llm_vision` (if assigned) |
| `application/vnd.openxmlformats…wordprocessingml.document` (`.docx`) | `python_docx` |
| `application/vnd.openxmlformats…spreadsheetml.sheet` (`.xlsx`) | `openpyxl` |
| `text/plain`, `text/markdown`, `text/csv`           | `passthrough`            |
| `text/html`                                          | `passthrough` (script-tag scrubbed) |
| anything else                                        | `unsupported` (no body)  |

The `llm_vision` rung uses the `documents.ocr` capability (§11). It
runs only when an admin has assigned a vision model to that
capability **and** the local OCR rung produced empty/garbage output
(< 16 useful characters per page on average). Every `llm_vision`
attempt charges the workspace 30-day budget like any other LLM
call; refusals from the budget envelope mark the file
`extraction_status = "failed"` with `last_error =
"budget_exceeded"`. The next worker tick retries when budget
returns.

### Sizing and timeouts

- **Hard cap**: documents larger than `documents.extraction.max_bytes`
  (default **50 MB**) skip extraction with `extraction_status =
  "unsupported"` and `last_error = "file_too_large"`.
- **Page cap**: PDFs and image batches stop at
  `documents.extraction.max_pages` (default **200**); subsequent
  pages are not extracted but the body that did extract remains
  searchable. The `pages_json` array notes the truncation so
  `read_doc` can tell the agent.
- **Time-out**: each extractor rung runs in a worker subprocess
  with a 120 s wall-clock cap; on time-out the worker advances to
  the next rung. Three full-pipeline failures flip the row to
  `failed`; the operator can `POST /documents/{id}/extraction/retry`
  to reset attempts.

### Status surface

`asset_document` responses include a denormalised
`extraction_status` and `extracted_at` so the UI can render a
status badge without a second fetch. The full extraction record
(body, pages, extractor, errors) lives behind a separate endpoint
because it can be large and is not always needed:

```
GET    /api/v1/documents/{id}/extraction
        → { status, extractor, body_preview, page_count,
            token_count, has_secret_marker, last_error,
            extracted_at }
GET    /api/v1/documents/{id}/extraction/pages/{n}
        → { page, char_start, char_end, body, more_pages }
POST   /api/v1/documents/{id}/extraction/retry
        → 202; resets attempts and re-queues the worker.
        Owner / manager only.
```

`/extraction` returns at most a 4 000-char `body_preview` for the
human UI; the agent consumes the same data through `read_doc`,
which paginates by token-window per §11.

### Redaction marker

The extraction worker passes `body_text` through the §11 hard-drop
secret patterns (Wi-Fi codes, alarm codes, IBAN-shaped tokens, API
tokens). When at least one match is replaced, the row is flagged
`has_secret_marker = true` and the document detail UI shows a
small banner:

> *"Extraction found a value that looks like a password or access
> code. The agent will not see the original; you may want to
> re-upload a less sensitive version."*

The original binary is untouched — only the extracted text is
redacted. Operators who explicitly want the agent to see the
secret should set the value in the appropriate structured field
(asset notes, instruction body) instead of a free-text scan.

### Settings-cascade additions

| key                                 | type | default | scope | spec |
|-------------------------------------|------|---------|-------|------|
| `documents.extraction.max_bytes`    | int  | `52428800` (50 MB) | D | §21 |
| `documents.extraction.max_pages`    | int  | `200`              | D | §21 |
| `documents.ocr.enabled`             | bool | `true`             | D/W | §21 |

`documents.ocr.enabled` lets a workspace opt out of the
LLM-vision fallback even when the deployment has assigned a model
to the `documents.ocr` capability — useful if the workspace wants
to keep its 30-day budget for chat.

### Audit log additions

```
asset_document.extracted          status transition pending → succeeded
asset_document.extraction_failed  status transition * → failed (with last_error)
asset_document.extraction_retried operator-initiated retry
```

`asset_document.extracted` carries `extractor` and `token_count`
so the manager's audit view can spot a sudden jump in vision-LLM
extractions (and the budget cost behind them).

### Webhook additions

```
asset_document.extracted        body extraction succeeded
asset_document.extraction_failed body extraction failed; carries last_error
```

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
   `inventory_effects_json` copied to the template).

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
