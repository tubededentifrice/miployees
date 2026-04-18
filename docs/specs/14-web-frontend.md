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
/recover                   → break-glass recovery
/styleguide                (dev + staging only)
/healthz, /readyz, /version  (no auth; see §12)
```

### Per-workspace public

```
/w/<slug>/enroll/<token>   → invited user completing enrollment
/w/<slug>/guest/<token>    → guest welcome page
```

### Shared (any authenticated role, workspace-prefixed)

Routes rendered under whichever shell matches the viewer's role
(`ManagerLayout` for managers, `EmployeeLayout` for workers) — picked
by a single `<Shell />` wrapper component in `App.tsx`. All paths
below are relative to `/w/<slug>/`.

```
today            week             task/<id>
my/expenses      me               history          issues/new
shifts           asset/<id>
```

### Worker-only (under /w/<slug>/)

```
chat             asset/scan
```

Footer bottom-nav: `Today · Week · Chat · My Expenses · Me`. Chat is
first-class, **not** a floating action button. The bottom-nav `Chat`
tab is the **mobile** entry to the agent — it navigates to the
full-screen `/chat` page. On desktop (≥720px) the worker shell drops
the bottom-nav (the shared `<SideNav />` takes over) and the agent
moves to the right-hand `.desk__agent` rail (§14 "Desktop shell")
shared with the manager layout, so `Chat` is no longer listed in the
left-nav. The `/shifts` tab is hidden when `time.clock_mode` is `auto`
or `disabled`.

### Owner/Manager (under /w/<slug>/)

```
dashboard                      properties
property/<id>                  property/<id>/closures
stays                          users
user/<id>                      user/<id>/leaves
user/<id>/availability         leaves
availability-overrides         holidays
permissions                    templates
schedules                      instructions
instructions/<id>              assets
asset/<id>                     asset_types
documents                      inventory
pay                            expenses
approvals                      audit
webhooks                       llm
settings
```

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
  My Week      → /week
  My Expenses  → /my/expenses
  My History   → /history
```

These routes render under `ManagerLayout` (the shared-route rule
above), so managers navigate their personal work without leaving the
desktop shell.

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
  `agent.message.appended`, and `agent.action.pending` drive
  `queryClient.invalidateQueries(...)` scoped to the matching
  `['w', slug, ...]` prefix. No polling.
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

## Accessibility (v1 gate)

WCAG 2.2 AA. Concretely:

- Logical tab order; focus ring visible at AA contrast on Moss.
- Forms labeled (no placeholder-only labels).
- Color never the sole indicator of state (icons + text too).
- **Click targets ≥ 44×44 CSS pixels** on every interactive element
  (back-links, footer tabs, task-card CTAs, checklist ticks, chat
  mic, icon-only buttons). Icons inside smaller graphic boundaries
  get transparent padding to reach the floor.
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
  (`/w/<slug>/today`, `/w/<slug>/shifts/clock-in`,
  `/w/<slug>/my/expenses/new`); on multi-workspace devices the
  install prompt is offered per workspace, so each installs as a
  distinct PWA with its own name (`Crewday — <workspace.name>`)
  and icon. Icons at 192, 512, maskable.

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
  `/holidays`.
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
- **Self-serve signup + workspace switcher.** `/signup`,
  `/signup/verify`, `/select-workspace`, and the user-menu
  "Switch workspace" action are specified (§03, §14 "Workspace
  switcher") but not yet in the mocks. Same timing as the path-
  prefix migration above.
