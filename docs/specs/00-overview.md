# 00 — Overview

## Vision

**crew.day** is a self-hosted workspace operations platform: one owner,
one or more properties, several workers, and many tasks repeating across
days, weeks, and seasons. It is designed so that the day-to-day operator is
an LLM agent and the humans are free to live in their house instead of
managing it.

The working mental model is **a hotel of one to fifty rooms, minus the
front desk**: guest stays flow in from Airbnb/VRBO; turnovers, cleanings,
and check-in prep auto-generate; the cook sees what to prepare tomorrow;
the driver sees tomorrow's airport run; the head of house sees everything.

## Glossary orientation

- **Workspace.** The tenancy boundary. One workspace = one employer
  entity (a family, an estate, a small property-management outfit).
  Every user-editable row carries `workspace_id`. A single v1
  deployment holds many workspaces simultaneously: the managed SaaS
  at `crew.day` provisions one per self-serve signup (§03, §15);
  self-hosted deployments default to one (bootstrapped by
  `crewday admin init`) but may run many when backed by Postgres.
  Every authenticated URL on every deployment lives under
  `<host>/w/<slug>/...`. "Workspace" replaces the v0 term
  `household` everywhere in the schema, API, and UI; see §20.
- **Villa (property).** A managed physical place. A villa is a
  **multi-belonging unit**: the same villa can belong to more than one
  workspace (e.g. a rental manager and the owning family both oversee
  the same house from their own workspaces). Users can therefore work
  across villas that live in different workspaces, and a user's
  workspace membership is the set of workspaces reachable through
  their assigned villas plus any explicit direct membership. See §02
  for the junction tables (`user_workspace`, `property_workspace`).
- **Agent-first guarantee.** Every user-facing action in crew.day is
  also exposed as a **CLI command** (host CLI or the embedded REST
  tool surface) — there is no human-only verb. An LLM agent driving
  the owner/manager-side or worker-side chat acts with the **full
  authority of its delegating user** — same permissions, same audit
  identity — via a delegated token (§03). Every action is attributed
  to the user in the audit log while being clearly flagged as
  agent-executed. High-impact actions still require explicit human
  approval (§11). The UI is a shell around those commands, not a
  separate capability. See §11 for the invariant, §13 for the CLI
  catalog.
- **Site surface.** The managed SaaS at `crew.day` ships three
  independently-deployed surfaces:
  `app.crew.day` (the product itself, specified here),
  `demo.crew.day` (the app with `CREWDAY_DEMO_MODE=1`, §24), and
  `crew.day` (the marketing landing + agent-clustered suggestion
  box, specified under [`docs/specs-site/`](../specs-site/)).
  Self-host deployments usually run only the app; the site is
  optional and never required for the product to function.

## Personas

### Primary

- **Owner / Manager (head of house).** A `user` with `grant_role =
  manager`, plus — for the owner case — membership in the workspace's
  system `owners` permission group (the governance anchor, see §05).
  Desktop most of the time, phone sometimes. Creates properties,
  invites and removes staff, defines tasks, approves expenses and
  payslips, reviews digests. There can be more than one owner or
  manager; owners have workspace-wide authority (via the `owners`
  group), managers may be scoped to specific properties via
  `role_grants.scope_property_id`. The legacy `grant_role = 'owner'`
  value was retired — see §20 "Owner" and §05 for the replacement
  model.
- **Worker (staff).** A `user` with `grant_role = worker`. Phone-
  first. Sees today's list, ticks things done, optionally attaches a
  photo, logs hours, reports an issue, submits an expense receipt,
  reads and writes task comments.
- **Agent (LLM operator).** Runs on a schedule or in response to
  events. Acts with the full authority of its delegating user — same
  permissions, same audit identity (with agent-execution flag). High-
  impact actions still require explicit human approval. The CLI is its
  ergonomic entry point.

### Secondary

- **Client.** A `user` with `grant_role = client` granted at property
  scope. Portal login showing occupancy, billable hours, and invoices
  for that property only. Cannot create or manage tasks.
- **Guest (STR occupant).** No login. Receives a tokenized link to a
  welcome page for their stay (wifi, house rules, check-out checklist,
  emergency contacts).
- **Accountant / payroll provider.** Never logs into crew.day
  directly; receives CSV exports (timesheets, payslips, expense
  ledger) via email or scheduled webhook.

## Primary use cases

1. **Everyday domestic staff management.** "Maria cleans Villa Sud
   Mondays and Thursdays; Arun drives to the airport on demand; Ben
   handles the pool every Saturday morning."
2. **Vacation rentals / Airbnb portfolio.** "When a guest checks out of
   Apt 3B at noon, the cleaner has a 2-hour turnover block with
   checklist; when the next stay begins at 16:00, inventory must be
   restocked."
3. **Seasonal / occasional operations.** "Open the pool on May 1, close
   on September 30. Prep the ski chalet every December; shut it every
   April."
4. **Ad-hoc requests.** "We have guests this weekend; schedule an extra
   deep clean and make sure flowers are in the entryway."
5. **Agent-driven operations.** An agent reads last week's completions,
   notices Ben missed two Saturdays, drafts a message to Ben, and queues
   a reassignment to the backup gardener pending manager approval.

## Goals (v1)

- **G1.** Capture properties, areas, users, work roles, tasks, schedules,
  instructions, inventory, time, and expenses in one coherent model.
- **G2.** Passkey-only login for humans; API tokens for agents. No
  passwords. Ever.
- **G3.** Mobile-first PWA for workers with **offline** task lists and
  queued completions.
- **G4.** iCal import from at least Airbnb, VRBO, and Booking.com, with
  automatic turnover task generation per property.
- **G5.** OpenAPI 3.1 over every feature, and a CLI that is a thin client
  to that API. Agent workflows are first-class, not bolted on.
- **G6.** Built-in LLM features: receipt OCR for expenses, natural-
  language task intake, daily digest, staff chat assistant. Default
  model via OpenRouter; per-capability model assignment table.
- **G7.** **One codebase, many deployments.** The same image runs
  as a single-container SQLite home install, a self-host compose
  stack, a managed multi-tenant SaaS, or anything in between. No
  deployment-mode switches, no fork codepaths. Where the backend
  genuinely can't support a feature (SQLite → no Postgres RLS, no
  `tsvector` full-text), the **capability registry** (§01) auto-
  disables it and the UI/API surface it as unavailable; operator
  preferences (e.g. whether signup is open) are runtime settings,
  not modes. See §01 "Capability registry", §16.
- **G8.** No binding to public interfaces by default; localhost or
  tailscale only unless explicitly overridden.
- **G9.** Append-only audit log for every mutation made by a human or
  agent.
- **G10.** Backup/restore is a single documented command in either
  deployment.
- **G11.** **Multi-tenant platform from day 1.** A single deployment
  holds many `workspace` rows simultaneously, on any supported
  backend. The managed SaaS instance at `crew.day`, a self-host
  compose stack, and a single-container SQLite install all run the
  same code and all support many workspaces. Workspace addressing
  is path-based (`<host>/w/<slug>/...`) everywhere. Isolation is
  enforced at the application layer by the `workspace_id` filter
  on every repository call, and — where the backend supports it —
  at the DB layer by Postgres RLS (capability `features.rls`, see
  §01). See §01 "Multi-tenancy runtime", §15, §16.
- **G12.** **Open self-serve SaaS signup.** The managed SaaS lets
  any visitor provision a workspace: email → magic link → passkey
  enrollment → workspace slug → ready. Rate-limited per IP/email,
  disposable-domain blocklist, tight usage caps until first human
  verification, abuse mitigations per §15. See §03.
- **G13.** **Plan + quota seams without payments.** Every
  `workspace` carries a `plan` and a quota blob (user count,
  property count, LLM budget, storage bytes); enforcement is live
  from day 1 on the free tier's caps. Payment processing is
  explicitly out of scope for v1 (see N1).
- **G14.** **Clean-architecture code structure from day 1.** Every
  bounded context (identity, places, tasks, stays, inventory,
  assets, time, payroll, expenses, billing, messaging, instructions,
  llm) is its own subpackage with a narrow public surface; sibling
  contexts interact only through that surface or through typed
  in-process events. An `import-linter` CI gate blocks cross-context
  submodule imports on every PR. This exists so the codebase can
  scale to many humans and agents in parallel, and so any single
  context can later be extracted as a separate service with only
  its adapter rewritten. See §01 "Module boundaries and bounded
  contexts" and §17.

## Non-goals (v1)

- **N1. Paid plans, metered billing, payments.** v1 ships the
  *seams* for plans, quotas, and usage meters (see G11 below) but
  no Stripe, no invoice PDFs, no dunning. Every SaaS workspace is
  on a single free tier with hard caps; paid tiers come later
  (§19 Beyond v1).
- **N2. Tax & statutory HR.** We compute gross pay; taxes, social
  contributions, and statutory leave rules are out of scope. Export CSV
  to your accountant.
- **N3. Payments.** No credit-card acceptance, no payroll bank runs.
  Expenses are recorded; reimbursement happens externally.
- **N4. Native mobile app code.** The native mobile app is a
  **separate project** and is out of scope for this repo. This repo
  owns the web platform — PWA, responsive design, passkey RP, deep-
  linkable URLs, the push-token registration API, and the agent-
  message notification fan-out. A future native shell
  (Capacitor / TWA / WKWebView / whatever the native team picks)
  consumes these as a black-box contract; see §14 "Native wrapper
  readiness". One native app per deployment covers every workspace
  the user belongs to (in-app workspace switcher, same as the web
  SPA); one-app-per-workspace is explicitly not the model.
- **N5. Realtime chat.** Task comments + email, no presence indicators
  or message-seen receipts. Those belong in WhatsApp, not in the
  operations system.
- **N6. Guest booking channel management.** We import reservations; we
  do not sell them or sync availability back to STR platforms.
- **N7. Integrated accounting.** CSV export is the contract with
  accounting systems.
- **N8. Local LLMs in v1.** OpenRouter only. `docs/specs/11` documents
  the seam for a local provider but it is out of scope for v1.

## Success criteria

- **Operational.** A workspace with 3 properties, 5 workers, 30
  weekly recurring tasks, and 2 STR calendars can be set up end-to-end
  (from empty DB to first completed task) in **under 60 minutes** by a
  non-technical owner following the Getting Started guide.
- **Performance.** Worker PWA first paint under 1.5s on a 2019 mid-
  range Android over 4G; a 50-row task list renders under 300ms
  server-side on SQLite with 100k tasks and 100k completions in history.
- **Reliability.** Offline completions queued on a phone sync within 60
  seconds of reconnect with zero loss across 10,000 synthesized events.
- **Agent UX.** An agent can go from zero context to completing a
  meaningful workflow (e.g. "move all of Maria's Monday tasks this week
  to Ana") in at most 4 tool calls using the CLI.

## Constraints

- **Language.** Python 3.14+ for server and CLI. TypeScript strict for
  the React SPA (`mocks/web/`).
- **Stack.** FastAPI + React (Vite, TypeScript strict) + hand-rolled CSS
  design system. No Tailwind, no Alpine, no Vue. SQLite default,
  Postgres 15+ supported. See §01.
- **Hosting.** The binary/image must run on a $5/month VPS with 1 vCPU
  and 1 GB RAM for a 5-employee workspace without swapping. Compose
  deployments assume 2 vCPU / 2 GB.
- **Privacy.** No PII leaves the deployment without explicit owner
  consent per capability (see §11 §15).

## Licensing

Apache-2.0 tentative. The spec contains nothing that would preclude
alternatives (AGPL-3.0, BUSL, Elastic-2.0); revisit before the first
tagged release.

## Document conventions

- **MUST / SHOULD / MAY** follow RFC 2119.
- All times in this spec are **UTC** unless otherwise noted.
- Diagrams are ASCII where possible, Mermaid where clarity demands.
- When an entity is mentioned for the first time in a document, it is
  defined or linked to its canonical definition in §02 (domain model).
- Cross-references use `§NN` (section) and `§NN-title` (title slug).
