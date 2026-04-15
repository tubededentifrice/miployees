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
- **Exempt endpoints** (never-agent, §11): `POST /payslips/{id}/
  payout_manifest`. Its response is not stored in the idempotency
  cache; the header is accepted but ignored. A replay re-executes,
  re-audits, and re-decrypts from the current secret store. (Other
  sensitive admin operations — envelope-key rotation, offline
  recovery, hard purge — have no HTTP surface; they run via
  `miployees admin <verb>` on the host and are covered under §11
  "Host-CLI-only administrative commands".)

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

- `?expand=property,area,assigned_employee` (per endpoint,
  documented fields).
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
POST   /auth/magic/send            # manager only
POST   /auth/magic/consume         # consume a break-glass code → magic link
GET    /auth/me
POST   /auth/logout
POST   /auth/tokens                # create
GET    /auth/tokens                # list (manager)
POST   /auth/tokens/{id}/revoke
POST   /auth/tokens/{id}/rotate

POST   /managers/invite            # existing manager invites another
```

### Properties / areas / stays

```
GET    /properties
POST   /properties
GET    /properties/{id}
PATCH  /properties/{id}
DELETE /properties/{id}

GET    /properties/{id}/areas
POST   /properties/{id}/areas
PATCH  /areas/{id}
DELETE /areas/{id}

GET    /stays
POST   /stays
PATCH  /stays/{id}
DELETE /stays/{id}
POST   /stays/{id}/welcome_link        # create/rotate
DELETE /stays/{id}/welcome_link

GET    /ical_feeds
POST   /ical_feeds
POST   /ical_feeds/{id}/poll           # manual poll trigger

GET    /property_closures              # filter: ?property_id=…&from=…&to=…
POST   /property_closures
PATCH  /property_closures/{id}
DELETE /property_closures/{id}
```

### Employees / roles / capabilities

```
GET    /employees
POST   /employees
GET    /employees/{id}
PATCH  /employees/{id}
POST   /employees/{id}/archive
POST   /employees/{id}/reinstate
POST   /employees/{id}/magic_link

GET    /employees/{id}/roles
POST   /employees/{id}/roles
PATCH  /employee_roles/{id}
DELETE /employee_roles/{id}

GET    /roles
POST   /roles
PATCH  /roles/{id}

GET    /capabilities              # resolved per-employee view
PATCH  /capabilities/{employee_id}

GET    /employee_leaves           # ?employee_id=…&from=…&to=…&approved=true|false
POST   /employee_leaves
PATCH  /employee_leaves/{id}
POST   /employee_leaves/{id}/approve
POST   /employee_leaves/{id}/reject
DELETE /employee_leaves/{id}
```

**`GET /capabilities` response shape** — resolved map per (employee,
property_role_assignment). Flat JSON:

```json
{
  "data": [
    {
      "employee_id": "emp_…",
      "property_role_assignment_id": "pra_…",
      "resolved": {
        "time.clock_in": {"value": true, "source": "property_role_assignment"},
        "tasks.photo_evidence_required": {"value": true, "source": "property_role_assignment"},
        "messaging.comments": {"value": true, "source": "role_default"}
      }
    }
  ]
}
```

`source` is one of `property_role_assignment | employee_role |
role_default | catalog_default`.

**`PATCH /capabilities/{employee_id}`** — body is a sparse JSON map
of `capability_key → (true | false | null)` plus an
`assignment_id` selector naming which `property_role_assignment`
to write to. `null` deletes the override key (restores inheritance).

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

POST   /turnover_templates/{property_id}/apply_to_upcoming
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

GET    /employees/{id}/payout_destinations
POST   /employees/{id}/payout_destinations   # body includes write-only `account_number_plaintext`
PATCH  /payout_destinations/{id}             # scoped to one employee; cross-employee writes → 422
POST   /payout_destinations/{id}/verify      # manager records that full number matches a paper/photo artifact
POST   /payout_destinations/{id}/archive
POST   /employees/{id}/pay_destination       # body: {destination_id}; sets employee.pay_destination_id; always approval-gated for agents
POST   /employees/{id}/reimbursement_destination  # same for reimbursement_destination_id
DELETE /employees/{id}/pay_destination       # clears the pointer
DELETE /employees/{id}/reimbursement_destination

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
POST   /payslips/{id}/payout_manifest     # MANAGER-SESSION ONLY (never-agent, §11). Streams decrypted account numbers JIT; not cached in the idempotency store; returns 410 once secrets are purged.

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

### LLM and approvals

```
GET    /llm/assignments
PUT    /llm/assignments/{capability}
GET    /llm/calls                  # audit of prior calls
GET    /approvals                  # agent_action rows
POST   /approvals/{id}/approve
POST   /approvals/{id}/reject
```

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
`$MIPLOYEES_DATA_DIR/files/{household_id}/{sha256[0:2]}/{sha256}`.
Setting `MIPLOYEES_STORAGE=s3` (recipe B) routes to the S3/MinIO
driver. API callers never see the storage path — only the ULID.

### Exports

```
GET    /exports/timesheets.csv?from=…&to=…
GET    /exports/payroll_register.csv?period_id=…
GET    /exports/expenses.csv?from=…&to=…
GET    /exports/tasks.csv?from=…&to=…
```

### Audit

```
GET    /audit                      # manager only
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
  "detail": "This action requires a manager approval",
  "approval_id": "appr_01J…",
  "expires_at": "2026-04-18T09:00:00Z"
}
```
