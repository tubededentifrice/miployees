# 00 — Overview

## Vision

**miployees** is a self-hosted workspace operations platform: one owner,
one or more properties, several employees, and many tasks repeating across
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
  Every user-editable row carries `workspace_id`. The v1 deployment
  ships with a **single workspace** seeded at first boot, but the
  schema, auth, and API surface are already multi-tenant-ready so that
  future releases can host more than one workspace per deployment
  without a data migration. "Workspace" replaces the v0 term
  `household` everywhere in the schema, API, and UI; see §20.
- **Villa (property).** A managed physical place. A villa is a
  **multi-belonging unit**: the same villa can belong to more than one
  workspace (e.g. a rental manager and the owning family both oversee
  the same house from their own workspaces). Employees can therefore
  work across villas that live in different workspaces, and an
  employee's workspace membership is the set of workspaces reachable
  through their assigned villas plus any explicit direct membership.
  See §02 for the junction tables.
- **Agent-first guarantee.** Every user-facing action in miployees is
  also exposed as a **CLI command** (host CLI or the embedded REST
  tool surface) — there is no human-only verb. An LLM agent driving
  the manager-side or employee-side chat can do anything a human can
  do in the same UI, subject to the approval gates in §11. The UI is a
  shell around those commands, not a separate capability. See §11 for
  the invariant, §13 for the CLI catalog.

## Personas

### Primary

- **Manager (owner / head of house).** Desktop most of the time, phone
  sometimes. Creates properties, hires and terminates staff, defines
  tasks, approves expenses and payslips, reviews digests. There can be
  more than one manager; all managers are peers.
- **Employee (staff).** Phone-first. Sees today's list, ticks things
  done, optionally attaches a photo, logs hours, reports an issue,
  submits an expense receipt, reads comments from the manager.
- **Agent (LLM operator).** Runs on a schedule or in response to events.
  Reads state via REST, writes via REST (with approval gating on
  high-impact actions). The CLI is its ergonomic entry point.

### Secondary

- **Guest (STR occupant).** No login. Receives a tokenized link to a
  welcome page for their stay (wifi, house rules, check-out checklist,
  emergency contacts).
- **Accountant / payroll provider.** Never logs into miployees directly;
  receives CSV exports (timesheets, payslips, expense ledger) via email
  or scheduled webhook.

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

- **G1.** Capture properties, areas, employees, roles, tasks, schedules,
  instructions, inventory, time, and expenses in one coherent model.
- **G2.** Passkey-only login for humans; API tokens for agents. No
  passwords. Ever.
- **G3.** Mobile-first PWA for employees with **offline** task lists and
  queued completions.
- **G4.** iCal import from at least Airbnb, VRBO, and Booking.com, with
  automatic turnover task generation per property.
- **G5.** OpenAPI 3.1 over every feature, and a CLI that is a thin client
  to that API. Agent workflows are first-class, not bolted on.
- **G6.** Built-in LLM features: receipt OCR for expenses, natural-
  language task intake, daily digest, staff chat assistant. Default
  model via OpenRouter; per-capability model assignment table.
- **G7.** Self-hosted, two supported deployments: **single-container
  SQLite** (minimal) and **docker-compose full-stack** (Postgres +
  MinIO + Caddy).
- **G8.** No binding to public interfaces by default; localhost or
  tailscale only unless explicitly overridden.
- **G9.** Append-only audit log for every mutation made by a human or
  agent.
- **G10.** Backup/restore is a single documented command in either
  deployment.

## Non-goals (v1)

- **N1. Multi-tenant SaaS.** One deployment = one workspace in v1.
  Tenancy may be added later; the schema already names every seam
  `workspace_id` (§02) but no multi-workspace enforcement is built.
- **N2. Tax & statutory HR.** We compute gross pay; taxes, social
  contributions, and statutory leave rules are out of scope. Export CSV
  to your accountant.
- **N3. Payments.** No credit-card acceptance, no payroll bank runs.
  Expenses are recorded; reimbursement happens externally.
- **N4. Native mobile apps.** PWA only. If we need capabilities a PWA
  cannot offer (geofencing, background sync beyond Periodic Background
  Sync, barcode scanning hardware APIs), we re-evaluate then.
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

- **Operational.** A workspace with 3 properties, 5 employees, 30
  weekly recurring tasks, and 2 STR calendars can be set up end-to-end
  (from empty DB to first completed task) in **under 60 minutes** by a
  non-technical manager following the Getting Started guide.
- **Performance.** Employee PWA first paint under 1.5s on a 2019 mid-
  range Android over 4G; a 50-row task list renders under 300ms
  server-side on SQLite with 100k tasks and 100k completions in history.
- **Reliability.** Offline completions queued on a phone sync within 60
  seconds of reconnect with zero loss across 10,000 synthesized events.
- **Agent UX.** An agent can go from zero context to completing a
  meaningful workflow (e.g. "move all of Maria's Monday tasks this week
  to Ana") in at most 4 tool calls using the CLI.

## Constraints

- **Language.** Python 3.12+ for server and CLI. TypeScript only where
  necessary for the service worker; avoid it elsewhere.
- **Stack.** FastAPI + HTMX + Tailwind. SQLite default, Postgres 15+
  supported. See §01.
- **Hosting.** The binary/image must run on a $5/month VPS with 1 vCPU
  and 1 GB RAM for a 5-employee workspace without swapping. Compose
  deployments assume 2 vCPU / 2 GB.
- **Privacy.** No PII leaves the deployment without explicit manager
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
