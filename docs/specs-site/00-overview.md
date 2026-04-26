# 00 — Site overview

## Vision

The public surface of crew.day. Everything a prospect sees before
they have an account, plus the feedback channel that links back
from the app. Hosted on `crew.day` itself; uses the app only as a
data consumer (for clustering) and as an embed target (for the demo
iframes).

The site does not store workspace data, does not hold user
sessions, and is not in the path of any authenticated workflow. It
is a marketing and feedback surface — functionally closer to a
landing page with a contact form than to the app.

## Audiences

- **Prospects.** The personas defined in app §00 — house owners,
  small property-management agencies, housekeepers — evaluating
  whether crew.day fits their situation. First contact is the
  landing page; conversion is either self-serve signup on
  `app.crew.day` or a live demo iframe on `demo.crew.day`.
- **Existing users.** Click the "Give feedback" link inside the
  app (rendered only when `CREWDAY_FEEDBACK_URL` is set; see
  §03). They land on the suggestion box to browse the public
  board or submit a new idea.
- **Partners, press, operators.** Read docs, pricing, changelog;
  optionally file a feedback item tagged as an enquiry.

## Surfaces

| Host | Role | Owned by |
|------|------|----------|
| `crew.day` | Landing, pricing, scenario picker + demo embed, suggestion box, public board, legal, changelog | This repo, `site/` tree (these specs) |
| `app.crew.day` | The product itself | This repo, `app/` tree (app specs) |
| `demo.crew.day` | App with `CREWDAY_DEMO_MODE=1` — ephemeral workspaces, iframe-embeddable | This repo, `app/` tree under app §24 |

The three are independently built and deployed. `crew.day` points
at the marketing host; `app.crew.day` at the app; `demo.crew.day`
at the demo. A self-hoster typically points their own domain
straight at the app and never deploys `site/` at all.

## Self-host posture

**The site is optional.** Self-hosters who run their own workspace
do not build, deploy, or maintain anything under `site/`. Concretely:

- `site/` has its own `docker-compose.yml` and is not referenced
  from the app's compose files.
- The app boots cleanly with no site in sight. The "Give feedback"
  link in the app is hidden when `CREWDAY_FEEDBACK_URL` is unset
  (default).
- The app's `feedback.cluster` capability (§03, app §11) is off
  by default on every deployment; a self-hoster does not pay LLM
  budget for it unless they explicitly turn it on.
- No migration, no settings flag, no UI toggle appears on a
  self-host deployment because of the existence of these specs.

The SaaS operator at `crew.day` runs the full stack — site + app +
demo — as three independent deployments behind their own reverse
proxies.

## Tech stack

**Frontend** — Astro 4+ with React islands.

- Astro emits static HTML per page; SEO, OpenGraph, and first
  paint come free. The landing is brochure-shaped; shipping a
  React SPA for it would be the wrong reach.
- React islands hydrate only the interactive bits: the scenario
  picker, the demo iframe/video swap, the suggestion form, the
  public board's filter controls.
- Strict TypeScript (`strict: true`), matching the app's posture
  (AGENTS.md "Type safety").
- **Design tokens and icons flow one-way from app to site.** The
  `mocks/web/src/styles/tokens.css` palette, the Lucide icon
  registry introduced in commit `e249f75`, and the BEM-style
  semantic-class rules from app §14 are copied or re-exported into
  `site/web/src/`. The site does not invent colour, shape, or icon
  primitives.
- No Tailwind, no utility classes, no inline `style=""`. Same rule
  as the app — see AGENTS.md "Application-specific notes".

**Backend** — FastAPI (Python 3.14+), SQLite default, optional
Postgres.

- Same `uv` / `ruff` / `mypy --strict` / `pytest` toolchain as the
  app — one mental model for reviewers.
- Narrow schema: `feedback_submission`, `feedback_cluster`,
  `feedback_vote`, plus one queue-ish `cluster_run` row to track
  the clustering job. See §02.
- No shared code with the app. `site/api/` is a separate Python
  package with its own `pyproject.toml`; it does not import from
  `app/`. The only link is the HTTP RPC in §03.
- Calls the app's clustering endpoint with a shared static secret
  (`SITE_APP_RPC_TOKEN`). The app is the LLM broker — budget,
  redaction, audit all live there, not here.

**Static content** — `.astro` files with MDX for long-form copy;
translated strings live under `site/web/src/content/<locale>/`
following the same deferred-i18n seam as app §18.

## Repo layout

```
site/
├── README.md
├── docker-compose.yml          # deploys web + api + caddy in isolation
├── Caddyfile
├── web/                        # Astro project
│   ├── package.json
│   ├── astro.config.mjs
│   ├── tsconfig.json
│   ├── src/
│   │   ├── pages/              # one .astro per route
│   │   ├── components/         # Astro + React mix
│   │   ├── islands/            # React-only components
│   │   ├── content/<locale>/   # MDX copy, one subtree per locale
│   │   ├── styles/             # tokens.css, globals.css
│   │   └── icons/              # shared Lucide exports
│   └── public/                 # static assets, demo videos, OG images
└── api/                        # FastAPI suggestion-box backend
    ├── pyproject.toml
    ├── site_api/
    │   ├── main.py
    │   ├── models.py
    │   ├── routes/
    │   ├── cluster_client.py   # thin HTTP wrapper over app's /_internal/cluster
    │   └── db.py
    ├── alembic/
    └── tests/
```

The `mocks/` and `app/` trees are untouched. `site/web/` shares no
`package.json` with `mocks/web/`, and `site/api/` shares no
`pyproject.toml` with `mocks/app/` or `app/`. Two build graphs,
two deploy pipelines, zero cross-import.

The site gets its own CI lane (lint, typecheck, build, unit
tests); a broken site build does not block an app release and vice
versa.

## Principles

- **Static first.** Anything that can be HTML at build time, is.
  Hydration pays only for the controls that genuinely need it.
- **Zero tracking on marketing pages.** No third-party analytics,
  no cookies, no pixels on the landing pages. The suggestion box
  is the only surface that writes anything server-side, and even
  it strips PII by default (§02).
- **App is a service, not a library.** `site/api/` reaches the
  app over HTTP with a versioned payload; no direct DB access, no
  shared ORM, no `import app.*`.
- **One source of visual truth.** Tokens and icons flow app → site;
  the site never defines a new brand primitive.
- **Accessible by default.** WCAG 2.2 AA, semantic HTML, keyboard
  path for every interaction, `prefers-reduced-motion` respected.
- **i18n-ready from day 1.** Every user-visible string goes
  through the deferred-i18n seam (app §18). English is the only
  shipped locale in v1; the structure is already there so adding
  French or Spanish is a content edit, not a refactor.

## Glossary orientation

- **Site.** The marketing surface at `crew.day` (these specs).
- **App.** The product at `app.crew.day` (docs/specs/).
- **Demo.** The app running with `CREWDAY_DEMO_MODE=1`, hosted at
  `demo.crew.day` (app §24).
- **Suggestion box.** The `crew.day/suggest` surface, its board,
  and the clustering that powers it (§02).
- **Clustering RPC.** The HTTP contract by which `site/api/`
  asks the app to classify a batch of submissions into clusters
  (§03).

These replace the ambiguous "frontend" and "landing page" in any
site spec. On the app side, "frontend" still means the app's
React SPA under `mocks/web/` (and future `app/web/`).
