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

Optional per-route OpenAPI extension declaring the inline
confirmation card shown when a delegated-token agent calls the
route and the delegating user's mode asks for confirmation. Full
semantics and starter list are in §11; the schema fields are:

| field            | required | description                                                                 |
|------------------|----------|-----------------------------------------------------------------------------|
| `summary`        | yes      | One-line template rendered against the resolved request payload. Placeholder syntax matches §18's i18n seam; `\|money:<currency-key>` is the one built-in filter. Example: `"Create expense {vendor} for {amount_minor\|money:currency}?"` |
| `verb`           | no       | Short audit-friendly verb; defaults to OpenAPI `summary`.                   |
| `risk`           | no       | `low \| medium \| high`; defaults to `medium`. `high` expands the details pane. |
| `fields_to_show` | no       | Ordered payload keys (with optional filters) rendered as a compact table.   |

The middleware pre-renders `summary` / `fields_to_show` when it
writes the `agent_action` row, stores the result, and never
re-templates — so the card is stable even if templates or data
change later. CI lint (§17) fails any placeholder that can't
resolve against the request model; deliberate omissions go in
`app/agent_confirm/_exclusions.yaml` with a reason.

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

`GET /capabilities` returns a resolved map per
`(user, property_work_role_assignment)` with `{value, source}` per
key — see §05 for the cascade and source values.
`PATCH /capabilities/{user_id}` accepts a sparse
`capability_key → true | false | null` map plus an `assignment_id`;
`null` clears the override.

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

`POST /expenses/autofill` accepts `multipart/form-data` with
`images[]` (1..2, ≤ 5 MB total), optional `hint_currency` /
`hint_vendor`; response shape is `llm_autofill_json` (§09).

`PATCH /shifts/{id}` requires a non-empty `adjustment_reason` when
the patch touches `started_at`, `ended_at`, `break_seconds`, or
`expected_started_at` — see §09 for the adjustment contract.

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

`GET /settings` returns the workspace defaults; `/catalog` lists
registered keys with type/default/description; `/resolved` walks
the cascade returning `{key: {value, source, source_id}}`.
Entity-level `GET` returns the sparse override map only; `PATCH`
accepts a partial map, `null` deletes the override (restores
inheritance). Cascade rules in §02 "Settings cascade".

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

Mode is self-only; no cross-user write endpoint. Oversight via
`auth.agent_mode_changed` in `audit_log`.

### Agent preferences

Free-form Markdown guidance stacked into the LLM system prompt
(see §11 "Agent preferences"). One endpoint per scope.

```
GET    /agent_preferences/workspace            # {body_md, token_count, updated_at, updated_by, writable}
PUT    /agent_preferences/workspace            # body: {body_md, save_note?}
GET    /agent_preferences/property/{id}
PUT    /agent_preferences/property/{id}
GET    /agent_preferences/me                   # self-read
PUT    /agent_preferences/me                   # self-write
GET    /agent_preferences/revisions/{pref_id}  # history listing
GET    /agent_preferences/revisions/{pref_id}/{rev} # single revision
```

`writable` is the resolved verdict from the action catalog —
`true` when the caller passes `agent_prefs.edit_workspace` /
`agent_prefs.edit_property` on the scope, `true` for
`/agent_preferences/me` called by the owning user, `false`
otherwise. On `false`, `GET` still returns `body_md` for
workspace and property scopes (any grant on the scope may
read), but `PUT` returns `403`. User-scope `GET` called by
anyone other than the owning user returns `404` regardless of
workspace grants.

`PUT` refuses bodies that match the secret patterns listed in
§11 "PII posture" with `422 preference_contains_secret` and a
pointer to the offending span. Bodies past the hard token cap
return `422 preference_too_large`.

Every successful `PUT` emits:

- One `agent_preference.updated` event on the webhook family
  (§10).
- One `audit_log` row with `action = agent_preference.updated`
  and `entity_kind = agent_preference`.
- One new `agent_preference_revision` row in the same
  transaction (§02).

Delegated-token requests (§03) may additionally set
`X-Agent-Channel` — `web_owner_sidebar | web_worker_chat |
offapp_whatsapp | offapp_sms` (absent = `desk_only`) — stored on
`agent_action.inline_channel`. On gate, the server emits the SSE
event `agent.action.pending` (scoped to `for_user_id`) with
`approval_id`, `card_summary`, `card_risk`, `card_fields_json`,
`inline_channel`, `requested_at`. Full flow in §11.

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

Worked request/response examples are served by the generated
OpenAPI document (`GET /api/openapi.json`) and by the mock
FastAPI app under `mocks/app/`. A 202 approval_required response
follows the RFC 7807 envelope with `approval_id` and
`expires_at` added to the body — see §11 for the pipeline.
