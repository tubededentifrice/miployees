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
workspace row and prints a magic link in the dev profile.

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
- Embedded **manager-side** and **employee-side** chat agents (§11)
  with conversation compaction.
- **WhatsApp (agent-mediated) + SMS fallback** for agent-originated
  outbound reach-out (§10). Moved from "Beyond v1" into v1.
- **Chat auto-translation** between employee-preferred and workspace-
  default languages on the employee agent (§10, §18). Moved from
  "deferred" into v1.

**Exit:** all capabilities run against Gemma 4 31B via OpenRouter with
bounded budget and audit; an agent driving the CLI experiences
approval-gated actions correctly; an employee writing in their own
language gets the agent replying in kind and the manager seeing the
workspace-default translation with a toggle for the original;
agent-originated WhatsApp reach-out respects quiet hours and per-
employee daily caps.

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

1. Additional locales (ES, FR, PT-BR, TL) for UI chrome, instruction
   bodies, and digests. Chat auto-translation for the employee
   agent already ships in v1 (see Phase 8).
2. Local LLM provider (Ollama) adapter.
3. **True multi-tenancy** — more than one workspace per deployment,
   with a workspace-switcher UI and workspace-admin roles. The
   **schema is already ready** (every user-editable row carries
   `workspace_id`; junction tables `villa_workspace` and
   `employee_workspace` exist; RLS seam is `workspace_id` per §15),
   so lifting the single-workspace lock is a policy + auth change,
   not a data migration. Bundled with SaaS lockout recovery that
   does not require host shell access — see §03.
4. Native mobile apps (only if PWA limitations become painful).
5. QuickBooks / Xero accounting export (beyond CSV).
6. OIDC for managers.
7. Owner-only dashboard (when a second-party manages on behalf of an
   owner).
8. Realtime chat (presence, typing indicators) — v1 uses SSE for
   task-state freshness; true realtime is separate.
9. Integrated guest messaging (Airbnb-style threads).
10. Additional outbound channels beyond email / WhatsApp / SMS
    (push, Slack, Matrix).
