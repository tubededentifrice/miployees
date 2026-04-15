# 20 — Glossary

Terms used across the spec. Definitive form; if code or doc disagrees,
fix the offender.

- **Actor.** The kind of principal responsible for an action, recorded
  on `audit_log` and shift/claim rows: `manager | employee | agent |
  system`.
- **Agent.** A non-human actor authenticated by an API token.
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
- **File.** Shared blob-reference row (§02 `file`). Pluggable backend;
  local disk in v1.
- **Handle.** Optional user-friendly slug (`maid-maria`) stored in a
  per-entity `handle` column where useful. Unique per parent scope.
- **Household.** The single tenant in a v1 deployment. All uniqueness
  constraints on user-editable rows are scoped to `household_id`.
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
  lands: a bank account, reloadable card, cash, or other. Employees
  may hold more than one; the employee row carries default pointers
  for pay and for reimbursements separately (§09). v1 does not
  execute payments — destinations are metadata for the operator.
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
