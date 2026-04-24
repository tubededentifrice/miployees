# 01 — Architecture

## High-level picture

```
+----------------------------+    +---------------------------+
|  Owner/Manager browser     |    |  Worker phone (PWA)       |
|  (React SPA + SW)          |    |  (React SPA + SW)         |
+-------------+--------------+    +-------------+-------------+
          |                                 |
          |   HTTPS, passkey session        |   HTTPS, passkey session
          v                                 v
+-------------------------------------------------------------+
|                   FastAPI app (ASGI)                        |
|                                                             |
|  +-----------+  +-----------+  +-----------+  +-----------+ |
|  | web.*    |  | api.v1.* |  | webhooks |  | admin     | |
|  | (SPA/SSE)|  | (OpenAPI)|  | (in/out) |  | (CLI/API) | |
|  +-----+----+  +-----+----+  +-----+----+  +-----+-----+ |
|        \            |              |             /        |
|         \           v              v            /         |
|          +-----------------------------------+            |
|          |            domain layer           |            |
|          |  (services, scheduling, policy)   |            |
|          +-----------------+-----------------+            |
|                            |                              |
|          +-----------------+-----------------+            |
|          |             adapters              |            |
|          |  db  |  storage  |  mail  |  llm  |            |
|          +--+---+-----+-----+---+----+---+---+            |
+-------------|---------|---------|--------|----------------+
              v         v         v        v
         +--------+ +--------+ +-------+ +----------+
         | SQLite | |  fs    | | SMTP  | | OpenRtr  |
         |  / PG  | | (/data)| | relay | | (Gemma)  |
         +--------+ +--------+ +-------+ +----------+

  Cron / APScheduler worker -- generates tasks from RRULEs, sends digests,
                              polls iCal, runs anomaly detection.
```

Agents (OpenClaw, Hermes, Claude Code, ad-hoc scripts) connect as **HTTPS
clients** to `api.v1.*` using a long-lived API token (§03). The CLI
(`crewday`) is a thin local client to the same HTTP surface.

## Component responsibilities

### `web.*` (React SPA + FastAPI backend)

- `mocks/app/` — FastAPI JSON API (`/api/v1/*`), SSE endpoint
  (`/events`), and an SPA catch-all (`GET *`) that serves the compiled
  `index.html` for any non-API path. No Jinja templates. See §14.
- `mocks/web/` — React SPA (Vite + TypeScript strict). Built into
  `mocks/web/dist/` at compile time; FastAPI serves the `dist/`
  tree as static files in production. In development, a `web-dev`
  service runs Vite HMR on `127.0.0.1:5173` and proxies API calls to
  the FastAPI container.
- Owns the session cookie (passkey-authenticated; §03).
- Hands the same underlying service functions to both web and API
  handlers. **No business logic lives in the handler layer.**

### `api.v1.*`

- Pure JSON, OpenAPI 3.1 (§12).
- No cookies, only `Authorization: Bearer <token>` (§03).
- Idempotency-Key header is honored on all `POST` mutations (§12).

### `webhooks`

- **Inbound**: iCal polling (outbound HTTP, not true webhooks, but
  colocated), email bounce handling via the SMTP provider's webhook,
  optional provider-specific reservation webhooks.
- **Outbound**: event stream (§11, §12) — POSTs signed with HMAC-SHA256
  to agent-supplied URLs on task.created, task.completed,
  task.overdue, stay.upcoming, etc.

### `admin`

- Intended surface for owner/manager destructive operations (rotate API
  tokens, export, purge, re-send a magic link) — reachable from web and
  API.

### Domain layer

- Pure Python, no FastAPI/HTTP imports. Exposes service functions like
  `tasks.create_one_off`, `schedules.generate_occurrences`,
  `payroll.compute_period`, `expenses.autofill_from_receipt`.
- Depends on **ports** (Protocol classes) for every side effect:
  `DbSession`, `Storage`, `Mailer`, `LLMClient`, `Clock`.

### Adapters

- **db**: SQLAlchemy 2.x + Alembic. SQLite and Postgres dialects are
  both supported. No dialect-specific SQL outside `app/adapters/db/`.
- **storage**: `Storage` protocol with a `LocalFsStorage` implementation
  writing to `$CREWDAY_DATA_DIR/uploads/<first-2-of-hash>/<hash>`,
  content-addressed. An `S3Storage` implementation is specified but
  not required for v1.
- **mail**: `Mailer` protocol with SMTP implementation (envelope sender
  from config). For v1, SMTP only; providers like Resend/SES can plug in
  later behind the same protocol.
- **llm**: `LLMClient` protocol with `OpenRouterClient` v1
  implementation. Capability routing lives in the domain layer (§11).

### Worker

- Single-process `APScheduler` running inside the web process by
  default (simplest deploy), switchable to a separate process (same
  image, different entrypoint) when the manager sets
  `CREWDAY_WORKER=external`.
- Jobs: `generate_task_occurrences`, `poll_ical`, `send_daily_digest`,
  `detect_anomalies`, `retry_failed_webhooks`, `prune_sessions`,
  `rotate_audit_log`, `refresh_exchange_rates` (daily, §09),
  `agent_dispatch_sweep` (§16, §23 — restart-safety net for the
  chat-gateway inbound dispatcher; the dispatcher itself is an
  event-bus subscriber, not a scheduled job),
  `llm_budget_refresh_aggregate` (60 s, §11 — re-sums the last 30
  days of `llm_usage` per workspace and writes back to
  `budget_ledger.spent_cents`; idempotent rewrite, crash-safe per-
  workspace SAVEPOINT).

## Module boundaries and bounded contexts

The layering above keeps HTTP out of the domain, but it does not stop
one domain module from reaching into another's internals. The app is
large enough — and worked on by enough humans and agents in parallel —
that the spec pins a **context map**: each bounded context is an
independent package with a narrow public surface, and cross-context
access is enforced in CI from day 1 of application coding.

### Context map

Each context is a subpackage under `app/domain/` with a matching
router under `app/api/v1/`:

| Context        | Scope                                                                              |
|----------------|------------------------------------------------------------------------------------|
| `identity`     | users, passkeys, sessions, API tokens, role grants, permission groups, workspaces  |
| `places`       | properties, units, areas, closures                                                 |
| `tasks`        | task templates, schedules, occurrences, completion, evidence, comments             |
| `stays`        | reservations, iCal feeds, stay task bundles, guest welcome                         |
| `instructions` | KB entries, versioning, scope resolution                                           |
| `inventory`    | items, movements, reorder, consumption hooks                                       |
| `assets`       | asset types, assets, actions, documents                                            |
| `time`         | shifts, clock in/out, geofence settings                                            |
| `payroll`      | pay rules, periods, payslips, CSV exports                                          |
| `expenses`     | expense claims, receipts, OCR                                                      |
| `billing`      | organizations, rate cards, work orders, quotes, vendor invoices (§22)              |
| `messaging`    | digests, notifications, outbound email, chat gateway (§23)                         |
| `llm`          | model router, agent runtime, approvals, preferences, budget (§11)                  |

Cross-cutting concerns that are **not** contexts but are importable
from every context (the "shared kernel"):

- `app/util/` — clock, ULID, hashids, money helpers.
- `app/audit/` — append-only log writer. Every mutation emits here
  in the same transaction; all contexts depend on it, it depends on
  none.
- `app/tenancy/` — `WorkspaceContext` carrier, slug resolver, RLS
  policy installer. All contexts depend on it.
- `app/events/` — in-process event bus and typed event registry.
- `app/adapters/*/ports.py` — Protocol classes that define adapter
  contracts (`DbSession`, `Storage`, `Mailer`, `LLMClient`,
  `Clock`).

### Boundary rules

1. **Public surface only.** Each context exposes exactly one public
   module — `app/domain/<context>/__init__.py` — re-exporting:
   service functions, value objects / refs used by siblings
   (`TaskRef`, `UserRef`, `WorkspaceRef`), and the context's
   repository port plus any context-specific adapter Protocols.
   Sibling contexts MUST NOT import from submodules; everything
   else is private.
2. **No cross-context table access.** Context X MUST NOT read or
   write tables owned by context Y, even through a shared
   `DbSession`. Cross-context queries go through Y's public surface
   (`identity.get_user_ref(ctx, id)`) or subscribe to Y's events.
   This is what makes extracting a context into its own service a
   matter of swapping an adapter, not a rewrite.
3. **Cross-context writes flow through events.** When task
   completion should affect inventory, `tasks.complete()` publishes
   `TaskCompleted`; `inventory` subscribes. Queries may be direct
   function calls against the public surface; writes go through the
   bus. Events are typed (`app/events/registry.py`), synchronous
   and in-process in v1, but the contract is transport-agnostic —
   the same event can later travel over a queue.
4. **Per-context repositories.** Each context defines its own
   repository port (`TaskRepository`, `ExpenseRepository`, …) in
   its public surface and a SQLAlchemy adapter under
   `app/adapters/db/<context>/`. `DbSession` stays the shared
   transactional primitive; specific table access is always
   mediated.
5. **Handlers are thin.** `app/api/v1/<context>.py` routers,
   `app/web/*` (SPA catch-all + SSE), and CLI commands call **only**
   their own context's public surface. Composing across contexts is
   done by coordinating service calls — never by reaching into
   another context's internals. Cross-context composition that
   recurs gets a new service function in the relevant context, not
   a shortcut in the handler.
6. **Shared kernel is tiny and stable.** `util`, `audit`, `tenancy`,
   `events`, and the `adapters/*/ports.py` Protocols are the only
   modules every context is allowed to import from. They change
   rarely and with review.

### Enforcement

- **CI import-boundary gate.** `import-linter` (or equivalent) runs
  on every PR with rules:
  - `domain.*` MUST NOT import `adapters.*`, `api.*`, or `web.*`.
  - `domain.<X>` MUST NOT import `domain.<Y>.<submodule>` — only
    `domain.<Y>` (the public surface).
  - `util`, `audit`, `tenancy`, `events`, and
    `adapters.*.ports` may be imported anywhere.
  A violation fails CI. See §17 for the gate definition.
- **Test boundary.** Unit tests for a context use only that
  context's public surface plus in-memory fakes of its ports. A
  test file that imports two sibling contexts' submodules is a
  code smell unless it lives under `tests/integration/`.
- **Migrations stay shared.** One Alembic timeline, one
  `migrations/versions/` directory. Per-context table definitions
  live under `app/adapters/db/<context>/models.py` and are
  collected into `alembic/env.py`. Contexts own their tables but
  not the migration timeline — a single timeline is needed for
  transactional schema evolution across shared primitives
  (`workspaces`, `audit_log`, `role_grants`).

## Multi-tenancy runtime

crew.day v1 ships as a **multi-tenant platform** from day 1 (§00).
A single deployment holds many `workspace` rows simultaneously —
including the managed SaaS deployment at `crew.day`. Self-hosted
deployments may run one workspace (the original "one family" story)
or many, using the same code.

### Workspace addressing

- Every authenticated HTTP surface lives under a path prefix:
  `<host>/w/<workspace_slug>/...`.
  - Web: `<host>/w/<slug>/today`, `<host>/w/<slug>/tasks`, …
  - API: `<host>/w/<slug>/api/v1/...`.
  - Guest links: `<host>/w/<slug>/guest/<token>`.
- Non-workspace surfaces stay at the bare host: `/signup`,
  `/select-workspace`, `/login`, `/recover`, `/healthz`,
  `/readyz`, `/version`, `/api/openapi.json`, `/docs`, `/redoc`.
- The slug is validated against an ASCII kebab regex
  (`^[a-z][a-z0-9-]{1,38}[a-z0-9]$`) and a reserved-word
  blocklist (`w`, `api`, `admin`, `signup`, `login`, `recover`,
  `select-workspace`, `healthz`, `readyz`, `version`, `docs`,
  `redoc`, `styleguide`, `unsupported`, `static`, `assets`).
  Slugs are unique across the deployment. See §02 `workspace.slug`.
- The slug is an identity label in the URL only. **Authorisation
  is always the user's `role_grants` + `user_workspace` membership,
  never the URL.** A user without membership in `<slug>` hitting
  `/w/<slug>/...` gets `404` (never `403`), so a SaaS tenant
  cannot enumerate workspaces.
- Deployments that later adopt per-workspace subdomains
  (`myorg.crew.day`) keep the slug in the path too
  (`myorg.crew.day/w/myorg/...`). The slug remains the
  canonical identifier; the subdomain is an additive isolation
  layer (separate browser origin for SW scope, storage jar,
  XSS blast radius — see §15).

### `WorkspaceContext`

Every domain service function takes a `WorkspaceContext` as its
first argument:

```python
class WorkspaceContext(Protocol):
    workspace_id: str       # ULID
    workspace_slug: str     # URL segment
    actor_id: str           # users.id or system ULID
    actor_kind: str         # user | agent | system
    actor_grant_role: str   # manager | worker | client | guest
    actor_was_owner_member: bool
    audit_correlation_id: str
```

- Resolved once per request by `app/tenancy/middleware.py` from the
  URL slug plus the session / token, and then passed down into
  every service call and every repository method.
- Repository adapters use `ctx.workspace_id` to filter **every**
  query. This is the application-level tenancy filter.
- The middleware rejects any request whose resolved workspace does
  not appear in `user_workspace` for the authenticated actor with
  `404`.

### DB-level isolation

Every workspace-scoped table carries a `workspace_id` column, and
every repository call filters by `ctx.workspace_id` — this
**application-level filter is the primary isolation layer on every
backend**. On Postgres, the `features.rls` capability (see
"Capability registry" below) additionally installs Row-Level
Security policies reading `current_setting('crewday.workspace_id')`
as defence-in-depth: if a context ever forgets the app-level
filter, RLS still stops the cross-tenant read. On SQLite, RLS is
not available; the app-level filter stands alone, and the
cross-tenant regression test in §17 runs on both backends to catch
any drift.

The choice of backend is a deployment tradeoff (§16), not a code
gate. Both backends fully support multi-tenancy and self-serve
signup; Postgres buys defence-in-depth RLS and better concurrent-
write scalability. See §15 for the full threat analysis and §16
for deployment topology guidance.

### Tenant filter enforcement

The primary isolation mechanism for SQLite — and defence-in-depth
on top of RLS on Postgres — is an **ORM-layer middleware that
auto-injects `AND workspace_id = :current_workspace_id` on every
query against a workspace-scoped table**. Implementation: a
SQLAlchemy event hook on `before_compile` inspects the query's
target tables, looks up each against a registry of workspace-
scoped table names (`app/tenancy/registry.py`), and adds the
filter transparently. Developers write queries normally and never
thread the filter by hand.

Genuine cross-tenant reads (deployment-admin tooling, identity
lookups during auth, analytics worker jobs) MUST wrap the block
in a `with tenant_agnostic():` context manager, which flips a
thread-local off; the event hook checks the flag and skips the
injection when set. `tenant_agnostic()` is the **single
searchable escape hatch** and every use MUST carry a
`# justification:` comment describing why cross-tenant access is
legitimate. CI greps for uncommented uses and fails the build.

Failure modes: if a query against a workspace-scoped table runs
outside both a `WorkspaceContext` **and** a `tenant_agnostic()`
block, the hook raises `TenantFilterMissing` before SQL compile.
No query reaches the database in an ambiguous state. Unit tests
in `tests/tenant/` assert this behaviour explicitly by issuing
each repository method without a context and expecting the
exception.

## Capability registry

There is **one codepath**. The code does not branch on "self-host
vs SaaS" or "SQLite vs Postgres" at the business-logic layer —
every feature works on every deployment. Where the environment
genuinely cannot support a feature (SQLite has no `tsvector`;
local filesystem has no per-prefix lifecycle; the OS has no
`CAP_NET_BIND_SERVICE`; …), the **capability registry** at
`app/capabilities.py` probes the environment once at boot and
exposes a frozen record of booleans. Features consult the
registry; unsupported features degrade gracefully (hidden,
disabled, or replaced by a compatible fallback). The UI renders
disabled capabilities with a "not available on this deployment"
explanation.

Capabilities come from two sources:

- **Environment probes (immutable for the process lifetime).**
  Computed once at boot from the live config: DB dialect,
  storage backend, outbound-mail provider, LLM client, OS
  features. Representative capabilities:
  - `features.rls` — Postgres only.
  - `features.fulltext_search` — Postgres (`tsvector`) or
    SQLite compiled with FTS5.
  - `features.concurrent_writers` — Postgres (true MVCC) or
    the SQLite WAL-mode tuned ≥ configured write concurrency.
  - `features.object_storage` — `CREWDAY_STORAGE=s3`.
  - `features.wildcard_subdomains` — `CREWDAY_PUBLIC_URL`
    matches a wildcard TLS cert installed on the reverse proxy.
  - `features.email_bounce_webhooks` — SMTP provider exposes
    bounce delivery.
  - `features.llm_voice_input` — LLM provider exposes
    speech-to-text.
- **Operator settings (mutable at runtime).** Admin-configurable
  preferences stored in `deployment_setting` (new table, §02)
  and read through the same registry interface. Representative
  settings:
  - `settings.signup_enabled` — whether `/signup/*` is open
    for this deployment. Default **true** on new deployments;
    operators of a private/home install flip it off.
  - `settings.signup_throttle_overrides` — per-deployment
    override of the §15 rate limits.
  - `settings.require_passkey_attestation` — on for
    regulated environments.
  - `settings.llm_default_budget_cents_30d` — deployment-wide
    default cap that seeds every new workspace's `quota_json`
    (§02).

The **registry is the only thing code reads**. Features do not
look at env vars, do not sniff the DB URL, and do not check
"am I in SaaS mode" — they ask `capabilities.fulltext_search`
or `capabilities.signup_enabled` and route accordingly. This
keeps feature behaviour testable (swap a registry fixture),
keeps deployments uniform (`crew.day` and a home-network
install run identical code), and keeps the boot log
auditable (the registry snapshot is logged once at boot).

Unsupported-feature UX is spec'd per feature in the feature's
own section; the platform-wide rule is: **never silent failure**.
A feature that is off must be visibly off — greyed out, marked
"not available on this deployment", or replaced with a clearly
labelled fallback. No 500s, no silently empty result sets.

## Repo layout

```
crewday/
├── README.md
├── AGENTS.md
├── CLAUDE.md              -> AGENTS.md (symlink)
├── pyproject.toml
├── uv.lock
├── docs/
│   └── specs/             <-- this suite
├── app/
│   ├── main.py            # thin re-export shim (backward compat); factory lives in app/api/factory.py
│   ├── config.py          # pydantic-settings
│   ├── api/
│   │   ├── factory.py     # FastAPI app factory — create_app(settings) -> FastAPI
│   │   ├── admin/
│   │   │   └── __init__.py  # admin_router scaffold, mounted at /admin/api/v1
│   │   └── v1/            # one router per context, thin
│   │       ├── __init__.py  # CONTEXT_ROUTERS registry (13 contexts, canonical order)
│   │       ├── identity.py
│   │       ├── places.py
│   │       ├── tasks.py
│   │       ├── stays.py
│   │       ├── instructions.py
│   │       ├── inventory.py
│   │       ├── assets.py
│   │       ├── time.py
│   │       ├── payroll.py
│   │       ├── expenses.py
│   │       ├── billing.py
│   │       ├── messaging.py
│   │       └── llm.py
│   ├── web/               # SPA catch-all + SSE handler (no Jinja)
│   ├── domain/            # bounded contexts; sibling imports
│   │   ├── identity/      # only via __init__.py public surface
│   │   ├── places/
│   │   ├── tasks/
│   │   ├── stays/
│   │   ├── instructions/
│   │   ├── inventory/
│   │   ├── assets/
│   │   ├── time/
│   │   ├── payroll/
│   │   ├── expenses/
│   │   ├── billing/
│   │   ├── messaging/
│   │   └── llm/
│   ├── adapters/
│   │   ├── db/
│   │   │   ├── ports.py           # DbSession, unit-of-work
│   │   │   ├── identity/          # SQLAlchemy models + repo impl
│   │   │   ├── places/
│   │   │   ├── tasks/
│   │   │   └── ...                # one per context
│   │   ├── storage/
│   │   │   └── ports.py
│   │   ├── mail/
│   │   │   └── ports.py
│   │   ├── auth/                  # WebAuthn + magic link + tokens
│   │   └── llm/
│   │       └── ports.py
│   ├── tenancy/           # WorkspaceContext, slug resolver, RLS installer
│   ├── audit/             # append-only log writer
│   ├── events/            # in-process event bus + typed registry
│   ├── worker/            # APScheduler jobs
│   └── util/              # clock, ulid, hashids, money helpers
├── mocks/
│   ├── app/               # FastAPI JSON API + SSE + SPA catch-all
│   └── web/               # Vite + React + TypeScript SPA
│       ├── src/
│       │   ├── main.tsx
│       │   ├── App.tsx
│       │   ├── routes.tsx
│       │   ├── layouts/
│       │   ├── components/
│       │   ├── pages/
│       │   ├── lib/       # fetchJson, queryClient, etc.
│       │   ├── context/   # SseContext, AuthContext, etc.
│       │   ├── styles/    # BEM globals + per-component CSS modules
│       │   └── types/
│       ├── vite.config.ts
│       └── tsconfig.json
├── cli/
│   └── crewday/
│       ├── __main__.py        # entry point
│       ├── _surface.json      # generated CLI descriptor (committed, CI-verified)
│       ├── _codegen.py        # build-time: openapi.json -> _surface.json
│       ├── _runtime.py        # dynamic click command builder
│       ├── _client.py         # httpx HTTP client (auth, retries, streaming)
│       ├── _output.py         # json/yaml/table/ndjson formatters
│       ├── _config.py         # profile loading
│       ├── _globals.py        # global flags
│       ├── _exclusions.yaml   # endpoints excluded from generation (with reasons)
│       └── _overrides/        # hand-written composite commands
├── migrations/            # alembic
├── tests/
│   ├── unit/
│   ├── integration/       # real DB (SQLite + PG via testcontainers)
│   ├── api/               # schemathesis contract tests
│   ├── e2e/               # playwright
│   └── load/              # locust
├── deploy/
│   ├── single/            # Dockerfile for minimal SQLite image
│   └── compose/           # docker-compose.yml + Caddyfile + MinIO
├── scripts/
└── .github/
    └── workflows/
```

Rationale:

- **`app/` vs `cli/` separation.** The CLI is shipped as an independent
  wheel (`crewday-cli`). Its command tree is **generated from the
  API's OpenAPI schema** at build time — the committed `_surface.json`
  descriptor is the join between the two packages. A CI parity gate
  (§17) prevents drift.
- **`domain/` is HTTP-unaware.** Tests at the domain level use fakes
  for all adapters; integration tests exercise real DB + real
  filesystem.
- **`adapters/` depends on `domain/`, not the other way round.** Makes
  swapping Postgres for SQLite, or MinIO for local FS, a one-file
  change.
- **One subpackage per bounded context** under `domain/` and mirrored
  under `adapters/db/`. Sibling contexts interact only through
  published public surfaces (see "Module boundaries and bounded
  contexts" above). The import-linter gate in §17 enforces this on
  every PR.
- **Shared-kernel modules live outside `domain/`.** `app/tenancy/`,
  `app/audit/`, `app/events/`, and `app/util/` hold the cross-cutting
  primitives every context is allowed to import; they carry no
  business rules of their own.

## Environments

| Env        | Purpose                                                | DB                 | Storage  | Mail       | LLM                    |
|------------|--------------------------------------------------------|--------------------|----------|------------|------------------------|
| dev        | local dev loop (uv run, hot reload)                    | SQLite             | local fs | Mailpit    | OpenRouter (or a mock) |
| ci         | pytest + playwright in GH Actions                      | SQLite + PG        | tmpfs    | fake       | record/replay          |
| staging    | operators' + managers' shared test instance            | Postgres           | local fs | real SMTP  | OpenRouter             |
| saas-stage | multi-tenant SaaS staging (e.g. `staging.crew.day`) | Postgres           | S3       | real SMTP  | OpenRouter             |
| self-host  | operator's own deployment (one or many workspaces)     | SQLite or Postgres | local fs | real SMTP  | OpenRouter             |
| saas-prod  | managed SaaS at `crew.day`                          | Postgres           | S3       | real SMTP  | OpenRouter             |

The row differences are deployment-time choices: DB engine,
storage backend, mail/LLM providers. They influence which
capabilities are live (see "Capability registry" above) but do
**not** change which codepaths run.

## Runtime dependencies (pinned families)

- `fastapi >= 0.115`
- `uvicorn[standard]`
- `sqlalchemy >= 2.0`, `alembic`
- `pydantic >= 2.6`, `pydantic-settings`
- `python-multipart` (file upload handling)
- `webauthn` (Duo Labs / py_webauthn) for passkeys
- `python-ulid`
- `apscheduler`
- `httpx` (outbound — iCal, OpenRouter)
- `icalendar` (RFC 5545 parsing + RRULE)
- `dateutil`
- `itsdangerous` (signed tokens for guest welcome pages, magic links)
- `weasyprint` (payslip PDFs)
- `click` (CLI)
- Dev: `pytest`, `pytest-asyncio`, `schemathesis`, `playwright`,
  `ruff`, `mypy`, `locust`.

The frontend (`mocks/web/`) is built with Node 22 (Vite) in a
multi-stage Docker build. The runtime image (`python:3.12-slim`) has
no Node; only the compiled `dist/` artefacts are copied into it.
The `docker-compose` `dev` profile adds a `web-dev` service that
runs Vite HMR on `127.0.0.1:5173` and proxies API calls to the
FastAPI container.

## Key runtime invariants

1. **All times persisted in UTC.** Display time is computed from the
   target property's timezone.
2. **Domain layer never touches `datetime.now()`** — always
   `clock.now()` through the `Clock` port. Determinism matters for
   scheduling.
3. **Every mutation originates from a `WorkspaceContext`** (see
   "Multi-tenancy runtime" above) carrying `workspace_id`,
   `workspace_slug`, `actor_id`, `actor_kind`
   (`user | agent | system`; delegated agents use `user` kind with
   `actor_grant_role` capturing the role under which the action was
   taken), `actor_was_owner_member`, and an `audit_correlation_id`.
   Persisted into `audit_log` in the same transaction as the
   mutation.
4. **Tenant isolation is enforced in two layers.** Every repository
   call filters by `ctx.workspace_id` (application layer), and in
   Postgres every workspace-scoped table additionally carries an
   RLS policy reading `current_setting('crewday.workspace_id')`
   (DB layer). See §15.
5. **Module boundaries are enforced in CI.** `import-linter` blocks
   cross-context submodule imports, domain-to-adapter imports, and
   domain-to-handler imports on every PR (§17). A red boundary
   check fails the build; "temporarily bypassing" is not a thing.
6. **Bind guard on public interfaces.** Default bind is
   `127.0.0.1:8000`. Loopback always passes; a non-loopback address
   passes only when it lives on an interface whose name matches a
   glob in `CREWDAY_TRUSTED_INTERFACES` (default `tailscale*`,
   replaced wholesale when overridden). `0.0.0.0` / `::` never pass
   on their own. Anything else requires
   `CREWDAY_ALLOW_PUBLIC_BIND=1`. The guard does not trust CIDR
   ranges or detect containers — it reads the live interface table.
   §16 recipes set the opt-in explicitly and gate reachability via
   the Docker port map or the internal compose network. See §15 and
   §16.
7. **Secrets are never logged.** Redactor filter on the root logger
   masks anything matching token, cookie, Authorization, passkey
   credential id.

## Decision log pointers

Further architectural decisions (e.g. "why SQLAlchemy and not SQLModel",
"why APScheduler and not Arq") live in
[`docs/adr/`](../adr/) once implementation begins. The spec references
ADRs but does not embed them.
