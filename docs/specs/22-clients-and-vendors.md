# 22 — Clients, vendors, work orders, and billing

crewday started life as a **single-employer, single-household**
operations tool. Real deployments extend beyond that shape:

- A **cleaning / property-management agency** whose workspace owns
  many external maids and serves many paying clients; the agency
  collects money from clients and pays its workers.
- A **household-as-client** whose workspace hires one or more
  external contractors (repair handymen, drivers) alongside (or
  instead of) its own payroll staff.
- A **mixed** setup: a workspace with payroll engagements, one-off
  contractor engagements invoicing for specific jobs, and a handful
  of agency-supplied engagements routed through a third-party
  vendor.

This section defines the entities, flows, and invariants that make
those shapes first-class while preserving the existing "family with
a maid" single-workspace default.

## Design summary

- Properties may belong to a **billing client** — a row in the
  unified `organization` table — via nullable
  `property.client_org_id`. When null, the workspace itself is the
  implicit billing target (the pre-existing behaviour).
- A client who is a person (not just a legal entity on paper) is
  represented in the identity model as a `users` row with a
  `role_grants` row of `grant_role = 'client'`, either on a
  property directly or on the workspace with `binding_org_id =
  <their org>` (§02, §05). Organizations hold billing data; grants
  hold login authority; the two are connected by `binding_org_id`.
- **Work engagements** (one per (user, workspace) — §02) carry the
  `engagement_kind` that decides which pay pipeline applies:
  payroll (payslips), contractor (vendor invoices), or
  agency-supplied (vendor invoices billed to the supplying
  organization rather than the worker).
- Work that gets billed externally — whether to a client, or by a
  contractor — is grouped into a **`work_order`**: an optional
  parent of one or more tasks, under which a **`quote`** and one or
  more **`vendor_invoice`** rows live.
- Agency billing is captured as **billable rates per (client, work_role)**
  with an optional per-user override; shifts carry a derived
  `client_org_id` for fast rollup and CSV export. v1 exports CSV;
  rendering a client-facing invoice PDF is deferred (see §19).
- All money-routing decisions made by agents — accepting a quote,
  approving a vendor invoice, marking one paid — are
  **unconditionally approval-gated** (§11), like
  `payout_destination.*`.

## `organization`

A counterparty of the workspace. May be a **client** (we bill them),
a **supplier** (they bill us, typically because they supply workers),
or both. One table, role flags.

| field              | type      | notes                                                                    |
|--------------------|-----------|--------------------------------------------------------------------------|
| id                 | ULID PK   |                                                                          |
| workspace_id       | ULID FK   | scoping                                                                  |
| name               | text      | display name ("Dupont family", "CleanCo SARL")                            |
| legal_name         | text?     | distinct from display when needed for invoices                            |
| is_client          | bool      | true if this org pays the workspace                                       |
| is_supplier        | bool      | true if this org supplies workers to the workspace                        |
| default_currency   | text      | ISO 4217; defaults to workspace default                                   |
| address_json       | jsonb     | same canonical shape as `property.address_json` (§04)                    |
| contacts_json      | jsonb     | array of `{label, name, email, phone_e164, role}` — free-form contacts   |
| tax_id             | text?     | VAT / SIRET / EIN as relevant — displayed on invoices                     |
| notes_md           | text?     | manager-visible                                                           |
| portal_user_id     | ULID FK?  | retained as a convenience pointer for "the single natural person for this org" (e.g. sole-trader supplier). Canonical client login goes through `role_grants(grant_role='client', binding_org_id=<org>)`. See "Client surface" below. |
| default_pay_destination_id | ULID FK? | for suppliers: where vendor_invoice payments route by default (§09) |
| created_at/updated_at | tstz    |                                                                          |
| deleted_at         | tstz?     | soft delete                                                               |

**Invariants.**

- At least one of `is_client` / `is_supplier` must be true.
  Write-time check; 422 otherwise.
- `default_pay_destination_id` must reference a `payout_destination`
  whose `organization_id = this.id` (see §09 "Payout destinations
  for organizations").
- Unique on `(workspace_id, legal_name)` when `legal_name` is set;
  unique on `(workspace_id, name)` always. Prevents duplicate org
  rows from ambiguating invoice routing.

### Starter rows

None — a fresh workspace has no organizations and no clients. The
workspace remains the implicit billing target for its own properties.
Organizations are created lazily when an owner/manager enters
"agency mode" by linking a property to a client, or registers a
supplier to route agency-supplied engagements.

## `property.client_org_id`

Added to the `property` row (§04):

| field         | type     | notes                                                    |
|---------------|----------|----------------------------------------------------------|
| client_org_id | ULID FK? | `organization.id` where `is_client = true`. Nullable.    |

- **Null** = workspace-owned, self-managed. Vendor invoices for
  work at this property are paid by the workspace directly; no
  client-billing rollup.
- **Set** = billable to that client. Shifts and work orders at this
  property carry the client forward; billable-rate resolution
  consults `client_rate` / `client_user_rate` (below).
- A property can only have one `client_org_id` at a time. Properties
  that are genuinely co-owned and split-billed are a deliberate
  non-goal in this iteration; see §19 "Beyond v1" for split-billing.
- Write-time check: the referenced org must have `is_client = true`
  and belong to the same `workspace_id`.

## Engagement kind

Carried on `work_engagement` (§02), the per-(user, workspace) row:

| field            | type     | notes                                                              |
|------------------|----------|--------------------------------------------------------------------|
| engagement_kind  | enum     | `payroll | contractor | agency_supplied`. Default `payroll`.        |
| supplier_org_id  | ULID FK? | `organization.id` where `is_supplier = true`. Required iff `engagement_kind = agency_supplied`, else null. |

Because the field lives on `work_engagement` rather than on the
user, the **same person** can be `payroll` in one workspace and
`contractor` or `agency_supplied` in another simultaneously. That
is the case we support for a live-in driver who is payroll of the
household and also takes freelance jobs through a dispatch agency.

Semantics:

- **`payroll`** — existing behaviour. Gets `pay_rule`, accrues into
  `pay_period`, is paid via `payslip`. May not have a
  `supplier_org_id`.
- **`contractor`** — an independent worker who **bills us**. Does
  not get `pay_rule` / `payslip`; payment flows through
  `vendor_invoice` rows. May hold their own `payout_destination`
  rows (the vendor invoice routes to one of them).
- **`agency_supplied`** — a worker provided by a third-party agency.
  The **agency** bills us; the engagement's `supplier_org_id`
  points at the supplier. Vendor invoices for this engagement route
  by default to `supplier_org.default_pay_destination_id`, not to a
  destination owned by the worker.

A work_engagement's `engagement_kind` is not immutable but changes
are audited and gated: switching a row from `payroll` to
`contractor` requires that the engagement have no `pay_rule` active
on or after the switch date, and any open `pay_period` with shifts
for that engagement must be locked or drained. The reverse switch
(contractor → payroll) requires at least one `pay_rule` to be
created in the same transaction.

### UI and assignment

`engagement_kind` does **not** affect task assignment, shifts,
work-role capabilities, or anything in §05 / §06 — the worker is
still a `users` row with `user_work_role` and
`property_work_role_assignment` rows driving scheduling and the
evidence policy. It only affects the pay pipeline and which UI
surfaces the person appears in when that particular engagement is
selected.

## Billable rates

Rates the workspace bills a client for work done by its workers.
Parallel to `pay_rule` (what we pay the worker) but oriented the
other way.

### `client_rate` (per client × work_role)

| field              | type     | notes                                       |
|--------------------|----------|---------------------------------------------|
| id                 | ULID PK  |                                             |
| workspace_id       | ULID FK  |                                             |
| client_org_id      | ULID FK  | must have `is_client = true`                |
| work_role_id       | ULID FK  | §05 (renamed from `role_id`)                |
| currency           | text     | ISO 4217                                    |
| hourly_cents       | int      |                                             |
| effective_from     | date     |                                             |
| effective_to       | date?    | null = ongoing                              |
| notes_md           | text?    |                                             |

Unique: `(client_org_id, work_role_id, effective_from)`.

### `client_user_rate` (per client × user override)

(In v0 this entity was called `client_employee_rate`.)

| field              | type     | notes                                       |
|--------------------|----------|---------------------------------------------|
| id                 | ULID PK  |                                             |
| workspace_id       | ULID FK  |                                             |
| client_org_id      | ULID FK  |                                             |
| user_id            | ULID FK  | references `users.id`                       |
| currency           | text     |                                             |
| hourly_cents       | int      |                                             |
| effective_from     | date     |                                             |
| effective_to       | date?    |                                             |

Unique: `(client_org_id, user_id, effective_from)`.

### Rate resolution

For a shift with `(client_org_id, user_id)` and date `d`:

1. `client_user_rate` matching `(client_org_id, user_id)`
   with `effective_from ≤ d < coalesce(effective_to, ∞)`.
2. For each `user_work_role` the user holds at the shift's
   workspace (narrowed by `property_work_role_assignment`):
   `client_rate` matching `(client_org_id, work_role_id)` with the
   same effective-range test. If multiple work roles resolve to
   different rates, the **highest-priority work_role** wins (work
   roles have an implicit priority by `work_role.key` in the
   catalog; owners/managers may override per client with
   `client_rate.priority` — deferred).
3. No match → the shift is **not billable** to this client and its
   hours surface in a "unpriced" bucket in the rollup CSV so the
   owner or manager can fix the rate card.

Rate resolution happens at **shift close time** and is snapshotted
onto a new `shift_billing` row (below) so later rate-card edits do
not retroactively rewrite history.

### `shift_billing`

A derived, append-only row per `(shift, client_org_id)` pair,
written when a shift closes against a property with
`client_org_id IS NOT NULL`.

| field              | type     | notes                                                |
|--------------------|----------|------------------------------------------------------|
| id                 | ULID PK  |                                                      |
| workspace_id       | ULID FK  |                                                      |
| shift_id           | ULID FK  |                                                      |
| client_org_id      | ULID FK  | denormalised from `property.client_org_id` at close  |
| user_id            | ULID FK  | the worker who performed the shift                   |
| work_engagement_id | ULID FK  | the engagement the shift is earned under (§02, §09)  |
| currency           | text     |                                                      |
| billable_minutes   | int      | = duration minus breaks                              |
| hourly_cents       | int      | snapshot of the resolved rate                        |
| subtotal_cents     | int      | `billable_minutes / 60 * hourly_cents`, rounded      |
| rate_source        | enum     | `client_user_rate | client_rate | unpriced`          |
| rate_source_id     | ULID?    | id of the resolving rate row; null when `unpriced`   |

Editing a shift's time fields (`adjusted = true` in §09)
re-derives its `shift_billing` row inside the same transaction.
Archiving a property or client never removes `shift_billing` rows;
they are historical.

## `work_order`

A billable envelope wrapping one or more tasks. Optional — a casual
one-off repair can skip work_order entirely and just have a task
with a single attached `vendor_invoice`. Work orders are for jobs
worth quoting up front or that span multiple tasks.

| field                     | type     | notes                                                      |
|---------------------------|----------|------------------------------------------------------------|
| id                        | ULID PK  |                                                            |
| workspace_id              | ULID FK  |                                                            |
| property_id               | ULID FK  | the location of the work                                   |
| client_org_id             | ULID FK? | derived cache from `property.client_org_id` at creation    |
| asset_id                  | ULID FK? | §21 — set when the work is about a specific asset          |
| title                     | text     | "Replace pool pump seal"                                   |
| description_md            | text     |                                                            |
| state                     | enum     | `draft | quoted | accepted | in_progress | completed | cancelled | invoiced | paid` |
| assigned_user_id          | ULID FK? | the contractor / agency-supplied worker doing the work     |
| requested_by_user_id      | ULID FK? | who opened the work order (typically an owner or manager)  |
| currency                  | text     | ISO 4217; defaults to property currency                    |
| accepted_quote_id         | ULID FK? | set when a quote is accepted; null otherwise               |
| accepted_at               | tstz?    |                                                            |
| completed_at              | tstz?    |                                                            |
| cancellation_reason       | text?    |                                                            |
| notes_md                  | text?    |                                                            |
| created_at/updated_at     | tstz     |                                                            |
| deleted_at                | tstz?    |                                                            |

### State machine

```
draft → quoted → accepted → in_progress → completed → invoiced → paid
  └───────────────────┘            ↑                    ↑
                                   └────────────────────┘
                                 (may skip `quoted`/`accepted`
                                  when an owner/manager invoices
                                  directly without a quote)
cancelled is reachable from any non-terminal state.
```

Tasks referencing a work_order (`task.work_order_id FK?`) inherit
its `assigned_user_id` as a default but may be re-assigned; all
such tasks appear grouped under the work_order in the
owner/manager UI.

### Invariants

- `currency` must equal `property.default_currency` at creation —
  multi-currency work_orders are out of scope (same rationale as
  per-period single-currency payroll in §09).
- Transitioning `draft → quoted` requires at least one `quote` row
  with `status = submitted`.
- Transitioning `quoted → accepted` is an approvable action
  (§11 "Workspace policy: which actions"):
  `work_order.accept_quote`. An owner,
  manager, or authorised client picks exactly one submitted quote;
  the work_order records
  `accepted_quote_id`, the chosen quote flips to `accepted`, all
  other submitted quotes on the same work_order flip to
  `superseded` in the same transaction.
- `completed → invoiced` requires at least one `vendor_invoice` row
  with `status ≥ submitted`.
- `invoiced → paid` is set automatically when all vendor invoices
  on the work_order reach `status = paid`.

## `quote`

A worker-proposed price for a work_order. The quoting worker's
`work_engagement` is almost always a `contractor` or
`agency_supplied` engagement; payroll engagements usually don't
quote (their labour is already paid for), but the model does not
forbid it — a salaried handyman may submit a quote for a genuinely
outside-scope job.

| field                | type     | notes                                                         |
|----------------------|----------|---------------------------------------------------------------|
| id                   | ULID PK  |                                                               |
| workspace_id         | ULID FK  |                                                               |
| work_order_id        | ULID FK  |                                                               |
| submitted_by_user_id | ULID FK  | who submitted the quote                                       |
| work_engagement_id   | ULID FK? | the engagement under which the quote is being made (null for one-off quotes by a user with no engagement in this workspace — rare but allowed) |
| currency             | text     | must equal `work_order.currency`                              |
| subtotal_cents       | int      | sum of line totals                                            |
| tax_cents            | int      | informational; local tax behaviour is out of scope            |
| total_cents          | int      | `subtotal + tax`                                              |
| lines_json           | jsonb    | see shape below                                               |
| valid_until          | date?    | informational; system does not auto-expire                    |
| status               | enum     | `draft | submitted | accepted | rejected | superseded | expired` |
| submitted_at         | tstz?    |                                                               |
| decided_at           | tstz?    |                                                               |
| decided_by_user_id   | ULID FK? | owner/manager who accepted or rejected; client-grant acceptances also fill this in |
| decision_note_md     | text?    |                                                               |
| attachment_file_ids  | ULID[]   | PDFs/photos of the worker's own quote document                |
| llm_autofill_json    | jsonb?   | reserved for future OCR of PDF quotes                         |
| created_at/updated_at| tstz     |                                                               |
| deleted_at           | tstz?    |                                                               |

### `lines_json` shape

```json
{
  "schema_version": 1,
  "lines": [
    {"kind": "labor",    "description": "Diagnosis + repair (3h)",
     "quantity": 3, "unit": "hour", "unit_price_cents": 6000, "total_cents": 18000},
    {"kind": "material", "description": "Replacement seal (OEM)",
     "quantity": 1, "unit": "unit", "unit_price_cents": 2400, "total_cents": 2400},
    {"kind": "travel",   "description": "Call-out fee",
     "quantity": 1, "unit": "unit", "unit_price_cents": 3500, "total_cents": 3500}
  ]
}
```

`kind` is free-form for display; v1 suggests `labor | material |
travel | other`. `total_cents` is recomputed server-side from
`quantity * unit_price_cents` on write; a mismatch raises 422.

### Acceptance

`quote.accept` is **unconditionally approval-gated** (§11): an
agent cannot accept a quote, even if it holds `expenses:approve` or
any other scope. The owner/manager/client approval UI shows the quote,
attachments, and the resolved `work_order` context. Acceptance
writes `quote.status = accepted`, `work_order.accepted_quote_id =
this.id`, and `work_order.state = accepted`. Subsequent
`vendor_invoice` rows on the same work_order validate that
`total_cents ≤ accepted_quote.total_cents` **plus a workspace-
configurable tolerance** (default 10 %); overruns raise a soft
warning at invoice submission that the owner/manager sees before
approving, but are not hard-blocked.

### Supersession and rejection

- Submitting a new quote on a work_order already in state `quoted`
  leaves prior submitted quotes in `submitted`; only
  owner/manager/client acceptance collapses the set.
- Explicit `quote.reject` is available; sets `status = rejected`
  and records a reason. The work_order remains in `quoted` if
  other submitted quotes exist, else transitions back to `draft`.
- `expired` is a manual status an owner/manager may set when a
  quote with a `valid_until` in the past is no longer usable; the
  system does not auto-expire, to avoid silent state changes.

## `vendor_invoice`

What the worker or supplier actually bills. Parallel in spirit to
`expense_claim` (§09) — OCR autofill, attachments, owner/manager
approval — but the counterparty is the biller, not the submitting
user,
and payment flows to a `payout_destination` chosen at approval
time.

| field                  | type     | notes                                                            |
|------------------------|----------|------------------------------------------------------------------|
| id                     | ULID PK  |                                                                  |
| workspace_id           | ULID FK  |                                                                  |
| work_order_id          | ULID FK? | nullable — a one-off repair can carry an invoice without an explicit work_order |
| property_id            | ULID FK  | required when `work_order_id` is null                            |
| vendor_user_id         | ULID FK? | exactly one of `vendor_user_id` / `vendor_organization_id` is set |
| vendor_work_engagement_id | ULID FK? | when `vendor_user_id` is set, this points at the biller's work_engagement in this workspace. Required for `contractor` kind; null for one-off quotes from a user with no engagement here. |
| vendor_organization_id | ULID FK? | set when the biller is the supplier org (agency_supplied workers) |
| billed_at              | date     | on the invoice                                                   |
| due_on                 | date?    |                                                                  |
| currency               | text     |                                                                  |
| subtotal_cents         | int      |                                                                  |
| tax_cents              | int      |                                                                  |
| total_cents            | int      |                                                                  |
| lines_json             | jsonb    | same shape as `quote.lines_json`                                 |
| payout_destination_id  | ULID FK? | where the money will go; see resolution below                    |
| exchange_rate_to_default | numeric? | snapshot at approval, like expense_claim                       |
| status                 | enum     | `draft | submitted | approved | rejected | paid | voided`         |
| submitted_at           | tstz?    |                                                                  |
| approved_at            | tstz?    |                                                                  |
| decided_by_user_id     | ULID FK? | owner/manager who approved or rejected                           |
| decision_note_md       | text?    |                                                                  |
| paid_at                | tstz?    |                                                                  |
| paid_by_user_id        | ULID FK? | owner/manager who marked it paid                                 |
| paid_reference         | text?    | bank reference, free-form                                        |
| attachment_file_ids    | ULID[]   | PDF / photo of the worker's invoice                              |
| proof_of_payment_file_ids | ULID[] | client-uploaded evidence that they paid (wire receipt, screenshot, bank notice). See "Proof of payment" below. |
| reminder_last_sent_at  | tstz?    | updated each time the invoice-reminder worker emits a nudge; null = never reminded |
| reminder_next_due_at   | tstz?    | worker-computed next firing from the cascade setting; cleared on `paid` / `voided` |
| llm_autofill_json      | jsonb?   | see §09 expense autofill; shape is the same, vendor field refers to biller |
| autofill_confidence_overall | numeric? |                                                             |
| created_at/updated_at  | tstz     |                                                                  |
| deleted_at             | tstz?    |                                                                  |

### Invariants

- Exactly one of `vendor_user_id` / `vendor_organization_id` is
  set; 422 otherwise.
- For a `work_engagement` of kind `agency_supplied`, the server
  **rejects** an invoice written with `vendor_user_id = user.id`:
  the invoice must be written with
  `vendor_organization_id = work_engagement.supplier_org_id`.
  Rationale: the supplying agency bills us, not the individual
  worker. Conversely, a `contractor` engagement must be billed via
  `vendor_user_id` + `vendor_work_engagement_id`, not through an
  organization.
- `vendor_user_id` without a matching `vendor_work_engagement_id`
  is allowed only when the user has no active work_engagement in
  this workspace — a first-invoice-then-onboard path that is rare
  but needed for emergency one-offs. The approval step inserts the
  engagement if missing.
- `currency` on submission must equal `work_order.currency` when
  `work_order_id IS NOT NULL`.
- Approving an invoice snapshots the exchange rate against the
  workspace default currency (ECB daily fix at approval time),
  same mechanism as `expense_claim` (§09).

### Payout destination resolution

On approval, if `payout_destination_id` is null the server fills it
by walking:

1. If `vendor_user_id` is set: the resolved
   `vendor_work_engagement_id.pay_destination_id` (§09). The
   engagement must be `contractor` kind; payroll engagements' pay
   destinations are for payslips only and using them here is a 422
   with `error = "payroll_destination_not_billable"`.
2. If `vendor_organization_id` is set: the org's
   `default_pay_destination_id`. 422 if null — an owner or manager
   must set one before approving.

The chosen destination is recorded on the invoice (immutable after
approval). The **approval step itself** is the money-routing
decision, and accordingly `vendor_invoice.approve` is on the
unconditionally approval-gated list (§11) — an agent can submit,
attach, and draft the invoice, but cannot commit payment routing.
`vendor_invoice.mark_paid` (the `approved → paid` transition) is
also unconditionally gated.

### Approval flow

Identical to `expense_claim.approve` in shape (§09) with three
differences:

- The "requester" in approvable-action audit is the submitting
  agent's delegating user (same rule as elsewhere), but the
  invoice's **biller** is captured separately in
  `vendor_user_id` / `vendor_organization_id` for audit
  clarity.
- Approval with a non-null `payout_destination_id` provided by the
  agent raises the same gate as `expense_claim.set_destination_override`
  — the owner/manager must re-confirm the chosen destination in the
  approval UI.
- Unlike expense_claim, vendor_invoice does **not** roll into a
  payslip. It is paid directly; `paid_at` is set when an owner or
  manager clicks "Mark paid" after pushing funds from their bank.
  `paid`
  is distinct from `approved` so the workspace can track an
  account-payable queue.

### Proof of payment (client upload)

When a workspace is a **client** on a vendor_invoice (either the
workspace itself is the billing target, or a client user holds a
`role_grants(grant_role='client', binding_org_id=<biller-org>)`
grant), the payer records that they paid by uploading one or more
files to `proof_of_payment_file_ids`.

- **Action.** `vendor_invoice.upload_proof` — see §05 action
  catalog. Allowed subjects: the biller-side workspace's owners and
  managers; on the client side, any user with a `client` grant that
  resolves to this invoice (workspace-scope `binding_org_id` match,
  or property-scope grant on the invoice's property). Agents may
  submit on behalf of the delegating user; this is **not**
  approval-gated (no money routing), but it emits
  `vendor_invoice.proof_uploaded` so owners/managers see it.
- **Shape.** Each file is a row in the shared `file` table (§02);
  `proof_of_payment_file_ids` holds the reference ids. Multiple
  proofs are allowed (two wire transfers → two files) and additive;
  a previously uploaded proof cannot be removed by the uploader —
  only workspace owners/managers may prune via
  `vendor_invoice.remove_proof` (logged, not approval-gated; kept
  to recover from accidental wrong-file uploads).
- **Relationship to `paid`.** Uploading proof does **not**
  automatically flip the invoice to `paid` — the owner/manager who
  controls payment reconciliation still triggers
  `vendor_invoice.mark_paid` after reconciling against their bank
  feed. Proof upload is a *signal*, not a state change. The
  owner/manager UI shows a "Proof uploaded · awaiting
  reconciliation" badge on invoices with proof but `status <>
  paid`.
- **Retention.** Proof files inherit the workspace's
  `files.retention_years` setting (§15) and are kept at least as
  long as the invoice row itself; a soft-deleted invoice retains
  its proofs.

### Payment-due reminders

Vendor invoices in `status = approved` automatically remind the
payer on a cadence resolved through the settings cascade (§02).
Reminders are **not** a new channel — they ride the existing
agent-message delivery chain in §10 (SSE → push → WhatsApp → email).

**Cascade keys (scope `W/P`):**

| key                              | default        | semantics                                                                                       |
|----------------------------------|----------------|-------------------------------------------------------------------------------------------------|
| `invoice_reminders.enabled`      | `true`         | master toggle                                                                                   |
| `invoice_reminders.offsets_days` | `[-3, 1, 7]`   | list of integer offsets from `due_on`; negative = before due date, positive = after (overdue)   |
| `invoice_reminders.stop_after_days` | `30`        | stop reminding after `due_on + N days`; the invoice is escalated to the owner/manager instead   |

A property-level setting (`property.settings_override_json`)
overrides the workspace default. Individual invoices carry no
reminder config — operators who want to silence one invoice set
`status = voided` or flip `invoice_reminders.enabled = false` at
property scope.

**Worker job (`send_invoice_reminders`, hourly).** For each
`vendor_invoice` with `status = approved` and
`reminder_next_due_at <= now`:

1. Look up the recipient (the client user on the client side — first
   active `role_grants(grant_role='client', binding_org_id=<this
   org>)` within the workspace; or the workspace's
   owners/managers if the biller is the workspace).
2. Build the reminder payload (`invoice_id`, `due_on`,
   `total_cents`, `currency`, offset phase: `upcoming | due_today |
   overdue`).
3. Hand to the agent-message delivery worker (§10) — the fallback
   chain picks the right channel per recipient.
4. Update `reminder_last_sent_at = now` and recompute
   `reminder_next_due_at` as the next entry in `offsets_days`
   after now, or null if past `stop_after_days`.
5. On `stop_after_days` exhaustion, fire
   `vendor_invoice.reminder_exhausted` (webhook + owner/manager
   digest item).

**Events.** `vendor_invoice.reminder_sent`,
`vendor_invoice.reminder_exhausted`, `vendor_invoice.proof_uploaded`
are appended to §10's webhook catalog.

### Relationship to payroll engagements

A **payroll** work_engagement (default `engagement_kind`)
**cannot** be the biller of a `vendor_invoice`. Its labour is
paid through payslips, not invoices. Attempts to write such a row
with `vendor_work_engagement_id` pointing at a payroll engagement
return 422 `error = "payroll_engagement_not_billable"`. An owner
or manager may change the engagement's `engagement_kind` to
`contractor` for a specific off-cycle job, but that is explicit —
no silent promotion. Note that this is **per workspace**: a user
who is payroll in Workspace A may still carry a separate
`contractor` engagement in Workspace B and bill freely from there.

## Payout destinations for organizations

Extends `payout_destination` (§09) so destinations can be owned by
either a user **or** an organization. See §09 for the full
extension; in summary:

- `payout_destination.user_id` is nullable (owning user; previously
  `employee_id` in v0).
- A new nullable `payout_destination.organization_id` is added.
- Exactly one of the two must be set; DB-level CHECK constraint.
- The existing per-user rules (read/write authority scoped to the
  owner via `payroll.self_manage_destinations`, approval gate on
  mutation, IBAN checksum, snapshot on use) apply identically when
  the owner is an organization, with "the worker themselves" reading
  as "any user with the `organizations.edit_pay_destination`
  grant-capability on the workspace".

## Billable-hour rollup and exports

v1 ships **rate capture and CSV export** only. Full PDF client
invoices with a state machine are deferred (§19).

### CSV: billable hours by client

`GET /api/v1/exports/client_billable.csv?client_org_id=...&from=YYYY-MM-DD&to=YYYY-MM-DD`

One row per `(client_org_id, user_id, work_role_id, date)`:

```
client_org_id, client_name, user_id, user_name, work_role_key,
date, hours, hourly_cents, currency, subtotal_cents, rate_source
```

Unpriced hours (see "Rate resolution") are exported with
`hourly_cents = null` and `rate_source = unpriced` so they are
visible rather than silently dropped.

### CSV: work-order ledger

`GET /api/v1/exports/work_orders.csv?...`

One row per work_order with aggregate quote and invoice totals,
state, client, and asset. Useful for agency managers reconciling
what was quoted vs. what was billed.

### CLI

Mirrors §13 conventions: `crewday exports client_billable
--client ... --from ... --to ...` and `crewday exports
work_orders ...`.

## `property_workspace_invite`

Adding a `managed_workspace` or `observer_workspace` link on a
property is **never one-sided**. The owner workspace creates an
**invite**; the target workspace's owners must accept before the
`property_workspace` row is materialised. The same flow runs in
reverse — a client workspace inviting an agency to manage a villa
uses the identical entity — so neither party can drag the other
into a managed relationship by fiat.

| field                 | type      | notes                                                                                             |
|-----------------------|-----------|---------------------------------------------------------------------------------------------------|
| id                    | ULID PK   |                                                                                                   |
| token                 | text      | URL-safe opaque token (32 bytes, base32); the invite link embeds this. Unique across the deployment. |
| from_workspace_id     | ULID FK   | the inviting workspace (must hold `owner_workspace` on the property at invite time)               |
| property_id           | ULID FK   | the property being shared                                                                         |
| to_workspace_id       | ULID FK?  | optional pre-addressed recipient (the target workspace). Null = open invite, first workspace to claim wins (subject to a short-lived lease, see below). |
| proposed_membership_role | text   | `managed_workspace \| observer_workspace`                                                          |
| initial_share_settings_json | jsonb | snapshot of the PII widening the inviter is offering — at minimum `{share_guest_identity: bool}` (see §15). Materialised onto the resulting `property_workspace` row if accepted. |
| state                 | text      | `pending \| accepted \| rejected \| revoked \| expired`                                            |
| created_by_user_id    | ULID FK   | must be a member of `owners` on `from_workspace_id`                                               |
| created_at            | tstz      |                                                                                                   |
| expires_at            | tstz      | default `created_at + 14 days`                                                                    |
| decided_at            | tstz?     |                                                                                                   |
| decided_by_user_id    | ULID FK?  | member of `owners` on the accepting workspace (or the revoking party on `from_workspace_id`)      |
| decision_note_md      | text?     |                                                                                                   |

**Actions** (added to the §05 catalog):

- `property_workspace_invite.create` — inviter-side; requires
  `owners` membership on `from_workspace_id`. Unconditionally
  approval-gated (§11) — an agent may draft but a human authorises
  the share.
- `property_workspace_invite.accept` — recipient-side; requires
  `owners` membership on the accepting workspace. Unconditionally
  approval-gated. Resolves to a new `property_workspace` row with
  `membership_role = proposed_membership_role`,
  `share_guest_identity = initial_share_settings_json.share_guest_identity`.
- `property_workspace_invite.reject` — recipient-side; writes
  `state = rejected` with an optional note. Not approval-gated.
- `property_workspace_invite.revoke` — inviter-side; same authority
  as `.create`. Writes `state = revoked` (only while
  `state = pending`).

**Shareable link (no account yet).** The invite's `token` is the
primary handle; the UI exposes the resulting URL
(`<host>/invites/<token>`) as copy-paste-and-share. The recipient
can receive it via WhatsApp, email, SMS, carrier pigeon — the
surface the inviter uses is up to them, and the token carries no
standing authority until an authenticated user on the accepting
side opens it. Hitting the URL:

- Prompts for workspace login if the visitor has no session.
- Shows a detail page (property name, inviting workspace name +
  slug, proposed role, PII widening, expiry).
- Offers **Accept** / **Reject** buttons, gated on the visiting
  user being an `owners` member of a candidate accepting workspace
  (they pick from their workspaces if they have more than one).
- Open invites (`to_workspace_id IS NULL`) allow any workspace
  whose owners the inviter authorised; acceptance takes a short
  lease (the first accept wins, a second accept inside the same
  transaction returns `409 invite_already_accepted`).

**Reverse direction.** When a client wants to invite their agency,
the flow is identical — the client workspace plays inviter and the
agency workspace plays recipient. Since the invite is keyed on
`from_workspace_id + property_id`, and `from_workspace_id` must
hold `owner_workspace` on that property at invite time, the
structure is fully symmetric.

**Audit and webhooks.**
`property_workspace_invite.{created,accepted,rejected,revoked,expired}`
are appended to the §10 webhook catalog. Expiry is driven by a
daily worker that flips `state = expired` and fires the webhook.
The resulting `property_workspace` row (on accept) still emits the
existing `property_workspace.shared` event so dashboards refresh.

## Client surface (client login)

Clients are first-class logins in v1. A person who pays the
workspace (or whose organization pays the workspace) holds a
`users` row, just like owners, managers, and workers. Their
authority comes from a `role_grants` row of
`grant_role = 'client'`, either:

- **Workspace-scope with `binding_org_id`** — the client sees
  everything in the workspace tagged to that org: shifts at
  properties where `property.client_org_id = binding_org_id`,
  work_orders / quotes / vendor_invoices billed to that org.
  This is the usual case for a client with more than one property
  in the workspace.
- **Property-scope** — the client sees data at one specific
  property only. Useful for a one-off engagement or when multiple
  clients co-manage a property without sharing the same billing
  org.

The login flow is the unified magic-link enrollment (§03). There
is no separate `client_user` actor — a client is a user with a
`client` grant. The same user may hold `owner` or `manager` grants
on other workspaces (Vincent's scenario — see §05 example).

### Acceptance authority

A client may **accept** a quote billed to their `binding_org_id`.
Because quote acceptance is unconditionally approval-gated (§11),
an agent-delegated token held by the client still routes through
the approval UI — the client must click "Approve" in person. The
workspace's owner/manager may also accept on the client's behalf
(for the cases where a client has granted the agency that
authority out of band).

A client may **reject** a quote or a vendor invoice unilaterally.
A client may **view** all `vendor_invoice` rows tagged to them but
cannot mark them paid; paid-state is an internal bookkeeping flag
owned by the workspace (the workspace is the one pushing the funds,
§09, §22).

### Redactions

On the client surface, worker identity and compensation are
redacted to the level the workspace's owner/manager has configured:

- Worker display name: visible by default; hideable via workspace
  setting `client.show_worker_names` (default: true).
- Worker pay_rule / rate: always hidden. Clients see
  `shift_billing` rates (what the agency charges) not
  `pay_rule` rates (what the agency pays).
- Worker profile details (phone, address, emergency contact):
  always hidden.

Existing guest-link mechanics (§04) are unrelated and continue to
serve per-stay welcome pages only. The `organization.portal_user_id`
column is retained as a convenience pointer ("the natural person
for this org") and seeded from the first `client` grant added for
an org, but it is not the authority source — grants are.

## Approvable actions added

Appended to §11 "Always-gated (not configurable)":

- `work_order.accept_quote`
- `vendor_invoice.approve`
- `vendor_invoice.mark_paid`
- `organization.update_default_pay_destination`
- `work_engagement.set_engagement_kind` (when switching *to* or
  *from* `payroll`, because it moves the engagement between pay
  pipelines)
- `property_workspace_invite.create` (proposes a new
  `managed_workspace` or `observer_workspace` link — same gate as
  adding a manager grant because it materially widens who can
  dispatch work and read PII).
- `property_workspace_invite.accept` (the other side of the same
  decision — accepting liability for dispatching work or observing
  at a property is a human call).
- `property_workspace.revoke` (drops a non-owner `property_workspace`
  link — required for a client to switch agencies without the
  outgoing agency's consent).

Not approval-gated (agent may draft and submit without a human
confirmation card, but every write is still audited):

- `vendor_invoice.upload_proof` — uploading evidence of payment is
  a signal, not a money-routing decision.
- `property_workspace_invite.reject` and `.revoke` — both walk a
  relationship *back* and so don't expose either party to new
  liabilities.

The same rationale as existing money-routing gates: agents can
draft, attach, and propose; humans decide who gets paid.

## Webhook events added

Appended to §10's catalog:

- `organization.created`, `organization.updated`,
  `organization.archived`.
- `client_rate.created`, `client_rate.updated`,
  `client_rate.archived`.
- `work_order.state_changed` with `from` / `to` in the payload.
- `quote.submitted`, `quote.accepted`, `quote.rejected`,
  `quote.superseded`.
- `vendor_invoice.submitted`, `vendor_invoice.approved`,
  `vendor_invoice.rejected`, `vendor_invoice.paid`,
  `vendor_invoice.proof_uploaded`,
  `vendor_invoice.reminder_sent`,
  `vendor_invoice.reminder_exhausted`.
- `shift_billing.resolved` (fires when a shift closes and the
  billing row is written; carries the `rate_source` so a dashboard
  can surface unpriced shifts).
- `property_workspace_invite.created`,
  `property_workspace_invite.accepted`,
  `property_workspace_invite.rejected`,
  `property_workspace_invite.revoked`,
  `property_workspace_invite.expired`.
- `property_workspace.shared`, `property_workspace.revoked`
  (fires when an owner workspace adds or removes another
  workspace's link to one of its properties — agencies and
  observers subscribe so their dashboards refresh without a
  full reload).

## Audit actions added

`organization.create`, `.update`, `.archive`;
`client_rate.create`, `.update`; `client_user_rate.create`,
`.update`; `work_order.create`, `.state_change`,
`.accept_quote`; `quote.submit`, `.accept`, `.reject`,
`.supersede`; `vendor_invoice.submit`, `.approve`, `.reject`,
`.mark_paid`, `.upload_proof`, `.remove_proof`,
`.reminder_sent`; `property_workspace_invite.create`,
`.accept`, `.reject`, `.revoke`, `.expired`.

## Out of scope (v1)

- **Split-billing a single property across multiple clients.** One
  property = one client. Co-ownership is modelled as two properties
  if genuinely necessary.
- **Client-facing PDF invoices + dunning + ageing reports.** Ship
  rates + CSV; render PDFs when a real agency asks for it.
- **Multi-currency within a single work_order.** Same rationale as
  §09 single-currency-per-pay-period.
- **Payment execution.** crewday does not move money — vendor
  invoices and payslips alike produce routing metadata; operators
  push funds from their bank and mark the row paid.
- **Real-time tax calculation.** Invoices and quotes carry an
  informational `tax_cents` line; computing VAT / sales tax from
  jurisdiction rules is a future localisation module.
