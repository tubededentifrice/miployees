# 05 — Roadmap

Phased delivery for the site. Phases are scope-capped goals, not
sprints — a phase ships when its goals are met and the quality
gates in §04 pass on `main`.

Each phase is independently deployable: the stack boots and serves
a coherent experience at the end of every phase, even if later
phases are empty.

## Phase 0 — Scaffolding

- Monorepo layout under `site/` as specified in §00.
- `site/web/` Astro 4+ project bootstrapped with strict TS,
  `site/api/` FastAPI package bootstrapped with the same
  `uv`/`ruff`/`mypy --strict`/`pytest` toolchain as the app.
- `site/docker-compose.yml` + `site/Caddyfile` per §04.
- Shared design: `tokens.css` and the Lucide icon registry
  copied from `mocks/web/` by a build step; CI check fails if
  either drifts.
- `/healthz`, `/readyz`, `/version` live on `site-api`.
- Static empty-state pages (`/`, `/404.astro`) with placeholder
  copy behind the i18n seam.
- CI lane: lint, typecheck, build, unit tests on every push to
  `main` under `site/**`.
- Docker images published to `ghcr.io` with tag = commit SHA.

**Exit:** `docker compose -f site/docker-compose.yml up --build`
brings up both services; `curl https://127.0.0.1:8000/api/healthz`
returns 200; the landing page renders with the "Try the demo" CTA
wired to a placeholder anchor.

## Phase 1 — Landing and demo embed

- Hero and three feature bands per §01.
- `ScenarioPicker` island with two-axis selection; the intent
  catalog from §01's intent table lives in `site/web/src/content/<locale>/scenarios.ts`.
- `DemoFrame` island with video-first, iframe-on-demand swap.
- Video assets produced for every `(scenario, intent)` pair using
  the `site/web/scripts/record-demo.ts` workflow against a local
  demo container.
- `/for-owners`, `/for-agencies`, `/for-housekeepers` with
  pre-selected persona.
- `/why-crewday`, `/pricing`, `/changelog` stubs with real copy.
- Legal pages (`/legal/terms`, `/legal/privacy`) — privacy reflects
  the posture in §02 and §04 even though the suggestion box is
  not live yet (zero PII = smallest possible privacy policy).
- i18n seam wired; English shipped as the only locale.
- **App-side:** app §24 adds the `start=` query-string extension
  the picker depends on. Landing as an additive PR to the app
  specs + app code at the same time this phase ships.

**Exit:** a visitor on `crew.day` picks Agency × "Schedule staff
across properties", clicks "Try it live", and lands inside a
freshly-minted demo workspace at `/w/<slug>/schedule` (the picker
emits `start=/schedule`; the demo prepends the workspace's slug)
with the right persona. A visitor who does not click "Try it live"
sees the matching video loop. Lighthouse mobile perf ≥ 95 on `/`
and every `/for-*` page; cross-browser tested on mobile Safari,
Chrome, and Firefox.

## Phase 2 — Auth-gated submit (operator-only review)

Store-and-review; no public board, no agent, no clustering. The
magic-link auth bridge between app and site ships in this phase
so the write path is authenticated from day one.

- `/suggest` renders the form (no board panel yet). The form
  only shows when a valid `__Host-suggest_session` cookie is
  present; otherwise the panel shows the "Log in to submit" CTA
  linking into `app.crew.day/login?return=/feedback-redirect`.
- `feedback_submission` table + migration (no `embedding` or
  `reformulated_*` columns yet — added in Phase 3). Submissions
  land with `status='pending'` and verbatim (redacted) body
  only, keyed by `submitter_user_hash` and
  `submitter_workspace_hash`.
- `used_token_nonce` table + migration; 15-minute prune cron.
- Regex redaction pass on insert per §02 "PII posture".
- Rate limits per §04 (10/hour/user, 50/day/user,
  100/day/workspace).
- `/suggest/thanks` with the "We'll review ideas soon" copy —
  no cluster information yet.
- `site-admin submissions list [--since]` / `show <id>` and
  `site-admin users ban` / `workspaces ban`.
- **App-side:** three env vars land together — `CREWDAY_FEEDBACK_URL`,
  `CREWDAY_FEEDBACK_SIGN_KEY`, `CREWDAY_FEEDBACK_HASH_SALT`
  — plus boot validation, `GET /feedback-redirect` endpoint
  (authenticated, agent-forbidden, rate-limited 20/hour/user),
  and the `PageHeader` overflow-menu entry that hrefs into it.
  Additive PR to the app.

**Exit:** an authenticated app user clicks "Give feedback",
arrives on `crew.day/suggest` with a fresh `__Host-suggest_session`
cookie, submits an idea, and lands a redacted row on the site.
A logged-out visitor sees the "Log in to submit" CTA and can
read nothing else yet (board is absent). A replayed token is
refused. Operator can walk the backlog via CLI.

## Phase 3 — Agent pipeline + public board

The big phase: three new capabilities on the app side, a vector
store on the site side, and the public board lights up.

- `reformulated_title`, `reformulated_body`, `embedding`,
  `moderation_decision`, `moderation_reason`, `detected_language`,
  `embedded_at` columns added to `feedback_submission`
  (migration).
- `feedback_cluster` (with `summary_embedding`), `feedback_vote`,
  `cluster_run` tables + migration.
- `sqlite-vec` integration on SQLite (Postgres+pgvector on the
  Postgres path). Vector index on `feedback_submission.embedding`
  and `feedback_cluster.summary_embedding`.
- **App-side:** three deployment-scope capabilities added to
  §11:
  - `feedback.moderate` + `/_internal/feedback/moderate`.
  - `feedback.embed` + `/_internal/feedback/embed`. Default
    assignment is the local `BAAI/bge-small-en-v1.5` via
    fastembed, bundled with the app image.
  - `feedback.cluster` + `/_internal/feedback/cluster`.
  - New capability tag `embeddings` on `llm_model`.
  - Three budget env vars, three enable toggles, all off by
    default. The managed SaaS flips all three on.
  - Prompt templates for moderate and cluster seeded into the
    app's prompt library (app §11 "Prompt library").
- **Site-side pipeline:**
  - Stage 1 (sync on submit): call `/moderate` with
    `policy.embed=true`. Reject → store + done. Keep → store
    reformulated + embedding.
  - Stage 2: local vector search for top-K (default 8) candidate
    clusters.
  - Stage 3: call `/cluster` with reformulated + candidates.
    Store assignment; if `new_cluster`, create the cluster with
    the returned `new_summary_embedding`.
  - Scheduled batch worker every 6 h: re-runs pending
    submissions through the pipeline, runs the merge-check pass
    on near-duplicate cluster pairs.
  - `new_cluster` acceptance gated by
    `SITE_NEW_CLUSTER_MIN_CONFIDENCE=0.65` (default; §02 stage 3);
    no time-based limit. Below threshold the site re-asks with
    `force_existing_only=true` and takes the agent's best
    existing-cluster pick.
- Public board at `/suggest` showing reformulated titles;
  cluster detail at `/suggest/cluster/<id>`.
- Vote widget with 30/hour/user-hash rate limit.
- Static-with-ISR build for the board (10-minute rebuild).
- `site-admin submissions rejected-list`, `unreject`,
  `reprocess`, and `clusters merge/split` CLI commands per §02.

**Exit:** a visitor submits; the agent moderates (rejecting
gibberish) and reformulates; two near-duplicate submissions
land in the same cluster; a substantially new idea creates its
own cluster; the board shows reformulated titles only. The
operator walks `rejected-list`, un-rejects one false-positive,
and confirms it re-enters the pipeline. The manifest endpoint
reports `embed_dim=384` and the site boots clean against it.
Deployment-scope budgets on `/admin` show non-zero spend for
all three capabilities.

## Phase 4 — Moderation and lifecycle

- `site-admin` CLI per §02: list/show/set/merge/split/notify.
- `lifecycle` pills render on cards and detail pages.
- Operator `response` text appears inline on the detail page
  and the card.
- Email-notification flow:
  - Opt-in tick on the form.
  - `notify_email` column populated only when ticked.
  - `site-admin notify <cluster-id>` sends transactional email
    to every subscribed address for that cluster.
  - Signed unsubscribe link in the footer; unsubscribe flips
    `notify_suppressed` across every submission with that
    address.
- `mod_action` audit table written for every CLI action.

**Exit:** the operator shepherds one real cluster from
`new` → `acknowledged` → `planned` → `in-progress` → `shipped`
with one notify email per stage, all reflected on the public
board; an unsubscribe link works end-to-end.

## Phase 5 — Polish

- Accessibility audit (WCAG 2.2 AA pass across every page;
  keyboard-only walk; screen-reader walk on `/` and `/suggest`).
- Lighthouse mobile perf ≥ 95 across every page including
  `/suggest` under load (100 visible clusters).
- Deferred-i18n content translated to one additional locale as
  a proof that the seam works (content team chooses
  French or Spanish).
- Metrics dashboard (Prometheus-compatible scrape) matches the
  observability plan in §04.
- Public documentation at `crew.day/docs/*` if the app ships
  docs concurrently — otherwise landing on the app's own
  surface.

**Exit:** v1.0.0 of the site tagged. Public release.

## Deferred beyond v1

- **Web moderation UI.** v1 keeps moderation on the CLI per
  §02; promoting it to a web tool is a separate spec pass.
- **Multi-language content beyond locale #2.** English + one
  is the v1 target; more locales are content edits, not code.
- **hCaptcha / reCAPTCHA.** Only if the per-user rate limits
  prove insufficient against compromised accounts.
- **mTLS on the clustering RPC** (§03). Additive; no breaking
  change.
- **Push notifications** for cluster lifecycle updates. Email
  covers v1; push would be a new subscription model.
- **Shareable cluster URLs with OG cards** specifically
  designed for socials. Currently every cluster detail page
  renders a default OG tag; a richer one requires a
  generator.

## Cross-refs to app roadmap

The site roadmap coordinates with app §19 on three points:

- **Phase 1 here** requires app §24 to grow the `start=`
  query param. Landing as part of the same release window.
- **Phase 2 here** requires the app to ship the full magic-link
  bridge: three env vars (`CREWDAY_FEEDBACK_URL`,
  `CREWDAY_FEEDBACK_SIGN_KEY`, `CREWDAY_FEEDBACK_HASH_SALT`),
  a new `GET /feedback-redirect` route (authenticated,
  agent-forbidden, rate-limited), and the `PageHeader` overflow-
  menu entry that hrefs to it. Additive; still off by default
  on self-host.
- **Phase 3 here** requires three new deployment-scope
  capabilities in app §11 (`feedback.moderate`,
  `feedback.embed`, `feedback.cluster`), their
  `/_internal/feedback/*` routes, a new `embeddings` tag on
  `llm_model`, and per-capability deployment-scope budget
  tracking. It also ships a bundled local embedding model
  (`BAAI/bge-small-en-v1.5` via fastembed) in the app image —
  a ~30 MB addition.

None of the three lands on self-host deployments by default.
They are SaaS-operator configurations.
