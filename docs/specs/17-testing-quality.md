# 17 — Testing and quality gates

## Test pyramid

| layer                | tool                      | runs on               | budget per run |
|----------------------|---------------------------|-----------------------|----------------|
| static (type/lint)   | `ruff`, `mypy --strict`   | every commit          | < 15s          |
| unit                 | `pytest`                  | every PR              | < 60s          |
| frontend unit        | `vitest` + `@testing-library/react` + `msw` | every PR | < 60s |
| integration (DB)     | `pytest` + testcontainers | every PR              | < 5min         |
| API contract         | `schemathesis` against `/openapi.json` | every PR | < 5min     |
| browser e2e          | `playwright` (headless)   | every PR              | < 10min        |
| visual regression    | `playwright` + `pixelmatch` | every PR            | < 5min         |
| load                 | `locust`                  | nightly               | 30min          |
| LLM regression       | `pytest` + fixtures       | on-demand + nightly   | varies         |
| security             | `osv-scanner`, `bandit`   | every PR              | < 2min         |
| CLI parity           | `scripts/check_cli_parity.py` | every PR         | < 10s          |

## Unit

- Pure domain, no network, no real DB. Fakes for `Clock`, `Storage`,
  `Mailer`, `LLMClient`.
- Every schedule / RRULE edge case has a parametrized test.
- Money math has property-based tests (`hypothesis`).
- Policy helpers (capability resolution, assignment algorithm, approval
  detection) get exhaustive truth-table tests.

## Integration

- Real SQLite file and a spun-up Postgres via `testcontainers`. Test
  matrix runs everything on both.
- A single `conftest.py` fixture gives each test its own DB.
- Migrations run once per worker; snapshots + truncate between tests
  for speed.
- Real filesystem Storage in `tmp_path`.
- Real Jinja template render tests for every email template.

## API contract

- `schemathesis run --checks all ./openapi.json` against a live dev
  server seeded with fixture data.
- Custom hooks enforce: `Authorization` present on non-public paths,
  idempotency honored, `ETag` round-trip.
- Breaking-change detection: `openapi-diff` between the current branch
  and `main` runs in CI; a breaking diff fails unless PR body contains
  `ALLOW-BREAKING-API`.

## End-to-end

- Playwright, Python bindings. Headed only locally; headless in CI.
- Covered journeys (minimum for GA):
  1. Install + first-boot owner enrollment.
  2. Add property, area, work_role, user; invite user; user enrolls
     passkey and completes first task.
  3. iCal feed imports stays; turnover tasks auto-generate; worker
     completes with photo evidence; guest opens welcome link.
  4. Expense submission with receipt; autofill population; approval;
     payslip issuance with reimbursement.
  5. Agent drives a task lifecycle via the CLI; action requiring
     approval is queued and approved.
- Passkey ceremonies are exercised via
  [WebAuthn virtual authenticator](https://playwright.dev/docs/api/
  class-cdpsession) in both Chromium and WebKit.

## Frontend

### Unit

- **vitest** + **@testing-library/react** for component and hook tests.
- **msw** (Mock Service Worker) intercepts `fetch` at the network level
  for request-level mocking in unit and integration tests — no actual
  HTTP traffic, no stubs in application code.

### Visual regression

- **Playwright** + **pixelmatch** for pixel-level comparison.
- `/styleguide` (dev + staging only) is the visual-regression baseline.
  A screenshot diff > **0.1%** on `/styleguide` fails the check.
  All other routes fail on > **0.5%** diff.
- Baselines are committed and updated intentionally; CI fails on any
  unreviewed diff.

## Load

- Locust scenarios:
  - "10 users clocking in at 08:00"
  - "Task list render for a property with 100k tasks history"
  - "Turnover day: 5 simultaneous completions with photo uploads"
- Pass criteria in §00 (success metrics) drive the budgets.

## LLM regression

- Fixture set per capability: receipts (good, bad, multi-page, non-
  English), intake strings, digests (happy, quiet day, anomalies).
- Expected shapes asserted via Pydantic; numeric fields allowed to
  drift within configured tolerance.
- `pytest -k llm` with `--replay` uses recorded cassettes; `--live`
  calls OpenRouter. Cassettes regenerated on demand.

## Quality gates (PR required)

- `ruff check`
- `ruff format --check`
- `mypy --strict`
- `pytest unit`
- `pytest integration` (SQLite + PG)
- `schemathesis`
- `playwright` smoke (the two shortest journeys above)
- `osv-scanner` (blocker on any unresolved high/critical)
- `bandit -ll`
- OpenAPI diff
- `cli-parity` (surface freshness + completeness + reverse check +
  operationId lint) — four checks:
  1. **Surface freshness** — regenerate `_surface.json` from current
     app, diff against committed version. Fail if stale.
  2. **Parity completeness** — every `operationId` in `openapi.json`
     must appear in `_surface.json` commands, exclusions, or override
     `covers=` declarations. Fail if any uncovered.
  3. **Reverse parity** — every `operationId` referenced in
     `_surface.json` must exist in `openapi.json`. Fail if a CLI
     command points at a removed endpoint.
  4. **operationId lint** — format must be
     `^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$`, first segment must be a
     known CLI group.
- Coverage threshold: 85% domain, 70% overall; tracked via codecov.

## Release gates

In addition to PR gates:

- Full Playwright journey suite.
- Full Locust load.
- Migration replay against a sanitized prod-like snapshot.
- SBOM generation (CycloneDX).
- Image signed with cosign.
- **Image non-root smoke test.** A CI step starts the release image
  with the stock entrypoint (no `--user` override), execs
  `id -u` inside it, and fails the build unless the result is
  non-zero. A second step runs `docker run --rm --user 0 <image>
  crewday-server serve` and asserts the process exits non-zero
  with the "refuses to run as root" error from §16. Both checks
  guard against regressions where a Dockerfile change drops the
  `USER crewday` directive or an orchestrator forces uid 0.

## Reproducibility

- `uv.lock` is the source of truth for Python deps.
- Dockerfile uses `--mount=type=cache` for `pip`/`uv` and `apt`.
- CI builds on Linux amd64 and arm64.
- Release notes include the exact image digest.

## Test data

- `crewday admin demo` seeds a realistic household for dev and e2e:
  - Main residence (Villa Sud, FR), vacation home (Chalet Alpe),
    one STR (Apt 3B Barcelona).
  - 5 employees across roles.
  - 30 task templates, 12 schedules.
  - 20 stays imported (synthetic iCal).
- Seeded deterministically from a single `--seed` integer so tests
  can reproduce.
