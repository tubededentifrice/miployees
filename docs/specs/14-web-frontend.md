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
/chat                     → full agent conversation (inbox for all task
                            notes, issue reports, expense photos, Q&A)
/expenses                 → submit + list own
/me                       → profile + language + passkeys
/history                  → Tasks / Chats / Expenses / Leaves tabs
/issues/new               → fallback issue form (still reachable from
                            chat attach flow; no footer entry)
/shifts                   → clock in/out + history (manual mode only;
                            hidden when `time.clock_mode = auto | disabled`)
```

**Footer (bottom nav):** exactly five items —
`Today · Week · Chat · Expenses · Me`. The `Issues` tab from v0 is
**removed**; issues are filed through the chat page's attach flow
(which emits to `/issues/new` under the hood). There is **no
floating chat FAB**; chat is a first-class footer tab.

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
/settings                 → workspace settings
```

### Manager desktop shell

The desktop manager UI is framed by three layout regions:

- `.desk__nav` — left-hand primary navigation (the list above).
- `.desk__main` — central content pane.
- `.desk__agent` — right-hand sidebar hosting the **workspace agent**
  conversation (§11 "Manager-side agent"). Components:
    - A compact header with the agent title and an **online
      indicator**. The active model assignment for capability
      `chat.manager` (default `google/gemma-4-31b-it`) is *not*
      surfaced here — the model picker lives on `/llm` (§11), not
      in the chat surface, to keep the conversation UI focused.
    - The running **chat log** with the workspace agent — markdown
      bubbles, voice-input button when capability is on. The log
      lazy-loads older messages when the user scrolls up and pins
      to the latest message on open.
    - A **pending-actions tray** listing `agent_action` rows that
      the agent has queued for approval (§11); each row has
      `approve` / `reject` buttons wired to the same endpoints as
      `/approvals`. The tray sits between the log and the
      composer, sized to its content, so approvals are always
      visible without scrolling the sidebar.
    - A **composer** fixed at the bottom of the sidebar — the
      manager can always ask the agent something without scrolling
      or hunting.
    - A **collapse toggle** so the sidebar can be hidden on narrow
      laptops; collapsed state renders as a thin vertical rail and
      is persisted per user.

The sidebar is load-bearing for the agent-first invariant (§11):
any verb the manager can click in `.desk__nav` or `.desk__main` can
also be requested of the agent in `.desk__agent`.

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
- **Bottom nav** (PWA): Today · Week · Chat · Expenses · Me. No
  floating chat FAB; chat is the third footer tab. The Issues tab
  from v0 is removed — attach an issue from the chat page.

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
- Task-scoped chat (§06 "Task notes are the agent inbox") embedded
  inline; the same conversation lives on `/chat` filtered to this
  task.
- Evidence: photo picker, note text area.
- Skip flow opens a modal with reason textarea.

## Chat page (employee)

`/chat` is a full agent conversation — the employee's **universal
inbox** for task notes, comments, issue reports, expense photos,
and Q&A grounded in instructions (§07).

- Voice-input mic button (capability-gated on `voice.employee`);
  audio transcribed via `voice.transcribe` before dispatch.
- **Auto-language detection** on inbound: the employee writes in
  their own language; the agent stores both the original and the
  workspace-language translation per §10. The employee always sees
  their own original.
- Issue reporting: the attach flow includes a "Report as issue"
  button that routes the thread segment into `/issues/new`.
- Expense uploads: the attach flow includes "Receipt" → camera
  picker → background `expenses.autofill` (§09).
- Task notes: when the employee opens a task, a sub-thread filtered
  to that task is shown; messages written there also appear on the
  task detail inline chat.

No DMs, no group chats outside a task thread — just the one
per-employee conversation with the workspace agent.

## Me page (employee)

Simplified from v0. Contents:

- **Profile** — display name, avatar, timezone, emergency contact.
- **Language preference** — BCP-47 picker (§05 `languages[0]`),
  used as the agent's reply language and the auto-translation
  source (§10, §18).
- **Passkeys** — list + "add passkey" + revoke.

Explicitly **not** on the Me page:

- No capabilities list (capabilities are manager-configured; the
  employee sees them implicitly through the features that work).
- No "switch to manager preview" link.

## History (employee)

`/history` is a new employee route with four tabs:

- **Tasks** — completed + skipped + cancelled tasks with filters.
- **Chats** — archived chat topics (post-compaction; full-text
  searchable, see §11 "Conversation compaction").
- **Expenses** — all submitted claims, with states.
- **Leaves** — one-off `employee_leave` rows and upcoming weekly-
  availability exceptions.

History is read-only.

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
- **Click targets are at least 44x44 CSS pixels** on any interactive
  element — back-links, footer tabs, task-card CTAs, checklist
  ticks, chat mic, icon-only buttons. Icons inside smaller graphic
  boundaries get transparent padding to reach the 44x44 minimum.
  This is the floor; many primary CTAs are larger.
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
