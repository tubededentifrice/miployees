# 19 — Roadmap

Phased delivery plan. Phases are budget-capped goals, not fixed
sprints. A phase ships when its goals are met and its quality gates
(§17) pass on `main`.

## Phase 0 — Project scaffolding

- Repo layout (§01), tooling (`uv`, `ruff`, `mypy`, `pytest`,
  `playwright`, Alembic, Caddy compose).
- `AGENTS.md`, `CLAUDE.md`, skill files, CI pipeline.
- Empty FastAPI app with `/healthz`, `/readyz`, `/version`.
- Tailwind + HTMX pipeline; styleguide page.
- Docker images; single + compose recipes baseline.

**Exit:** CI green on empty build; `miployees admin init` creates a
household row and prints a magic link in the dev profile.

## Phase 1 — Identity

- Passkeys (managers + employees), magic links, sessions.
- API tokens with scopes + per-token audit.
- Audit log core.
- Basic manager UI: profile, passkeys, tokens.

**Exit:** a manager and an employee can be enrolled end-to-end on
devices; a token can drive the API; every action appears in the audit
log.

## Phase 2 — Places and people

- Properties, areas, employees, roles, capabilities.
- Property detail manager UI.
- Employee profile and capability management.
- CLI covers all of the above.

**Exit:** full CRUD for the identity+places core; seed demo passes.

## Phase 3 — Tasks and schedules

- Task templates, schedules with RRULE + RDATE/EXDATE, task
  generation worker.
- Task detail and today view for employees.
- Completion, evidence, comments, skip/cancel.
- Assignment algorithm.
- Blackout dates (property closures, employee leave).

**Exit:** a weekly recurring task is created by the manager, the
worker generates occurrences, the assigned employee completes them
with evidence, audit trail is complete.

## Phase 4 — Instructions

- Instruction CRUD with versioning, scope resolution, attachments,
  linking.
- Task page renders resolved instructions.

**Exit:** a global house rule, a property SOP, and an area safety note
all surface on the right tasks with the right badges.

## Phase 5 — Stays and iCal

- iCal feed polling per provider (Airbnb, VRBO, Booking, generic).
- Stay model + manager UI + calendar.
- Turnover templates + auto-generated turnover bundles.
- Guest welcome page with tokenized URL and check-out checklist.

**Exit:** an imported Airbnb calendar yields correct turnover bundles
with check-out-checklist visible to the guest via the welcome link.

## Phase 6 — Inventory

- Items, movements, consumption on task completion, reorder worker,
  barcode scanner UI.
- Reports (low stock, burn rate).

**Exit:** a turnover bundle consumes inventory; threshold breaches
produce restock tasks; burn-rate report looks right.

## Phase 7 — Time, payroll, expenses

- Shifts with clock-in/out + geofence capability.
- Pay rules, periods, payslips with PDF.
- Expense claims with LLM-powered receipt autofill.
- CSV exports.

**Exit:** a month closes cleanly: shifts → payslips → approved
expenses → reimbursement included → CSV export.

## Phase 8 — LLM features

- OpenRouter client, model assignment table, redaction layer.
- Natural-language task intake, daily digests, anomaly detection,
  staff chat assistant, agent approval workflow.

**Exit:** all capabilities run against Gemma 4 31B via OpenRouter with
bounded budget and audit; an agent driving the CLI experiences
approval-gated actions correctly.

## Phase 9 — PWA and offline

- Manifest + service worker.
- Offline task list, queued completions, photo-then-completion
  ordering.

**Exit:** the scripted offline scenario (airplane mode, complete 5
tasks with photos, back online) syncs within 60s with zero loss.

## Phase 10 — Polish and hardening

- Accessibility audit pass (WCAG 2.2 AA).
- Security review (threat model items from §15).
- Performance tune to §00 targets.
- Docs site under `docs/` (Hugo or MkDocs — TBD).
- Release automation (semantic-release), SBOM, cosign.

**Exit:** v1.0.0 tagged. Public release.

## Beyond v1

Items explicitly deferred, in rough priority order:

1. Additional locales (ES, FR, PT-BR, TL) and the multilingual content
   model for tasks and instructions.
2. SMS / WhatsApp channels.
3. Local LLM provider (Ollama) adapter.
4. Multi-tenant SaaS mode (including SaaS lockout recovery that does
   not require host shell access — see §03).
5. Native mobile apps (only if PWA limitations become painful).
6. QuickBooks / Xero accounting export (beyond CSV).
7. OIDC for managers.
8. Owner-only dashboard (when a second-party manages on behalf of an
   owner).
9. Realtime chat.
10. Integrated guest messaging (Airbnb-style threads).
