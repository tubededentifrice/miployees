# 05 — Users, work roles, and worker settings

> Historical note: in v0 this document was titled "Employees and
> roles". v1 merges every human login into a single `users` table
> and expresses permissions through `role_grants` (§02). What used
> to be the `employee` entity no longer exists; the people who do
> work are `users` with one or more `user_work_role` rows and a
> `work_engagement` per workspace. This document covers the
> **work** side of that model (which jobs a user performs, how worker-
> facing behaviour is configured); §02 and §03 cover identity, grants, and
> auth.

## User (as worker)

Every human is a `users` row (§02). A user becomes a **worker** in
a given workspace when they hold a `role_grants` row with
`grant_role = 'worker'` on that workspace or one of its properties
**and** at least one `user_work_role` row binding them to a
`work_role` in that workspace.

- A user without any `user_work_role` cannot be granted
  `grant_role = 'worker'` on a workspace — the write fails with
  422 `error = "worker_requires_work_role"`. Property-scoped
  worker grants may exist without a workspace-level work-role
  binding, but only if the grant has an explicit
  `work_role_id` override inline (see below).
- A user **can** hold zero work roles and still exist in the
  system as an owner, manager, or client.
- A user may hold the same `work_role` in more than one
  workspace — each binding is an independent `user_work_role`
  row. Rates, worker settings, and schedules are per (user, workspace).

### Fields that formerly lived on `employee`

Several columns that used to sit on the v0 `employee` row now live
on distinct entities:

- Identity (display name, email, avatar, timezone, language,
  locale, phone, emergency contact, notes) — on `users` (§02).
- Engagement data (engagement_kind, supplier_org_id,
  pay_destination_id, reimbursement_destination_id, started_on,
  archived_on) — on `work_engagement` (§02, §22), scoped per
  (user, workspace).
- Permission / authority — on `role_grants` (§02).

A user who performs work under more than one workspace has one
`users` row, one or more `work_engagement` rows (one per
workspace), zero or more `user_work_role` rows per workspace, and
whichever `role_grants` rows the workspace's owner/manager sees fit.

## Work role

A work role is a named job definition the workspace uses: maid,
cook, driver, gardener, pool_tech, handyman, nanny, personal
assistant, concierge, property_manager, etc. Work roles are
**workspace-defined** — the system ships a starter set but they are
regular rows, renameable and addable.

(In v0 this entity was called `role`. It was renamed to `work_role`
in v1 because `role` is now ambiguous with `grant_role` from §02.)

### Fields

| field             | type     | notes                                   |
|-------------------|----------|-----------------------------------------|
| id                | ULID PK  |                                         |
| workspace_id      | ULID FK  |                                         |
| key               | text     | stable slug: `maid`, `cook`. Unique per `(workspace_id, key)`. Editable but changing it audit-logs as `work_role.rekey` and breaks external references that hard-code the slug. |
| name              | text     | display: "Maid", "Cuisinier/ère"        |
| description_md    | text     |                                         |
| default_settings_json | jsonb | optional recommended worker-setting defaults to copy into a new `work_engagement.settings_override_json` when the first role in this workspace is assigned. These are provisioning hints, not a second runtime resolver. |
| icon_name         | text     | Lucide icon name, PascalCase (e.g. `BrushCleaning`, `Wrench`). See §14 "Icons". |
| deleted_at        | tstz?    |                                         |

### Starter roles

Seeded on first boot; each is just a row, editable/removable later:

`maid`, `cook`, `driver`, `gardener`, `handyman`, `nanny`,
`pool_tech`, `concierge`, `personal_assistant`, `property_manager`.

## User work role

Links a user to a work role **within a workspace**, with per-assignment
overrides, so the same person can be both cook (full pay rate) and
driver (lower rate) in the same workspace, or `maid` in Workspace A
without being `maid` in Workspace B.

(In v0 this entity was called `employee_role`.)

### Fields

| field               | type     | notes                                   |
|---------------------|----------|-----------------------------------------|
| id                  | ULID PK  |                                         |
| user_id             | ULID FK  | references `users.id`                   |
| workspace_id        | ULID FK  | the workspace this job applies to       |
| work_role_id        | ULID FK  | references `work_role.id`               |
| started_on          | date     |                                         |
| ended_on            | date?    |                                         |
| pay_rule_id         | ULID FK? | override the default pay rule on the user's `work_engagement` in this workspace |

Unique: `(user_id, workspace_id, work_role_id, started_on)`.

**Invariant.** Every active `user_work_role` row must correspond to
a `work_role` whose `workspace_id` matches the row's `workspace_id`
— a user cannot borrow a work role definition across workspaces;
each workspace defines its own catalog.

**Invariant.** If the user holds a `role_grants` row with
`grant_role = 'worker'` on this workspace, they must have ≥ 1
`user_work_role` row here. The inverse is not required — a user
may hold only a property-scoped worker grant plus the
corresponding `user_work_role`, with no workspace-scope grant.

## Property work role assignment

A `user_work_role` may be constrained to one or more properties. A
maid might work both Villa Sud and Apt 3B at different rates. If no
property assignments exist, the `user_work_role` is eligible for
**all** properties of the workspace — useful for generalists.

(In v0 this entity was called `property_role_assignment`.)

| field                    | type     | notes                                   |
|--------------------------|----------|-----------------------------------------|
| id                       | ULID PK  |                                         |
| user_work_role_id        | ULID FK  | replaces v0's `employee_role_id`        |
| property_id              | ULID FK  |                                         |
| schedule_ruleset_id      | ULID FK? | recurring weekly rota at this property; see §06 "Schedule ruleset (per-property rota)". Null = property assignment with no declared rota — user is eligible per `user_weekly_availability` alone. |
| property_pay_rule_id     | ULID FK? | rarer: per-property rate override       |

A maid handling Villa Sud on weekday mornings and Apt 3B on
weekday afternoons has two `property_work_role_assignment` rows,
each pointing at its own `schedule_ruleset` (or sharing one if the
pattern is the same). The assignment algorithm in §06 uses both
the availability precedence stack and the per-property rota to
decide eligibility; generalists (no assignments) keep their
workspace-wide eligibility and skip the rota filter.

## Work engagement (pointer)

`work_engagement` carries the per-(user, workspace) pay pipeline
data (engagement_kind, supplier_org_id, pay_destination_id,
reimbursement_destination_id, started_on, archived_on). Its
canonical definition is in §02; the pipeline behaviour it drives
(payslips, vendor invoices) is in §09 and §22.

A user who holds one or more `user_work_role` rows in a workspace
**must** have a `work_engagement` row in that workspace (active or
archived). The write-side invariant is: creating the first
`user_work_role` for a (user, workspace) creates the
`work_engagement` row if missing. Archiving every `user_work_role`
for that (user, workspace) does not auto-archive the engagement —
the operator does that explicitly.

## Archive / reinstate

v1 archive semantics distinguish three scopes:

1. **Archive a `user_work_role`** — the user is no longer eligible
   for that job in that workspace. Existing assignments for that
   row are unassigned on the next generation tick; historical
   completions stay. No auth change.
2. **Archive a `work_engagement`** — the user is off-boarded from
   one workspace. Sets `archived_on = today` on the engagement,
   archives every `user_work_role` they hold in that workspace,
   and removes them from forward-looking task assignments for that
   workspace. Fires `work_engagement.archived`. Historical pay,
   bookings, and payslips are preserved. Other workspaces where the
   same user has engagements are untouched.
3. **Archive a `users` row** — the person is off-boarded
   deployment-wide. Revokes passkeys and sessions immediately
   (§03), archives every `work_engagement` they hold, and records
   `users.archived_at`. `role_grants` rows persist for audit but
   resolve as inactive. Fires `user.archived`. Archiving a user
   while they hold the **sole** `owner` grant on any scope is
   blocked (see §02 `users.archived_at` invariant).

Reinstatement follows the same hierarchy: reinstate a
user_work_role, a work_engagement, or the whole user. Reinstating
a whole user issues them a fresh magic link (since their prior
passkeys are gone) and fires `user.reinstated`.

The words "end", "terminate", "off-board", "rehire", "soft-off" are
**not** used in the schema, API, or UI. When writing new code or
docs, use archive/reinstate.

## Worker settings

Worker-facing runtime behaviour uses the single settings cascade from
§02. There is no parallel capability resolver. Two rules keep the
model simple:

1. **Permissions decide whether a user may do something.**
2. **Settings decide how that thing behaves.**

Examples:

- Whether a worker may submit an expense is a permission decision
  (`expenses.submit` in the action catalog below).
- Whether submitted expenses should trigger OCR autofill is a setting
  (`expenses.autofill_receipts`) resolved through the settings cascade.
- Whether a worker can comment on a task is a permission decision
  (`task_comment.create`).
- Whether a task requires a full checklist or photo evidence is a
  settings decision (`tasks.checklist_required`,
  `evidence.policy`).

### Recommended role defaults

`work_role.default_settings_json` exists so a workspace can keep
role-specific recommendations ("maids default to auto clocking and
required evidence at Villa Sud") without reintroducing a second live
runtime policy system. These defaults are copied into the appropriate
workspace / property / work-engagement setting layers when an
owner/manager accepts them; once copied, runtime resolution happens
only through the settings cascade in §02.

### Evidence-policy stack

The evidence-policy stack is an instance of the **settings cascade**
(§02 "Settings cascade"), canonical key `evidence.policy`, scope
`W/P/U/WE/T`. The description below documents the domain-specific
semantics; the cascade mechanics (layer columns, override shape,
resolution order) are canonical in §02.

A dedicated key in the unified settings cascade computes whether a
task needs photo evidence. Five layers, in order
from broadest to most specific:

1. **Workspace default** — always concrete (`require | optional |
   forbid`), seeded at first boot; never `inherit`.
2. **Property** — `inherit | require | optional | forbid`.
3. **Unit** — `inherit | require | optional | forbid`. Single-unit
   properties see no behavioural change; the unit inherits from the
   property.
4. **Work engagement** (per (user, workspace)) —
   `inherit | require | optional | forbid`.
5. **Task** (template-derived, with per-task override) —
   `inherit | require | optional | forbid`.

**Most specific wins.** Resolution walks from the task inward:
task → work_engagement → unit → property → workspace, stopping at
the first **concrete** (non-`inherit`) value; layers set to
`inherit` (the non-root default) pass through. The common case is
"follow the workspace default unless a property, a unit, a specific
engagement, or a specific task deliberately narrows or widens the
rule." `forbid` at any layer is absolute — even a later `require`
on a more specific layer cannot override it (see §06 "Evidence
policy inheritance" for the override-vs-forbid interaction and §09
for how `require | optional | forbid` interact with completion).

## Permissions: surface, groups, and action catalog

v1 splits permission into two independent layers — see §02
"Unified identity" and `role_grants` / `permission_group` /
`permission_rule`. This section documents what each **surface**
(persona) sees, the workspace-wide **action catalog** consulted
by the resolver, and the root-only actions that only members of
the `owners` permission group may perform.

### Surface grants at a glance

The `role_grants.grant_role` enum is the **surface** — which UI
shell the user sees and which rows RLS lets them read. Authority
to perform a specific action is resolved separately through the
action catalog + `permission_rule` rows.

| role      | typical user                                  | primary surface                                              |
|-----------|-----------------------------------------------|--------------------------------------------------------------|
| `admin`   | deployment operator (self-host owner, SaaS ops) | the bare-host `/admin` shell (§14): LLM + provider config, deployment-wide usage, workspace lifecycle, signup settings, admin-team management, deployment audit. Only valid on `scope_kind = 'deployment'` grants. |
| `manager` | head of household, co-manager, agency staff   | full admin dashboard (properties, tasks, payroll, orgs). Which actions they may *perform* depends on rules + owners-group membership. |
| `worker`  | a maid, driver, cook, contractor              | PWA: assigned tasks, own bookings, own expenses, own profile |
| `client`  | a villa owner who pays an agency              | read-only portal for bookings/invoices billed to them; accept/reject quotes (gated) |
| `guest`   | a short-term stay occupant (post-v1)          | reserved; v1 uses tokenized `guest_link` (§04) not grants    |

Governance (archive the workspace, transfer it to another person,
edit permission rules, hard-purge data) is anchored to the
`owners` **permission group** on each scope, not to a grant_role.
A workspace creator is auto-seeded as `grant_role = manager` +
member of `owners` (see §02 "Bootstrap"); subsequent admins can
be added to `owners` without giving them `manager`, and vice
versa.

### Deployment scope

`scope_kind = 'deployment'` is the process-level scope: a single
synthetic row per running deployment, seeded at first boot with
`id = '00000000000000000000000000'` (the reserved system ULID
already used for the pseudo-system actor). Every deployment has
exactly one, and there is no cascade with workspace scopes — a
user's deployment grants are independent of every workspace
grant they hold.

- Only two `grant_role` values are valid here: `admin` (surface
  grant → `/admin` shell) and — for symmetry with other scopes —
  `owners` group membership (governance anchor for the
  deployment). No `manager`, `worker`, `client`, or `guest`
  grants on the deployment.
- The **`owners@deployment`** permission group is seeded on first
  boot with exactly one member: the operator who ran
  `crewday admin init` (recipe A / B) or the first workspace
  created on a managed deployment (recipe D). That user is also
  auto-issued a `role_grants` row with
  `(scope_kind='deployment', scope_id='00000000000000000000000000',
  grant_role='admin')` so the `/admin` shell is reachable from
  their first login.
- The **`managers@deployment`** permission group is seeded empty
  and tracks everyone currently holding
  `(scope_kind='deployment', grant_role='admin')` — same derived-
  from-grants pattern as `managers@<workspace>`.
- Extending the admin team is `groups.manage_members` +
  `role_grants.create` on the deployment scope (both default to
  `owners@deployment` only); adding to `owners@deployment` itself
  requires the root-only `groups.manage_owners_membership`.

### Admin surface

Users holding `(scope_kind='deployment', grant_role='admin')`
see the `/admin` shell on the bare host (§14 "Admin shell").
They do **not** get workspace access through this grant — a
deployment admin who is also a workspace manager holds two
separate `role_grants` rows. The manager left-nav renders an
"Administration" link (§14) when the caller has any active
deployment grant, which deep-links to `/admin/dashboard`; the
link is omitted for users with no deployment grant.

Admins see:

- LLM provider config, capability → model assignments, pricing
  table, the deployment-wide daily/30-day usage aggregate, and
  per-workspace spend rollups (§11).
- Workspace lifecycle actions: list, trust, archive; raise or
  lower a workspace's rolling 30-day budget cap.
- Self-serve signup toggle + throttle settings (§03, §15).
- Deployment audit (every deployment-scoped action; §15).
- Admin-team membership (`owners@deployment`,
  `managers@deployment`) and deployment-scope permission rules.

Admins never see (from the `/admin` surface alone):

- The contents of any workspace (tasks, bookings, PII, stays).
  RLS still filters by `workspace_id`; the deployment grant does
  not widen workspace reads. A user who needs both views holds a
  workspace grant too, switches through the workspace picker,
  and is audit-logged as that workspace's actor there.
- Decrypted account numbers, payout manifests, or any
  interactive-session-only endpoint response (§11).

### Worker surface (web UI / PWA)

Users whose highest surface grant in a scope is `worker` see only:

- Their own profile (read + limited update: display name, avatar,
  timezone, emergency contact, language). Avatar uploads go
  through `POST /me/avatar` (§12) and are **self-only** — a
  manager can rename an employee but cannot replace their face.
  Clearing the avatar reverts to the initials circle computed
  from `display_name`.
- Tasks assigned to them, plus unassigned tasks at properties in
  their scope that match their `user_work_role`s.
- Instructions scoped to those properties/areas/global (read-only).
- Their own bookings, payslips (read-only), and expense claims.
- Staff-visible subset of property notes (§04) — not access codes or
  wifi passwords unless an owners-group member explicitly shares.
- Comments on tasks they can see, plus authoring comments on those.
- The staff chat assistant when the workspace enables chat for that
  worker via the settings cascade / agent policy.

Workers never see:

- Other users' wages, hours, or pay rules.
- Admin invite links.
- The API token list.
- The audit log.
- Financial aggregates.

This worker surface is an RLS / data-filter concern, not a rule
concern. A worker-surface user who is also an `owners` member
would have broad authority in principle, but because the worker
PWA does not expose administrative actions at all, there is no
UI surface for them to exercise it. Power users in this shape
switch to the manager surface (via a separate `role_grants`
record with `grant_role = 'manager'`).

### Client surface (web UI)

Users whose grant in a scope is `client` see only:

- Properties they are billed for (property-scope grant) or
  properties tagged with their `binding_org_id` (workspace-scope
  grant). Read-only view: name, address, a sanitized work log.
- Bookings at those properties: date, role_key, duration, rate,
  amount (via `booking_billing` rollups).
- Work orders and quotes billed to them: full detail so they can
  accept (gated — see §11) or reject.
- Vendor invoices billed to them: full detail, including
  `payout_destination` redacted beyond last 4 IBAN digits (§15).
  May upload proof-of-payment files against any invoice billed to
  them (action `vendor_invoices.upload_proof`, §22). May **not**
  mark an invoice paid — that remains an internal bookkeeping flag
  of the billing workspace.
- Invoice reminders reach them on the usual fallback chain (SSE →
  push → WhatsApp → email, §10) per the `invoice_reminders.*`
  cascade settings (§22). Reminders are opt-out per user.

Clients never see:

- Other clients of the workspace.
- Workers' pay rules (they see agency billing rates, not worker
  compensation).
- Staff-only instructions, staff chat, audit log, API tokens,
  workspace settings.
- Any data tagged to a `binding_org_id` other than their own.

Client-surface users may be subjects of `permission_rule` rows
on the actions they are eligible for (e.g. `quotes.accept`,
`vendor_invoices.approve_as_client`), giving the owner precise
control over which clients may sign off on what.

### Manager surface

Users with `grant_role = 'manager'` see the admin dashboard.
Whether a specific action is allowed to them is resolved per
action against the action catalog below. A manager who is also
a member of the scope's `owners` group additionally picks up the
root-only actions (archive workspace, transfer scope, edit
rules, admin purge). A manager who is not in `owners` can be
given any other action through a `permission_rule` — including
every administrative action except the root-only set — by
members of `owners`.

### Action catalog

Every authority check in the system names an `action_key` from
this catalog. The resolver described in §02 "Permission
resolution" treats these keys as canonical; writes to
`permission_rule` referencing a key not in this catalog fail
with 422 `unknown_action_key`.

Each entry declares:

- `key` — dotted, stable. Prefer existing namespaces (`users.*`,
  `properties.*`, `tasks.*`, `expenses.*`, `work_orders.*`,
  `quotes.*`, `vendor_invoices.*`, `permissions.*`, `groups.*`,
  `organizations.*`, `scope.*`, `workspaces.*`, `admin.*`,
  `deployment.*`).
- `valid_scope_kinds` — which `permission_rule.scope_kind`
  values the key accepts. E.g. `expenses.approve` is
  workspace+property; `workspace.archive` is workspace only.
- `default_allow` — ordered list of system-group keys granted
  the action when no rule matches. Empty list = default-deny.
- `root_only` — `true` means only members of the scope's
  `owners` group may perform it, regardless of any rule. Used
  for governance-critical actions.
- `root_protected_deny` — `true` means owners cannot be denied.
  Non-owners may still be allowed via rules. Used for
  administrative actions that must never be accidentally
  locked out.

The catalog below is the v1 canonical set. New actions require a
spec edit here before the backend can accept them.

#### Root-only actions (governance)

These are always restricted to `owners` members on the scope (or
a containing scope when the target is a property). Rules
targeting these keys are accepted at write time for future
extensibility but have no effect on the resolver.

| action_key                       | valid_scope_kinds              | notes                                                                                   |
|----------------------------------|--------------------------------|-----------------------------------------------------------------------------------------|
| `workspace.archive`              | `workspace`                    | Archive an entire workspace. §15.                                                       |
| `organization.archive`           | `organization`                 | Archive an org-scope record. §22.                                                       |
| `scope.transfer`                 | `workspace`, `organization`, `deployment` | Transfer governance (install a new sole member in `owners`, remove self). §15. On `deployment`, hands the operator seat to someone else. |
| `permissions.edit_rules`         | `workspace`, `property`, `organization`, `deployment` | Create, revoke, or edit `permission_rule` rows on the scope. Root-only so "editing rules" can never be delegated into a foot-gun. |
| `groups.manage_owners_membership`| `workspace`, `organization`, `deployment` | Add/remove members of the `owners` group specifically. Distinct from `groups.manage_members` below. |
| `admin.purge`                    | `workspace`                    | Hard-delete workspace data. CLI only (§13); the action still flows through the resolver to keep the audit trail consistent. |
| `deployment.rotate_root_key`     | `deployment`                   | Envelope-key rotation. Host-CLI-only (§13); flows through the resolver for the audit row, but has no HTTP surface. |

#### Rule-driven actions (ship with sane defaults)

Everything below is fully rule-configurable. `default_allow`
captures who the system assumes should do each action absent
any explicit rule — so a fresh install works with zero
configuration.

| action_key                              | valid_scope_kinds              | default_allow                 | root_protected_deny | spec |
|-----------------------------------------|--------------------------------|-------------------------------|:---:|------|
| `scope.view`                            | `workspace`, `property`, `organization` | `owners, managers, all_workers, all_clients` | ✅ | §14 |
| `scope.edit_settings`                   | `workspace`, `property`, `organization` | `owners, managers`            | ✅ | §02 |
| `users.invite`                          | `workspace`, `property`, `organization` | `owners, managers`            | ✅ | §03 |
| `users.archive`                         | `workspace`, `property`, `organization` | `owners, managers`            | ✅ | §05 |
| `users.edit_profile_other`              | `workspace`, `property`        | `owners, managers`            | —  | §02 |
| `employees.read`                        | `workspace`                    | `owners, managers`            | —  | §12 — gates the manager roster surface (`GET /employees`). Workers read their own profile via `/auth/me`; the cross-roster view is manager-only by design. |
| `role_grants.create`                    | `workspace`, `property`, `organization` | `owners, managers`            | ✅ | §02 |
| `role_grants.revoke`                    | `workspace`, `property`, `organization` | `owners, managers`            | ✅ | §02 |
| `groups.create`                         | `workspace`, `organization`    | `owners, managers`            | ✅ | §02 |
| `groups.edit`                           | `workspace`, `organization`    | `owners, managers`            | —  | §02 |
| `groups.manage_members`                 | `workspace`, `organization`    | `owners`                      | ✅ | §02 |
| `properties.create`                     | `workspace`                    | `owners, managers`            | —  | §04 |
| `properties.archive`                    | `workspace`, `property`        | `owners, managers`            | ✅ | §04 |
| `properties.edit`                       | `workspace`, `property`        | `owners, managers`            | —  | §04 |
| `properties.read`                       | `workspace`                    | `owners, managers`            | —  | §12 — gates the **full** projection of the workspace properties roster (`GET /properties`): every field, including the §22 governance-adjacent `client_org_id` / `owner_user_id` and the per-property `settings_override` blob. The endpoint itself accepts every authenticated workspace member — workers fall through to a narrowed projection scoped to the properties they hold a `role_grant` on, with those three governance fields masked to `null` / `null` / `{}` (cd-yjw5). The cross-roster listing is no longer 403 for workers because the worker pages (`HistoryPage`, `NewTaskModal`, `SubmitExpenseForm`) need the name + city + timezone of properties they already see in property-pinned data; the masking keeps the privacy posture intact. |
| `properties.view_access_codes`          | `workspace`, `property`        | `owners, managers`            | —  | §04 |
| `work_roles.manage`                     | `workspace`                    | `owners, managers`            | —  | §05 |
| `tasks.create`                          | `workspace`, `property`        | `owners, managers, all_workers` | — | §06 |
| `tasks.assign_other`                    | `workspace`, `property`        | `owners, managers`            | —  | §06 |
| `tasks.complete_other`                  | `workspace`, `property`        | `owners, managers`            | —  | §06 |
| `tasks.skip_other`                      | `workspace`, `property`        | `owners, managers`            | —  | §06 |
| `tasks.comment`                         | `workspace`, `property`        | `owners, managers, all_workers` | — | §06 — post a `kind='user'` message on a task's agent-inbox thread. Workers carry it by default because the thread is where they report progress; the domain service gates `kind='agent' \| 'system'` separately. |
| `tasks.comment_moderate`                | `workspace`, `property`        | `owners, managers`            | —  | §06 — delete another user's comment, or edit a `kind='user'` comment after the 5-minute author grace window. Service checks this via `app.authz.require` on the non-author branch of `delete_comment`; owners short-circuit via their `owners` group membership. |
| `bookings.view_other`                   | `workspace`, `property`        | `owners, managers`            | —  | §09 |
| `bookings.amend_other`                  | `workspace`, `property`        | `owners, managers`            | —  | §09 |
| `bookings.assign_other`                 | `workspace`, `property`        | `owners, managers`            | —  | §09 |
| `bookings.cancel`                       | `workspace`, `property`        | `owners, managers`            | —  | §09 |
| `bookings.create_pending`               | `workspace`, `property`        | `owners, managers, all_workers` | — | §09 |
| `payroll.lock_period`                   | `workspace`                    | `owners, managers`            | ✅ | §09 |
| `payroll.issue_payslip`                 | `workspace`                    | `owners, managers`            | ✅ | §09 |
| `payroll.view_other`                    | `workspace`, `property`        | `owners, managers`            | —  | §09 |
| `pay_rules.edit`                        | `workspace`, `property`        | `owners, managers`            | —  | §09 |
| `expenses.submit`                       | `workspace`, `property`        | `owners, managers, all_workers` | — | §09 |
| `expenses.approve`                      | `workspace`, `property`        | `owners, managers`            | —  | §09 |
| `expenses.reimburse`                    | `workspace`                    | `owners, managers`            | ✅ | §09 |
| `leaves.create_self`                    | `workspace`                    | `owners, managers, all_workers` | — | §09 |
| `leaves.edit_others`                    | `workspace`                    | `owners, managers`            | —  | §09 |
| `leaves.view_others`                    | `workspace`                    | `owners, managers`            | —  | §09 |
| `inventory.adjust`                      | `workspace`, `property`        | `owners, managers`            | —  | §08 |
| `inventory.stocktake`                   | `workspace`, `property`        | `owners, managers`            | —  | §08 — open, edit, and commit an `inventory_stocktake` session. |
| `instructions.edit`                     | `workspace`, `property`        | `owners, managers`            | —  | §07 |
| `assets.edit`                           | `workspace`, `property`        | `owners, managers`            | —  | §21 |
| `api_tokens.manage`                     | `workspace`                    | `owners, managers`            | ✅ | §03 |
| `audit_log.view`                        | `workspace`, `property`, `organization` | `owners, managers`            | ✅ | §02 |
| `organizations.create`                  | `workspace`                    | `owners, managers`            | —  | §22 |
| `organizations.edit`                    | `workspace`, `organization`    | `owners, managers`            | —  | §22 |
| `organizations.edit_pay_destination`    | `workspace`, `organization`    | `owners, managers`            | ✅ | §22 |
| `work_orders.view`                      | `workspace`, `property`        | `owners, managers, all_workers, all_clients` | — | §22 |
| `work_orders.create`                    | `workspace`, `property`        | `owners, managers`            | —  | §22 |
| `work_orders.assign_contractor`         | `workspace`, `property`        | `owners, managers`            | —  | §22 |
| `quotes.submit`                         | `workspace`, `property`        | `owners, managers` (contractors with property grant also match via rule) | — | §22 |
| `quotes.accept`                         | `workspace`, `property`        | `owners, managers, all_clients` | — | §22 |
| `vendor_invoices.submit`                | `workspace`, `property`        | `owners, managers`            | —  | §22 |
| `vendor_invoices.approve`               | `workspace`, `property`        | `owners, managers`            | ✅ | §22 |
| `vendor_invoices.approve_as_client`     | `workspace`, `property`        | `all_clients`                 | —  | §22 |
| `vendor_invoices.upload_proof`          | `workspace`, `property`        | `owners, managers, all_clients` | — | §22 |
| `vendor_invoices.remove_proof`          | `workspace`, `property`        | `owners, managers`            | —  | §22 |
| `property_workspace_invite.create`      | `workspace`, `property`        | `owners, managers`            | —  | §22 |
| `property_workspace_invite.accept`      | `workspace`                    | `owners, managers`            | —  | §22 |
| `property_workspace_invite.reject`      | `workspace`                    | `owners, managers`            | —  | §22 |
| `property_workspace_invite.revoke`      | `workspace`, `property`        | `owners, managers`            | —  | §22 |
| `property_workspace.revoke`             | `workspace`, `property`        | `owners, managers`            | ✅ | §22 |
| `messaging.comments.author_global`      | `workspace`, `property`        | `owners, managers, all_workers` | — | §10 |
| `messaging.report_issue.triage`         | `workspace`, `property`        | `owners, managers`            | —  | §10 |
| `time.clock_self`                       | `workspace`, `property`        | `owners, managers, all_workers` | — | §09 |
| `time.edit_others`                      | `workspace`, `property`        | `owners, managers`            | —  | §09 |
| `agent_prefs.edit_workspace`            | `workspace`                    | `owners, managers`            | —  | §11 |
| `agent_prefs.edit_property`             | `workspace`, `property`        | `owners, managers`            | —  | §11 |
| `deployment.view`                       | `deployment`                   | `owners, managers`            | ✅ | §14 |
| `deployment.llm.view`                   | `deployment`                   | `owners, managers`            | —  | §11 |
| `deployment.llm.edit`                   | `deployment`                   | `owners, managers`            | —  | §11 |
| `deployment.usage.view`                 | `deployment`                   | `owners, managers`            | —  | §11 |
| `deployment.workspaces.view`            | `deployment`                   | `owners, managers`            | —  | §16 |
| `deployment.workspaces.trust`           | `deployment`                   | `owners, managers`            | —  | §03, §16 |
| `deployment.workspaces.archive`         | `deployment`                   | `owners`                      | ✅ | §16 |
| `deployment.budget.edit`                | `deployment`                   | `owners, managers`            | —  | §11 |
| `deployment.signup.edit`                | `deployment`                   | `owners, managers`            | —  | §03, §16 |
| `deployment.settings.edit`              | `deployment`                   | `owners`                      | ✅ | §16 |
| `deployment.audit.view`                 | `deployment`                   | `owners, managers`            | ✅ | §15 |

`deployment` entries use the system groups seeded on the
deployment scope: `owners@deployment` (the operator seat,
always ≥ 1 member) and `managers@deployment` (derived from every
active `(scope_kind='deployment', grant_role='admin')` grant).
`all_workers@deployment` and `all_clients@deployment` are not
seeded — a deployment has no workers and no clients, only
admins.

Notes:

- Workers, clients, and contractors **default** to the actions
  they need to do their job:
  - `all_workers` carries `expenses.submit`,
    `messaging.comments.author_global`, `tasks.create`,
    `tasks.comment`, `bookings.create_pending`,
    `time.clock_self`, and any task/booking actions scoped to
    themselves (viewing / amending / declining *your own*
    booking is not in this catalog — those are identity-scoped
    actions, not scope-scoped, and do not flow through the
    resolver).
    Workers may create tasks; if
    `is_personal = true` (the quick-add default) the task is
    private to the creator; otherwise it is a normal team task.
    Workers may propose ad-hoc bookings via `bookings.create_pending`
    (status `pending_approval` — see §09).
  - `all_clients` carries `quotes.accept` (subject to §11
    gating), `work_orders.view`, and
    `vendor_invoices.approve_as_client`.
- Catalog entries with `root_protected_deny = ✅` mean: the
  `owners` group cannot be denied this action even by a
  deliberate deny rule. Non-owners can still be granted the
  action via an allow rule.
- `scope.view` appearing at `default_allow: owners, managers,
  all_workers, all_clients` is intentional — it means "every
  surface-entitled user may see the scope exists". RLS still
  filters which rows they can read (§15). Property-pinned
  worker access works through the same lens via `role_grant`:
  a `role_grant(grant_role='worker', scope_property_id=<P>)`
  row gives the worker `scope.view` on property `P` (and on
  every entity owned by it) without granting any
  workspace-wide membership; conversely, a workspace-wide
  worker grant (`scope_property_id IS NULL`) fans out across
  every live property in the workspace. The `GET /properties`
  roster (cd-yjw5, §12) consults this directly: workers see
  the union of properties their grants visit, and the
  governance-adjacent fields (`client_org_id`,
  `owner_user_id`, `settings_override`) are masked to safe
  defaults so the cross-roster listing never reveals the §22
  billing-org / owner-of-record coupling to a worker.
- Identity-scoped actions ("edit my own profile", "amend my own
  booking", "decline my own booking", "view my own payslip") are
  **not** listed as workspace-assignable action keys; they are
  self-service verbs anchored on the authenticated `users` /
  `work_engagement` record. Their runtime behaviour still follows
  the settings cascade (`bookings.pay_basis`,
  `bookings.auto_approve_overrun_minutes`, `evidence.policy`, etc.).

### How a rule narrows or widens a default

Examples a workspace owner might configure through the admin
UI, each becoming one or more `permission_rule` rows:

- "Only my spouse approves expenses": delete the implicit
  default by inserting a workspace-scope rule
  `(expenses.approve, all_workers, deny)`, then
  `(expenses.approve, all_managers, deny)`, then
  `(expenses.approve, user=spouse, allow)`. Cleaner, insert a
  single `(expenses.approve, group=family, allow)` plus
  denylist the broader groups.
- "Julie can accept quotes on behalf of DupontFamily at Villa
  du Lac but nowhere else": property-scope rule at Villa du
  Lac `(quotes.accept, user=Julie, allow)`. No other rule
  needed — the workspace-scope `all_clients` default already
  covers her if she is a client; the property rule widens it
  for her specifically.
- "Kids can view tasks but never complete them on behalf of
  others": workspace-scope rules `(tasks.assign_other,
  group=kids, deny)`, `(tasks.complete_other, group=kids,
  deny)`, `(tasks.skip_other, group=kids, deny)`; the default
  `all_workers` action set still lets them complete their own
  tasks (identity-scoped).
- "Our cleaning agency (CleanCo) may view — but not edit —
  vendor invoices at Villa du Lac": property-scope
  `(vendor_invoices.approve, user=Julie, deny)` overrides
  the workspace default while the read default for clients
  continues to apply.

### Rule administration UX (anchor)

Rendered at `/permissions` (§14). Mocks are the living UI spec;
minimum behavioural shape the implementation must satisfy:

- **Groups** page: list workspace groups; for each, show members
  (or "auto-populated from grant_role=X" for derived ones),
  allow add/remove on user-defined groups and `owners`.
- **Action rules** page: grouped by action; each row shows
  default + current effective rules at workspace + per-property
  override widgets. A live "who can do this?" preview resolves
  against a chosen user to make the model debuggable.

## Permissions (API tokens)

Covered in §03. Three token kinds:

- **Scoped standalone token** — carries an explicit `scopes` list
  and bypasses `role_grants` entirely. Created by owners/managers
  via the `api_tokens.manage` action (see catalog above).
- **Delegated token** — inherits the delegating user's
  `role_grants` and work-role bindings at request time; when the
  user's grants change, the delegated token's authority changes
  immediately. Created from a passkey session by the user whose
  chat agent will use it (§11).
- **Personal access token (PAT)** — limited to the `me:*` scope
  family, mints are row-filtered to the creating user's own data.
  Creating a PAT is an **identity-scoped self-service verb**
  (same category as "edit my own profile" and "amend my own
  booking"); it has **no entry in the action catalog** and cannot
  be assigned through a `permission_rule`. Every authenticated
  user — worker, client, or manager — may create up to 5 PATs
  for themselves. A manager cannot create a PAT for someone
  else: the subject narrowing is anchored on the session, not
  a body field.

## Example (real world)

> Maria is a maid at Villa Sud (twice a week) and a nanny at the
> main residence (once a week). The manager expects photo evidence
> for cleaning but not for nannying; her bookings are scheduled by
> the manager, and pay derives from the booked hours regardless of
> exact arrival / departure time.

Both properties live in the same workspace `HomeOps`. This is
modeled as:

- 1 `users` row (Maria)
- 1 `role_grants` row:
  `(user=Maria, scope='workspace', scope_id=HomeOps, grant_role='worker')`
- 1 `work_engagement` row:
  `(user=Maria, workspace=HomeOps, engagement_kind='payroll', ...)`
- 2 `user_work_role` rows:
  `(user=Maria, workspace=HomeOps, work_role=maid)`,
  `(user=Maria, workspace=HomeOps, work_role=nanny)`.
- 2 `property_work_role_assignment` rows:
    - `(user_work_role=maid, property=Villa_Sud)`
    - `(user_work_role=nanny, property=Main_Residence)`.

Maria's behavioural differences are expressed through the settings
cascade, not a live capability chain. Example overrides:

- `work_engagement.settings_override_json`:
  `{bookings.pay_basis: "scheduled", notifications.email_digest: true}`
- `property(Villa_Sud).settings_override_json`:
  `{evidence.policy: "require"}`
- `property(Main_Residence).settings_override_json`:
  `{evidence.policy: "optional"}`

Pay rules are separate and attach to `pay_rule.work_engagement_id`
pointing at Maria's `HomeOps` engagement (§09).

## Example (multi-workspace: Vincent)

> Vincent owns a villa "Villa du Lac". He runs his own
> operations there (one live-in driver, Rachid, on payroll)
> and also pays an agency "CleanCo" to send a maid, Joselyn,
> twice a week. Vincent also owns a seaside apartment that he
> manages entirely on his own, with no agency involvement.

This needs two workspaces:

- `VincentOps` — Vincent's own workspace.
- `AgencyOps` — CleanCo's workspace (serves many clients).

### Users

- `users(Vincent)` — one row.
- `users(Rachid)` — one row.
- `users(Joselyn)` — one row.
- `users(Julie)` — CleanCo's manager.

### Organizations

- `organization(DupontFamily)` — Vincent's billing legal entity
  (tax id, pay destination, etc.). `is_client = true`,
  `is_supplier = false`. Lives in `AgencyOps`'s scope as a client
  row.
- `organization(CleanCo)` — the agency itself, as a counterparty
  only when viewed from Vincent's side. Not needed unless
  `VincentOps` wants to bill-back its own costs.

### Role grants

- `role_grants(Vincent,  scope='workspace',    scope_id=VincentOps, role='manager')` + `permission_group_member(group=owners@VincentOps, user=Vincent)`
- `role_grants(Rachid,   scope='workspace',    scope_id=VincentOps, role='worker')`
- `role_grants(Vincent,  scope='organization', scope_id=DupontFamily, role='manager')` + `permission_group_member(group=owners@DupontFamily, user=Vincent)`
- `role_grants(Vincent,  scope='workspace',    scope_id=AgencyOps,  role='client', binding_org_id=DupontFamily)`
- `role_grants(Julie,    scope='workspace',    scope_id=AgencyOps,  role='manager')` + `permission_group_member(group=owners@AgencyOps, user=Julie)` (CleanCo governance anchor)
- `role_grants(Joselyn,  scope='workspace',    scope_id=AgencyOps,  role='worker')`

### Properties

- `property(Villa_du_Lac)`:
  `owner_user_id = Vincent`, `client_org_id = DupontFamily` (§22).
- `property(Seaside_Apt)`:
  `owner_user_id = Vincent`, `client_org_id = NULL` (self-managed).

### `property_workspace`

- `(Villa_du_Lac, VincentOps,  membership_role='owner_workspace')`
- `(Villa_du_Lac, AgencyOps,   membership_role='managed_workspace')`
- `(Seaside_Apt,  VincentOps,  membership_role='owner_workspace')`

### Work engagements

- `work_engagement(Rachid,   workspace=VincentOps, kind='payroll', ...)`
- `work_engagement(Joselyn,  workspace=AgencyOps,  kind='payroll', ...)`
- `work_engagement(Julie,    workspace=AgencyOps,  kind='payroll', ...)`
  (optional — only if Julie draws pay from CleanCo through this system.)

### Work roles

- `user_work_role(Rachid,  workspace=VincentOps, work_role=driver)`
- `user_work_role(Joselyn, workspace=AgencyOps,  work_role=maid)`

Plus `property_work_role_assignment` rows narrowing Joselyn's maid
role to `Villa_du_Lac` only (she has other clients too), and
Rachid's driver role to `Villa_du_Lac` and `Seaside_Apt`.

### What each person sees

- **Vincent** logs in and has a workspace switcher:
    - `VincentOps` (owner) — full view of Rachid, both properties,
      inventory, assets, finances, and a "billed from CleanCo"
      panel fed from his client grant on `AgencyOps`.
    - `AgencyOps` (client, binding DupontFamily) — read-only view
      of Joselyn's bookings at Villa du Lac, vendor invoices
      CleanCo has raised against DupontFamily, accept/reject
      quotes.
- **Rachid** logs in and sees only `VincentOps`, his assigned
  tasks at Villa du Lac and Seaside Apt, his bookings, his profile.
- **Joselyn** logs in and sees only `AgencyOps`, her assigned
  tasks at the properties she has `property_work_role_assignment`
  rows for (including Villa du Lac), her bookings, her profile. She
  does not see Rachid or Vincent's direct operation; the shared
  property `Villa_du_Lac` appears in her list because it sits in
  `AgencyOps` via the junction, but its `owner_workspace` metadata
  is hidden from her worker view.
- **Julie** logs in, sees `AgencyOps` as a manager: every CleanCo
  worker, every CleanCo client property (including Villa du Lac
  as billed to DupontFamily), every vendor invoice.
