# 20 — Glossary

Terms used across the spec. Definitive form; if code or doc disagrees,
fix the offender.

- **Actor.** The kind of principal responsible for an action, recorded
  on `audit_log` and shift/claim rows: `user | agent | system`. The
  v0 `manager` and `employee` actor kinds collapse into `user`;
  `actor_grant_role` captures the role under which the action was
  taken.
- **Agent.** A non-human actor. Standalone agents are authenticated
  by scoped API tokens (`actor_kind = 'agent'`). Embedded agents use
  **delegated tokens** that act with the full authority of their
  delegating user (`actor_kind` = the user's kind). See §03, §11.
- **Agent (embedded).** The owner/manager-side or worker-side chat
  agent described in §11. Default model `google/gemma-4-31b-it`;
  tool surface is the full CLI + REST surface of the delegating user
  (no filtered catalog). Voice input is capability-gated.
- **Agent preferences.** Free-form Markdown guidance that shapes
  how the composition/conversation LLM capabilities in §11 talk
  back. Three layers stacked broadest-first: workspace →
  property → user. Soft rules only; does not override the
  structured settings cascade (§02). Stored in
  `agent_preference` (§02) with full history in
  `agent_preference_revision`. Editing is gated by
  `agent_prefs.edit_workspace` / `agent_prefs.edit_property`
  (§05) at workspace and property scopes; user-layer prefs are
  self-writable only. Reads are open to anyone with a grant on
  the scope (CLI/API); the UI restricts the editor to users
  with write access. See §11 "Agent preferences".
- **Agent approval mode.** Per-user enum on `users.agent_approval_mode`
  — `bypass | auto | strict`, default `strict`. Decides when the
  user's own embedded chat agent pauses for an inline confirmation
  card in the same chat channel before executing a mutating
  delegated-token request. `bypass` never pauses, `auto` pauses on
  routes carrying an `x-agent-confirm` annotation, `strict` pauses
  on every mutation. Workspace policy (§11) still gates its own
  list regardless of mode. See §11 "Per-user agent approval mode".
- **Action confirmation annotation (`x-agent-confirm`).** Optional
  OpenAPI route extension in §12 declaring the inline confirmation
  card's `summary` template, `risk`, `fields_to_show`, and `verb`.
  Single source of truth shared by the CLI (§13), the REST
  middleware, and the chat UI — so the card copy is authored once
  per action, not per surface. See §11 "Action confirmation
  annotation".
- **Inline confirmation card.** The "[Confirm] [Reject]" card
  rendered in a user's chat channel (the right-hand `.desk__agent`
  sidebar on desktop for either role, or the mobile entry — manager
  bottom-dock drawer / worker `/chat` page) when their own embedded
  agent proposes a gated action. The same row also appears on
  `/approvals` for owner/manager oversight. See §11 "Inline approval
  UX".
- **Auto-clock.** The `auto` value of `time.clock_mode` (§05, §09).
  First checklist tick or task action of the day opens a shift;
  `time.auto_clock_idle_minutes` of inactivity closes it. Per-property
  override can force `manual` or `disabled`. See §09 "Disputed
  auto-close" for the re-open semantics.
- **Anomaly suppression.** A manager-recorded rule that silences a
  specific `(anomaly_kind, subject_id)` pair until a required
  `suppressed_until` timestamp (§11). Permanent suppression is not
  offered by design.
- **Approvable action.** A write that requires owner or manager
  approval at the **workspace-policy** layer, regardless of token
  scope (§11). Lands on `/approvals` desk as an `agent_action`
  row, TTL 7 days by default. Distinct from the per-user inline
  confirmation card produced by **agent approval mode**, which is
  decided by the delegating user themselves in their own chat.
- **Archive / reinstate.** The canonical verbs for off-boarding and
  bringing back a worker's `work_engagement` (§05). Replaces "end",
  "terminate", "rehire".
- **Area.** A subdivision of a property (kitchen, pool, Room 3).
  Optionally scoped to a specific unit (`area.unit_id`); null means
  shared/property-level.
- **Asset.** A tracked physical item installed at a property — an
  appliance, piece of equipment, or vehicle. Carries condition, status,
  warranty, and QR token. See §21.
- **Asset action.** A maintenance operation defined on an asset
  (e.g. "clean filter every 30 days"). Can be linked to the task
  scheduling system; completion updates `last_performed_at`. See §21.
- **Asset document.** A file attached to an asset or a property —
  manual, warranty, invoice, certificate, etc. Carries optional
  `expires_on` and `amount_cents` for TCO tracking. See §21.
- **Asset type.** A catalog entry describing a category of equipment
  (e.g. "Air conditioner", "Pool pump"). System-seeded types ship
  with default maintenance actions; managers add workspace-custom
  types. See §21.
- **Assignment.** The linkage of a user to a work_role+property
  (`property_work_role_assignment`) and, per task, the pointer
  `task.assigned_user_id`. There is no separate `task_assignment`
  entity — task assignment is just a column.
- **Audit log.** Append-only ledger of all state-changing actions.
- **Availability override.** A date-specific override of a user's
  weekly availability pattern. Adding work is self-service
  (auto-approved); reducing availability requires owner/manager
  approval. Only approved overrides affect the assignment algorithm.
  See §06 `user_availability_overrides`.
- **Binding org.** On a `role_grants(grant_role='client')` of
  workspace scope, the `binding_org_id` narrows the client's view
  to data tagged to that organization within the workspace. See §02.
- **Break-glass code.** Single-use recovery code issued to users
  who hold a `manager` surface grant or membership in any
  `owners` permission group; generates exactly one magic link on
  redemption (§03).
- **Worker setting.** A runtime behaviour key resolved through the
  single settings cascade (§02, §05): workspace → property → unit →
  work_engagement → task, with the most specific concrete value
  winning. Permissions stay separate — see **Action catalog** and
  **Permission rule**.
- **Condition (asset).** The physical state of an asset: `new | good |
  fair | poor | needs_replacement`. Changes are audit-logged as
  `asset.condition_changed`. See §21.
- **Checklist item.** A row in `task_checklist_item` — one tickable
  line on a task, seeded from the template's
  `checklist_template_json`. Per-item tick state is authoritative.
- **Client (grant role).** A user granted read-visibility to data
  they are billed for, plus the ability to accept/reject quotes
  and invoices tied to them. Held via a `role_grants` row with
  `grant_role = 'client'` and either a `binding_org_id` (workspace
  scope) or a property scope. See §02, §22.
- **Completion.** Terminal state for a task; has evidence and a
  completing user. Under concurrent writes, last-write-wins with a
  `task.complete_superseded` audit entry for the displaced one (§06).
- **Correlation ID.** Per-request identifier (or caller-supplied
  `X-Correlation-Id`) that groups audit rows. Not a workflow-lifetime
  concept.
- **Digest run.** A single execution of the daily summary
  email/notification pipeline (§10). Records the digest template,
  recipients, send time, and delivery outcomes.
- **Employee.** v0 entity name; **replaced by `users` + `work_engagement`
  in v1**. Retained in prose as a general term for "a person who
  performs work you pay for". No longer a schema table — see
  "User" and "Work engagement".
- **User leave.** Approved absence window (§06) keyed by `user_id`.
  See §06 `user_leave`. Unapproved requests do not affect assignment.
- **Evidence.** Artifact attached to a completion — photo, note, or
  checklist snapshot.
- **Evidence policy.** Photo-evidence requirement resolved by
  walking a five-layer stack workspace → property → unit →
  work_engagement → task (§05 "Evidence-policy stack", §06
  "Evidence policy inheritance"). Values are
  `inherit | require | optional | forbid`;
  the workspace root is always concrete. **Most specific wins** — the
  walk goes from task inward, stopping at the first concrete
  (non-`inherit`) value — with one domain-specific rule: `forbid` at
  any layer is absolute (camera picker hidden regardless of more
  specific layers).
- **File.** Shared blob-reference row (§02 `file`). Pluggable backend;
  local disk in v1.
- **Guest link.** A tokenized URL sent to a stay guest that opens a
  welcome page showing property info, wifi, house rules, and a
  guest-visible checklist. See §04.
- **Handle.** Optional user-friendly slug (`maid-maria`) stored in a
  per-entity `handle` column where useful. Unique per parent scope.
- **Household.** **v0 term; replaced by Workspace in v1.** Retained
  here so historical references in migrations, ADRs, and older code
  remain resolvable. New code and new docs use Workspace.
- **iCal feed.** An external calendar subscription (Airbnb, VRBO,
  Booking, generic) polled periodically to import stays into a unit.
  Feed URLs are stored in `secret_envelope` (§15). See §04.
- **Instruction.** A standing SOP attached at global / property /
  area / link scope (§07). `instruction_link` is canonical;
  `task.linked_instruction_ids` is a denormalized cache.
- **Inventory movement.** An append-only ledger row recording a change
  to `inventory_item.on_hand`. Reason enum: `restock | consume |
  adjust | waste | transfer_in | transfer_out | audit_correction`.
  See §08.
- **Issue.** A user-reported problem tracked with state
  (`open | in_progress | resolved | wont_fix`) and possibly converted
  to a task.
- **Magic link.** Single-use, signed URL used to enroll or recover a
  passkey. Consumes a break-glass code (if that's the source)
  regardless of whether the link is later clicked.
- **Grant role (surface).** The UI-shell / data-filter persona
  a user holds on a scope: `manager | worker | client | guest`.
  Stored on
  `role_grants`. See §02.
- **Manager (grant role).** A user placed on the **admin UI
  shell** on a scope. What they may actually *do* is resolved
  per-action via the action catalog and `permission_rule` —
  the `manager` grant names the surface, not a fixed authority
  level. All managers are peers in v1. Held via a `role_grants`
  row with `grant_role = 'manager'`.
- **Model assignment.** The capability → model mapping (§11).
- **Off-app reach-out.** Deferred feature in which the agent may send
  a WhatsApp message to a user for low-stakes checks. Opt-in is the
  presence of an active `chat_channel_binding`; unlinking it opts
  the user out (§23). SMS is intentionally not supported. See §10.
- **Owner / owners group.** **v0** had an `owner` grant_role
  with "exactly one per scope". **v1** replaces this with the
  `owners` **permission group**: a system group seeded per
  scope whose members hold root authority. The invariant is now
  "the `owners` group has ≥ 1 active member". `grant_role =
  'owner'` no longer exists. See `permission_group` in §02.
- **Owner workspace.** On `property_workspace`, the workspace whose
  creator controls access grants for the property
  (`membership_role = 'owner_workspace'`). Other workspaces that
  share the property are `managed_workspace` or `observer_workspace`.
  See §02.
- **Passkey.** WebAuthn platform or roaming authenticator credential.
- **Permission group.** A named set of users that can appear as
  the subject of a `permission_rule`. Workspace- or
  organization-scoped. Four system groups are seeded per scope:
  `owners` (explicit membership, the governance anchor),
  `managers`, `all_workers`, `all_clients` (derived from
  `role_grants`). User-defined groups (`family`, `parents`,
  `front_desk`, etc.) are also explicit. Groups do not nest. See
  §02.
- **Permission rule.** A row in `permission_rule` saying, on a
  scope (workspace / property / organization), a subject (user
  or group) is `allow`'d or `deny`'d an `action_key`. Deny
  beats allow within a scope; more-specific scope beats
  broader. Catalog defaults apply when no rule matches. See §02
  "Permission resolution".
- **Action catalog.** The canonical list of administrative
  actions the permission resolver understands. Each entry
  declares `key`, `valid_scope_kinds`, `default_allow` (system
  groups granted by default), `root_only` (owners-only,
  un-ruleable — e.g. `workspace.archive`, `admin.purge`), and
  `root_protected_deny` (owners cannot be denied). Canonical
  list in §05.
- **Root-only action.** An action flagged `root_only` in the
  catalog. Only members of the scope's `owners` group may
  perform it, regardless of any `permission_rule`.
- **Surface grant.** Synonym for the `role_grants.grant_role`
  persona — `manager | worker | client | guest`. Names the UI
  shell and the RLS filter, not the authority.
- **Pay period.** A date-bucket inside which shifts roll up into a
  payslip. `open → locked → paid`; `paid` is set automatically when
  every contained payslip reaches `paid`.
- **Payout destination.** A per-user **or** per-organization record
  naming where money lands: bank account, reloadable card, wallet,
  cash, or other. Users may hold more than one; the
  `work_engagement` row carries default pointers for pay and for
  reimbursements separately (§09). Full account numbers live in
  `secret_envelope`; only a `display_stub` (IBAN last-4 + country,
  card last-4, wallet handle) is returned over the API. Creating,
  editing, or changing a default is always approval-gated for bearer
  tokens (scoped and delegated) — crewday does not execute
  payments, but routing decisions are security-critical and treated
  accordingly.
- **Payout snapshot.** The immutable `payout_snapshot_json` captured
  on a payslip at the `draft → issued` transition. Records where pay
  and each reimbursement went (`display_stub` only — never full
  account numbers), independent of later destination edits or
  archives. The stored payslip PDF is rendered from the snapshot, so
  the PDF is always safe to keep long-term.
- **Payout manifest.** A streaming, not-stored JSON artifact from
  `POST /payslips/{id}/payout_manifest` that decrypts full account
  numbers at the moment the operator pushes funds. **Owner/manager
  passkey session only** (no bearer tokens, even via approval — see
  "Interactive-session-only endpoint" below). Every fetch is audit-
  logged; no blob is persisted; the idempotency cache does not retain
  the response; a second fetch within 5 minutes raises a digest
  alert. Once the payout secrets are GDPR-erased, the endpoint
  returns 410 Gone (§09, §15).
- **Interactive-session-only endpoint.** An HTTP endpoint that
  refuses all bearer tokens (scoped and delegated) and requires a
  live passkey session, because the response contains decrypted
  secret material that must not land in any persisted store
  (including `agent_action.result_json`). v1 list (§11): the payout
  manifest. Owner/manager passkey session only.
- **Delegated token.** A bearer token created by a logged-in user
  that inherits their full `role_grants` and work-role bindings.
  Audit records use the delegating user's identity
  (`actor_kind = 'user'`, `actor_id`), with `agent_label` and
  `agent_conversation_ref` fields flagging the action as
  agent-executed and linking back to the triggering conversation.
  See §03.
- **Host-CLI-only administrative command.** A `crewday admin`
  verb with no HTTP surface at all, agent or human: envelope-key
  rotation, offline lockout recovery, hard-delete purge (§11). Run
  on the deployment host; authorisation is by shell access.
- **Payslip.** A computed pay document for one
  (work_engagement, pay_period).
- **Pending (task).** A task whose `scheduled_for_utc` is within the
  next hour (or already past for a one-off). Distinct from
  `scheduled`; used to populate the worker "today" list (§06).
- **Personal task.** A task with `is_personal = true`: visible only to
  the `created_by` user and workspace owners; hidden from non-owner
  managers, team dashboards, reports, and audit surfaces. Created via
  quick-add on `/today` or `/week` where personal is the default; the
  creator may flip "share to team" before submitting. See §06
  "Self-created and personal tasks" and §15 "Personal task visibility".
- **Property.** A managed physical place containing one or more
  units. `kind` (§04) gates stay lifecycle rule seeding: `residence`
  none, `str`/`vacation` default `after_checkout` rule, `mixed` same
  with `guest_kind_filter`.
- **QR token.** A unique 12-character Crockford base32 identifier
  assigned to every asset at creation. Encodes into a QR code for
  phone scanning; the URL pattern is
  `https://<host>/asset/scan/<qr_token>`. See §21.
- **Property closure.** A dated blackout on a property (or a specific
  unit) that prevents schedule generation (§06). iCal "Not available"
  VEVENTs become rows of this kind automatically.
- **Property kind.** Classification of a property: `residence |
  vacation | str | mixed`. Drives default area seeding, lifecycle rule
  templates, and scheduling behaviour. See §04.
- **Public holiday.** A workspace-managed holiday date with
  configurable scheduling effect (`block | allow | reduced`) and
  optional payroll multiplier. Manager-configured per holiday. See
  §06 `public_holidays`.
- **Pull-back (scheduling).** The process of moving a pre-arrival task
  to an earlier date when the ideal date falls on an unavailable day
  (leave, holiday, day off). Bounded by `max_advance_days` on the
  lifecycle rule. See §06 "Pull-back logic for before_checkin tasks".
- **Role.** Ambiguous in v0. In v1, prefer **Work role** (the job:
  maid, cook, driver) or **Grant role (surface)** (the UI
  persona: manager/worker/client/guest). The v0 `role` entity
  was renamed `work_role` in v1; stable slug `work_role.key` is
  editable but external integrations should prefer the ULID.
- **Role grant.** A row in `role_grants` attaching a user to a
  scope with a surface `grant_role` (no authority column — v1
  dropped `capability_override`). Authority comes from
  permission-group membership plus `permission_rule` rows;
  `role_grants` only says which UI shell the user sees and
  which rows RLS lets them read. See §02.
- **Schedule.** Description of when tasks materialize (RRULE).
  `paused_at` wins over `active_from/active_until`.
- **Scope (instruction).** The visibility level of an instruction:
  `global` (all properties), `property` (one property), `area` (one
  area within a property), or linked via `instruction_link`. See §07.
- **Session.** Browser-bound server-side record tied to a passkey.
- **Shift.** A clocked-in interval tied to a
  `work_engagement` (which identifies the user × workspace).
  `status` is `open | closed | disputed`.
- **SKU / item.** An inventory entry per property.
- **Stay.** A reservation of a unit within a property (guest, owner,
  staff, other — see `guest_kind`) for a date range. Overlap
  detection is per-unit. Lifecycle rules generate task bundles around
  stay events.
- **Stay lifecycle rule.** A trigger-based configuration that
  generates task bundles around stay events: `before_checkin`,
  `after_checkout`, or `during_stay`. Replaces the simpler
  turnover-template pointer. See §06.
- **Stay status.** Lifecycle state of a guest stay: `tentative |
  confirmed | in_house | checked_out | cancelled`. See §04.
- **Stay task bundle.** A set of tasks generated by a stay lifecycle
  rule for a specific stay. Replaces `turnover_bundle`. Tasks in a
  bundle share `stay_task_bundle_id`. See §06.
- **TCO (total cost of ownership).** The all-in cost of an asset:
  purchase price + expense lines + document invoices, divided by years
  owned for an annual figure. Reported per asset and aggregated per
  property. See §21.
- **Template (task template).** Reusable task definition.
- **Token.** API token; `mip_<keyid>_<secret>` on the wire.
- **Turnover.** The set of tasks generated around a stay — now
  modeled as `stay_task_bundle` rows generated by `stay_lifecycle_rule`
  entries. The `after_checkout` trigger is the direct successor of the
  former `turnover_bundle`. Gating depends on the rule's
  `guest_kind_filter` and the property's `kind`.
- **Unit.** A bookable subdivision of a property. Every property has
  at least one unit; single-unit properties auto-create a default
  unit with the unit layer hidden in the UI. Stays, lifecycle bundles,
  and iCal feeds are unit-scoped. See §04.
- **Unavailable marker.** Historical name for iCal blocks that are
  not stays; these are now modeled as `property_closure` rows with
  `reason = ical_unavailable`.
- **Welcome link.** Tokenized public URL exposing the guest welcome
  page for a stay. Revocation or expiry both serve a 410 with the
  same layout; wording differs.
- **Workspace.** The tenancy boundary in v1. One workspace = one
  employer entity. Every user-editable row carries `workspace_id`.
  All uniqueness constraints on user-editable rows are scoped to
  `workspace_id`. The v1 deployment ships a **single workspace**
  seeded at first boot, but the schema, auth, and API surface are
  already multi-tenant-ready — see §02 "Migration" and §19 "Beyond
  v1" for the path to true multitenancy. Replaces the v0
  "household."
- **Workspace usage budget.** Per-workspace rolling-30-day dollar
  envelope over every LLM call charged to the workspace. Stored on
  `workspace_budget.cap_usd_30d`; prod default $5, demo default $0.10.
  At cap, all LLM calls refuse with the structured `budget_exceeded`
  error until older calls age out of the window. Adjusted only by the
  operator via `crewday admin budget set-cap` — no HTTP surface; see
  §11 "Workspace usage budget".
- **Pricing table.** `app/config/llm_pricing.yml`; per-model USD cost
  per 1k input and output tokens. Loaded at process start; hot-
  reloadable via `crewday admin budget reload-pricing`. Free-tier
  models (`:free` suffix on OpenRouter) price at zero. An unknown
  model_id prices at zero and logs a WARNING every call. See §11.
- **Free-tier model.** An OpenRouter model whose id ends in `:free`.
  Priced at zero in the pricing table; still metered for telemetry.
  Default for every live capability on the demo deployment (§24).
- **At-cap refusal.** Pre-flight client-side refusal of an LLM call
  when the workspace's rolling 30-day spend plus the projected call
  cost would exceed `cap_usd_30d`. Returns the structured
  `budget_exceeded` shape. Not an `audit_log` event; it is
  operational telemetry. See §11 "At-cap behaviour".
- **Demo deployment.** A crewday container running with
  `CREWDAY_DEMO_MODE=1`, separate DB, separate OpenRouter key, and
  a separate root key. Unauthenticated visitors land via signed
  demo cookies bound to ephemeral workspaces with fake data; 24-
  hour rolling TTL from last activity. iCal, SMTP, webhooks,
  passkeys, magic links, token creation, OCR, voice, daily digest,
  and anomaly detection are disabled. Full spec in §24; deployment
  recipe in §16 "Recipe C".
- **Demo workspace.** An ephemeral `workspaces` row paired with a
  `demo_workspace` row on the demo deployment. Garbage-collected
  by the `demo_gc` worker every 15 minutes when `expires_at` passes.
  FK `ON DELETE CASCADE` on every `workspace_id` column removes
  every dependent row. See §24.
- **Demo scenario.** A seed fixture under `app/fixtures/demo/` that
  initialises one demo workspace with a cast of personas
  (owner, manager, worker, client), properties, tasks, stays, and
  inventory. Selected by the `?scenario=<key>` query param on the
  first iframe load. v1 scenarios: `villa-owner`, `rental-manager`,
  `housekeeper`. See §24.
- **Demo session.** A signed `__Host-crewday_demo` cookie binding a
  browser to one or more `(scenario, workspace_id, persona_user_id)`
  tuples. Not a `sessions` row; not a credential. Flags:
  `Secure; HttpOnly; SameSite=None; Path=/; Partitioned;
  Max-Age=2592000`. See §03 "Demo sessions" and §24 "Demo cookie".
- **CHIPS (Cookies Having Independent Partitioned State).** Browser
  feature that keys cookie storage to the `(top-frame-origin,
  cookie-origin)` pair when the cookie carries `Partitioned`. The
  demo cookie opts in so the same demo app embedded on different
  landing pages gets separate cookie partitions — and therefore
  separate demo workspaces. See §15 "Demo deployment" and §24.
- **Organization.** A counterparty of the workspace tracked in a
  single `organization` table, flagged as client (`is_client`),
  supplier (`is_supplier`), or both. Replaces the need for
  separate "client" and "supplier" tables. See §22.
- **Client.** An `organization` with `is_client = true` — the
  agency's billing counterparty. A property may point at one
  client via `property.client_org_id`; when null, the workspace
  is its own billing target. See §22.
- **Supplier (supplying organization).** An `organization` with
  `is_supplier = true` — an agency that provides workers to our
  workspace. A `work_engagement` with
  `engagement_kind = agency_supplied` references one via
  `supplier_org_id`; the supplier's `default_pay_destination_id`
  routes the supplier's vendor invoices. See §22.
- **Engagement kind.** Per-`work_engagement` enum
  (`payroll | contractor | agency_supplied`) deciding which pay
  pipeline the worker is on. `payroll` → `pay_rule` + `payslip`
  (§09). `contractor` and `agency_supplied` → `vendor_invoice`
  (§22). Does not affect task assignment, shifts, worker settings,
  or evidence policy. Crossing the `payroll` boundary in either
  direction is unconditionally approval-gated. See §05, §22.
- **Work order.** A billable envelope wrapping one or more tasks
  at a single property, with an assigned contractor or agency-
  supplied worker, an optional accepted quote, and one or more
  vendor invoices. Optional: a casual one-off job can skip
  `work_order` and attach a `vendor_invoice` directly to a task.
  See §22.
- **Quote.** A worker-proposed price for a `work_order`. Acceptance
  is an unconditionally approval-gated action (§11); the accepted
  quote's total acts as a ceiling on subsequent
  `vendor_invoice` totals (with a workspace-configurable
  tolerance). States: `draft | submitted | accepted | rejected |
  superseded | expired`. See §22.
- **Vendor invoice.** A bill from a contractor or supplying
  organization, paid from a `payout_destination` chosen at
  approval time. Parallel to `expense_claim` in shape (OCR
  autofill, attachments, owner/manager approval) but the
  counterparty is the biller, not the submitting user. Approval
  and `mark_paid` are unconditionally approval-gated. States:
  `draft | submitted | approved | rejected | paid | voided`. See
  §22.
- **Chat gateway.** Transport layer through which the user's
  embedded agent (§11) can exchange messages across web and future
  external channels. Shipped v1 uses only the shared `.desk__agent`
  web sidebar (both roles, desktop) and its mobile counterparts
  (worker `/chat` page, manager bottom-dock drawer); WhatsApp / SMS /
  Telegram remain deferred. See §23.
- **Channel adapter.** Per-transport implementation of the
  gateway's `ChannelAdapter` protocol — parses inbound envelopes,
  sends text / buttons / media / templates, and verifies webhook
  signatures. Off-app adapters are deferred; the spec keeps their
  slugs and contracts ready. See §23.
- **Chat channel binding.** Row in `chat_channel_binding` tying a
  `(channel_kind, address)` pair to exactly one `users` row.
  Created `pending`, promoted to `active` via the link-challenge
  ceremony, revoked by user action or `STOP` keyword. One binding
  per `(user, channel_kind)` and one per `(channel_kind, address)`
  at a time. See §23.
- **Link challenge.** 6-digit code sent during the chat-channel
  binding ceremony (§23) over the channel being linked. Stored as
  argon2id hash, 15-minute TTL, five-attempt cap. Accepting the
  code either by inbound reply or UI POST promotes the binding to
  `active`.
- **Chat thread / chat message.** `chat_thread` + `chat_message`
  are the planned unified message substrate for web and future
  external channels. The deferred design keeps one live thread per
  user across all channels; messages carry `channel_kind`,
  `direction`, affordance metadata, and media references. See §23.
- **Session window (WhatsApp).** Meta's 24-hour rule: outside the
  window since the user's last inbound, outbound from the gateway
  must be a pre-approved template message. Tracked on
  `chat_channel_binding.last_message_at`; the WhatsApp adapter
  auto-wraps free-form bodies into the `chat_agent_nudge` template
  past the window. Non-WhatsApp channels have no equivalent
  constraint. See §23.
- **Client rate / billable rate.** Per-client hourly rate keyed to
  a work_role (`client_rate`) with optional per-user override
  (`client_user_rate`). Resolved at shift close and snapshotted
  onto `shift_billing`. Rate-card edits do not rewrite history.
  See §22.
- **User.** A single login identity row (`users`); every human in
  v1 has exactly one. Authority comes from `role_grants`; pay
  pipelines come from `work_engagement`. No `kind` column,
  no manager/employee split. See §02.
- **Work engagement.** The per-(user, workspace) employment
  relationship carrying `engagement_kind` (payroll / contractor /
  agency_supplied), `supplier_org_id`, and pay-destination
  defaults. Pay-pipeline tables (pay_rule, payslip, shift,
  expense_claim) key off `work_engagement_id`. See §02, §22.
- **Work role.** A named job bundle (maid, cook, driver, …),
  formerly `role` in v0. Resolved against a user in a given
  workspace via `user_work_role`. See §05.
- **Worker (grant role).** A user operating in a scope as staff.
  Requires at least one `user_work_role` on a workspace-scope
  grant; surface narrows further via
  `property_work_role_assignment`.
- **Shift billing.** Append-only derived row capturing a shift's
  resolved billable rate, currency, minutes, and subtotal at the
  moment the shift closes. Drives the "billable hours by client"
  CSV. See §22.
