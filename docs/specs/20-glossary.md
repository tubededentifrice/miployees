# 20 — Glossary

Terms used across the spec. Definitive form; if code or doc disagrees,
fix the offender.

- **Actor.** The kind of principal responsible for an action, recorded
  on `audit_log` and shift/claim rows: `manager | employee | agent |
  system`.
- **Agent.** A non-human actor. Standalone agents are authenticated
  by scoped API tokens (`actor_kind = 'agent'`). Embedded agents use
  **delegated tokens** that act with the full authority of their
  delegating user (`actor_kind` = the user's kind). See §03, §11.
- **Agent (embedded).** The manager-side or employee-side chat
  agent described in §11. Default model `google/gemma-4-31b-it`;
  tool surface is the full CLI + REST surface of the delegating user
  (no filtered catalog). Voice input is capability-gated.
- **Auto-clock.** The `auto` value of `time.clock_mode` (§05, §09).
  First checklist tick or task action of the day opens a shift;
  `time.auto_clock_idle_minutes` of inactivity closes it. Per-villa
  override can force `manual` or `disabled`. See §09 "Disputed
  auto-close" for the re-open semantics.
- **Anomaly suppression.** A manager-recorded rule that silences a
  specific `(anomaly_kind, subject_id)` pair until a required
  `suppressed_until` timestamp (§11). Permanent suppression is not
  offered by design.
- **Approvable action.** A write that requires manager approval
  regardless of token scope (§11). Default TTL 7 days.
- **Archive / reinstate.** The canonical verbs for off-boarding and
  bringing back an employee (§05). Replaces "end", "terminate", "rehire".
- **Area.** A subdivision of a property (kitchen, pool, Room 3).
- **Assignment.** The linkage of an employee to a role+property
  (`property_role_assignment`) and, per task, the pointer
  `task.assigned_employee_id`. There is no separate `task_assignment`
  entity — task assignment is just a column.
- **Audit log.** Append-only ledger of all state-changing actions.
- **Break-glass code.** Manager-only single-use recovery code that
  generates exactly one magic link on redemption (§03).
- **Capability.** A per-employee, per-property-role feature flag,
  resolved from a four-level sparse JSON stack (property_role_
  assignment → employee_role → role → catalog default). Explicit
  `false` blocks inheritance; absent keys inherit. Canonical catalog
  in §05.
- **Checklist item.** A row in `task_checklist_item` — one tickable
  line on a task, seeded from the template's
  `checklist_template_json`. Per-item tick state is authoritative.
- **Completion.** Terminal state for a task; has evidence and an
  employee. Under concurrent writes, last-write-wins with a
  `task.complete_superseded` audit entry for the displaced one (§06).
- **Correlation ID.** Per-request identifier (or caller-supplied
  `X-Correlation-Id`) that groups audit rows. Not a workflow-lifetime
  concept.
- **Employee leave.** Approved absence window (§06). Unapproved
  requests do not affect assignment.
- **Evidence.** Artifact attached to a completion — photo, note, or
  checklist snapshot.
- **Evidence policy.** Photo-evidence requirement resolved by
  walking a four-layer stack workspace → villa → employee → task
  (§05 "Evidence-policy stack", §06 "Evidence policy inheritance").
  Values are `inherit | require | optional | forbid`; the workspace
  root is always concrete. First concrete value, root-first, wins.
  `forbid` at any layer is absolute.
- **File.** Shared blob-reference row (§02 `file`). Pluggable backend;
  local disk in v1.
- **Handle.** Optional user-friendly slug (`maid-maria`) stored in a
  per-entity `handle` column where useful. Unique per parent scope.
- **Household.** **v0 term; replaced by Workspace in v1.** Retained
  here so historical references in migrations, ADRs, and older code
  remain resolvable. New code and new docs use Workspace.
- **Instruction.** A standing SOP attached at global / property /
  area / link scope (§07). `instruction_link` is canonical;
  `task.linked_instruction_ids` is a denormalized cache.
- **Issue.** An employee-reported problem tracked with state and
  possibly converted to a task.
- **Magic link.** Single-use, signed URL used to enroll or recover a
  passkey. Consumes a break-glass code (if that's the source)
  regardless of whether the link is later clicked.
- **Manager.** Human with elevated scope. All managers are peers in v1.
- **Model assignment.** The capability → model mapping (§11).
- **Passkey.** WebAuthn platform or roaming authenticator credential.
- **Pay period.** A date-bucket inside which shifts roll up into a
  payslip. `open → locked → paid`; `paid` is set automatically when
  every contained payslip reaches `paid`.
- **Payout destination.** A per-employee record naming where money
  lands: bank account, reloadable card, wallet, cash, or other.
  Employees may hold more than one; the employee row carries default
  pointers for pay and for reimbursements separately (§09). Full
  account numbers live in `secret_envelope`; only a `display_stub`
  (IBAN last-4 + country, card last-4, wallet handle) is returned
  over the API. Creating, editing, or changing a default is always
  approval-gated for bearer tokens (scoped and delegated) —
  miployees does not execute
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
  numbers at the moment the operator pushes funds. **Manager-
  session only** (no bearer tokens, even via approval — see
  "Interactive-session-only endpoint" below). Every fetch is audit-logged; no
  blob is persisted; the idempotency cache does not retain the
  response; a second fetch within 5 minutes raises a digest alert.
  Once the payout secrets are GDPR-erased, the endpoint returns 410
  Gone (§09, §15).
- **Interactive-session-only endpoint.** An HTTP endpoint that
  refuses all bearer tokens (scoped and delegated) and requires a
  live passkey session, because the response contains decrypted
  secret material that must not land in any persisted store
  (including `agent_action.result_json`). v1 list (§11): the payout
  manifest. Manager passkey session only.
- **Delegated token.** A bearer token created by a logged-in user
  (manager or employee) that inherits their full permissions. Audit
  records use the delegating user's identity (`actor_kind`,
  `actor_id`), with `agent_label` and `agent_conversation_ref`
  fields flagging the action as agent-executed and linking back to
  the triggering conversation. See §03.
- **Host-CLI-only administrative command.** A `miployees admin`
  verb with no HTTP surface at all, agent or human: envelope-key
  rotation, offline lockout recovery, hard-delete purge (§11). Run
  on the deployment host; authorisation is by shell access.
- **Payslip.** A computed pay document for one (employee, pay_period).
- **Pending (task).** A task whose `scheduled_for_utc` is within the
  next hour (or already past for a one-off). Distinct from
  `scheduled`; used to populate the employee "today" list (§06).
- **Property.** A managed physical place. `kind` (§04) gates
  turnover-bundle generation: `residence` never, `str`/`vacation`
  always, `mixed` only for non-owner stays.
- **Property closure.** A dated blackout on a property that prevents
  schedule generation (§06). iCal "Not available" VEVENTs become
  rows of this kind automatically.
- **Role.** A named capability bundle (maid, cook, …). `role.key` is a
  stable slug but editable; external integrations should prefer the
  ULID.
- **Schedule.** Description of when tasks materialize (RRULE).
  `paused_at` wins over `active_from/active_until`.
- **Session.** Browser-bound server-side record tied to a passkey.
- **Shift.** A clocked-in interval for an employee. `status` is
  `open | closed | disputed`.
- **SKU / item.** An inventory entry per property.
- **Stay.** A reservation of a property (guest, owner, staff, other —
  see `guest_kind`) for a date range.
- **Template (task template).** Reusable task definition.
- **Token.** API token; `mip_<keyid>_<secret>` on the wire.
- **Turnover.** The set of tasks generated on stay check-out — a
  `turnover_bundle` parent with its child tasks. Gating depends on
  `property.kind` and the stay's `guest_kind`.
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
