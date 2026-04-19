# 14 — Web frontend

Two audiences, one codebase, same patterns: a React SPA that ships a
mobile-first PWA for workers and a desktop shell for owners and
managers.

**The mocks under `mocks/web/` are the living spec.** Page anatomy,
component composition, interaction copy, and visual detail live in
the React code — go there first. This file captures only the
**enduring constraints** every future implementation must satisfy:
principles, the route contract, the design language, and the
accessibility / performance / PWA gates.

## Principles

- **React SPA.** FastAPI serves `index.html` for any non-API GET;
  React Router owns client-side navigation. See §01 for the
  `mocks/app/` + `mocks/web/` split.
- **Mobile-first** for the worker surface; breakpoints stack up.
- **Hand-rolled semantic CSS design system** (BEM globals + optional
  per-component CSS modules). No Tailwind, no utility classes, no
  Alpine, no Vue. Tokens live in `mocks/web/src/styles/tokens.css`;
  global rules in `globals.css`; semantic class names only (see
  AGENTS.md).
- **Light / dark / system theme.** Preference is one of `light`,
  `dark`, or `system` (tracks the OS `prefers-color-scheme`), persisted
  per user on the `crewday_theme` cookie. The resolved value is
  applied via `[data-theme="light"|"dark"]` on `<html>` and `<body>`,
  and mirrored to `color-scheme` so native scrollbars and form
  controls stay theme-consistent. Light is primary; a segmented
  control on `/me` is the authoritative surface for the preference.
- **Progressive enhancement.** Passkey ceremonies and camera capture
  require JavaScript; task reading degrades gracefully without it.
- **Offline-first PWA** for workers — today's tasks and a completion
  queue survive loss of connection.

## Design language

Palette, type, shape, and motion are brand-level decisions that
outlive any screen, so they're pinned here rather than in the mocks:

- **Palette.** Warm neutral **Paper** base; high-contrast **Ink**
  text; **Moss** primary action (houses are physical places and
  green reads as "go"; rare in dashboards, so the product feels
  distinct); **Rust** destructive; **Sand** warnings; **Sky**
  informational. Full token map (light + dark) is the authoritative
  `mocks/web/src/styles/tokens.css` — reference it, don't re-declare
  values in spec prose.
- **Typography.** Display/headings: *Fraunces* variable serif. Body:
  *Inter Tight* variable. Monospace: *JetBrains Mono* (dev-facing
  views only).
- **Shape.** 10px radii for cards; 4px for tags; 999px for pills and
  FABs. Soft moss-tinted shadows. Subtle SVG-noise page texture at
  ~2% opacity — not a flat white.
- **Motion.** Only meaningful motion (page enter, list add, item
  tick-to-checkmark). No bouncing, no gradient sweeps, no parallax.
  Respect `prefers-reduced-motion`.

## Route contract

Canonical navigation. The authoritative tree is
`mocks/web/src/App.tsx`; this list exists so an agent can answer
"what routes should exist" without loading React code.

### URL shape

Every authenticated route lives under a path prefix that names the
active workspace (§01 "Workspace addressing"):

```
<host>/w/<workspace_slug>/<route>
```

The slug is part of every URL emitted by the app — deep links,
guest welcome URLs, email notifications, agent handoff links.
Public routes (signup, login, workspace picker, health) live at
the bare host. React Router mounts the full tree below under
`/w/:slug/*` so `useParams()` in `<Shell />` resolves the active
workspace once and the rest of the tree is unaware of the prefix.

### Public (bare host, no workspace prefix)

```
/                          → auth redirect (to /login or /select-workspace)
/signup                    → SaaS self-serve signup (§03)
/signup/verify             → magic-link landing after signup email
/select-workspace          → picker for users with ≥2 workspaces (see below)
/login                     → passkey login
/recover                   → self-service lost-device recovery (§03);
                             owners / managers see a step-up break-glass code field
/recover/enroll            → magic-link landing; fresh passkey ceremony
/me/email/verify           → email-change magic-link landing (swap + notice)
/styleguide                (dev + staging only)
/healthz, /readyz, /version  (no auth; see §12)
```

### Admin (bare host, deployment-admin grant only)

```
/admin                     → redirects to /admin/dashboard
/admin/dashboard           → operator landing page: deployment health, recent audit, usage
/admin/llm                 → LLM providers, capability → model, pricing, deployment-wide spend
/admin/agent-docs          → system-side virtual files (§11 "Agent knowledge tools" — agent_doc table, §02)
/admin/chat-gateway        → deployment-default WhatsApp provider, templates, webhook, overrides (§23)
/admin/usage               → per-workspace spend, cap adjust, pause state
/admin/workspaces          → list, trust, archive; per-workspace summary card
/admin/settings            → deployment-scope settings: self-serve signup policy (§03),
                             capability registry read-out, and the raw key/value store.
                             (/admin/signup redirects here.)
/admin/admins              → admin-team membership, deployment permission rules
/admin/audit               → deployment-scope audit log
```

The `/admin` surface never carries a workspace prefix — it is
deployment-level by construction. Routes return `404` for
callers without any deployment grant (§12 "Admin surface");
the React shell renders a "you don't have access, ask your
operator" card when `GET /admin/api/v1/me` 404s so deep-links
from a password manager don't silently land on `/login`.

### Per-workspace public

```
/w/<slug>/accept/<token>   → click-to-accept invite (new or existing user; §03)
/w/<slug>/guest/<token>    → guest welcome page
```

### Shared (any authenticated role, workspace-prefixed)

Routes rendered under whichever shell matches the viewer's role
(`ManagerLayout` for managers, `EmployeeLayout` for workers) — picked
by a single `<Shell />` wrapper component in `App.tsx`. All paths
below are relative to `/w/<slug>/`.

```
today            schedule         task/<id>
my/expenses      me               history          issues/new
asset/<id>
```

The legacy routes `/week`, `/me/schedule`, `/bookings`, and
`/shifts` all 302-redirect to `/schedule` (§06 "Schedule view"
is the single canonical surface for "my time"). Deep-links,
bookmarks, agent tool outputs, and CLI examples that still name
any of those URLs continue to land on the right page. The
standalone `/bookings` page is retired in v1 — booking rows
surface as a section of the `/schedule` day drawer (below), so
a worker never has to cross-reference two calendars to answer
"what am I doing, and will I be paid for it".

### Worker-only (under /w/<slug>/)

```
chat             asset/scan       kb
```

Footer bottom-nav: `Today · Schedule · Chat · My Expenses · Me`. Chat is
first-class, **not** a floating action button. The bottom-nav `Chat`
tab is the **mobile** entry to the agent — it navigates to the
full-screen `/chat` page. On desktop (≥720px) the worker shell drops
the bottom-nav (the shared `<SideNav />` takes over) and the agent
moves to the right-hand `.desk__agent` rail (§14 "Desktop shell")
shared with the manager layout, so `Chat` is no longer listed in the
left-nav. Booked hours (per §09) live on `/schedule`: the day
drawer surfaces the day's bookings with inline **amend**
(overrun / underrun reason), **decline** (future bookings only),
and — from an otherwise empty day — **propose ad-hoc booking**.
There is no clock-in / clock-out button anywhere in the UI: the
booking *is* the time record (§09 "Bookings").

### Owner/Manager (under /w/<slug>/)

```
dashboard                      properties
property/<id>                  property/<id>/closures
stays                          users
user/<id>                      user/<id>/leaves
user/<id>/availability         leaves
availability-overrides         holidays
permissions                    templates
scheduler                      schedules
instructions                   instructions/<id>
assets                         asset/<id>
asset_types                    documents
inventory                      pay
expenses                       approvals
audit                          webhooks
tokens                         settings
kb
```

The `documents` page renders an **Extraction** badge per row
(`pending`, `extracting`, `succeeded`, `failed`, `unsupported`,
`empty`) sourced from the `asset_document.extraction_status`
denorm; clicking it opens a small disclosure with the extractor
used, the page count, the token count, the
`has_secret_marker` warning when set, and a "Retry" button for
owners and managers (§21 "Document text extraction"). The doc
detail view adds a collapsed "Extracted text" panel that lazily
loads pages from `GET /documents/{id}/extraction/pages/{n}`.

The `scheduler` page is the **"who is booked where"** calendar.
It renders a time grid (day or week) with one row per user (or one
row per property — the toggle is a view preference, not two pages)
and overlays three kinds of event:

- **Rota slots** (`schedule_ruleset_slot` resolved per
  `property_work_role_assignment`) — the background band for the
  cell, coloured by property.
- **Materialised tasks** at `scheduled_for_local` — cards inside
  the slot they fall in, linking to `/task/<id>`.
- **Stay lifecycle bundle markers** (§06) — badges on the day
  column, linking to the relevant unit/stay.

Filters: property, work role, user. The page shares the
`/api/v1/scheduler/calendar` feed (§12) with the worker and client
views; it also surfaces two soft warnings sourced from that feed —
**rota gaps** (rota slot with no assigned task during the window)
and **cross-workspace overlaps** (the same user has rota slots in
two linked workspaces that collide on a day; the server cannot
reject these on write, see §06). Edits to rulesets and slots are
inline on the `/w/<slug>/schedules` page (the "Rota" tab added
alongside task schedules) — the `/scheduler` page itself is
view-first, edit-on-click.

SSE invalidation: `schedule_ruleset.upserted`,
`schedule_ruleset.deleted`, and the existing `task.*` /
`stay_task_bundle.*` events all invalidate
`['scheduler-calendar']`.

The worker and manager surface `/schedule` (self-only; also the
target of the legacy `/me/schedule` and `/week` URLs) is the hub
for "my time". It renders the same calendar feed as `/scheduler`
narrowed to the caller, and adds the self-service edit affordances
described below. It replaces the flat `/week` task list; `/me` no
longer carries weekly-availability, leave, or availability-override
panels — those move here. `/me` does not surface a link into
`/schedule`; the page is reached via the sidebar / bottom-tabs
entry (present on every employee and manager surface per the
"Today · Schedule · Chat · My Expenses · Me" footer nav above),
which makes an extra card on `/me` redundant.

**Layout responsiveness.** Mobile (`<720px`) renders a vertical
**agenda**: one row per day, each row showing the day's rota band
(coloured by property per §05), the day's assigned tasks as compact
chips (title + time, up to N before an "+M more" collapse), and
any leave / override / public-holiday / closure markers. The agenda
is backed by a **bidirectional infinite query** (one network page =
one ISO week, 7 days): on first paint the worker lands on today —
auto-anchored under a sticky **month/year ribbon** — and can scroll
in either direction without paging. Top and bottom
`IntersectionObserver` sentinels extend the past / future a week
at a time as they approach the viewport edge; scroll position is
preserved across prepends so the world doesn't jump under the
thumb. A floating **"Today" pill** (FAB + duplicate inline jump in
the ribbon) appears whenever today scrolls out of view, taking the
worker back to "now" in one tap. There is no Prev/Next paginator on
phone — a network-blocking weekNav button is strictly worse than
infinite scroll for the surface a worker checks twenty times a day.
Desktop (`≥720px`) renders the existing **week grid** (Mon..Sun
columns) with rota as the background band and tasks tiled inside;
a "next/prev/this week" navigator matches `/scheduler`. Both
layouts drill into the same **day drawer** on click (mobile sheet,
desktop side panel).

**Day drawer.** Shows, for the focused date: rota slots
(read-only), the day's tasks (each linking to `/task/<id>`,
read-only — workers don't self-reassign), the day's **bookings**
(per §09, with inline amend / decline / propose), availability
summary (effective hours resolved through §06 "Availability
precedence stack"), and any covering leave or override. Action
buttons: **Request leave** and **Request override**.

- **Bookings** render as one row per booking with
  `scheduled_start–scheduled_end`, a status pill, a duration
  badge, and the property name. The inline actions are:
  - **Amend** (future or past) — opens the `POST
    /bookings/{id}/amend` dialog (§09 "Amend"). The mock ships
    a stub "+15 min" quick-amend that posts a fixed reason; the
    production shell is a proper time-and-reason form. Pending
    self-amends above the threshold render a `pending manager`
    chip instead of a number until the manager decides.
  - **Decline** (future, `status = scheduled` only) — calls
    `POST /bookings/{id}/decline`. Unilateral per §09 "Worker
    decline"; the row flips to `pending_approval` and disappears
    from the active list until the manager reassigns.
  - **Propose ad-hoc booking** — visible only on a day with no
    existing bookings and no rota. Opens a form prefilled with
    the signed-in worker's engagement and a chooser for
    property + start + end, posting to `POST /bookings` with
    `status = pending_approval` (§09 "Ad-hoc bookings").
- **Request override** opens an inline form pre-filled with the
  weekday's pattern hours. The worker can flip "available" off
  (→ `user_availability_override.available = false`) or pick
  custom hours. On submit the server computes
  `approval_required` per §06: widening availability (adding a
  day, extending hours) lands approved; narrowing availability
  (removing a day, reducing hours) lands pending. The UI shows
  the resolved state explicitly — never pretend a pending
  override is live.
- **Request leave** opens a multi-day form (category, start,
  end, note). Always pending approval; surfaced to the
  owner/manager via `/leaves` and the daily digest.
- **Inline override on desktop.** A worker may also edit the
  day directly from the week grid — clicking a rota band opens
  the same override form pre-filled. The mock ships click-to-edit;
  the production shell may upgrade to a draggable edge gesture
  ("Outlook-like") provided the underlying write is the same
  `POST /api/v1/me/availability-overrides` call and the
  approval-required rule is evaluated server-side. The UI MUST
  NOT render a drag as committed until the server confirms the
  approval state.
- **Public holidays and property closures** are read-only on
  this surface (managers edit them under `/holidays` and
  `/property/<id>/closures` respectively); they render as
  non-interactive markers.

**Day-cell booking markers.** In both the phone agenda row and
the desktop week-grid cell, a booking in `status =
pending_approval` or carrying a non-null `pending_amend_minutes`
flips the cell into a `--pending` visual state (sand-coloured
edge + small dot). A cancelled or no-show booking stays visible
with a rust-coloured edge so a worker scrolling history doesn't
assume it happened. The cell only carries a count marker for
pending rows (sand dot + edge); inline booking chips inside the
cell are deferred — the drawer is the canonical read surface and
bookings never compete with task chips for attention.

**Pending banner.** The page header on `/schedule` renders a
compact banner at the top whenever the window contains any
booking in `pending_approval` or with `pending_amend_minutes !=
null`. It reads e.g. "2 bookings need attention — 1 waiting for
manager approval, 1 declined awaiting reassignment" and links
each count into the matching day's drawer. The banner is the
single skimmable answer to "do I have anything open?" so a
pending amend from 10 days ago can't fall off the viewport.

**/api/v1/me/schedule** includes a `bookings[]` array alongside
`tasks`, `leaves`, and `overrides` — same window, same worker,
single round-trip. Invalidation follows the existing booking SSE
events (`booking.amended`, `booking.declined`,
`booking.approved`, `booking.reassigned`): any of them bumps the
`['my-schedule', …]` query keys. The manager / agency list
feed (`GET /bookings`, `?pending_amend=true`) is unchanged and
remains the audit-and-admin surface (§12).

**Past-booking recap.** There is deliberately no flat "recent
bookings" list in the worker UI. The bidirectional infinite
agenda already renders history day-by-day (scroll up, open the
drawer); the formal payroll recap lives on `/pay` (owner-
managers) and the settled-period payslips on `/me` (§09 "Pay
rollup and exports").
If a future customer asks for a per-day booking export, that's
a new surface — it does not mean reintroducing a standalone
`/bookings` page.

**Manager use of /schedule.** Under `MY WORK → My Schedule`, the
manager gets the exact same page rendered against their own
`user_work_role` — a manager who also works slots sees and edits
them here. Cross-employee edits live on `/scheduler` instead.

**/scheduler inline edits (per-date only).** On `/scheduler`,
managers can edit *per-date* facts without leaving the page:

- Click a **task tile** → task detail modal with inline
  **Reassign** and **Reschedule** actions
  (`POST /api/v1/scheduler/tasks/{id}/reschedule`,
  `POST /api/v1/scheduler/tasks/{id}/reassign`); saving
  fires the existing `task.updated` SSE event.
- Click a **rota slot band** → a small chooser:
  **"Edit just this date"** (creates a
  `user_availability_override` for that user and date — the
  manager is the creator, so auto-approved per §06) or
  **"Edit the pattern"** (navigates to `/schedules` with the
  ruleset pre-selected — the recurring edit path).
- Click an **empty cell** → quick-add menu: add override,
  record leave, or add property closure.

Drag-to-mutate a rota slot **is not supported** on `/scheduler`.
Ruleset edits are high-blast-radius (one drag changes every future
week); they stay behind the explicit chooser above and the
dedicated `/schedules` editor. Per-date drags (move a task to
another day, extend a one-day override) are allowed because each
drag writes exactly one row.

The `kb` page (worker and manager) is the human-facing
counterpart of the agent's `search_kb` tool — a single search
input that returns mixed instruction + document hits with the
same `why` provenance label, and a "Read" action that opens the
instruction page or the document detail. It is built on the
same `/kb/search` endpoint the agent uses, so the relevance
ranking the user sees is the relevance ranking the agent sees.

The former `/w/<slug>/llm` page is gone in v1 — LLM provider and
capability config is a deployment-level concern rendered on
`/admin/llm` (§11, "Admin shell" below). Workspace managers
still see the "Agent usage — N%" tile on `/settings` (§11).

### Administration link

The manager left-nav renders a single entry, **Administration**,
in the `ADMIN` section, visible **only** to users whose `GET
/api/v1/me` payload carries `is_deployment_admin: true` — i.e.
at least one active `(scope_kind='deployment')` `role_grants` row
(§05). The link deep-links to `/admin/dashboard` on the bare
host; it breaks the `/w/<slug>/...` pattern by design, because
the admin shell is workspace-agnostic. Users without a
deployment grant never see the link (same RBAC posture as any
other manager nav entry).

From the admin side the inverse is also true: `/admin` carries
a **Back to workspaces** button that returns the user to
`/select-workspace` (or directly to `/w/<their-only-slug>/...`
if they hold exactly one workspace grant).

- `/tokens` — admin surface for workspace API tokens (scoped and
  delegated; §03). Gated on `api_tokens.manage`. Personal access
  tokens (§03) are **not** listed here — they live on `/me` under
  the "Personal access tokens" panel, revocable only by the
  subject. Each token row expands inline to show its per-token
  request log (method, path, status, IP prefix, correlation id).

### Workspace switcher

`/select-workspace` (bare host) is the landing page after login
for users with two or more workspaces (resolved from
`GET /api/v1/me/workspaces`, §12). It renders one card per
workspace (name, slug, last-seen role) and a `Go →` button that
navigates to `/w/<slug>/today` (worker) or `/w/<slug>/dashboard`
(manager). Users with exactly one workspace skip this page and
are redirected straight into it. A persistent "Switch workspace"
link in the user menu re-opens the picker at any time.

### Desktop shell

Both the worker and owner/manager desktop layouts share the same
three-region grid:

- `.desk__nav` — left-hand primary navigation.
- `.desk__main` (manager) / `.phone__body` (worker) — central
  content pane.
- `.desk__agent` — right-hand sidebar hosting the role-appropriate
  agent (§11). Collapsible to a 52px rail; collapse state persists in
  the `crewday_agent_collapsed` cookie.

The sidebar mounts **once** as a sibling of `<Outlet />` in each
layout (`EmployeeLayout`, `ManagerLayout`) so chat state, composer
draft, and `EventSource` subscription survive client-side navigation.
The component is shared (`mocks/web/src/components/AgentSidebar.tsx`);
a `role` prop selects the per-role agent log/message endpoints
(`/api/v1/agent/{employee|manager}/{log,message}`) and gates the
manager-only "Pending approvals" block. It is load-bearing for the
agent-first invariant (§11) — any verb reachable in `.desk__nav` or
`.desk__main` must also be requestable in `.desk__agent`.

On mobile (`< 720px`) `.desk__agent` collapses out of the grid in
both layouts. The worker shell exposes the agent through the
bottom-nav `Chat` tab → full-screen `/chat`; the manager shell
exposes it through a single bottom dock button (`.desk__bottom-dock`)
that opens `.desk__agent` as an off-canvas right drawer.

The **MY WORK** group is the first section in the manager left-nav,
placed before all operational sections:

```
MY WORK
  My Day       → /today
  My Schedule  → /schedule
  My Expenses  → /my/expenses
  My History   → /history
```

These routes render under `ManagerLayout` (the shared-route rule
above), so managers navigate their personal work without leaving the
desktop shell.

### Admin shell

The deployment admin surface lives at the bare host under
`/admin/*` and uses a dedicated layout (`AdminLayout`). It
follows exactly the same design language as the manager shell
(three-region `.desk` grid, semantic class names, `.desk__nav`
+ `.desk__main` + `.desk__agent`, Moss primary action, same
tokens) — the user should feel they stepped sideways, not into
a different product. What differs:

- **No workspace slug**, no workspace switcher. A single "Back
  to workspaces" affordance in the nav footer returns the user
  to `/select-workspace`.
- **Left-nav sections** (`OPERATE` / `USAGE` / `ADMIN`):
    - OPERATE: Dashboard, Workspaces, Signup
    - USAGE: LLM & agents, Usage
    - ADMIN: Admins, Permissions, Settings, Audit log
- **Right-rail agent** is the same shared `<AgentSidebar />`
  component with `role="admin"`. It hits
  `/admin/api/v1/agent/{log,message,actions}` instead of the
  per-role workspace endpoints, and every send carries the
  `X-Agent-Page` header (§12) so the agent knows which admin
  route the user is on.
- **`/approvals` is intentionally absent** from the admin shell.
  Admin-side gated actions land inline in the admin chat
  (channel `web_admin_sidebar`, §11) — the deployment has no
  committee.
- **Mobile.** Same pattern as the manager shell: hamburger drawer
  for nav, bottom-dock button opens the agent as an off-canvas
  right drawer. The `/admin` surface is not PWA-installed — it
  is low-frequency operator tooling, not day-job UI.

## Implementation contracts

The mocks decide *how* screens look; these constraints decide *what
the platform must guarantee*.

- **Data layer.** TanStack Query (`@tanstack/react-query`) manages
  all server state through a typed `fetchJson<T>` wrapper that
  handles auth headers, CSRF, the active workspace slug, and JSON
  parsing. Every request URL is built as
  `/w/${workspaceSlug}/api/v1/...` from the `WorkspaceContext`
  React context (set once per mount of `<Shell />`).
- **Workspace-scoped query keys.** Every TanStack Query cache key
  includes the active `workspace_slug` as its first segment
  (e.g. `['w', 'acme', 'tasks', 'today']`). Switching workspaces
  does not clear the cache — the old keys stay resident — but no
  query from one slug can ever be served to a page rendering
  another slug. This is the client-side counterpart to the §01
  tenant-isolation invariant.
- **Optimistic mutations.** `onMutate` snapshots cache; `onError`
  rolls back; `onSettled` invalidates. On concurrent writes (§06
  last-write-wins) the UI surfaces a "Completed by <name>" toast —
  never silently drop local state.
- **SSE-driven invalidation.** One `EventSource('/w/${slug}/events')`
  per active workspace, re-established on workspace switch. Events
  `task.updated`, `approval.resolved`, `expense.decided`,
  `agent.message.appended`, `agent.action.pending`, and the
  `agent.turn.{started,finished}` pair (§11) drive
  `queryClient.invalidateQueries(...)` or direct
  `setQueryData(...)` scoped to the matching `['w', slug, ...]`
  prefix. No polling.
- **Route-split bundles.** Worker and owner/manager entry points are
  separate. Shared routes (see route contract above) land in both
  bundles. Only manager-only operational surfaces (`/dashboard`,
  `/properties`, `/approvals`, `/permissions`, etc.) are excluded from
  the worker bundle.
- **Inline approvals.** When `agent.action.pending` arrives for the
  current user, the chat surface (the right-hand `.desk__agent` rail
  on desktop for either role, the off-canvas drawer on manager mobile,
  or the full-screen `/chat` page on worker mobile) renders a
  confirmation card whose buttons call `/approvals/{id}/{decision}` —
  shared with the `/approvals` desk. Full flow and card-copy source in
  §11.
- **Agent turn indicator.** While an `agent.turn.started` is
  outstanding for the active thread (§11 "Agent turn lifecycle"),
  every chat surface — the shared `.desk__agent` rail, the manager
  mobile drawer, the worker full-screen `/chat`, and the
  task-scoped chat under `/tasks/{id}/chat` — renders a single
  non-interactive "typing" bubble in the log: three animated dots
  styled as a `chat-msg--typing` / `agent-msg--typing` variant of
  the regular agent bubble. The bubble carries a visually-hidden
  `sr-only` label "Agent is typing" (routed through the i18n seam,
  §18) so the existing `aria-live="polite"` log announces it; per
  §"Accessibility" colour is not the sole indicator of state. The
  client derives the visible state from SSE alone and clears the
  bubble on any of: a matching `agent.turn.finished`, a new
  `agent.message.appended` on the same scope, an
  `agent.action.pending` on the same scope (the turn resolved into
  an approval card, not a reply), an `EventSource` reconnect (stale
  state from the dropped session), or a local 60-second timeout
  (client-side safety net; the server should always pair its
  events). The indicator is not focusable and not a click target;
  it does not count toward the `chat-log` unread state.
- **Agent preferences surface.** The `/settings` page exposes an
  "Agent preferences" section with the workspace blob (editor if
  the user passes `agent_prefs.edit_workspace`, otherwise a
  disabled textarea with a "read via CLI" pointer). Each
  `/property/<id>` page carries the property blob under the same
  rules with `agent_prefs.edit_property`. `/me` carries the
  user's own blob (always editable by self). Each editor shows
  a live token counter (4 k soft / 16 k hard), a "sent to the
  model as written" banner (§15), and a "Revisions" link opening
  the history modal backed by `/agent_preferences/revisions/…`
  (§12). Full rules in §11 "Agent preferences".
- **Deferred external chat seam.** Off-app adapters are prepared in
  the architecture but not enabled in shipped v1. The mocks must not
  expose binding-management pages, phone-number linking, or provider
  configuration as active product surfaces. When those adapters are
  revisited, §23 remains the reference design.
- **Avatar editor on `/me`.** Clicking the large avatar on `/me`
  opens an in-modal editor (native `<dialog>`, same pattern as the
  new-task modal). The modal accepts an image from a file picker
  (`<input type=file accept="image/*">`) or, on mobile, the front
  camera (`capture="user"`). Selected image renders inside a
  circular viewport the user pans (pointer-drag) and zooms
  (range slider, pinch on touch). The stage sizes responsively
  (`min(320px, 86vw)` wide, 1:1 aspect) and pan/scale math is
  stage-pixel relative, so the crop is correct at any width.
  Below 640 px the dialog promotes to a full-height sheet
  (`.modal--sheet` class, 100 vw × 100 dvh, safe-area inset
  padding, action row anchored to the bottom) — tapping a
  32-pixel handle on the phone shouldn't compete with the
  stage for space. Save serialises the crop box to a 512×512
  WebP via an offscreen `<canvas>` and POSTs
  `multipart/form-data` to `/api/v1/me/avatar`; the server
  re-crops authoritatively from the original bytes + crop-box
  form fields (see §12). Cancel closes without writing. A
  "Remove photo" action (visible only when an avatar is set)
  calls `DELETE /api/v1/me/avatar`. The `<Avatar />` component
  renders an `<img>` at the configured size when `avatar_url`
  is present, else the initials circle; every site that shows
  a user (lists, tables, nav footer) goes through this
  component so a single upload updates every surface after the
  `me` / `employees` query invalidates. No presentational
  classes on the `<img>` — the circular crop lives on `.avatar`.

## Icons

- **Lucide only.** Every UI glyph (buttons, chips, banners, empty
  states, navigation) uses a [Lucide](https://lucide.dev) icon
  rendered from `lucide-react`. No emoji, no heroicons, no inline
  SVG, no icon fonts.
- **Data fields that reference an icon store the Lucide name**
  (PascalCase string, e.g. `Snowflake`, `Refrigerator`,
  `BrushCleaning`). The web renders them through a small whitelist
  (`components/AssetIcon.tsx`) — unknown names fall back to a
  generic glyph. This keeps server payloads string-based and keeps
  the bundle from pulling every Lucide icon.
- **Typographic unicode** (`✓`, `⊘`, `←`, `→`, `·`) is allowed —
  these are font glyphs, not decorative emoji, and they colour with
  `currentColor`. Prefer a Lucide icon when the glyph lives in JSX
  and carries semantic weight (e.g. a "done" banner); keep unicode
  for `::before`/`::after` CSS content and for directional text
  affordances in link labels.
- `aria-hidden="true"` on any icon that is purely decorative; a
  text label must sit alongside or be provided via `aria-label` on
  the parent.

## Page header

Every authenticated route — worker, manager, client, admin — renders
its chrome through a single `PageHeader` component
(`mocks/web/src/components/PageHeader.tsx`). Consistency here is what
makes the PWA feel like a native app: the same bar, in the same
place, with the same three slots, regardless of surface.

- **Three slots.** `leading` (navigation affordance: hamburger or
  back), `title` + `sub` (page identity), `trailing` (at most one
  primary action; everything else collapses into a `⋯` overflow
  menu). No floating back-links inside content panels; no ad-hoc
  action rows above content — every top-of-page affordance goes
  through the header.
- **One primary action.** The trailing slot renders at most one
  button. Secondary actions live in the overflow menu (opened by a
  `⋯` icon-button that sits in the slot when the menu is
  non-empty). This rule holds at every breakpoint: if a page wants
  two buttons on desktop, it still picks one for the bar on phone
  and the second lives in the overflow menu there.
- **Sticky on phone, safe-area aware.** Below 720px the header is
  `position: sticky; top: 0` with `padding-top:
  env(safe-area-inset-top)` so a notched iOS PWA renders the bar
  under the notch, not behind it. The bar is a compact 52px + safe
  area; the title uses the body sans at ~1.05rem (not the large
  Fraunces display — the compact bar has to survive portrait 360px
  phones without truncation). On desktop (≥720px) the header stays
  non-sticky and keeps the large Fraunces title.
- **Back button from a route map.** `PageHeader` accepts a `back`
  prop, but sub-pages normally omit it — the component resolves the
  parent through `mocks/web/src/lib/routeParents.ts` (`/task/:id →
  /today`, `/asset/:id → /assets`, `/instructions/:id →
  /instructions`, `/property/:id → /properties`,
  `/user/:id → /users`, `/history → /me`, etc.). The leading slot
  renders a left-chevron icon-button (Lucide `ChevronLeft`,
  ≥44×44 tap target per Accessibility). This retires every
  `className="back-link"` inside page bodies; a page that wants a
  non-default parent passes `back={{ to, label }}`.
- **Hamburger folds into the leading slot.** The legacy
  `.desk__mobile-bar` strip that showed a hamburger + "crew.day"
  wordmark above the page header on manager/admin phone is gone.
  The page header reads a `ShellNavContext`
  (`mocks/web/src/context/ShellNavContext.tsx`) provided by
  `ManagerLayout` / `AdminLayout`; when that context reports a
  drawer is available, the leading slot renders a `Menu` icon-button
  whose `onClick` toggles the drawer. Sub-pages still win the slot
  with a back button — if both a drawer and a back-parent exist,
  back wins (the user can open the drawer from inside a parent
  page, which is where the hamburger is useful). On the worker
  shell, `ShellNavContext` is absent — no hamburger ever renders.
- **Default: every authenticated route has a title.** The title
  is the first thing a screen reader announces on navigation and
  doubles as the landmark for VoiceOver's rotor.
- **Opt-out for immersive / identity-rooted surfaces.** A page may
  skip `PageHeader` entirely when the content itself establishes
  identity and a compact bar would only steal vertical real estate.
  In v1 that means `/chat` (full-screen conversation; the bottom
  tab is the navigational label) and `/me` (opens on a large avatar
  + name card that reads as the title). Any new opt-out must be
  justified in the PR — the default is "has a header".
- **Overflow menu.** `overflow` is an array of `{ label, icon?,
  onSelect, destructive? }` items. Rendered as a native `<dialog>`
  popover on all breakpoints for keyboard + screen-reader
  behaviour parity; closes on Escape, on outside click, and on
  selection. Destructive items render in Rust. When the array is
  empty the `⋯` button is not rendered.
- **No app branding on inner pages.** The crew.day wordmark lives
  in the left-nav / drawer and on the public shell only. A user
  on `/today` should see "Today", not "crew.day" above it — the
  brand is set; the page identity is what needs the eye-time.

## Accessibility (v1 gate)

WCAG 2.2 AA. Concretely:

- Logical tab order; focus ring visible at AA contrast on Moss.
- Forms labeled (no placeholder-only labels).
- Color never the sole indicator of state (icons + text too).
- **Click targets ≥ 44×44 CSS pixels** on every interactive element
  (header leading/trailing icon-buttons, footer tabs, task-card
  CTAs, checklist ticks, chat mic, icon-only buttons). Icons inside
  smaller graphic boundaries get transparent padding to reach the
  floor.
- No layout jumps > 100ms; loading states use `aria-busy`.
- Buttons are buttons; links are links; no `div` onclick.
- Release playbook tests with NVDA / VoiceOver / TalkBack.

## Browser support

- Latest 2 versions of Chromium, Safari, Firefox.
- iOS 16+ and Android 10+.
- Legacy / unsupported: `/unsupported.html`.

## Performance targets

- Today screen LCP < 1.5s on a 2019 mid-range Android over 4G from a
  nearby region.
- Worker bundle on `/today`: < 170 KB gzipped.
- Owner/manager bundle on `/dashboard`: < 220 KB gzipped.
- Service worker install: < 1s on first visit.
- Offline → online reconciliation: < 60s for 50 queued actions.

## PWA constraints

- Generated by **Vite PWA plugin** (Workbox) in
  `mocks/web/vite.config.ts`.
- **One service worker per workspace.** The worker is registered
  with `scope: '/w/<slug>/'`, so its fetch interception and cache
  do not span workspaces. A user with access to multiple
  workspaces has one SW registration per slug; each has its own
  Cache Storage bucket, IndexedDB partition, and Background Sync
  queue. Uninstalling (leaving) a workspace unregisters its SW.
  The bare-host surface (`/signup`, `/login`, `/select-workspace`)
  has **no** service worker.
- **Slug-keyed cache entries.** Workbox strategies for API routes
  include the slug in the cache key
  (`/w/<slug>/api/v1/tasks/today`), so even if a key somehow
  escapes the scope, it cannot collide with another workspace's
  entry.
- **Cache-first shell** for JS/CSS/logo.
- **Stale-while-revalidate** for today's tasks; the cached response
  is served immediately and TanStack Query refreshes via
  invalidation when the background fetch lands.
- **Background Sync outbox** for write-behind completions — body +
  idempotency key stored in per-slug IndexedDB, FIFO, replayed on
  reconnect. Offline taps that reference a pending photo use a
  local `blob` id; the service worker uploads the photo first,
  then replays the completion with the real `file_id`.
- **Caps.** 50 queued completions; 50 MB queued photo bytes (both
  configurable) **per workspace**. Older entries are evicted with
  a visible "could not keep queued for longer" warning.
- **Manifest.** `display: standalone`, `theme_color: #3F6E3B`,
  `background_color: #FAF7F2`. Shortcuts are workspace-scoped
  (`/w/<slug>/today`, `/w/<slug>/schedule`,
  `/w/<slug>/my/expenses/new`); on multi-workspace devices the
  install prompt is offered per workspace, so each installs as a
  distinct PWA with its own name (`crew.day — <workspace.name>`)
  and icon. Icons at 192, 512, maskable.

## Native wrapper readiness

The native mobile app is a **separate project** (§00 N4); this repo
owns the web platform and publishes a stable contract the native
shell can build against. The working assumption is that the native
project ships **one app per deployment** that covers every workspace
the user belongs to (same in-app workspace switcher as the web SPA;
per-workspace PWA installs remain a browser-only affordance — the
native app is not replicated per tenant). A Capacitor / TWA /
WKWebView shell that loads the PWA is the expected baseline; richer
native bridges are additive.

Guarantees this repo makes to the wrapper:

- **Every authenticated route is a plain HTTPS URL** under
  `/w/<slug>/...` with no custom schemes. A tap on a push
  notification, an email link, or an OS deep-link handler routes
  to the same route the web SPA uses; the native shell decides
  whether to open it in a native view or in its embedded WebView.
- **Passkey RP is strictly bound to the hostname** (§15). A native
  app that declares `associatedDomains`
  (`webcredentials:<host>`, `applinks:<host>`) and the matching
  Android `assetlinks.json` relation can share passkeys with the
  web SPA without any server-side change. This repo does **not**
  ship `/.well-known/assetlinks.json` or
  `/.well-known/apple-app-site-association` in v1 — they are a
  future deployment-level configuration, off by default, authored
  when the native project publishes its first signed build. Until
  then the wrapper falls back to its embedded WebView's cookie
  session for passkey flows.
- **Responsive down to 360 px wide.** Every authenticated route —
  worker and manager alike — is usable at `min-width: 360px` on a
  touch device, with tap targets ≥ 44×44 CSS pixels (Accessibility
  gate above). The manager shell collapses `.desk__nav` to a
  hamburger and `.desk__agent` to a bottom-dock drawer below
  720 px; this has to remain true under any future layout change.
  The release playbook (§17) verifies the full authenticated
  sitemap at a 360 px Playwright viewport.
- **No User-Agent gating.** The server never rejects a request
  based on UA string alone. A custom UA like
  `crewday-android/1.2.3 (Android 14; Pixel 9)` is acceptable and
  surfaces in audit (`http_user_agent`). A future `X-Crewday-Client`
  header is reserved — when set it is logged and telemetered but
  never load-bearing for auth or routing decisions.
- **Same-origin CSP is compatible with WebView.** The strict
  `frame-ancestors 'none'` (§15) only restricts iframe embedding;
  it does **not** restrict WKWebView / Android WebView / Chrome
  Custom Tabs / TWA, which are not iframes. The native shell
  loads the PWA directly and sees the same CSP as a desktop
  browser.
- **Session cookies survive WebView navigation.** The
  `__Host-crewday_session` cookie (§15) is `Secure; HttpOnly;
  SameSite=Lax`. It works in a WebView over HTTPS the same way it
  works in a desktop browser; the native shell does not need a
  separate bearer-token path just to keep the user signed in.
- **Agent-message delivery over push** is specified in §10 and the
  REST surface `/me/push-tokens` is reserved in §12. Activation is
  gated on the native project: until it ships, `POST /me/push-tokens`
  returns `501 push_unavailable` and the delivery worker collapses
  to the email tier.

What this repo deliberately does **not** provide:

- No device-authorization / OAuth flow — the wrapper authenticates
  the same way the web does (passkey over HTTPS in the embedded
  browser). If the native project later needs a bearer-token
  handshake it can layer one on using the existing PAT surface
  (§03 "Personal access tokens") without a spec change here.
- No native-side signing keys, no app attestation check. If the
  native project introduces one it files a spec change here.
- No custom URL scheme (`crewday://...`). Every link is HTTPS.
- No per-tenant branding, icons, or app IDs. A deployment that
  wants branded apps operates its own fork of the native project.

## Internationalization readiness

All user-facing strings go through an i18n helper backed by a JSON
message catalog, even though v1 ships English only. See §18.

## Gaps vs. mocks

The mocks are the living spec, but several contracts named above
are not yet implemented there. Track new gaps via `bd create` rather
than adding to this list:

- **User / manager terminology.** `mocks/web/src/App.tsx` still
  routes `/employees`, `/employee/:eid`, `/employee/:eid/leaves`;
  the spec (§05) canonicalises these to `/users`, `/user/<id>`,
  `/user/<id>/leaves`. Mocks will rename during the
  `user_work_roles` / `work_engagement` migration.
- **Availability & holidays.** No mock pages yet for
  `/availability-overrides`, `/user/<id>/availability`, or
  `/holidays` (the manager CRUD surfaces). The worker-facing
  self-service flows (request leave, request override) live on
  `/schedule` and ship with the mocks — see §06 "Schedule ruleset
  (per-property rota)" for the model and this section for the
  worker surface. `/scheduler` inline per-date edits are specified
  here but not yet fully implemented in the mocks; the day-drawer
  click-to-reassign / click-to-override paths are the first
  increment.
- **Agent sidebar (`.desk__agent`).** The shared component
  `mocks/web/src/components/AgentSidebar.tsx` mounts in both
  `EmployeeLayout` and `ManagerLayout` (role-scoped via a `role`
  prop). Full wiring — SSE `agent.action.pending`, inline confirmation
  cards (currently manager-only), voice input, compaction-aware lazy
  load — is still to come.
- **PWA service worker.** Vite PWA plugin is not yet wired in
  `mocks/web/vite.config.ts`; offline outbox and background sync
  are specified but unimplemented. Once wired, the SW registration
  must use `scope: '/w/<slug>/'` (see "PWA constraints" above).
- **Workspace path prefix.** The spec canonicalises every
  authenticated route as `/w/<slug>/<route>` (§01 "Workspace
  addressing"). The current `mocks/web/src/App.tsx` route tree
  is still single-workspace and unprefixed. Migration is
  deliberately deferred to the first app-code phase (§19 Phase 1)
  so the mock rewrite and the real routing middleware land in
  lockstep. Deep-linking, NavLink construction, and
  `fetchJson<T>` URL building will all gain a workspace-slug
  parameter at that point.
- **Self-serve signup.** `/signup`, `/signup/verify`, and
  `/select-workspace` are specified (§03) but not yet in the
  mocks. Same timing as the path-prefix migration above.

### Workspace switcher (mocks parity)

The shared `<SideNav />` renders a chip immediately under the brand
row that lists every workspace the current user has an active
grant on (workspace-scope `role_grant` rows or transitively via
`property_workspace`). The chip shows the active workspace's
display name and the user's grant role on it (`Manager`,
`Worker`, `Client`); clicking it opens a menu of the alternates.

- **Cookie-backed.** The selected workspace lives in
  `crewday_workspace`; the server is authoritative. `POST
  /workspaces/switch/{wsid}` sets the cookie. The mock honours the
  same cookie everywhere `current_workspace_id(request)` is read.
- **Hidden when single-workspace.** The chip does not render when
  the user has only one workspace, so the default tenant case
  stays uncluttered.
- **Cache invalidation.** Switching workspaces drops every cached
  TanStack Query entry — every query is potentially scoped to the
  previous tenant.

This is the mock-side stand-in for the future `/w/<slug>/...`
addressing scheme (§01); the URL path itself remains
single-workspace and unprefixed in the mocks until the real
routing middleware lands (§19 Phase 1).

### Property "Sharing & client" tab

`/property/{id}` carries a "Sharing & client" tab in addition to
Overview, Areas, Stays, Assets, Instructions, Closures, and
Settings. It surfaces the multi-belonging model from §02 + §04:

- **Memberships table** — every workspace linked to the property
  via `property_workspace`, with its `membership_role`
  (Owner / Managed / Observer) and the date the link was added.
- **Billing client card** — when `property.client_org_id` is set,
  the linked organization's name, legal name, tax ID, currency,
  and role flags. Empty state when null.
- **Owner of record** — a display-only line for `owner_user_id`
  when set (the natural person behind the owner workspace).
- **Invite / Revoke controls** — visible only when the active
  workspace is the property's `owner_workspace`. "Invite
  workspace" opens a dialog that collects the proposed role
  (`managed` or `observer`), an optional target workspace (by
  slug) for a pre-addressed invite, and a toggle for
  `share_guest_identity` (off by default). Submitting creates a
  `property_workspace_invite` (§22) and shows the resulting
  invite URL with a **Copy link** control so the inviter can
  share it through WhatsApp, email, or another channel; the
  dialog also offers an "Email invite to owner of <to_workspace>"
  shortcut for pre-addressed invites. "Revoke" removes any
  non-owner `property_workspace` row; it is distinct from
  revoking a pending invite. Both destructive paths route through
  the always-approval-gated set in §22.
- **Pending invites** — table of `property_workspace_invite` rows
  this workspace originated but have not yet been accepted or
  rejected. Each row offers **Copy link** and **Revoke**.
  Invites inbound to this workspace appear on the workspace-level
  inbox (`/inbox`) rather than the per-property page — the
  accepting owner picks which workspace to accept into there.

The Organizations page (`/organizations`) under ADMIN lists every
organization in the active workspace, with a detail panel
showing rate cards (`client_rate` / `client_user_rate`), recent
`booking_billing` rows, and inbound / outbound `vendor_invoice`
entries. Empty state guides the operator to "agency mode" via
the property page.

### Client portal shell (§22)

The third role pill alongside Employee / Manager renders the
**client portal** — a separate `ClientLayout` with a narrower
nav: Properties (only those whose `client_org_id` matches one of
the user's `binding_org_id`s on the active workspace), **Scheduler**
(read-only rota calendar scoped to the client's own properties;
user cells render **first name + work role only** per §15
cross-workspace visibility — no last names, no contact, no tasks
the client has no business seeing), Billable hours (read-only
`booking_billing` rollup), Quotes (with accept / reject controls —
acceptance still routes through the unconditionally
approval-gated set in §22 in production; the mock applies it
in-memory), Invoices (read-only `vendor_invoice` list, no mark-paid
control, **Upload proof** control on any invoice in `status =
approved` that drops files into `proof_of_payment_file_ids`).
Reminders (§22) follow the usual agent-message delivery chain;
clients silence them by unbinding WhatsApp (§23) or toggling the
per-workspace `invoice_reminders.enabled` setting if they are
admin on their own workspace. The agent sidebar is intentionally
not mounted here: clients don't drive the crew.day agent in v1.
