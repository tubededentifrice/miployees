# 01 — Landing and demo embed

The brochure site plus the scenario-driven demo embed. What pages
exist, what they show, how the "who is this for" picker resolves
to a demo URL, and when the visitor gets an iframe vs a silent
video loop.

## Route map

Every route is static HTML emitted by Astro. Dynamic behaviour
lives in React islands noted per route.

| Route | Purpose | Islands |
|-------|---------|---------|
| `/` | Landing. Hero, three feature bands, scenario picker + demo embed, footer. | `ScenarioPicker`, `DemoFrame` |
| `/why-crewday` | Long-form "why another operations tool", pain points, anti-goals. | — |
| `/for-owners` | Deep dive into the villa-owner story; auto-opens the picker with persona pre-selected. | `ScenarioPicker`, `DemoFrame` |
| `/for-agencies` | Same for rental-manager. | `ScenarioPicker`, `DemoFrame` |
| `/for-housekeepers` | Same for housekeeper. | `ScenarioPicker`, `DemoFrame` |
| `/pricing` | v1 is a placeholder: "Self-host free; managed pricing — contact us via suggestion box." | — |
| `/changelog` | Rendered from MDX in `content/<locale>/changelog/`. | — |
| `/legal/terms` | Terms of service (generic SaaS stub). | — |
| `/legal/privacy` | Privacy policy — see §04 for the full posture. | — |
| `/suggest` | Suggestion box surfaces (form + board). See §02. | `SuggestionForm`, `SuggestionBoard` |
| `/404.astro` | Static 404 page. | — |

Nothing authenticated, no `/w/<slug>/*` paths, no session cookies,
no `/api/*` routes served by Astro itself. All dynamic calls go to
`crew.day/api/*` which Caddy routes to `site/api/` (§04).

## Hero

Single moss-tinted hero band. Three pieces:

1. **Headline.** One line, < 60 chars, translated through the i18n
   seam. Default English: "Run your houses like a hotel. Without
   the front desk."
2. **Subhead.** One sentence, names the audience and the promise.
   Default: "crew.day is the operations back-office for owners and
   small property-management outfits — designed so an LLM agent
   runs the day-to-day and the humans live in the house."
3. **Primary and secondary call-to-action.**
   - Primary: "Try the demo" — scrolls to `#try-it`, which is the
     scenario picker + embed section below.
   - Secondary: "Sign up" — linked to `https://app.crew.day/signup/start`.

No carousel, no autoplay video above the fold, no background video.
First paint must be legible on a cold 3G connection.

## Feature bands

Three fixed bands on `/`, in this order. Each is a plain Astro
section with a heading, one paragraph of copy, and an illustration
(SVG, no large raster hero shots):

1. **Agent-first.** One paragraph per app §11. "Every button is a
   CLI command; every CLI command is a tool the agent can call;
   every action is audited."
2. **Places, people, tasks.** Short paragraph per app §02–§06.
   Emphasises the owner-manager / worker / client split.
3. **Self-hostable, single-binary.** One paragraph per app §16
   Recipe A. Addresses the "my data, my box" prospect.

Deep dives live on the `/for-*` pages. `/` stays short.

## Scenario picker — the two-axis selector

The core lead-gen control. Renders as a React island at
`<section id="try-it">` on `/`, `/for-owners`, `/for-agencies`,
and `/for-housekeepers`. On the `/for-*` variants, the persona
axis is pre-selected and hidden.

### Axes

**Persona** — three fixed values. Labels below are default English
copy; the `scenario_key` is the app §24 identifier the picker
resolves to, unchanged.

| Persona label | `scenario_key` | Default `?as=` |
|---------------|----------------|-----------------|
| "I own a villa" | `villa-owner` | `owner` |
| "I run a property-management agency" | `rental-manager` | `manager` |
| "I work as a housekeeper" | `housekeeper` | `worker` |

**Intent** — per-persona. The intent label is i18n copy; it
resolves to a triple `(scenario_key, as, start)` where `start` is a
URL-encoded path **inside** the demo workspace — i.e. the path
**after** the demo's workspace prefix `/w/<workspace-slug>`. The
demo server prepends the freshly-minted workspace's slug at redirect
time (§24), so the picker never has to know — and never could
predict — what the slug will be.

| Persona | Intent label | Resolves to `start=` |
|---------|--------------|----------------------|
| Villa owner | "Organise my cleaner" | `/schedule` |
| Villa owner | "See what's happening at home" | `/dashboard` |
| Villa owner | "Manage incoming Airbnb stays" | `/stays` |
| Villa owner | "Chat with the agent about my property" | `/chat` |
| Rental manager | "Schedule staff across properties" | `/schedule` |
| Rental manager | "Track work orders" | `/work-orders` |
| Rental manager | "See payroll at a glance" | `/payroll` |
| Rental manager | "Invite a new client" | `/clients` |
| Housekeeper | "See today's tasks" | `/today` |
| Housekeeper | "Complete a task with photo" | `/today?focus=next-task` |
| Housekeeper | "Log hours" | `/me/hours` |
| Housekeeper | "Chat with the manager" | `/chat` |

The intent list is data, not code. It lives in
`site/web/src/content/<locale>/scenarios.ts` as a typed export the
island imports at build time — translators only touch the label
strings; the routes stay in English TypeScript.

### Behaviour

- The picker defaults to `persona = "villa-owner"` and the first
  intent in that persona's list.
- Changing persona resets intent to that persona's first entry.
- Every change updates the URL hash to
  `#try-it?persona=<persona>&intent=<intent-slug>` so a chosen
  configuration is shareable (deep-linkable).
- Hitting "Run this demo" swaps the below-the-fold `<DemoFrame>`
  from its current state (video or previous iframe) to the new
  target. See "Iframe vs video" below.

### Contract with app §24

The picker emits URLs of the form

```
https://demo.crew.day/app?scenario=<scenario_key>&as=<as>&start=<url-encoded-path>
```

Two of the three query params already exist in app §24 ("Visitor
lifecycle"). **`start` is new.** This site spec requires that app
§24 grow it as an additive, validated parameter:

- `start` is the path **inside** the workspace, URL-encoded
  (e.g. `start=%2Fschedule`). It MUST start with `/` and MUST NOT
  begin with `/w/` — the demo server prepends the freshly-minted
  workspace's `/w/<slug>` segment itself. A `start` that already
  contains `/w/` is rejected and treated as absent (§24).
- `start` is validated against a per-scenario allowlist declared
  on the fixture. Unknown paths fall back to the scenario's
  default landing route with a single server log line (same
  pattern as an unknown `scenario_key`).
- No tracking or persistence of `start` on the server beyond the
  initial redirect; the value is not cookied, not audited, and
  not echoed back.
- `start=` longer than 256 bytes → truncated and treated as
  absent; there is no path here to weaponise the query into a DoS
  vector.

The cross-reference is tracked in app §24 as a future-extension
note; the full update lands when the site picker ships (see §05
Roadmap, Phase 1).

## Iframe vs video

Every demo cell is available in two modes. Default is video; iframe
is opt-in.

### Why video first

A live iframe mints a real `demo_workspace` (app §24) on every
view. At landing-page-scale traffic, that would churn workspaces
constantly, spin the GC job, and thrash the demo deployment's
OpenRouter key. The `$5/day global cap` on the demo would be
exceeded by ambient impressions alone.

### The swap

- **Video (default).** A pre-recorded, silent, looping `.webm`
  (VP9) + `.mp4` (H.264) pair drives the cell — captions in the
  overlay describe what is happening. 30-45 seconds, no sound.
  Recorded against the same scenario fixture the iframe would
  boot. One video file per `(scenario_key, intent-slug)` pair,
  stored under `site/web/public/demo/<scenario>/<intent-slug>.{webm,mp4}`.
- **Iframe (opt-in).** A "Try it live" button inside the cell
  swaps the `<video>` for an `<iframe>` pointing at the §24 URL
  above. The button carries a discreet notice: "Opens a fresh
  demo workspace — you can break anything in it."
- Once the visitor has swapped to iframe for a given cell, further
  picker changes swap the iframe target directly without
  reverting to video, until page reload.

### Video production workflow

Videos are recorded with Playwright driving a local demo
container, using the same fixture that §24 seeds. A recording
script lives under `site/web/scripts/record-demo.ts`. Each
recording is deterministic (fixed clock, fixed RNG seed) so the
same command re-records the same frames.

The script is part of the site build's developer tooling only — it
is not run in CI. A human ships a new video when the underlying
scenario copy changes, tracked via a Beads task.

### Iframe CSP and cookies

The picker cannot loosen any demo-side header. The demo enforces
its own `frame-ancestors` via app §15 and app §24. The site's
requirement is simpler:

- The site sets `frame-ancestors 'self'` for its own pages — the
  marketing site is never embedded (§04).
- The site's CSP allows the demo origin in `child-src` / `frame-src`:
  `frame-src https://demo.crew.day;`. Nothing else.
- The iframe has `sandbox="allow-scripts allow-same-origin
  allow-forms allow-popups allow-popups-to-escape-sandbox
  allow-top-navigation-by-user-activation"`. The popups flag is
  needed because the app's agent can open documentation tabs;
  top-navigation-by-user-activation lets a visitor follow a
  "Sign up" link inside the demo to `app.crew.day/signup` without
  the click being swallowed.
- The iframe is loaded with `loading="lazy"` and mounted only
  after the visitor clicks "Try it live"; the video renders
  without any cross-origin network call.

## Copy and i18n

- English is the only shipped locale in v1. All user-visible
  strings route through the same deferred-i18n seam as app §18.
- Strings live under `site/web/src/content/<locale>/` as MDX (for
  prose) or as typed `.ts` dictionaries (for labels inside
  islands).
- No string is hardcoded inside a React island or `.astro` file.
  Reviewer rejects any PR that does — same rule as app §14 §18.
- Brand voice: plain, specific, second-person where it fits. No
  emoji, no exclamation marks, no "synergy". Aim is that a
  prospect who reads one page knows whether this product is for
  them.

## Accessibility and performance

- WCAG 2.2 AA. Tab order matches visual order on every page.
  `prefers-reduced-motion` suppresses video autoplay and the
  iframe-swap transition.
- Lighthouse performance ≥ 95 on mobile simulated 3G for `/`,
  `/for-*`, and `/pricing`. The suggestion board is allowed to be
  heavier since it is authenticated-intent.
- No Cumulative Layout Shift from the demo cell: the `<video>`
  and `<iframe>` share a fixed aspect-ratio container (16:9
  desktop, 9:19 mobile) so the swap is visually silent.

## Design system adoption

- `site/web/src/styles/tokens.css` is a symlink or copy of
  `mocks/web/src/styles/tokens.css`. The build fails if it
  drifts — a CI check diffs the two.
- `globals.css` covers layout primitives (`.container`,
  `.section`, `.prose`) that are specific to long-form marketing
  content and do not appear in the app. These live only on the
  site side.
- Icons come from `site/web/src/icons/` which re-exports the app's
  Lucide registry (commit `e249f75`). Adding a new icon to the
  site is two edits: add to the app's registry, re-run the
  generator that produces `site/web/src/icons/index.ts`.

## Cross-surface rules

- **No route in the site links to a `/w/<slug>/*` URL of the
  app.** Those are per-workspace and meaningless to a
  prospect. Links to the app all go to `https://app.crew.day/`
  (root), `app.crew.day/signup/start`, or the specific help
  anchors on `crew.day/docs/*` when docs ship.
- **The site does not read any cookie set by the app or the
  demo.** Different origins, CHIPS partitioning on the demo side
  (app §24). The site's scenario picker has no knowledge of any
  existing demo workspace the visitor may already hold.
- **The site respects the demo's `X-Demo-Reseeded: 1` header** by
  doing nothing. App §24 explicitly says the header is silent by
  default; this site does not add a toast.
