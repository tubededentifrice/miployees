# 14 — Web frontend

Two audiences, one codebase, same patterns: HTMX-powered server-
rendered HTML, Tailwind CSS, progressive enhancement. Employees get a
mobile-first PWA; managers get the same components with a wider
information architecture.

## Design pillars

- **Mobile-first by default.** Breakpoints stack up, not down.
- **Server-rendered HTML + HTMX** for interactivity. No SPA.
- **Tailwind 3.x with a custom design system** (tokens below). Not the
  default purple-on-white look — see the "Design language" section.
- **Progressive enhancement.** Nothing critical requires JavaScript
  except passkey ceremonies; task ticking, clock-in, expense
  submission all have graceful no-JS fallbacks.
- **Accessibility.** WCAG 2.2 AA: focus-visible, color contrast,
  semantic HTML, ARIA where HTML semantics are insufficient.
- **Offline-first PWA** for employees: today's tasks and a completion
  queue survive loss of connection.
- **No purple bias, no dark-mode default.** Light theme is the
  primary; dark is a manual toggle persisted per user.

## Design language

Palette (Tailwind extension):

- **Paper** — warm neutral base (`#FAF7F2` light, `#151310` dark).
- **Ink** — high-contrast text (`#1F1A14`).
- **Moss** — primary action (`#3F6E3B`). Chosen because houses are
  physical places and green reads as "go"; rare in dashboards so the
  product feels distinct.
- **Rust** — destructive/critical (`#B04A27`).
- **Sand** — warnings (`#D9A441`).
- **Sky** — informational (`#4F7CA8`).
- **Night** — dark-mode Ink equivalent (`#F4EFE6`).

Typography:

- **Display / headings:** "Fraunces" variable (serif with attitude;
  open-source). Fallback: `ui-serif`.
- **Body:** "Inter Tight" variable. Fallback: `ui-sans-serif`.
- **Monospace:** "JetBrains Mono" (only in dev-facing views).

Shape language:

- Rounded 10px radii for primary cards; 4px for tags; 999px for
  pills and FABs.
- Soft shadow (`shadow-md` with a moss-tinted alpha) on elevation-1
  cards; no heavy drop shadows.
- Subtle grain texture (SVG noise, 2% opacity) on the page
  background — not a flat white. Rendered once via an SVG filter,
  cacheable.

Motion:

- Only meaningful motion: page enter (150ms fade+rise 4px), list add
  (spring-in), item tick (scale-to-checkmark). No bouncing, no
  gradient sweeps, no parallax.
- Respect `prefers-reduced-motion`.

## Route map

### Public (no auth)

```
/                         → redirect to /login or /today (if session)
/login                    → passkey sign-in
/enroll/<token>           → passkey enrollment (magic link lands here)
/recover                  → enter a break-glass code
/guest/<token>            → tokenized guest welcome page
/healthz, /readyz, /metrics  (manager-scoped)
```

### Employee

```
/today                    → today's tasks (home)
/week                     → week list
/task/<id>                → task detail + complete/skip/comment/evidence
/issues/new               → report an issue
/messages                 → comments with open threads
/shifts                   → clock in/out + history
/expenses                 → submit + list own
/me                       → profile + passkeys + capabilities (read only)
```

### Manager

Everything above plus:

```
/properties               → property list
/property/<id>            → property hub (areas, stays, tasks, inventory, instructions, closures)
/property/<id>/closures   → property closure calendar (incl. iCal unavailable markers)
/stays                    → stays list & calendar
/employees                → staff list
/employee/<id>            → profile, roles, capabilities, shifts, payslips, leaves
/employee/<id>/leaves     → leave ledger (approve/reject)
/leaves                   → cross-employee leave inbox (pending approvals)
/templates                → task templates
/schedules                → schedule list & previews
/instructions             → knowledge base
/inventory                → per-property stock
/pay                      → periods, payslips, rules
/expenses                 → approvals queue
/approvals                → agent action approvals
/audit                    → audit log viewer
/webhooks                 → subscriptions
/llm                      → model assignments, call log, budget
/settings                 → household settings
```

### Calendar surfaces

- The `/stays` calendar overlays four layers: stays (coloured by
  source), turnover bundles (neutral pattern), property closures
  (greyed), and employee leave (narrow strip per employee, toggle-
  able). The same component is reused on `/property/<id>/closures`
  with the stay/turnover layers hidden.
- Closure rows with `reason = ical_unavailable` render read-only
  (the source is the upstream iCal feed); editing them surfaces an
  inline "Edit in Airbnb / VRBO" hint linked to the feed.

## HTMX patterns

- Every form `hx-post`s to its canonical REST endpoint with
  `hx-target` and `hx-swap`. No manual fetch in JS.
- **Optimistic UI** on task completion: the DOM swaps to "Completed"
  first; server confirms with `hx-swap-oob` correcting if rejected.
  If a second actor completed the same task between the optimistic
  swap and the server response (see §06 last-write-wins), the server
  returns the winning completion record and the UI shows a subtle
  "Completed by <name> · your note was kept in the audit log" toast —
  no data is silently lost.
- **Inline validation**: `hx-post` a small validator endpoint with
  `hx-trigger="blur changed delay:200ms"`; server returns an OOB
  error fragment.
- **Polling-free freshness**: the app subscribes to a
  **server-sent-events** feed (`/events`) scoped to the current user
  (and their properties for managers). Task state changes, new
  comments, and approvals are delivered as events; HTMX's
  `hx-trigger` listens for custom events and re-swaps affected
  fragments.
- **No hx-boost everywhere** — only where it meaningfully improves
  navigation continuity (inside property detail tabs, for example).

## JavaScript inventory

Strictly bounded. The full JS payload on an employee phone is under
**40 KB gzipped**:

- `htmx.min.js` (≈15 KB).
- `htmx-ext-sse.js` (≈1 KB).
- `passkeys.js` — our own, WebAuthn ceremonies only.
- `camera.js` — our own, opens `<input type="file" capture>` with a
  light preview and compresses via `<canvas>` before upload.
- `offline.js` — service worker registration + queue UI.
- `barcode.js` — dynamic import, only loaded when the user taps
  "Scan".

No framework, no React, no Alpine, no HTMX extensions beyond SSE.

## PWA

### Manifest

`/static/manifest.webmanifest`:
- `display: standalone`, `theme_color: #3F6E3B`, `background_color:
  #FAF7F2`.
- Icons: 192, 512, maskable.
- `shortcuts`: "Today", "Clock in", "New expense".

### Service worker

Under `/sw.js`. Responsibilities:

- Pre-cache the shell: CSS, JS, logo, `/offline.html`.
- **Today's tasks cache**: on every `/today` visit, cache the most
  recent response (ETag-aware). Stale-while-revalidate.
- **Instruction content cache**: any `/instructions/<id>` loaded is
  cached indefinitely, with background revalidation.
- **Completion queue**: if `/api/v1/tasks/<id>/complete` fails due to
  network, store the request (with body, idempotency key, captured
  timestamp) in IndexedDB. Background Sync API retries; on success
  the stored entry is purged. The UI shows a small "queued" pip.
- Queue ordering is FIFO; idempotency keys ensure server-side safety
  on replays.
- Evidence photos are uploaded when online only — offline taps can
  queue a completion that references a photo pending upload (local
  `blob` id), and the service worker uploads photo first, then
  replays the completion with the real `file_id`.

### Constraints

- Max queued completions before UI nags the user to reconnect: 50.
- Max queued photo bytes: 50 MB (configurable). Older entries are
  evicted with a visible "could not keep queued for longer" warning.

## Today screen (employee)

The most-used screen. Anatomy:

- **Top:** personalized greeting, date, property dropdown if the
  employee serves multiple, a big "Clock in" pill (if capability on)
  or "Clocked in @ 08:12 → Clock out".
- **Now:** the task with the nearest start time; full details inline.
- **Upcoming today:** list of collapsed cards.
- **Completed today:** collapsed count + expandable list.
- **Bottom nav** (PWA): Today · Week · Issues · Expenses · Me (+
  Chat bubble if capability).

Each task card shows:

- Title, property, area, priority indicator.
- Checklist progress (if any).
- Estimated duration.
- A single primary action: "Start", "Mark done", or "Complete with
  photo" depending on state and requirements.
- Attached instructions collapsed under an info icon; tap to expand.

## Task detail (employee)

- Header with status pill.
- Big primary CTA sticky to the top on scroll.
- Checklist as tappable rows; each tap saves via HTMX.
- Instructions accordion (area → property → global).
- Comments threaded; `@mentions` dropdown.
- Evidence: photo picker, note text area.
- Skip flow opens a modal with reason textarea.

## Manager screens

- Lists default to table mode on desktop, card mode on mobile.
- Filter bar is a single HTMX form that refreshes the list on change.
- Bulk actions on tables: select rows + action menu → all done via
  HTMX posts, with `hx-confirm` for destructive ones.
- Stay calendar uses a compact month/week view rendered server-side;
  no FullCalendar dependency.

## Internationalization readiness

- All user-facing strings flow through Jinja2 `{% trans %}` tags
  backed by Babel, even though v1 ships English only. See §18.

## Accessibility checklist (v1 gate)

- Logical tab order.
- Focus ring visible on Moss at AA contrast.
- Forms labeled (no `placeholder`-only labels).
- Images from users get an optional `alt` prompt at upload.
- Color never the sole indicator of state (icons + text too).
- No layout jumps > 100ms; loading states use `aria-busy`.
- Buttons are buttons; links are links; no `div` onclick.
- Test with NVDA on Windows, VoiceOver on iOS, TalkBack on Android
  in the release playbook.

## Browser support

- Latest 2 versions of Chromium, Safari, Firefox.
- iOS 16+ and Android 10+ (covers ~97% of domestic-staff phones in
  target markets).
- IE and legacy Edge: not supported; `/unsupported.html` if detected.

## Performance targets

- Today screen LCP < 1.5s on a 2019 mid-range Android over 4G from a
  nearby region.
- JS bundle on /today: < 40 KB gzipped.
- Service worker install: < 1s on first visit.
- Offline → online reconciliation: < 60s for 50 queued actions.

## Styleguide page

`/styleguide` (dev + staging only) renders every component with every
state for visual QA and design review.
