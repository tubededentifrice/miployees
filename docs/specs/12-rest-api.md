# 12 — REST API

The REST API is the **canonical surface**. The web UI and the CLI are
both clients of it. Everything the system can do is reachable here
unless explicitly noted.

## Versioning

- URL-versioned: `/api/v1/...`.
- Breaking changes ship as `/api/v2/...` alongside v1 for at least 6
  months.
- Within a major version, changes are additive (new fields, new
  endpoints, new enum values).
- Clients must ignore fields they do not recognize.

## Base URL

- `https://<host>/api/v1/`
- JSON only (`Content-Type: application/json`).
- UTF-8.

## Authentication

`Authorization: Bearer mip_<key_id>_<secret>` — see §03.

## OpenAPI

- OpenAPI 3.1 document served at `GET /api/openapi.json`.
- Generated from FastAPI's Pydantic models; kept in sync by CI gate
  `/update-openapi` (the committed `openapi.json` under `docs/api/`
  must match the live output).
- Swagger UI at `/docs` (dev + staging only), ReDoc at `/redoc`.

### `operationId` convention

Every route must set `operation_id` in its FastAPI decorator. Format:
`{group}.{verb}` dot-separated (e.g. `tasks.complete`,
`pay.periods.lock`). The first segment must match a known CLI group;
CI enforces uniqueness across the entire schema.

Nested groups use dots: `auth.tokens.create`, `pay.rules.set`.

### CLI surface extensions (`x-cli`)

Every non-hidden route must carry
`openapi_extra={"x-cli": {...}}` in its FastAPI decorator. This
extension is the single source of truth for CLI command generation
(see §13 "CLI generation from OpenAPI").

Extension schema:

| field       | required | description                                                  |
|-------------|----------|--------------------------------------------------------------|
| `group`     | yes      | CLI group name (e.g. `tasks`, `pay`)                         |
| `verb`      | yes      | CLI verb name (e.g. `complete`, `list`)                      |
| `summary`   | yes      | One-line help text (overrides OpenAPI `summary` when it is too API-centric) |
| `aliases`   | no       | List of verb aliases (e.g. `["ls"]` for `list`)              |
| `params`    | no       | Per-param overrides: flag names, short aliases (`-p` for `--property`), file-upload hints, help text beyond what OpenAPI infers |
| `hidden`    | no       | `true` excludes browser-only endpoints (WebAuthn ceremonies, file blob redirect) from CLI generation; requires a reason in the exclusions list |
| `composite` | no       | Names a hand-written override module that replaces the generated command |
| `streaming` | no       | `true` for endpoints that stream ndjson (audit tail, calls list) |
| `never_agent` | no     | `true` for endpoints that agents should never call (informational only) |
| `mutates`   | no       | `true` (default) if the route commits state; `false` for read-only endpoints (`list`, `show`, resolvers, previews). Consumed by §11 "Per-user agent approval mode" — read-only endpoints always execute silently regardless of user mode. HTTP method is the fallback when this flag is absent: `GET` / `HEAD` / `OPTIONS` = read, others = mutating. |

CI fails any route that lacks an `operation_id` or an `x-cli`
extension, unless the route is explicitly listed in
`cli/miployees/_exclusions.yaml` (see §13). The parity gate in §17
enforces each independently.

### Agent confirmation extension (`x-agent-confirm`)

An optional OpenAPI extension that declares the inline
confirmation card rendered when the caller is a delegated-token
embedded agent and the delegating user's mode asks for it (§11
"Per-user agent approval mode"). Presence is the single signal
that an action is "impactful enough to pause on" under
`auto` mode; absence means the route executes silently under
`auto` (and surfaces a generic card only under `strict`).

Extension schema:

| field            | required | description                                                                 |
|------------------|----------|-----------------------------------------------------------------------------|
| `summary`        | yes      | One-line template, rendered against the resolved request payload at request time. Placeholder syntax matches §18's i18n seam; the built-in filter `\|money:<currency-key>` renders minor-unit integers as localized amounts. Example: `"Create expense {vendor} for {amount_minor\|money:currency}?"` |
| `verb`           | no       | Short audit-friendly verb (e.g. `"Create expense"`); defaults to OpenAPI `summary` |
| `risk`           | no       | `low \| medium \| high`; defaults to `medium` for mutations. `high` forces the "Details" pane open by default in the chat card. |
| `fields_to_show` | no       | Ordered list of payload keys (with optional `\|<filter>`) rendered as a compact table under the summary. Defaults to all request-body top-level fields. |

The extension lives alongside `x-cli` on the same route and is
emitted into both `_surface.json` (§13) and the generated
OpenAPI. The middleware pre-renders `summary` and
`fields_to_show` against the resolved payload at the moment the
`agent_action` row is written, stores the result in
`agent_action.card_summary` / `card_fields_json`, and never
re-templates — so the card the user sees at decision time is
stable even if the underlying rows or templates change later.

**CI lint** (§17): every mutating route is inspected; any
placeholder in `summary` or `fields_to_show` that cannot be
resolved against the route's request model is a build failure,
so cards never render blank values in production. Routes that
deliberately omit `x-agent-confirm` list their `operationId` in
`app/agent_confirm/_exclusions.yaml` with a one-line reason,
mirroring `x-cli` exclusions.

Not every mutating route needs a bespoke card. A starter list of
v1 routes that carry `x-agent-confirm` is in §11 "Action
confirmation annotation"; new routes default to "no annotation"
and opt in surgically as product surfaces need them.

## Common conventions

### Dates and times

- All timestamps: RFC 3339 UTC (`2026-04-15T09:38:00Z`).
- Local-only times (e.g. a schedule's `dtstart_local`): ISO-8601
  without offset; `timezone` field on the parent resource.

### IDs

- ULID strings, Crockford base32.
- Never integers in URLs.

### Pagination

- Cursor-based: `GET /tasks?cursor=<opaque>&limit=<int>`.
- Response:
  ```json
  {"data": [...], "next_cursor": "…", "has_more": true}
  ```
- `limit` default 50, max 500.
- **No offset** pagination.

### Filtering

- Query parameters named after fields: `?property_id=prop_…
  &state=pending`.
- Ranges: `?scheduled_for_utc_gte=...&scheduled_for_utc_lt=...`.
- Lists: `?state=pending,overdue`.
- Full-text: `?q=pool`.

### Sorting

- `?sort=-scheduled_for_utc,priority` (minus prefix = descending).
- Only documented per-resource sortable fields are allowed; others
  return 400.

### Errors

Problem Details (RFC 7807):

```json
{
  "type": "https://miployees.dev/errors/validation",
  "title": "Validation error",
  "status": 422,
  "detail": "property_id must be provided",
  "instance": "/api/v1/tasks",
  "errors": [
    {"loc": ["body", "property_id"], "msg": "field required"}
  ]
}
```

Canonical error `type` URIs:

- `validation`, `not_found`, `conflict`, `unauthorized`,
  `forbidden`, `rate_limited`, `upstream_unavailable`,
  `idempotency_conflict`, `approval_required`.

### Idempotency

- All `POST` mutating endpoints accept `Idempotency-Key` header.
- Server stores `(token_id, idempotency_key) -> (status, body_hash)`
  for 24h. Replays return the stored response.
- Different body hash with the same key → 409
  `idempotency_conflict`.
- **Exempt endpoints** (interactive-session-only, §11):
  `POST /payslips/{id}/payout_manifest`. Its response is not stored in the idempotency
  cache; the header is accepted but ignored. A replay re-executes,
  re-audits, and re-decrypts from the current secret store. (Other
  sensitive admin operations — envelope-key rotation, offline
  recovery, hard purge — have no HTTP surface; they run via
  `miployees admin <verb>` on the host and are covered under §11
  "Host-CLI-only administrative commands".)

### Agent audit headers

Optional headers that agents (or any bearer-token caller) may set on
mutating requests to enrich the audit trail (§02, §11):

- `X-Agent-Reason` — free text, up to 500 chars. Stored in
  `audit_log.reason`. Agents should set it on every mutating call.
- `X-Agent-Conversation-Ref` — opaque string, up to 500 chars.
  Stored in `audit_log.agent_conversation_ref`. Links the audit
  entry back to the conversation or prompt that triggered the action.
- `X-Correlation-Id` — ULID or opaque string. Groups multiple
  requests into a logical workflow. If absent, generated server-side
  and echoed via `X-Correlation-Id-Echo`.

### Rate limiting

- Per token (see §03). 429 responses carry `Retry-After`.

### Request/response shape

All resources follow a consistent envelope on collections only:

- Collection: `{"data": [...], "next_cursor": "…", "has_more": false,
  "total_estimate": 1234}`
- Single resource: bare object.
- Creation: 201 with `Location:` + body.
- Deletion: 204.

### Concurrency

- `ETag` / `If-Match` on every updatable resource. Mismatch → 412
  `precondition_failed`.

### Expansion

- `?expand=property,area,assigned_user` (per endpoint, documented
  fields).
- Expansion is explicit — default payloads are lean.

## Resource groups

Each resource has the standard REST verbs; this section lists the
non-obvious ones.

### Auth

```
POST   /auth/webauthn/begin_registration
POST   /auth/webauthn/finish_registration
POST   /auth/webauthn/begin_login
POST   /auth/webauthn/finish_login
POST   /auth/magic/send            # owner or manager only
POST   /auth/magic/consume         # consume a break-glass code → magic link
GET    /auth/me
POST   /auth/logout
POST   /auth/tokens                # create
GET    /auth/tokens                # list (owner/manager)
POST   /auth/tokens/{id}/revoke
POST   /auth/tokens/{id}/rotate
```

### Properties / areas / stays

```
GET    /properties
POST   /properties
GET    /properties/{id}
PATCH  /properties/{id}
DELETE /properties/{id}

GET    /properties/{id}/units
POST   /properties/{id}/units
GET    /units/{id}
PATCH  /units/{id}
DELETE /units/{id}
PUT    /units/{id}/restore
GET    /units/{id}/settings            # sparse overrides
PATCH  /units/{id}/settings

GET    /properties/{id}/areas
POST   /properties/{id}/areas
PATCH  /areas/{id}
DELETE /areas/{id}

GET    /stays                          # filter: ?unit_id=…&property_id=…
POST   /stays                          # body must include unit_id
PATCH  /stays/{id}
DELETE /stays/{id}
POST   /stays/{id}/welcome_link        # create/rotate
DELETE /stays/{id}/welcome_link

GET    /ical_feeds                     # filter: ?unit_id=…&property_id=…
POST   /ical_feeds                     # body may include unit_id
POST   /ical_feeds/{id}/poll           # manual poll trigger

GET    /stay_lifecycle_rules           # filter: ?property_id=…&unit_id=…
POST   /stay_lifecycle_rules
GET    /stay_lifecycle_rules/{id}
PATCH  /stay_lifecycle_rules/{id}
DELETE /stay_lifecycle_rules/{id}

GET    /stay_task_bundles              # filter: ?stay_id=…&unit_id=…&lifecycle_rule_id=…&state=…
GET    /stay_task_bundles/{id}

GET    /property_closures              # filter: ?property_id=…&unit_id=…&from=…&to=…
POST   /property_closures              # body may include unit_id
PATCH  /property_closures/{id}
DELETE /property_closures/{id}
```

### Users / work roles / capabilities

```
GET    /users
POST   /users/invite              # body: {email, grants[], work_engagement?, user_work_roles?}
GET    /users/{id}
PATCH  /users/{id}
POST   /users/{id}/archive
POST   /users/{id}/reinstate
POST   /users/{id}/magic_link

GET    /users/{id}/role_grants
POST   /role_grants
PATCH  /role_grants/{id}
DELETE /role_grants/{id}

# Permission groups + rules (§02, §05)
GET    /permission_groups                    # ?scope_kind=workspace|organization&scope_id=…
POST   /permission_groups                    # user-defined groups only; action: groups.create
GET    /permission_groups/{id}
PATCH  /permission_groups/{id}               # rename / description; action: groups.edit
DELETE /permission_groups/{id}               # user-defined groups only; action: groups.edit
GET    /permission_groups/{id}/members
POST   /permission_groups/{id}/members       # body: {user_id}; action: groups.manage_members (owners: groups.manage_owners_membership)
DELETE /permission_groups/{id}/members/{user_id}

GET    /permission_rules                     # ?scope_kind=…&scope_id=…&action_key=…
POST   /permission_rules                     # action: permissions.edit_rules (root-only)
DELETE /permission_rules/{id}                # action: permissions.edit_rules (root-only)

GET    /permissions/action_catalog           # read-only; the compile-time catalog from §05
GET    /permissions/resolved                 # ?user_id=…&action_key=…&scope_kind=…&scope_id=…
                                             # returns {effect, source_layer, source_rule_id?, matched_groups[]}

GET    /work_engagements          # ?user_id=…&workspace_id=…
GET    /work_engagements/{id}
PATCH  /work_engagements/{id}
POST   /work_engagements/{id}/archive
POST   /work_engagements/{id}/reinstate

GET    /users/{id}/user_work_roles
POST   /user_work_roles
PATCH  /user_work_roles/{id}
DELETE /user_work_roles/{id}

GET    /work_roles
POST   /work_roles
PATCH  /work_roles/{id}

GET    /property_work_role_assignments
POST   /property_work_role_assignments
PATCH  /property_work_role_assignments/{id}
DELETE /property_work_role_assignments/{id}

GET    /capabilities              # resolved per-user view
PATCH  /capabilities/{user_id}

GET    /user_leaves               # ?user_id=…&from=…&to=…&approved=true|false
POST   /user_leaves
PATCH  /user_leaves/{id}
POST   /user_leaves/{id}/approve
POST   /user_leaves/{id}/reject
DELETE /user_leaves/{id}

GET    /user_availability_overrides   # ?user_id=…&from=…&to=…&approved=true|false
POST   /user_availability_overrides
PATCH  /user_availability_overrides/{id}
POST   /user_availability_overrides/{id}/approve
POST   /user_availability_overrides/{id}/reject
DELETE /user_availability_overrides/{id}

GET    /public_holidays                   # ?from=…&to=…&country=…
POST   /public_holidays
GET    /public_holidays/{id}
PATCH  /public_holidays/{id}
DELETE /public_holidays/{id}
```

**`GET /capabilities` response shape** — resolved map per (user,
property_work_role_assignment). Flat JSON:

```json
{
  "data": [
    {
      "user_id": "usr_…",
      "property_work_role_assignment_id": "pwra_…",
      "resolved": {
        "time.clock_in": {"value": true, "source": "property_work_role_assignment"},
        "tasks.photo_evidence_required": {"value": true, "source": "property_work_role_assignment"},
        "messaging.comments": {"value": true, "source": "work_role_default"}
      }
    }
  ]
}
```

`source` is one of `property_work_role_assignment | user_work_role |
work_role_default | catalog_default`.

**`PATCH /capabilities/{user_id}`** — body is a sparse JSON map of
`capability_key → (true | false | null)` plus an `assignment_id`
selector naming which `property_work_role_assignment` to write to.
`null` deletes the override key (restores inheritance).

### Tasks / templates / schedules

```
GET    /task_templates
POST   /task_templates
GET    /tasks
POST   /tasks                      # ad-hoc
POST   /tasks/from_nl              # natural language intake
POST   /tasks/from_nl/commit       # commit a preview
GET    /tasks/{id}
PATCH  /tasks/{id}
POST   /tasks/{id}/assign
POST   /tasks/{id}/start
POST   /tasks/{id}/complete
POST   /tasks/{id}/skip
POST   /tasks/{id}/cancel
POST   /tasks/{id}/comments
GET    /tasks/{id}/evidence
POST   /tasks/{id}/evidence        # multipart/form-data

GET    /schedules
POST   /schedules
GET    /schedules/{id}/preview?for=30d   # upcoming occurrences
POST   /schedules/{id}/pause
POST   /schedules/{id}/resume

POST   /stay_lifecycle_rules/{property_id}/apply_to_upcoming
       # body: {"from": "2026-04-15", "to": "2026-07-15",
       #        "rebuild_patched": false}
       # default window: [today, today+90d]. `rebuild_patched=true`
       # forces full regeneration even for stays that would otherwise
       # patch in place (§04).
```

### Instructions

```
GET    /instructions
POST   /instructions
PATCH  /instructions/{id}          # creates a new revision
GET    /instructions/{id}/revisions
POST   /instructions/{id}/archive
POST   /instructions/{id}/link
DELETE /instructions/{id}/link/{link_id}
GET    /tasks/{id}/instructions    # resolved set
```

### Inventory

```
GET    /inventory                  # items
POST   /inventory
PATCH  /inventory/{id}
POST   /inventory/{id}/movements   # append a movement
GET    /inventory/{id}/movements
POST   /inventory/{id}/adjust      # set on_hand to observed
GET    /inventory/reports/low_stock
GET    /inventory/reports/burn_rate
```

### Time, payroll, expenses

```
GET    /shifts
POST   /shifts/clock_in
POST   /shifts/{id}/clock_out
PATCH  /shifts/{id}                # manager adjust

GET    /pay_rules
POST   /pay_rules
PATCH  /pay_rules/{id}

GET    /users/{id}/payout_destinations
POST   /users/{id}/payout_destinations       # body includes write-only `account_number_plaintext`
PATCH  /payout_destinations/{id}             # scoped to one user; cross-user writes → 422
POST   /payout_destinations/{id}/verify      # owner/manager records that full number matches a paper/photo artifact
POST   /payout_destinations/{id}/archive
POST   /work_engagements/{id}/pay_destination        # body: {destination_id}; always approval-gated for agents
POST   /work_engagements/{id}/reimbursement_destination  # same for reimbursement_destination_id
DELETE /work_engagements/{id}/pay_destination        # clears the pointer
DELETE /work_engagements/{id}/reimbursement_destination

GET    /pay_periods
POST   /pay_periods
POST   /pay_periods/{id}/lock
POST   /pay_periods/{id}/reopen

GET    /payslips
GET    /payslips/{id}
GET    /payslips/{id}.pdf                 # rendered from payout_snapshot_json; never contains full account numbers
POST   /payslips/{id}/issue
POST   /payslips/{id}/mark_paid
POST   /payslips/{id}/void
POST   /payslips/{id}/payout_manifest     # OWNER/MANAGER SESSION ONLY (interactive-session-only, §11). Streams decrypted account numbers JIT; not cached in the idempotency store; returns 410 once secrets are purged.

GET    /expenses
POST   /expenses                   # multipart for receipts
POST   /expenses/{id}/submit
POST   /expenses/{id}/approve
POST   /expenses/{id}/reject
POST   /expenses/autofill          # multipart/form-data; image in → structured JSON out
```

**`POST /expenses/autofill` request.** `Content-Type:
multipart/form-data` with fields:

- `images[]` (1..2, total ≤ 5 MB, `image/jpeg | image/png | image/heic | application/pdf`)
- `hint_currency` (optional ISO-4217, improves accuracy for ambiguous receipts)
- `hint_vendor` (optional text)

Response is the `llm_autofill_json` shape defined in §09.

**`PATCH /shifts/{id}` adjustment rules.** If the patch touches
`started_at`, `ended_at`, `break_seconds`, or `expected_started_at`,
the body must include a non-empty `adjustment_reason`; the server sets
`adjusted = true`. Otherwise `adjustment_reason` is optional and
`adjusted` is unchanged. See §09.

### Assets / documents

```
GET    /asset_types                   # list; ?category=…&workspace_only=bool
POST   /asset_types
GET    /asset_types/{id}
PATCH  /asset_types/{id}
DELETE /asset_types/{id}              # workspace-custom only; system → 403

GET    /assets                        # ?property_id=…&status=…&condition=…&asset_type_id=…&area_id=…&q=…
POST   /assets
GET    /assets/{id}                   # includes computed TCO, next_due per action
PATCH  /assets/{id}
DELETE /assets/{id}
PUT    /assets/{id}/restore

GET    /assets/{id}/actions
POST   /assets/{id}/actions
PATCH  /asset_actions/{id}
DELETE /asset_actions/{id}
POST   /asset_actions/{id}/activate   # create template + schedule from action metadata
POST   /asset_actions/{id}/perform    # log one-off performance (creates + completes task)

GET    /assets/{id}/documents         # documents for this asset
POST   /assets/{id}/documents         # multipart; file + metadata

GET    /documents                     # ?asset_id=…&property_id=…&kind=…&expires_before=…
GET    /documents/{id}
PATCH  /documents/{id}
DELETE /documents/{id}

GET    /properties/{id}/documents     # documents for this property
POST   /properties/{id}/documents     # multipart; file + metadata

GET    /asset/scan/{qr_token}         # redirect or error (§21 QR)

GET    /assets/reports/tco?property_id=…
GET    /assets/reports/replacements?within_days=…
GET    /assets/reports/maintenance_due
```

### Settings

```
GET    /settings                               # workspace defaults (full map)
PATCH  /settings                               # update workspace defaults
GET    /settings/catalog                       # all registered keys + metadata
GET    /settings/resolved                      # ?entity_kind=...&entity_id=...
GET    /properties/{id}/settings               # sparse overrides
PATCH  /properties/{id}/settings               # set/clear overrides (null = inherit)
GET    /work_engagements/{id}/settings         # sparse overrides
PATCH  /work_engagements/{id}/settings
GET    /tasks/{id}/settings                    # sparse overrides
PATCH  /tasks/{id}/settings
```

`GET /settings` returns the workspace defaults as a flat
`dotted.key → value` map plus the workspace policy (approvals,
danger zone). `GET /settings/catalog` returns all registered keys
with their type, catalog default, override scope, and description.

`GET /settings/resolved?entity_kind=property&entity_id=prop_…`
walks the cascade and returns `{key: {value, source, source_id}}`
for every registered key. See §02 "Settings cascade" for
resolution rules.

Entity-level `GET` returns the sparse override map only (keys the
entity has explicitly set). `PATCH` accepts a partial map; setting
a key to `null` deletes the override (restores inheritance).

### LLM and approvals

```
GET    /llm/assignments
PUT    /llm/assignments/{capability}
GET    /llm/calls                  # audit of prior calls
GET    /approvals                  # agent_action rows; ?scope=desk|inline|me filters
POST   /approvals/{id}/approve     # body: {note?}
POST   /approvals/{id}/reject      # body: {note}
GET    /me/agent_approval_mode     # {mode: bypass|auto|strict}
PUT    /me/agent_approval_mode     # body: {mode}; self only
```

Mode writes for another user are **not exposed** — every user
controls their own `agent_approval_mode`. Oversight is through
`audit_log` (the `auth.agent_mode_changed` event) rather than a
permission-group-gated admin endpoint.

Delegated-token requests (§03) carry two agent headers in
addition to the tokens described above (§11 "Agent action
approval" flow):

- `X-Agent-Channel` — enum
  `web_owner_sidebar | web_worker_chat | offapp_whatsapp | offapp_sms`;
  absent means `desk_only`. Written onto
  `agent_action.inline_channel`.
- `X-Agent-Reason` / `X-Agent-Conversation-Ref` — free-text and
  opaque conversation id, as already documented.

An SSE event joins the existing catalog:

- `agent.action.pending` — fired when the middleware writes an
  `agent_action` with `gate_destination = inline_chat` and
  `for_user_id = <uid>`; delivered to that user's open tabs only.
  Payload includes `approval_id`, `card_summary`, `card_risk`,
  `card_fields_json`, `inline_channel`, and `requested_at`.

### Messaging

```
POST   /comments                   # agent/manager creates a comment
POST   /issues
PATCH  /issues/{id}
POST   /issues/{id}/convert_to_task

POST   /webhooks
GET    /webhooks
POST   /webhooks/{id}/disable
POST   /webhooks/{id}/enable
POST   /webhooks/{id}/replay
```

### Files

```
POST   /files                      # multipart
GET    /files/{id}                 # metadata
GET    /files/{id}/blob            # signed redirect or stream
```

Files are stored through a pluggable backend driver (§02 `file`
entity). v1 ships the `local` driver only, writing under
`$MIPLOYEES_DATA_DIR/files/{workspace_id}/{sha256[0:2]}/{sha256[2:4]}/{sha256}`.
Setting `MIPLOYEES_STORAGE=s3` (recipe B) routes to the S3/MinIO
driver. API callers never see the storage path — only the ULID.

### Clients, work orders, invoices (§22)

```
GET    /organizations                         # ?role=client|supplier|both&q=…
POST   /organizations
GET    /organizations/{id}
PATCH  /organizations/{id}
DELETE /organizations/{id}                    # soft delete; 409 if referenced by properties
POST   /organizations/{id}/payout_destinations     # body includes write-only `account_number_plaintext`
POST   /organizations/{id}/default_pay_destination # {destination_id}; always approval-gated for agents

GET    /organizations/{id}/client_rates       # rate card for one client
POST   /client_rates
PATCH  /client_rates/{id}
DELETE /client_rates/{id}                     # soft delete

POST   /client_user_rates
PATCH  /client_user_rates/{id}
DELETE /client_user_rates/{id}

POST   /work_engagements/{id}/engagement_kind  # body: {kind, supplier_org_id?}; always approval-gated when crossing payroll boundary

GET    /work_orders                           # ?property_id=…&client_org_id=…&state=…
POST   /work_orders
GET    /work_orders/{id}                      # includes child tasks, quotes, vendor_invoices
PATCH  /work_orders/{id}
POST   /work_orders/{id}/accept_quote         # body: {quote_id}; always approval-gated
POST   /work_orders/{id}/cancel               # body: {reason}
DELETE /work_orders/{id}                      # soft delete

GET    /work_orders/{id}/quotes
POST   /quotes                                # body: {work_order_id, currency, lines, ...}
PATCH  /quotes/{id}                           # only while status = draft
POST   /quotes/{id}/submit                    # draft → submitted
POST   /quotes/{id}/reject                    # manager
POST   /quotes/{id}/supersede                 # manager

GET    /work_orders/{id}/vendor_invoices
POST   /vendor_invoices                       # multipart for the invoice document
POST   /vendor_invoices/{id}/submit
POST   /vendor_invoices/{id}/approve          # always approval-gated for agents; manager selects destination
POST   /vendor_invoices/{id}/reject
POST   /vendor_invoices/{id}/mark_paid        # always approval-gated for agents
POST   /vendor_invoices/autofill              # multipart/form-data; image in → structured JSON out
```

### Exports

```
GET    /exports/timesheets.csv?from=…&to=…
GET    /exports/payroll_register.csv?period_id=…
GET    /exports/expenses.csv?from=…&to=…
GET    /exports/tasks.csv?from=…&to=…
GET    /exports/client_billable.csv?client_org_id=…&from=…&to=…   # §22
GET    /exports/work_orders.csv?client_org_id=…&from=…&to=…       # §22
```

### Audit

```
GET    /audit                      # owner/manager only
GET    /audit/export.jsonl         # streamed
```

### Health

```
GET    /healthz                    # liveness (no auth)
GET    /readyz                     # readiness (no auth)
GET    /version                    # git sha, release, openapi hash (no auth)
```

## Webhook signatures

See §10 for the envelope and headers.

## Examples

### Creating a one-off task

```http
POST /api/v1/tasks HTTP/1.1
Authorization: Bearer mip_…
Idempotency-Key: 01J-nl-intake-042
Content-Type: application/json

{
  "title": "Airport pickup — Mr. Chen",
  "property_id": "prop_01J…",
  "expected_role_id": "role_driver",
  "scheduled_for_local": "2026-04-21T06:30:00",
  "duration_minutes": 90,
  "priority": "high"
}
```

Response `201 Created`, body is the created task resource, with
`Location: /api/v1/tasks/task_01J…`.

### Completing with evidence

```http
POST /api/v1/tasks/task_01J…/complete HTTP/1.1
Authorization: Bearer mip_…
Content-Type: multipart/form-data; boundary=…

--…
Content-Disposition: form-data; name="payload"
Content-Type: application/json

{"note_md": "All good, linens restocked"}
--…
Content-Disposition: form-data; name="photo"; filename="before.jpg"
Content-Type: image/jpeg

<bytes>
--…
```

### Error

```http
HTTP/1.1 409 Conflict
Content-Type: application/problem+json

{
  "type": "https://miployees.dev/errors/approval_required",
  "title": "Approval required",
  "status": 202,
  "detail": "This action requires an owner or manager approval",
  "approval_id": "appr_01J…",
  "expires_at": "2026-04-18T09:00:00Z"
}
```
