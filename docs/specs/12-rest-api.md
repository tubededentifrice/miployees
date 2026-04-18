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

Every workspace-scoped endpoint lives under a path prefix keyed by
the workspace's URL slug (§01 "Workspace addressing", §02
`workspace.slug`):

- `https://<host>/w/<slug>/api/v1/...` — all workspace-scoped
  routes.
- `https://<host>/api/v1/...` — **only** endpoints that cannot be
  scoped to a workspace. The exhaustive list of bare-host routes is:
  `/api/openapi.json`, `/api/v1/signup/{start,verify}`,
  `/api/v1/auth/{login,logout,magic-link/redeem,webauthn/*}`,
  `/api/v1/me/workspaces` (returns the caller's accessible
  workspaces for the switcher), `/api/v1/healthz`,
  `/api/v1/readyz`, `/api/v1/version`. Anything else 404s at the
  bare host.
- `https://<host>/admin/api/v1/...` — **deployment-scoped** routes,
  reachable only by callers whose `role_grants` include
  `(scope_kind='deployment', grant_role='admin')` or membership in
  a deployment permission group. The full set — `llm/*`, `usage`,
  `workspaces`, `signup`, `settings`, `admins`, `audit`, and the
  admin chat agent (`agent/{log,message,actions}`) — is enumerated
  in the "Admin surface" subsection below. Tokens without any
  deployment grant return `404` on every route under this prefix
  (same tenant-enumeration posture as §01); the `/admin` SPA
  route returns a polite "ask your operator" page instead.

The slug in the URL is the tenant identifier; authorisation is
still the user's `role_grants` + `user_workspace` membership
(§03, §02). A user without membership in `<slug>` hitting
`/w/<slug>/api/v1/...` gets `404`, not `403` — per §01, SaaS
tenants cannot enumerate each other.

- JSON only (`Content-Type: application/json`).
- UTF-8.

## Authentication

`Authorization: Bearer mip_<key_id>_<secret>` — see §03. The token's
scope pins which workspace(s) it may reach; a token scoped to
workspace A used against `/w/<slug-B>/api/v1/...` returns `404`.

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
`cli/crewday/_exclusions.yaml` (see §13). The parity gate in §17
enforces each independently.

**Workspace slug in the CLI.** Workspace-scoped routes carry
`/w/<slug>/` in their path. The CLI generator does not emit
`<slug>` as a per-command positional argument; instead, the
generated `crewday` CLI exposes it as a **global flag**
(`--workspace <slug>` / `-W <slug>`), resolved once and
interpolated into every request URL. Profiles in `_config.py`
may default the flag per-profile so operators do not have to
repeat it. Bare-host routes (signup, login, `me/workspaces`,
health) ignore the flag.

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
  "type": "https://crewday.dev/errors/validation",
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
  `crewday admin <verb>` on the host and are covered under §11
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
- `X-Agent-Page` — opaque string, up to 500 chars, set by the web
  shells on every message the user sends to their embedded agent.
  Shape: `route=/<route-pattern>; params=<k>=<v>,…; entity=<id?>`
  (e.g. `route=/admin/llm; params=capability=chat.manager`). The
  chat endpoint parses it into a structured `page_context` system
  section injected into the agent's prompt — see §11 "Page context"
  — so the agent can answer "restart this capability" without the
  user having to name the page or the entity. Stored verbatim on
  the `chat_message` row for replay; audit rows derived from a
  chat message echo it as `audit_log.ui_page` for operator debug.
  The admin chat agent relies on this header to disambiguate
  workspace actions (there is no URL slug on `/admin`) and to pick
  the right admin tool.

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

**Bare-host routes** (no `/w/<slug>/` prefix — identity is not
workspace-scoped; §01, §03):

```
POST   /api/v1/signup/start                      # SaaS self-serve
POST   /api/v1/signup/verify                     # magic link redeem + WS provisioning
POST   /api/v1/auth/webauthn/begin_registration
POST   /api/v1/auth/webauthn/finish_registration
POST   /api/v1/auth/webauthn/begin_login
POST   /api/v1/auth/webauthn/finish_login
POST   /api/v1/auth/magic/send                   # owner or manager only (manual re-issue)
POST   /api/v1/auth/magic/consume                # consume a break-glass code → magic link
POST   /api/v1/auth/recover/start                # self-service lost-device; body: {email, break_glass_code?}. Always 200 {status:"sent_if_exists"}.
GET    /api/v1/auth/me
POST   /api/v1/auth/logout
GET    /api/v1/me/workspaces                     # switcher payload for the current session

# Self-service email change (§03 "Self-service email change"). Passkey
# session only; PATs and delegated tokens cannot touch email.
POST   /api/v1/me/email/change_request           # body: {new_email}; emails a magic link to new address, notice to old address.
POST   /api/v1/auth/email/verify                 # body: {token}; requires passkey session on same user; swaps users.email atomically.
POST   /api/v1/auth/email/revert                 # body: {token}; 72h revert link from the original old-address notice.

# Click-to-accept invite (§03 "Additional users (invite)"). Identity-
# scoped; same token lands new users in a passkey ceremony and existing
# users on an Accept card. Rejected unless the token's purpose == 'accept'.
GET    /api/v1/invites/{token}                   # introspect the invite (grants, inviter, expiry) pre-accept
POST   /api/v1/invites/{token}/accept            # activate the pending grants atomically

# Personal access tokens (§03 "Personal access tokens"). Identity-
# scoped, passkey-session only, `me:*` scopes only, subject is always
# the authenticated user. Not listed on the workspace admin page.
GET    /api/v1/me/tokens                         # list PATs the caller owns
POST   /api/v1/me/tokens                         # create a PAT (plaintext shown once)
POST   /api/v1/me/tokens/{id}/revoke
POST   /api/v1/me/tokens/{id}/rotate
GET    /api/v1/me/tokens/{id}/audit              # per-token request history

# Device push tokens for the future native app (§02 `user_push_token`,
# §10 "Agent-message delivery", §14 "Native wrapper readiness"). Identity-
# scoped, self-only. The native shell registers its FCM/APNS token here
# after passkey sign-in so the agent-message delivery worker can fan out
# to the user's installed devices. Reserved surface: until push delivery
# is wired and deployment-level FCM/APNS credentials are provisioned,
# POST returns `501 push_unavailable`; GET and DELETE are always live so
# a sign-out can prune a stale row even on a deployment with push off.
GET    /api/v1/me/push-tokens                    # list caller's registered devices (no raw token bytes)
POST   /api/v1/me/push-tokens                    # register; body: {platform: "android"|"ios", token, device_label?, app_version?}
PUT    /api/v1/me/push-tokens/{id}               # refresh last_seen_at / swap rotated token (self-only)
DELETE /api/v1/me/push-tokens/{id}               # unregister on sign-out (self-only)
```

**Workspace-scoped routes** (scoped and delegated API tokens scope
to a workspace; §03). Personal access tokens live on the bare host
above, not here:

```
POST   /w/<slug>/api/v1/auth/tokens              # create scoped or delegated
GET    /w/<slug>/api/v1/auth/tokens              # list (owner/manager — excludes PATs)
POST   /w/<slug>/api/v1/auth/tokens/{id}/revoke
POST   /w/<slug>/api/v1/auth/tokens/{id}/rotate
GET    /w/<slug>/api/v1/auth/tokens/{id}/audit   # per-token request history
```

All subsequent resource groups in this document live under
`/w/<slug>/api/v1/` unless noted. The bare-path forms shown in
earlier resource-group listings (`/properties`, `/tasks`, `/stays`,
…) are relative to the workspace base URL — concatenate with
`https://<host>/w/<slug>/api/v1/` to get the absolute URL.

### Admin surface (`/admin/api/v1/...`)

Deployment-scoped endpoints. All require a caller that passes the
matching `deployment.*` action key (§05 "Action catalog") resolved
against the synthetic deployment scope. A caller with no
deployment grant receives `404` on every path under this prefix —
the surface does not advertise its own existence to tenants.

Authorisation accepts two principals only:

1. A passkey session whose user holds any active
   `(scope_kind='deployment')` `role_grants` row. The `/admin`
   SPA routes run under this principal exclusively.
2. A **deployment-scoped API token** (§03 `api_token`). Scoped
   tokens may carry scopes drawn from the `deployment:*` family —
   `deployment.llm:{read,write}`, `deployment.usage:read`,
   `deployment.workspaces:{read,write,archive}`,
   `deployment.signup:{read,write}`, `deployment.settings:write`,
   `deployment.audit:read`. Mixing `deployment:*` with workspace
   scopes on the same token is 422 `error = "deployment_scope_conflict"`.
   Delegated tokens minted by a deployment admin inherit the user's
   deployment grants as well as whichever workspace grants that user
   happens to hold.

```
# Caller identity
GET    /admin/api/v1/me                              # deployment-admin caller's identity + capabilities
GET    /admin/api/v1/me/admins                       # listing of deployment admins & groups

# LLM graph (moved from per-workspace /w/<slug>/llm; see §11)
# Everything deployment-scope; gated by deployment.llm.{view,edit}.

# Providers — the upstream services we call.
GET    /admin/api/v1/llm/providers                   # list; shape per §11 llm_provider
POST   /admin/api/v1/llm/providers                   # create
GET    /admin/api/v1/llm/providers/{id}
PUT    /admin/api/v1/llm/providers/{id}              # edit (no key rotation)
DELETE /admin/api/v1/llm/providers/{id}              # refuses if any enabled provider-model points at it
PUT    /admin/api/v1/llm/providers/{id}/key          # rotate API key; interactive-session-only (§11)

# Models — provider-agnostic catalogue.
GET    /admin/api/v1/llm/models                      # list; includes capabilities[]
POST   /admin/api/v1/llm/models                      # create
GET    /admin/api/v1/llm/models/{id}
PUT    /admin/api/v1/llm/models/{id}
DELETE /admin/api/v1/llm/models/{id}                 # refuses if any provider-model references it

# Provider × model — pricing + per-combo overrides live here.
GET    /admin/api/v1/llm/provider-models             # list; filters ?provider_id= / ?model_id=
POST   /admin/api/v1/llm/provider-models             # create
GET    /admin/api/v1/llm/provider-models/{id}
PUT    /admin/api/v1/llm/provider-models/{id}
DELETE /admin/api/v1/llm/provider-models/{id}        # refuses if any enabled assignment references it

# Assignments — priority-ordered chain per capability.
GET    /admin/api/v1/llm/assignments                 # grouped by capability
POST   /admin/api/v1/llm/assignments                 # add a rung to a chain
GET    /admin/api/v1/llm/assignments/{id}
PUT    /admin/api/v1/llm/assignments/{id}            # edit max_tokens/temperature/extra_api_params
DELETE /admin/api/v1/llm/assignments/{id}
PATCH  /admin/api/v1/llm/assignments/reorder         # body: [{capability, ids_in_priority_order: [...]}]

# Capability inheritance.
GET    /admin/api/v1/llm/capability-inheritance
PUT    /admin/api/v1/llm/capability-inheritance/{capability}  # body: { inherits_from } ; 422 on cycle
DELETE /admin/api/v1/llm/capability-inheritance/{capability}

# Prompt library — hash-self-seeding; see §11 "Prompt library".
GET    /admin/api/v1/llm/prompts                     # one row per capability, current active body
GET    /admin/api/v1/llm/prompts/{id}                # full body + default_hash + is_customised flag
PUT    /admin/api/v1/llm/prompts/{id}                # snapshot old body → revision; bump version
GET    /admin/api/v1/llm/prompts/{id}/revisions      # full history
POST   /admin/api/v1/llm/prompts/{id}/reset-to-default   # writes a revision containing the current code default

# Pricing sync — replaces pricing/reload from earlier drafts.
POST   /admin/api/v1/llm/sync-pricing                # trigger the OpenRouter sync; streams per-row deltas

# Agent docs — system-side virtual files for the chat agents (§02 agent_doc, §11).
GET    /admin/api/v1/agent_docs                      # one row per active slug, with is_customised flag
GET    /admin/api/v1/agent_docs/{slug}               # full body + default_hash + roles + capabilities
PUT    /admin/api/v1/agent_docs/{slug}               # snapshot old body → revision; bump version
GET    /admin/api/v1/agent_docs/{slug}/revisions     # full edit history
POST   /admin/api/v1/agent_docs/{slug}/reset-to-default  # write a revision containing the current code default

# Call feed — unchanged shape; writes from the new pipeline.
GET    /admin/api/v1/llm/calls                       # deployment-wide call feed (ndjson + --follow)
                                                     # filters: ?capability= / ?provider_model_id= / ?assignment_id= /
                                                     # ?fallback_attempts_gt=0 (chain-walked calls only)
GET    /admin/api/v1/llm/calls/{id}/raw              # raw_response_json if present + unexpired; 404 otherwise

# Chat gateway (deployment-default provider; §23)
GET    /admin/api/v1/chat/providers                  # deployment-default WhatsApp/Telegram providers — stubs only
PUT    /admin/api/v1/chat/providers/{kind}           # set/rotate envelope secret for this channel kind
GET    /admin/api/v1/chat/overrides                  # workspaces that opted into a custom provider
GET    /admin/api/v1/chat/templates                  # Meta template sync state
POST   /admin/api/v1/chat/templates/{name}/resync    # request re-submission to Meta
GET    /admin/api/v1/chat/health                     # last webhook ts, 24h delivery error rate

# Usage aggregates
GET    /admin/api/v1/usage/summary                   # rolling 30d spend, per capability & per workspace
GET    /admin/api/v1/usage/workspaces                # table: workspace → cap / spent / % / paused
PUT    /admin/api/v1/usage/workspaces/{id}/cap       # raise or lower workspace budget cap

# Workspace lifecycle
GET    /admin/api/v1/workspaces                      # list all workspaces (id, slug, plan, verification, size)
POST   /admin/api/v1/workspaces/{id}/trust           # promote to verification_state='trusted'
POST   /admin/api/v1/workspaces/{id}/archive         # archive; owners-group only
GET    /admin/api/v1/workspaces/{id}                 # summary card: counts, usage, most recent activity

# Self-serve signup
GET    /admin/api/v1/signup/settings                 # signup_enabled, throttles, disposable-domain path
PUT    /admin/api/v1/signup/settings                 # edit any of the above

# Deployment settings + capability registry
GET    /admin/api/v1/settings                        # every `deployment_setting` row, resolved
PUT    /admin/api/v1/settings/{key}                  # owners-only; root-only keys (e.g. trusted_interfaces) refuse

# Admin team
GET    /admin/api/v1/admins                          # every role_grants with scope_kind='deployment'
POST   /admin/api/v1/admins                          # grant admin to an existing user by email / id
POST   /admin/api/v1/admins/{id}/revoke
GET    /admin/api/v1/admins/groups                   # owners + managers deployment groups, members
POST   /admin/api/v1/admins/groups/owners/members    # add; root-only
POST   /admin/api/v1/admins/groups/owners/members/{user_id}/revoke

# Deployment audit
GET    /admin/api/v1/audit                           # audit_log rows where scope_kind='deployment'
GET    /admin/api/v1/audit/tail?follow=1             # ndjson stream; same filters as workspace audit

# Admin chat agent (§11, §14). Contextual via X-Agent-Page.
GET    /admin/api/v1/agent/log                       # chat log for this admin
POST   /admin/api/v1/agent/message                   # POST body, plus X-Agent-Page
GET    /admin/api/v1/agent/actions                   # pending gated actions for this admin
POST   /admin/api/v1/agent/action/{id}/approve
POST   /admin/api/v1/agent/action/{id}/deny
```

**SSE.** `/admin/events` mirrors `/w/<slug>/events` for
deployment-scoped events (`admin.usage.updated`,
`admin.workspace.archived`, `admin.llm.assignment_updated`,
`admin.audit.appended`, `agent.message.appended` scoped to
`for_user_id`, `agent.action.pending`, and the
`agent.turn.{started,finished}` pair from §11 "Agent turn
lifecycle").

**Demo mode.** Every admin route 404s under
`CREWDAY_DEMO_MODE=1` (§24). The demo has no operator seat and no
deployment audit worth exposing.

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

### Users / work roles / settings

```
GET    /users
POST   /users/invite              # body: {email, grants[], work_engagement?, user_work_roles?}
GET    /users/{id}
PATCH  /users/{id}
POST   /users/{id}/archive
POST   /users/{id}/reinstate
POST   /users/{id}/magic_link

POST   /me/avatar                 # multipart; self-only; replaces users.avatar_file_id
DELETE /me/avatar                 # self-only; clears avatar_file_id → initials fallback

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

# Self-service shortcuts for the /schedule surface (§14 "Schedule view").
# Each is syntactic sugar over the generic endpoint above with
# `user_id = current_user.id` enforced server-side — a worker cannot
# use them to author requests for another user (a manager still uses
# the full endpoints, scoped by `user_id` param). SSE: POSTs emit
# `user_leave.upserted` / `user_availability_override.upserted`
# so `/schedule` and `/scheduler` both invalidate in lockstep.
GET    /me/schedule                       # self-only calendar feed for /schedule
                                          #   params: from, to (ISO dates;
                                          #     defaults [today, today+14d])
                                          #   returns: rota slots, assigned tasks,
                                          #     approved leaves + overrides + holidays
                                          #     covering the window, pending items
                                          #     flagged separately so the UI can
                                          #     render "pending approval" state
                                          #     without treating them as live.
POST   /me/leaves                         # body: {category, starts_on, ends_on, note_md?}
                                          #   creates user_leave with approval_required
                                          #   always true; returns 201 with pending row.
GET    /me/availability_overrides         # self-only list of every
                                          #   user_availability_override (any
                                          #   approval state), for the /me and
                                          #   /schedule surfaces. Keyed to the
                                          #   caller; managers use the generic
                                          #   `/user_availability_overrides?user_id=`
                                          #   form for other users.
POST   /me/availability_overrides         # body: {date, available, starts_local?, ends_local?, reason?}
                                          #   server computes `approval_required` per
                                          #   §06 "Approval logic (hybrid model)":
                                          #     adding hours → auto-approved,
                                          #     reducing hours → pending.
                                          #   Returns 201 with resolved state so the
                                          #   UI does not need to re-derive it.
```

### Tasks / templates / schedules

```
GET    /task_templates
POST   /task_templates
GET    /tasks
POST   /tasks                      # ad-hoc; body: {title, scheduled_start?,
                                   #   property_id?, area?, notes?,
                                   #   is_personal?} → Task; requires
                                   #   tasks.create permission (§05)
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

GET    /schedule_rulesets                # per-property recurring rota (§06)
POST   /schedule_rulesets
GET    /schedule_rulesets/{id}
PATCH  /schedule_rulesets/{id}
DELETE /schedule_rulesets/{id}           # soft-delete; fails if wired to an
                                         #   active property_work_role_assignment
POST   /schedule_rulesets/{id}/slots     # body: {weekday, starts_local, ends_local}
                                         #   422 `rota_overlap` on conflict (§06)
PATCH  /schedule_rulesets/{id}/slots/{slot_id}
DELETE /schedule_rulesets/{id}/slots/{slot_id}

GET    /scheduler/calendar               # "who is booked where" feed
                                         #   params: from, to (ISO dates),
                                         #     user?, property?, role?
                                         #   returns: { slots: [rota...],
                                         #     tasks: [task...],
                                         #     stay_bundles: [bundle...] }
                                         #   caller-role scoping:
                                         #     manager/owner → full workspace,
                                         #     worker → self-only,
                                         #     client → properties with matching
                                         #       client_org_id, users serialised
                                         #       as {first_name, work_role} only
                                         #       per §15 "Client rota visibility".

POST   /scheduler/tasks/{id}/reschedule  # manager-only inline edit from /scheduler
                                         #   (§14 "/scheduler inline edits").
                                         #   body: {scheduled_for_local} (ISO dt,
                                         #     property-local). Rejects drops that
                                         #     cross the user's leave / closure /
                                         #     approved-override boundaries with
                                         #     422 `availability_conflict`. Emits
                                         #     `task.updated` SSE.
POST   /scheduler/tasks/{id}/reassign    # body: {assigned_user_id}. Runs the §06
                                         #   availability precedence stack; 422
                                         #   `availability_conflict` if the new
                                         #   assignee is not available for
                                         #   `scheduled_for_local`. Manager-only.

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
GET    /expenses/pending_reimbursement   # ?user_id=me|<id>; returns approved-but-not-reimbursed totals grouped by owed_currency (§09)
POST   /expenses                   # multipart for receipts
POST   /expenses/{id}/submit
POST   /expenses/{id}/approve      # snaps owed_* fields; see §09 "Amount owed to the employee"
POST   /expenses/{id}/reject
POST   /expenses/autofill          # multipart/form-data; image in → structured JSON out

GET    /exchange_rates                              # ?as_of=…&quote=…&source=…
GET    /exchange_rates/{base}/{quote}?as_of=YYYY-MM-DD   # single row; as_of defaults to today
POST   /exchange_rates/refresh                       # manager-only; force a refresh_exchange_rates run for this workspace → {job_correlation_id}
POST   /exchange_rates/manual                        # manager-only; body {base, quote, as_of_date, rate}; 409 if ecb row exists
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

GET    /documents/{id}/extraction     # status + body_preview + page_count (§21)
GET    /documents/{id}/extraction/pages/{n}   # paginated extracted-text page-window
POST   /documents/{id}/extraction/retry       # owner/manager only; resets attempts

GET    /asset/scan/{qr_token}         # redirect or error (§21 QR)

GET    /assets/reports/tco?property_id=…
GET    /assets/reports/replacements?within_days=…
GET    /assets/reports/maintenance_due
```

### Knowledge base (KB)

Backs the agent's `search_kb` / `read_doc` tools (§11 "Agent
knowledge tools") and the in-app KB search box (§14). All scoped
to the calling workspace; results are filtered by the caller's
read access — owners/managers see every reachable instruction and
asset_document, workers see what their property/area grants
allow.

```
GET    /kb/search                     # ?q=…&kind=instruction|document&property_id=…&asset_id=…&document_kind=…&limit=10&offset=0
GET    /kb/doc/{kind}/{id}            # full body or page-window; ?page=N for documents
GET    /kb/system_docs                # list_system_docs(); only docs whose role tag matches the caller
GET    /kb/system_docs/{slug}         # read_system_doc(slug)
```

`/kb/search` returns
`{ results: [{kind, id, title, snippet, score, why}], total }`.
`/kb/doc/{kind}/{id}` for `kind = "instruction"` returns the
current revision body; for `kind = "document"` returns
`{title, body, page, page_count, more_pages, source_ref}` with
`page` defaulting to 1 and the page-window capped at the
`documents.read.page_token_cap` setting (default 4 000 model-
tokens).

`/kb/system_docs` is open to any authenticated user, but the
returned list is filtered by the requester's role grants — a worker
sees no admin-tagged docs.

The deployment-admin surface (§ "Admin") exposes parallel routes
under `/admin/api/v1/agent_docs/*` for editing the system-doc
overrides themselves; KB search and read tools never call those.

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

LLM provider / model / assignment / prompt management **has moved**
to the `/admin/api/v1/llm/*` deployment surface (see the admin
section above). Workspace owners and managers keep the narrow usage
surface and the approvals desk only:

```
GET    /llm/calls                  # workspace-scoped call feed (audit)
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
offapp_whatsapp` (absent = `desk_only`) — stored on
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
`$CREWDAY_DATA_DIR/files/{workspace_id}/{sha256[0:2]}/{sha256[2:4]}/{sha256}`.
Setting `CREWDAY_STORAGE=s3` (recipe B) routes to the S3/MinIO
driver. API callers never see the storage path — only the ULID.

`GET /files/{id}/blob` is authorized per the file's attach site:
a file referenced by `users.avatar_file_id` is readable by any
authenticated user who can see that user's `display_name` (so
avatars render everywhere the user appears). Task / expense /
issue attachments stay gated by their owning resource's ACL.

### Avatar upload (`POST /me/avatar`)

Convenience endpoint for the /me avatar editor (§14). One call
replaces the two-step "upload file, then PATCH user". Self only;
managers cannot set another user's avatar.

- **Request.** `multipart/form-data` with a single `image` part.
  Accepts `image/png`, `image/jpeg`, `image/webp`, `image/heic`
  (sniffed, not trusted). Max 10 MB (§15).
- **Server processing.** Decode, re-encode as 512×512 WebP with
  the submitted crop box applied (see §14 for the client-side
  crop payload — sent as additional form fields `crop_x`,
  `crop_y`, `crop_size` in source-image pixels; the server
  re-crops authoritatively). EXIF stripped per §15. Any
  `retain_exif` workspace override does **not** apply to avatars.
- **Persistence.** New `file` row; previous `avatar_file_id`
  soft-deleted (`deleted_at`). `users.avatar_file_id` updated in
  the same transaction.
- **Audit.** Emits `user.avatar_changed` with
  `{before_file_id, after_file_id}`.
- **Response.** `200` with the updated `user` serialisation
  including `avatar_url` (see below).

`DELETE /me/avatar` clears `avatar_file_id` (soft-deletes the
previous `file` row), emits `user.avatar_changed` with
`after_file_id = null`, and returns the updated user.

### `avatar_url` in user serialisations

Wherever the API returns a user (`/me`, `/users`,
`/employees`, embeds in `/tasks`, …), the payload includes an
`avatar_url` field:

- `null` when `avatar_file_id is null`.
- Otherwise a deployment-relative path (`/api/v1/files/{id}/blob`)
  the client can drop straight into an `<img src>`. No signed
  URLs in v1 — auth is cookie-based and the endpoint enforces
  visibility server-side.

`avatar_initials` stays as the fallback string for the
no-avatar case; clients render the image when `avatar_url` is
present and the initials circle otherwise.

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
POST   /vendor_invoices/{id}/proof            # multipart; appends to proof_of_payment_file_ids — client or owner/manager
DELETE /vendor_invoices/{id}/proof/{file_id}  # remove a proof file — owner/manager only
POST   /vendor_invoices/autofill              # multipart/form-data; image in → structured JSON out

# property_workspace invites (§22)
POST   /property_workspace_invites            # inviter-side; body: {property_id, to_workspace_id?, proposed_membership_role, initial_share_settings_json, note_md?}
GET    /property_workspace_invites            # list pending invites originated from this workspace (?state=…)
GET    /property_workspace_invites/{id}       # inviter-side detail
POST   /property_workspace_invites/{id}/revoke
```

**Invite token endpoints** (no `/w/<slug>/` prefix — the token is
the identity). These extend the existing `/api/v1/invites/...`
shape used by user-level invites:

```
GET    /api/v1/property_workspace_invites/{token}               # introspect (property, inviter workspace, role, expiry, share widening)
POST   /api/v1/property_workspace_invites/{token}/accept        # body: {accepting_workspace_id}; user must be owners on that workspace
POST   /api/v1/property_workspace_invites/{token}/reject        # body: {note_md?}
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
