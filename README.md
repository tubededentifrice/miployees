# crewday

A self-hosted, agent-first system for managing household and short-term-rental
staff across one or more properties: maids, cooks, drivers, gardeners,
handymen, nannies, pool technicians, and the like.

Think **hotel operations for a single owner**: properties, rooms, guests,
staff roles, task schedules, standing instructions, inventories, timesheets,
payslips — all accessible to both humans (via a mobile-first web app) and LLM
agents (via a documented REST API and a thin CLI that wraps it).

> **Status:** pre-implementation. This repository currently contains
> **specifications only**. See [`docs/specs/`](docs/specs/) for the full
> design. No code has been written yet.

## Why

Running a household with staff across multiple properties is operationally
the same problem as running a small hotel, but the tooling is fifteen years
behind. Existing hotel PMS software assumes a hotel's org chart, a front desk,
and a commercial chart of accounts. Household managers end up juggling
WhatsApp groups, paper checklists, and spreadsheets.

crewday starts from a different premise: **the operator is an LLM agent**,
and the humans (owner, head of house, staff) interact through the surfaces
that are natural to them — a phone for the cleaner, an email digest for the
owner, a REST API for the agent.

## Core design choices

- **Agent-first.** Every feature is exposed over REST/OpenAPI before it is
  exposed in the UI. The CLI is a thin client over the REST API, so any
  agent can drive the whole system from any machine.
- **Passkeys only** for human login. No passwords. Managers bootstrap
  employees via emailed magic links that register a WebAuthn credential on
  the employee's phone.
- **Self-hosted, single-household, multi-property.** One deployment manages
  one owner's portfolio of properties (home, vacation home, rentals). No
  multi-tenant plumbing.
- **FastAPI + React SPA + SQLite/Postgres.** FastAPI on the server, a
  Vite + React + TypeScript strict SPA on the client (served by the
  same FastAPI process from `dist/`), with SQLite by default and
  Postgres for larger deployments. The mocks app is split as
  `mocks/app/` (JSON API + SPA fallback) and `mocks/web/` (SPA).
- **LLM-native.** Receipt OCR, natural-language task intake, daily digests,
  and a staff chat assistant all ship in v1. Default model is
  `google/gemma-4-31b-it` via OpenRouter, with a per-capability model
  assignment table so any model can be swapped in for any job.
- **PWA with offline support.** Staff open the site on their phone, add it
  to the home screen, and today's tasks remain tickable without connection.

## How it's organized

The spec is split across focused documents. Start at
[`docs/specs/00-overview.md`](docs/specs/00-overview.md) for the full tour.

| # | Document | Purpose |
|---|----------|---------|
| 00 | [`overview.md`](docs/specs/00-overview.md) | Vision, personas, goals, non-goals |
| 01 | [`architecture.md`](docs/specs/01-architecture.md) | Stack, components, repo layout |
| 02 | [`domain-model.md`](docs/specs/02-domain-model.md) | Entities, ERD, ID strategy |
| 03 | [`auth-and-tokens.md`](docs/specs/03-auth-and-tokens.md) | Passkeys, magic links, API tokens |
| 04 | [`properties-and-stays.md`](docs/specs/04-properties-and-stays.md) | Properties, areas, iCal, guests |
| 05 | [`employees-and-roles.md`](docs/specs/05-employees-and-roles.md) | Staff model, roles, capabilities |
| 06 | [`tasks-and-scheduling.md`](docs/specs/06-tasks-and-scheduling.md) | Task model, RRULE, evidence |
| 07 | [`instructions-kb.md`](docs/specs/07-instructions-kb.md) | Global / house / room SOPs |
| 08 | [`inventory.md`](docs/specs/08-inventory.md) | Supplies, linens, reorder |
| 09 | [`time-payroll-expenses.md`](docs/specs/09-time-payroll-expenses.md) | Clock-in, pay rules, expense claims |
| 10 | [`messaging-notifications.md`](docs/specs/10-messaging-notifications.md) | Comments, issues, email, webhooks |
| 11 | [`llm-and-agents.md`](docs/specs/11-llm-and-agents.md) | OpenRouter, model assignment, audit |
| 12 | [`rest-api.md`](docs/specs/12-rest-api.md) | OpenAPI surface, conventions |
| 13 | [`cli.md`](docs/specs/13-cli.md) | `crewday` CLI for agents |
| 14 | [`web-frontend.md`](docs/specs/14-web-frontend.md) | React SPA, PWA, offline, a11y |
| 15 | [`security-privacy.md`](docs/specs/15-security-privacy.md) | Threat model, secrets, GDPR |
| 16 | [`deployment-operations.md`](docs/specs/16-deployment-operations.md) | Packaging, backups, observability |
| 17 | [`testing-quality.md`](docs/specs/17-testing-quality.md) | Test strategy, CI gates |
| 18 | [`i18n.md`](docs/specs/18-i18n.md) | Deferred locales, seam design |
| 19 | [`roadmap.md`](docs/specs/19-roadmap.md) | Phased delivery plan |
| 20 | [`glossary.md`](docs/specs/20-glossary.md) | Terms used across the spec |

For agent-development conventions (how to work on this codebase), see
[`AGENTS.md`](AGENTS.md).

## Non-goals

Explicitly **not** in scope for v1 (see `docs/specs/00-overview.md` for the
full list and rationale):

- Multi-tenancy / SaaS billing
- Tax calculation, statutory filings, or legal HR compliance
- Guest booking / payment acceptance (we import reservations; we do not sell them)
- Integrated accounting (QuickBooks, Xero) — CSV export only
- Native mobile apps
- Real-time two-way chat between managers and employees (use comments + email)

## License

Apache-2.0 (TBD — see
[`docs/specs/00-overview.md`](docs/specs/00-overview.md) §"Licensing").
