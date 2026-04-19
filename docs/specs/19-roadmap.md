# 19 — Roadmap

Phased delivery plan. Phases are budget-capped goals, not fixed
sprints. A phase ships when its goals are met and its quality gates
(§17) pass on `main`.

## Phase 0 — Project scaffolding

- Repo layout with **bounded-context subpackages from day 1**
  (§01 "Module boundaries and bounded contexts") —
  `app/domain/<context>/` plus `app/tenancy/`, `app/audit/`,
  `app/events/`, `app/util/`.
- Tooling: `uv`, `ruff`, `mypy --strict`, `pytest`, `playwright`,
  Alembic, Caddy compose, **`import-linter`** wired into the
  pre-commit + CI pipeline (§17).
- `AGENTS.md`, `CLAUDE.md`, skill files, CI pipeline.
- Empty FastAPI app with `/healthz`, `/readyz`, `/version`,
  `/signup/start`, `/signup/verify` stubs returning 501.
- Vite + React + TS strict pipeline; styleguide page; all mock
  routes prefixed under `/w/:slug/*`; SPA served by FastAPI at
  `127.0.0.1:8100`.
- **Capability registry** (§01) scaffolded: `app/capabilities.py`
  probes DB dialect, storage backend, mail provider, LLM client
  at boot; logs the snapshot once; exposes booleans to the rest
  of the code. One codepath, no deploy-mode switch.
- Docker images; recipe A + B baseline plus recipe D topology
  example (Postgres + S3 + Caddy) as a deployment-docs reference.

**Exit:** CI green on empty build with import-linter active;
`crewday admin workspace create --slug myhome` creates a
workspace row and prints a magic link in the dev profile; the
same image boots cleanly on SQLite and on Postgres, logs its
capability snapshot, and passes the cross-tenant regression test
on both.

## Phase 1 — Identity + multi-tenancy

- **Multi-tenant `workspaces` table** with slug, plan, quota_json,
  verification_state (§02); path-prefix addressing `/w/<slug>/...`
  live across web, API, SSE, CLI (§01 §12 §14).
- **SaaS self-serve signup** (§03 "Self-serve signup (SaaS)"):
  `/signup/start`, magic link, `/signup/verify`, passkey
  enrollment, workspace provisioning; rate limits, disposable-
  domain blocklist, tight pre-verification caps (§15).
- **Workspace switcher** and `GET /api/v1/me/workspaces` for
  users with more than one workspace (§14).
- **Postgres RLS policies** installed via migration on every
  workspace-scoped table; `crewday.workspace_id` session
  variable threaded from `WorkspaceContext` (§01 §15).
- **Cross-tenant regression test** green on SQLite and Postgres
  (§17).
- Passkeys (all users), magic links, sessions, `role_grants`.
- API tokens with scopes + per-token audit (tokens scoped to a
  workspace; cross-tenant use returns 404).
- Audit log core, including workspace-level events
  (`workspace.signup.completed`, `workspace.trusted`,
  `workspace.switched`).
- Basic owner/manager UI: profile, passkeys, tokens, workspace
  selector, "Agent usage — N%" placeholder widget.

**Exit:** a visitor provisions a workspace on the SaaS stack
end-to-end; an operator provisions a workspace on self-host via
`crewday admin init`; a user with access to two workspaces switches
between them without losing session; a token scoped to workspace A
used against `/w/<slug-B>/...` returns 404; every action appears in
the audit log with the correct `workspace_id`.

## Phase 2 — Places and people

- Properties, **units within properties**, areas, users, work_roles,
  and the settings cascade UI.
- Property detail owner/manager UI (incl. unit management for multi-
  unit properties).
- User profile and worker-settings management.
- CLI covers all of the above.

**Exit:** full CRUD for the identity+places core; seed demo passes;
CLI generation pipeline produces commands for all Phase 2 endpoints;
`cli-parity` gate green.

## Phase 3 — Tasks and schedules

- Task templates, schedules with RRULE + RDATE/EXDATE, task
  generation worker.
- Task detail and today view for workers.
- Completion, evidence, comments, skip/cancel.
- Assignment algorithm with **availability precedence stack** (leave,
  overrides, holidays, weekly pattern).
- Blackout dates (property closures, user leave).
- **User availability overrides** (self-service add, owner/manager-
  approval reduce).
- **Public holidays with scheduling effects** (`block | allow |
  reduced`).
- **Unified self-service tasks + MY WORK on manager shell** — any role
  may create personal tasks via quick-add on `/today` / `/schedule`
  (`is_personal = true` by default, §06 §15); managers access their
  own work surfaces inside the desktop shell via a new MY WORK nav
  group (§14).

**Exit:** a weekly recurring task is created by the owner/manager,
the scheduler generates occurrences, the assigned worker completes
them with evidence, audit trail is complete. Availability overrides
and holidays correctly affect assignment.

## Phase 4 — Instructions

- Instruction CRUD with versioning, scope resolution, attachments,
  linking.
- Task page renders resolved instructions.

**Exit:** a global house rule, a property SOP, and an area safety note
all surface on the right tasks with the right badges.

## Phase 5 — Stays and iCal

- iCal feed polling per provider (Airbnb, VRBO, Booking, generic),
  with per-unit feed mapping.
- Stay model (unit-scoped) + manager UI + calendar.
- **Stay lifecycle rules** (`before_checkin`, `after_checkout`,
  `during_stay`) + auto-generated **stay task bundles** with
  pull-back scheduling.
- Guest welcome page with tokenized URL, unit-aware info merge, and
  check-out checklist.

**Exit:** an imported Airbnb calendar yields correct stay task bundles
(including pre-arrival prep via `before_checkin` rules) with check-out
checklist visible to the guest via the welcome link; pull-back
scheduling correctly moves pre-arrival tasks when the ideal date is
unavailable.

## Phase 6 — Inventory

- Items, movements, consumption on task completion, reorder worker,
  barcode scanner UI.
- Reports (low stock, burn rate).

**Exit:** a stay task bundle consumes inventory; threshold breaches
produce restock tasks; burn-rate report looks right.

## Phase 6b — Assets, actions & documents

- Asset types catalog (system-seeded + workspace-custom).
- Asset CRUD with condition/status tracking, QR tokens.
- Asset actions with scheduling integration (§06).
- Asset documents (manuals, warranties, invoices) with expiry alerts.
- TCO reporting and replacement forecasts.
- Guest-visible assets on the welcome page (§04).

**Exit:** an owner registers a pool pump from the pre-seeded catalog;
recurring maintenance actions generate tasks via the schedule worker;
completing a filter-clean task updates `last_performed_at`; the daily
digest surfaces an expiring warranty; TCO report sums purchase price,
expenses, and document invoices correctly.

## Phase 7 — Time, payroll, expenses

- **Bookings** as the canonical billable / payable atom (no clock-in
  / clock-out); amend pipeline; per-engagement pay basis;
  per-client cancellation policy; salaried-vs-hourly distinction.
  See §09.
- Pay rules, periods, payslips with PDF.
- Expense claims with LLM-powered receipt autofill.
- CSV exports.

**Exit:** a month closes cleanly: bookings → payslips → approved
expenses → reimbursement included → CSV export.

## Phase 7b — Clients, vendors, work orders (§22)

- **`organization`** as a unified client/supplier entity; properties
  gain `client_org_id`; `work_engagement` (§02) carries
  `engagement_kind` and `supplier_org_id`.
- **Client rate cards** (`client_rate` + `client_user_rate`) and
  booking-completion rate snapshotting via `booking_billing`.
- **`work_order`** with child tasks, **`quote`** with owner/manager
  approval gate, **`vendor_invoice`** with OCR autofill and
  approval gate.
- **Payout destinations for organizations** (same model, different
  owner kind).
- CSV exports: **billable hours by client**, **work-order ledger**.
- CLI parity for everything above.

**Exit:** an agency workspace manages three clients, two payroll
workers, one contractor, and one agency-supplied worker; bookings
at a client property produce `booking_billing` rows; a repair job
flows draft → quoted → accepted → in_progress → completed →
invoiced → paid with agent-submitted drafts and manager approvals;
the billable CSV reconciles against the payroll register.

## Phase 8 — LLM features

- OpenRouter client, model assignment table, redaction layer.
- Natural-language task intake, daily digests, anomaly detection,
  staff chat assistant, agent approval workflow.
- Embedded **owner/manager-side** and **worker-side** chat agents
  (§11) with conversation compaction.
- **Chat gateway seam (§23)** — keep the transport-agnostic runtime,
  message schema, and adapter interfaces ready, but ship **web-only**
  channels in v1. WhatsApp / SMS / Telegram remain deferred even
  though the design reference is written down now.
- **Chat auto-translation** between worker-preferred and workspace-
  default languages on the worker agent (§10, §18). Moved from
  "deferred" into v1.

**Exit:** all LLM features run against Gemma 4 31B via OpenRouter with
bounded budget and audit; an agent driving the CLI experiences
approval-gated actions correctly; a worker writing in their own
language gets the agent replying in kind and the owner/manager seeing
the workspace-default translation with a toggle for the original; the
shared `.desk__agent` web sidebar (both roles, desktop) and its mobile
counterparts (worker `/chat` page, manager bottom-dock drawer) prove
enough value that enabling external transports can be judged on
product evidence instead of speculation; **workspace-level agent usage budget** (§11) ships with a
rolling-30-day meter, a default $5 cap, hard-refuse at-cap behaviour,
and a manager-visible percentage-only widget on `/settings` (no
dollars, no tokens); the cap is adjusted by the operator via
`crewday admin budget set-cap` — no HTTP surface, consistent with the
existing host-CLI-only administrative commands class.

## Phase 9 — PWA and offline

- Manifest + service worker.
- Offline task list, queued completions, photo-then-completion
  ordering.
- **Native wrapper readiness gate** (§14): every authenticated
  route passes a 360 px Playwright viewport check — worker and
  manager shells alike; deep-link routing from cold start;
  passkey ceremony inside an embedded-browser fixture. Wrapper
  contract green before the native-app project can start
  consuming it.

**Exit:** the scripted offline scenario (airplane mode, complete 5
tasks with photos, back online) syncs within 60s with zero loss;
the 360 px sitemap check is green for every authenticated route;
the reserved `/me/push-tokens` endpoint answers `501
push_unavailable` on a v1 deployment with no push backend wired.

## Phase 9b — Demo deployment

- Separate `demo.crew.day` container with its own DB, its own
  OpenRouter key, its own root key, its own demo-cookie signing key.
- `CREWDAY_DEMO_MODE=1` boot guard; refuses to start outside the
  demo URL allowlist.
- Scenario fixtures under `app/fixtures/demo/` (villa-owner,
  rental-manager, housekeeper) seeded per-visitor on first request.
- Signed `__Host-crewday_demo` cookie with CHIPS partitioning for
  cross-origin iframe embedding on the landing page (a separate
  project; not in this repo).
- Per-workspace rolling 30-day cap of **$0.10** and deployment-wide
  daily kill-switch of **$5**; all live capabilities route to
  OpenRouter `:free` models by default.
- `demo_gc` worker every 15 minutes; 24-hour rolling TTL from last
  visitor activity.
- iCal polling, SMTP, webhooks, passkeys, magic links, token
  creation, interactive-session-only endpoints, OCR, voice, daily
  digest, and anomaly detection all disabled on the demo deployment
  (null adapters or pre-baked fixture responses).
- Manager-visible "Agent usage — N%" widget on `/settings` already
  lives in Phase 8; demo exercises it as its hero budget UX.
- Playwright dogfood suite runs nightly against a throw-away demo
  container per scenario.

**Exit:** a visitor lands in an iframe, creates a task via chat,
reassigns it, completes it with photo evidence, and sees the
persona-switcher work across the scenario's seeded users. After
24 h of inactivity, the workspace is purged; a returning visitor with
the same cookie gets a silent reseed. Hitting the workspace cap shows
the at-cap banner; hitting the deployment cap pauses every demo agent
until UTC midnight. See §24.

## Phase 10 — Polish and hardening

- Accessibility audit pass (WCAG 2.2 AA).
- Security review (threat model items from §15).
- Performance tune to §00 targets.
- Docs site under `docs/` (Hugo or MkDocs — TBD).
- Release automation (semantic-release), SBOM, cosign.

**Exit:** v1.0.0 tagged. Public release.

## Beyond v1

Items explicitly deferred, in rough priority order:

1. Additional locales (ES, FR, PT-BR, TL) for UI chrome, instruction
   bodies, and digests. Chat auto-translation for the worker agent
   already ships in v1 (see Phase 8).
2. Local LLM provider (Ollama) adapter.
3. **Paid plans + Stripe billing.** v1 ships the plan/quota seam
   (§00 G13, §02 `workspace.plan` / `quota_json`) and the free
   tier only. Beyond v1: plan catalog, Stripe subscriptions,
   proration, dunning, tax handling per region, and tenant self-
   serve plan changes. The multi-tenancy platform itself already
   shipped in Phase 1 (§01 §03 §15).
4. **Native mobile app(s).** Separate project(s) — one cross-platform
   shell, or one per platform (Android + iOS) if the native team
   prefers. This repo already ships the wrapper contract (§14
   "Native wrapper readiness"); the native work is the shell, OS
   integration, and app-store plumbing. The first product motivator
   is **delivering agent messages via OS push** (§10 tier 2). When
   the native project goes live it also flips `POST /me/push-tokens`
   from `501 push_unavailable` to active and provisions deployment-
   level FCM/APNS credentials. Everything on the web platform side
   is already in place at that point.
5. QuickBooks / Xero accounting export (beyond CSV).
6. OIDC for owners/managers.
7. Owner-only dashboard (when a second-party manages on behalf of an
   owner). `organization.portal_user_id` (§22) already grants a
   `role_grants(grant_role='client')` row for client-facing read
   access.
8. **Client invoice PDFs + ageing / dunning** — full counterpart to
   the payslip PDF flow: render `client_invoice.pdf` from a
   template, run a `draft → issued → paid → voided` state machine,
   attach dunning emails. The v1 agency scope ships rate capture
   and CSV only (§22 "Out of scope"); this is the next increment.
9. **Split-billing a single property across multiple clients** — a
   co-owned villa where two families each pay half. Requires a
   `property_billing_split` mapping and a rewrite of rate
   resolution. Deferred until a real user asks.
10. Realtime chat between humans (peer presence, human-to-human
    typing indicators). v1 uses SSE for task-state freshness and for
    the agent-turn indicator (§11 "Agent turn lifecycle"); true
    peer-to-peer presence is separate.
11. Integrated guest messaging (Airbnb-style threads).
12. External chat-gateway adapters beyond the in-app web surfaces:
    **WhatsApp** first, then SMS, Telegram, push, Slack, Matrix.
13. **SMS inline approvals.** When SMS is eventually enabled, free-
    text reply parsing across concurrent pending approvals is
    ambiguous. Revisit only when an adapter gains an interactive
    primitive or a disambiguation scheme proves reliable enough.
14. **Booking presence enhancements.** A one-tap "Arrived" beacon
    on the worker PWA that stamps `arrived_at` on the booking
    (purely informational, never affects pay/bill); door-lock or
    NFC integration for stronger "she was on premises" proof; a
    per-day signable-PDF export for jurisdictions that require
    worker signatures on time records. None ship in v1; all are
    additive once a real customer asks. See §09 "Out of scope".
15. **Per-property cancellation override.** A single client owning
    multiple villas with varying cancellation policy. v1 keeps it
    per-client (§22); the property layer in the cascade is added
    when a real customer needs it. See §09 "Cancellation policy".
16. **Embedded marketplace (§25).** Deployment-scope discovery
    layer where agencies publish `marketplace_listing` rows (with
    GeoJSON service areas) and clients post `service_request`
    rows (with a place of intervention). Accepted matches
    auto-create a §22 `property_workspace_invite` so dispatching
    reuses the existing agency↔client flow; the platform takes a
    configurable % fee captured in an append-only
    `platform_fee_event` ledger. Gated by the deployment setting
    `settings.marketplace_enabled`, default off. v1 ships **no**
    marketplace routes, entities, or UI — only the design
    reservation in §25 and the capability/setting seams in §01.
    When implemented, the migration is strictly additive and
    hooks into the existing §10 `booking_billing.resolved` /
    `vendor_invoice.approved` webhook events.
