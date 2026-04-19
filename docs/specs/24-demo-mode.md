# 24 — Demo mode

A separate deployment of the crew.day app that lets **unauthenticated
visitors** try the product against fake data, with each visitor isolated
in their own ephemeral workspace. Demo mode is not a feature flag on the
production deployment — it is an entirely separate container, separate
database, separate OpenRouter key, separate DNS host. Prod never runs with
demo code paths enabled and demo never reads prod rows.

The demo deployment is the canonical host-evidence that the product's
agent-first design works: a visitor chatting to the embedded agent can
drive the whole app without ever seeing the token or passkey surfaces.

## Scope

- **In scope:** task / schedule / stay / inventory / expense flows, both
  embedded chat agents (§11 `chat.manager` + `chat.employee`), natural-
  language task intake, the settings cascade, and the worker PWA
  completion loop — enough to sell the product.
- **Out of scope:** iCal ingest, SMTP / email, webhook deliveries,
  passkey enrollment, magic-link recovery, payroll issuance, payout
  manifest endpoints, real money movement, daily digests, anomaly
  detection, receipt OCR, and voice transcription. All are replaced by
  no-op or pre-baked-fixture implementations — see "Disabled integrations"
  below.
- **Landing page is out of scope for this repo.** The demo app exposes
  an **iframe contract** (below); the marketing landing page that
  presents scenario pickers and embeds the app is a separate project.

## Entity

One new table. Every other demo row lives on the regular schema, scoped
by `workspace_id` like any production row.

```
demo_workspace
├── id                       ULID PK (matches a row in `workspaces`)
├── scenario_key             text — e.g. "rental-manager", "villa-owner"
├── seed_digest              sha256 of the fixture bundle used to seed
├── created_at               tstz
├── last_activity_at         tstz — MAX(session_touched_at, last_mutation_at)
├── expires_at               tstz — last_activity_at + ttl (24h default)
└── cookie_binding_digest    sha256 of the signed cookie's subject id
```

`demo_workspace.id` is also a row in `workspaces` (FK), so every existing
`workspace_id` filter continues to work. The presence of a
`demo_workspace` row is the only thing that marks the workspace as demo
— there is no `workspace.is_demo` flag on the production table.

## Deployment shape

- **Host:** `demo.crew.day` (subdomain distinct from any prod host).
- **Container:** a regular crewday image run with `CREWDAY_DEMO_MODE=1`.
  That env var is refused on any deployment whose `CREWDAY_PUBLIC_URL`
  does not match the demo allowlist — a belt-and-suspenders guard
  against turning on demo mode in production by accident.
- **Database:** separate SQLite file or Postgres instance. Never points
  at a prod DB.
- **OpenRouter key:** a dedicated demo key. Revoking it pauses demo
  without touching prod.
- **Storage:** local filesystem only; no S3; uploaded files garbage-
  collected with the workspace (§ "Garbage collection").
- **Object storage:** binary evidence (photo uploads) allowed. Hard-
  capped at 5 MiB per file and 10 files per workspace lifetime — enough
  to demo the "photo evidence" flow without turning the demo into free
  image hosting.

See §16 "Recipe C — Demo deployment" for the compose snippet.

## Visitor lifecycle

### First visit

1. Visitor's browser navigates (or an iframe on the landing page loads)
   `https://demo.crew.day/app?scenario=<scenario_key>`.
2. No demo cookie on the request → the server:
   a. Validates `scenario_key` against the scenario catalog (below); an
      unknown key falls back to the default scenario and a server log
      line.
   b. Creates a new `workspaces` row and the matching `demo_workspace`
      row.
   c. Seeds the workspace from `fixtures/demo/<scenario_key>.yml`
      (§ "Seeding").
   d. Issues a signed demo cookie (§ "Demo cookie") bound to the
      workspace and the scenario.
   e. Redirects / responds with the initial page, logged-in as the
      scenario's primary persona.
3. Subsequent requests on the same cookie read the bound workspace.

### Returning visit

- Cookie present, workspace alive → normal request flow; `last_activity_at`
  is bumped on every authenticated request (min-interval 5 s to avoid
  row-write amplification).
- Cookie present, workspace **garbage-collected** → the server mints a
  new workspace and a new cookie for the **same scenario** the cookie
  named, seeds it, and responds normally. **No banner, no modal** —
  the visitor simply sees fresh data. A short `X-Demo-Reseeded: 1`
  response header is set on the first response after a reseed; the
  mock UI can show a discreet toast ("Demo refreshed with new sample
  data") if the landing page asks for one. Default is silent.
- Cookie present but tampered with or signed by an old key → treat as
  absent, mint fresh.

### Scenario switching

A visitor who loads two iframes with two different `scenario` values
ends up with **two demo workspaces** — each bound to its **own**
signed cookie, not to a shared cookie with two bindings. The
landing page renders a third iframe? Three cookies, three
workspaces. Scenarios are independent playgrounds by design, and
cookie scope enforces the separation at the browser layer, not
only at the server.

A visitor switching scenarios in the same tab (say, the landing
page swaps its iframe's `scenario` query param) MUST be treated as
a fresh session: the per-scenario cookie name is never reused, so
the `rental-manager` scenario cannot inherit state authored in the
`villa-owner` scenario even if both were visited seconds apart. See
§ "Demo cookie" for the cookie name and path scoping that enforces
this; see § "Garbage collection" for the independent TTLs.

### Garbage collection

- Worker job `demo_gc` runs every **15 minutes**.
- Any `demo_workspace` whose `expires_at < now()` is purged in a single
  transaction: the `workspaces` row, every child row keyed by its
  `workspace_id`, every uploaded file under
  `$DATA_DIR/demo/<workspace_id>/`.
- Cascade is enforced by FK `ON DELETE CASCADE` on every
  `workspace_id` column. A weekly job re-runs an integrity check and
  fails the container start if any orphan rows survive.
- Per-deployment hard caps (§ "Abuse controls") stop the GC from
  being the only line of defence.

## Demo cookie

Name: `__Host-crewday_demo_<scenario_id>` — one cookie per scenario,
not a single cookie holding multiple bindings. The `<scenario_id>`
segment is the scenario's stable identifier (e.g.
`rental_manager`, `villa_owner`), matching the keys used in
`app/fixtures/demo/`. The `__Host-` prefix requires `Path=/`, so
path scoping is handled via the workspace slug inside the cookie
payload rather than in the cookie's `Path` attribute — the demo
app enforces a same-scenario check on every request.

Flags: `Secure; HttpOnly; SameSite=None; Path=/; Partitioned;
Max-Age=2592000` (30 days).

Each cookie is an `itsdangerous` signed blob holding exactly one
binding:

```json
{
  "v": 1,
  "scenario": "rental-manager",
  "binding": { "workspace_id": "wks_01J…", "persona_user_id": "usr_01J…" },
  "iat": 1713552000
}
```

- One cookie per scenario means a visitor concurrently running two
  scenarios has two distinct cookies; neither can pivot into the
  other's workspace even if a request is somehow mis-routed. The
  cookie name is never reused across scenarios — reassigning
  `rental-manager` to a fresh workspace rotates the cookie's
  signature input, not the cookie name.
- `SameSite=None; Secure; Partitioned` is required for the cross-origin
  iframe use case: the demo app loads inside an iframe on the landing
  page and must still read its own cookie. The `Partitioned` attribute
  opts into CHIPS (Cookies Having Independent Partitioned State), so
  the cookie's storage is keyed to the top-frame origin. A visitor on a
  different landing page (say, a blog embedding the same iframe) gets a
  separate partition and therefore a separate workspace — which is
  correct behaviour, not a bug.
- The cookie is **signed**, not encrypted; its contents are not secret.
  Tampering invalidates the signature and the server treats the cookie
  as absent.
- The signing key is a deployment secret (`CREWDAY_DEMO_COOKIE_KEY`,
  32 bytes base64) rotated by the operator; a rotation invalidates
  every live demo cookie on the next request, which is fine — visitors
  are reseeded.
- No `crewday_csrf` cookie on demo: the demo mode writes are scoped to
  the workspace the cookie names and do not touch money or secrets, so
  the CSRF double-submit is dropped in favour of the cookie's own
  binding plus a `Sec-Fetch-Site` check. See §15 "Demo deployment".

The demo cookie is **not a session** (§03): no `sessions` row, no
passkey credential id, no `role_grants` lookup. Authority inside the
demo is resolved straight from the scenario fixture — the seeded users
already carry `role_grants`, and the cookie picks which seeded user
the request acts as (§ "Persona switching").

## Persona switching

Each scenario seeds a fixed cast — owner, managers, workers, clients.
The cookie carries, per binding, a `persona_user_id` pointing at one
seeded user:

```json
{ "scenario": "rental-manager", "workspace_id": "…", "persona_user_id": "usr_01J…" }
```

- The iframe URL can name the starting persona via
  `?scenario=rental-manager&as=manager` where `as=` is validated against
  a small allowlist per scenario (e.g. `manager | worker | client |
  owner`), not free ULIDs.
- Inside the demo, a floating switcher (`.demo-persona-switcher`) lets
  the visitor change persona without leaving the workspace. Switching
  rewrites `persona_user_id` on the cookie binding and triggers a page
  reload; data is unchanged, permissions flip.
- The switcher is **only** rendered when `CREWDAY_DEMO_MODE=1` and the
  cookie's binding has more than one valid persona for its scenario.

Every demo write is audited with `actor_kind = 'user'`,
`actor_id = persona_user_id`, and a synthetic `agent_label =
'demo'` so the audit trail remains coherent — and, once the demo is
GC'd, it goes with the rest of the rows.

## Seeding

Scenario fixtures live under `app/fixtures/demo/` in the production
image (they are part of the build, not runtime-loaded from disk) and
are the single source of truth for demo content. Each fixture is a
YAML file describing the workspace, properties, units, users, role
grants, task templates, stays, inventory, a handful of past
bookings, and a few in-flight items to make the UI look alive on
landing.

- Timestamps in fixtures are **relative** (`T-2d`, `T+3h`, `stay:+7d`),
  resolved to absolute UTC at seed time. Nothing in the fixture is a
  hard date; a demo seeded "now" must always look current.
- Property and user names are ASCII stock names (Bernard, Maria, Villa
  Sud) shared with the mocks' `mock_data.py` where practical — one
  canonical cast.
- Evidence photos referenced by completed tasks are pre-bundled under
  `app/fixtures/demo/_media/` and copied into the workspace's upload
  directory at seed time, so the UI has real `file` rows to render.

Scenarios shipped with v1:

| key              | headline persona        | fixture highlights |
|------------------|-------------------------|--------------------|
| `villa-owner`    | Owner of one property   | Single villa, weekly maid, upcoming Airbnb stay bundle, one staff worker |
| `rental-manager` | Agency managing many    | 3 properties, 4 staff workers, mixed payroll/contractor engagements, a work order mid-flight |
| `housekeeper`    | Worker-first            | Lands directly in the worker PWA on `/today` with 4 tasks already populated |

Each fixture declares a `default_persona` and a `personas` allowlist
that the `?as=` query param is validated against. Adding a scenario is
a pure-data change plus a one-line entry in the scenario catalog; no
schema migration.

## Iframe contract

The demo app is the **only** crew.day surface with a relaxed CSP
`frame-ancestors`. The production app and staging both keep the
§15 default (`frame-ancestors 'none'`).

- CSP on `demo.crew.day` is `frame-ancestors https://crew.day
  https://*.crew.day`. Landing-page domains outside this list cannot
  embed the demo.
- The allowlist is an env var (`CREWDAY_DEMO_FRAME_ANCESTORS`,
  whitespace-separated); default empty → demo runs standalone (no
  iframe) unless the operator sets it.
- `X-Frame-Options` is not set on demo responses (the header is
  incompatible with an allowlist of multiple origins; CSP supersedes).
- The demo app exposes **no `postMessage` bridge** in v1. Scenario
  selection happens at URL time; the landing page cannot mutate the
  iframe beyond navigating it.

Every other security header from §15 (HSTS, Referrer-Policy,
Permissions-Policy, nosniff, COOP, CORP) stays as-is on demo.

## Disabled integrations

In demo mode, these adapters are bound to no-op implementations at
process start and the endpoints that would trigger them either
return stub success (so UI stays alive) or `501 Not Implemented in
demo` (so the agent sees a clear error). The behaviour is declared
centrally in `app/config/demo.py` and asserted at boot.

| integration         | demo behaviour                                                        |
|---------------------|------------------------------------------------------------------------|
| SMTP (§10)          | `Mailer` → null-impl; `email_delivery` rows written with `sent_at=now, provider_message_id='demo:<ulid>', suppressed=true`. UI reads "Email sent" but nothing leaves the host. |
| Webhooks (§10)      | Delivery worker short-circuits; `webhook_delivery` rows land as `status='suppressed_demo'`. |
| iCal polling (§04)  | Scheduled job is disabled entirely. Seed fixture pre-populates stays; the "Refresh feed" button returns 501. |
| Passkeys (§03)      | `/auth/passkey/*` return 501. The Login / Profile pages hide the "Add passkey" button when `CREWDAY_DEMO_MODE=1`. |
| Magic links (§03)   | `/auth/magic/send` returns 501. Break-glass codes are not issued. |
| API tokens (§03)    | `POST /api/v1/auth/tokens` returns 501 (both scoped and delegated). The embedded agents get their delegated-token equivalent from the demo cookie's persona binding, bypassing the normal mint path. |
| Payslip payout manifest (§09) | 501 always. The "Download payout manifest" button is hidden on demo. |
| `admin.*` CLI       | Not available from the HTTP surface in prod either; on demo the admin container is simply not run. |
| OCR (`expenses.autofill`) | Capability disabled in demo; receipt upload accepts the file and stores a pre-baked structured result so the UI still demonstrates autofill. |
| Voice transcribe    | Capability disabled; mic button hidden. |
| Daily digest        | Capability disabled. `/digest/preview` renders a pre-baked fixture marked with a "Sample output" chip. |
| Anomaly detection   | Capability disabled. `/anomalies` renders the fixture list with the same chip. |

Routes that would be hidden from the agent's tool surface in a real
workspace (interactive-session-only endpoints, host-CLI-only admin)
continue to be hidden in demo — demo does not widen the agent's reach.

### LLM capability scope

Only the following capabilities run live on a demo workspace:

- `chat.manager`, `chat.employee` — the two embedded agents.
- `chat.compact` — routinely invoked as chat threads grow.
- `chat.detect_language`, `chat.translate` — for the auto-translation
  demo UX.
- `tasks.nl_intake` — natural-language task creation is the hero demo
  feature.

All other capabilities are **disabled** with a "Not available in
demo" inline notice at the call site. In particular, `documents.ocr`
(§11, §21) is **disabled** in demo: every uploaded document goes
through local extractors only. An image-only PDF or photo with no
extractable text records `extraction_status = "unsupported"` and
the agent is told to suggest the visitor try a text PDF — there is
no LLM-vision fallback eating the demo budget. The agent still has
full `search_kb` / `read_doc` / `list_system_docs` access; only
the optional vision rung of the extraction pipeline is silenced.

The live capabilities route to **OpenRouter free-tier models** by
default (suffix `:free`, e.g. `google/gemma-3-27b-it:free`). The
workspace usage budget (§11 "Workspace usage budget") still meters
calls against a tiny dollar cap (`$0.10 / rolling 30 days` by default)
as a fallback against provider pricing changes or accidental routing
to a paid model. Hitting the workspace cap triggers the same hard-
refuse as prod (§11 "At-cap behaviour"); the chat surface shows
"Demo agents are rate-limited — load a fresh scenario to reset".
In that last copy, "load a fresh scenario" means "open a new iframe"
— the visitor clicks away from the current demo and comes back via
the landing page.

A second line of defence lives at the deployment level:
`CREWDAY_DEMO_GLOBAL_DAILY_USD_CAP` (default `$5`) tracks spend
across every demo workspace in the container. Exceeding it pauses
**all** chat capabilities on the demo deployment until UTC midnight
and emits a `demo.global_cap_exceeded` structured log line for
operator monitoring.

## Abuse controls

Demo is an unauthenticated surface; it will be scraped and fuzzed.

- **Mint throttle:** 10 new demo workspaces per IP per hour. Excess
  returns `429`. The counter is shared across scenarios.
- **Mutation rate:** 60 writes per workspace per minute (regardless of
  persona). Matches the §15 API rate limit for an authenticated user.
- **LLM rate:** 10 chat turns per workspace per minute, in addition to
  the workspace budget cap.
- **Upload cap:** 5 MiB per file, 10 files per workspace lifetime, 25
  MiB per IP per day.
- **Payload cap:** every text field has a 32 KiB hard max — demo
  doesn't need a place to paste a book.
- Requests from IPs on the operator's deny-list (`CREWDAY_DEMO_BLOCK_CIDR`)
  are refused at the edge.

None of the above go through the §15 `secret_envelope` or passkey
paths; they are all edge controls.

## Data isolation and PII posture

- **No real PII may enter a demo workspace.** The "Invite user" UI
  sends an email address to a null Mailer; the server records it on
  the `users` row (to keep the UI consistent) but strips the local
  part — `alice@example.com` becomes `a*@e*.com` — before render on
  any subsequent page load. Addresses collected during a demo live
  exactly as long as the workspace does.
- **Agent prompts** on demo use the production redaction layer (§11).
  Agent preferences (§11) are writable and stored on the workspace,
  but pinned to the demo's OpenRouter key — never shared with prod.
- **No upstream telemetry.** Demo deployments explicitly do not emit
  product telemetry; the §16 observability block still ships logs and
  metrics to the operator's local stack if they wire one up.

## What the demo looks like to the agent

When an embedded chat agent runs inside a demo workspace, it cannot
tell the difference from a real workspace beyond the tool-call
results it gets back. Tools are the same; the endpoints that are
disabled in demo return structured 501 errors with a stable shape the
agent can reason about:

```json
{
  "error": "demo_disabled",
  "operation": "payroll.issue",
  "message": "This action is not available in the demo.",
  "docs_ref": "docs/specs/24-demo-mode.md#disabled-integrations"
}
```

The agent is welcome to explain to the visitor that the action isn't
available in the demo — that is, after all, part of the product's
honesty contract.

## Testing

- `tests/demo/test_mint_flow.py` covers first-visit, returning-visit,
  and reseed-after-gc flows against every scenario fixture.
- `tests/demo/test_disabled_integrations.py` enforces the disable list
  by asserting every row in the table above responds as documented.
- `tests/demo/test_abuse_controls.py` exercises the mint throttle,
  mutation rate, and upload cap against a stubbed clock.
- A nightly CI job runs the Playwright "dogfood" suite against a
  one-off demo container, one scenario at a time, and uploads the
  trace for inspection.

## Out of scope for v1

- **Demo → prod conversion.** A "move my demo data to a real
  workspace" button is tempting; it requires email verification,
  passkey registration, and a data copy with PII review. Defer.
- **Multi-language demos.** v1 seeds the fixture in English only; the
  §18 i18n seam still renders the UI chrome in the browser's locale.
- **Shareable demo URLs.** Each visitor has their own cookie; sharing
  a URL does not share the workspace. If that becomes a need, it's a
  separate "read-only snapshot" feature.
- **Demo-specific analytics.** We do not record who poked at what.
  Server access logs plus the regular audit log are enough.
