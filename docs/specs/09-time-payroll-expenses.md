# 09 — Time, payroll, expenses

Three tightly-linked features for workers who expect to get paid
correctly and for owners and managers who want to stop keeping
booking notes in a phone's notes app.

## Scope by `work_engagement.engagement_kind`

The pipelines below — pay rules, pay periods, payslips, expense
claims, payout destinations — all apply to work engagements with
`engagement_kind = payroll` (§05, §22). **Contractors** and
**agency-supplied** workers are paid via a separate pipeline
(`work_order` / `vendor_invoice`, §22) and do **not** get pay rules,
pay periods, or payslips. They still produce `booking` rows when an
owner or manager schedules them for hourly work, which is how their
hours are captured for the client-billing rollup (§22 "Billable-hour
rollup and exports"); a contractor's `booking` has no corresponding
`pay_period_entry` line because their pay runs through their invoices,
not the payroll cycle. Per-task contractor work — a one-off airport
run quoted at €50, a quoted repair — flows through `work_order` /
`vendor_invoice` directly and skips bookings entirely; the invoice is
the record.

`expense_claim` remains available to any engagement kind — a
contractor who paid for materials out-of-pocket can still submit
receipts, and the resulting reimbursement routes to whichever
`payout_destination` the approval selects. But more commonly a
contractor folds materials into their own `vendor_invoice` line
items instead of filing claims, and the owner or manager chooses
per case.

## Bookings

A **booking** is a worker × property × time-window commitment. It is
the **canonical billable and payable atom** of crew.day: scheduled
time *is* paid time and billed time, by default. This replaces the
v0 clock-in / clock-out shift model — there is no clock state, no
idle timer, no dispute machine. The booking is the contract; tasks
done within it are the work; task-completion timestamps (§06) are
forensic evidence consulted only when the contract is questioned.

> **Why no clock-in / clock-out?** The clock-tap is weak evidence
> (workers forget, double-tap, can game it by ticking tasks early),
> and household / cleaning bookings are commercially per-slot anyway:
> the client books 4h, the agency bills 4h, the maid is paid 4h —
> regardless of whether the actual broom-time was 3h 40 or 4h 10.
> Genuine variances (the place was a disaster; client cancelled
> last-minute) are handled explicitly via the **amend** operation
> below, with audit trail and policy. Labour-law compliance is
> satisfied by the booking + (when amended) `actual_minutes`
> record — no minute-by-minute self-reporting needed.

### Model

```
booking
├── id
├── workspace_id
├── work_engagement_id       # FK → work_engagement.id (pay pipeline key)
├── user_id                  # denormalised from work_engagement.user_id for fast queries
├── property_id              # optional; null for "unassigned-property" work (e.g. a driver doing pickups)
├── client_org_id            # derived cache from property.client_org_id at status=completed; nullable (§22)
├── kind                     # enum: work | travel (§02). `travel` is optional, agency-only itemisation.
├── status                   # enum: scheduled | completed | cancelled_by_client | no_show_worker | cancelled_by_agency | adjusted | pending_approval (§02 booking_status)
├── scheduled_start          # utc
├── scheduled_end            # utc; scheduled_end > scheduled_start
├── actual_minutes           # int?; nullable. Set ONLY when amended (overrun, underrun, or worker-submitted close). Defaults derive from (scheduled_end - scheduled_start - break_seconds/60).
├── actual_minutes_paid      # int; what payroll multiplies. Defaults to scheduled minutes; updated only when an amend is approved (or auto-approved within threshold).
├── break_seconds            # owner/manager-entered or worker-entered at close. Default 0; set in the same amend operation as actual_minutes.
├── notes_md                 # optional
├── adjusted                 # bool; true after any approved amend that touched scheduled or actual time fields
├── adjustment_reason        # text? required when `adjusted = true`
├── pending_amend_minutes    # int?; the worker-requested overrun pending manager approval (>= scheduled minutes only). Cleared on approve/reject. Recorded for labour-law compliance even before pay catches up.
├── pending_amend_reason     # text? mandatory when `pending_amend_minutes` is set
├── declined_at              # tstz?; set when the assigned worker declines this booking. The booking is bumped to the manager queue (status returns to `pending_approval` for reassignment).
├── declined_reason          # text? optional; surfaced to the manager
├── created_by_actor_kind/id
├── created_at / updated_at
└── deleted_at
```

`client_org_id` is written when status transitions to `completed`
(or at the moment an amend is approved on a completed booking) by
copying `property.client_org_id` at that moment. It is the key used
by the billable-hour rollup (§22); a sibling `booking_billing` row
is created in the same transaction with the resolved rate and
subtotal. A booking whose property has no `client_org_id` leaves the
field null and skips billing-row creation (self-managed workspaces
see no change).

`status` transitions:

```
pending_approval ──► scheduled ──► completed ──► (adjusted)
       │                 │              ▲
       │                 ├─► cancelled_by_client
       │                 ├─► cancelled_by_agency
       │                 └─► no_show_worker
       └─ rejected → deleted (manager declines an ad-hoc proposal)
```

- **`pending_approval`** — created by a worker as an ad-hoc proposal
  (see "Ad-hoc bookings" below). The manager reviews and either
  approves into `scheduled` or rejects.
- **`scheduled`** — the default created shape; the contract is set,
  the worker is committed.
- **`completed`** — the booking's `scheduled_end` has passed and no
  cancellation / no-show flag fired. `client_org_id` and
  `booking_billing` are written at this transition.
- **`cancelled_by_client`** — the paying client cancelled. The
  cancellation policy (per-client; see below) decides the fee.
- **`cancelled_by_agency`** — the workspace itself cancelled (rare;
  e.g. property closure, weather, owner request when the workspace
  *is* the owner). Worker is paid per the engagement's
  `cancellation_pay_to_worker` rule (default: paid in full if
  cancellation lead time < 24h, unpaid otherwise).
- **`no_show_worker`** — the booking's window passed with **zero
  task activity at the property** and no manager confirmation.
  Auto-detected by the daily worker job; manager can override
  back to `completed` if it was a tracking glitch (e.g. tasks done
  but never ticked).
- **`adjusted`** — terminal subscript on `completed`: an approved
  amend changed the time fields after completion. Renders as
  "Completed (edited)" in the UI.

`is_pay_bearing` is a derived flag on the booking (not a column):
`true` for engagements with `engagement_kind = payroll` whose
`pay_rule.kind ∈ (hourly, per_task)`, **or** for `contractor` /
`agency_supplied` engagements that produce billing rollups.
`false` for `monthly_salary` engagements — see "Salaried engagements"
below.

### Creation

Bookings are created by:

1. **Manager**, via `POST /bookings` (§12). Most common path: the
   manager fills the calendar from `/scheduler` (§14) by dragging a
   worker onto a property × time window, or by clicking a recurring
   slot in `schedule_ruleset_slot` (§06) which materialises the
   booking. Workers are notified via the agent-message delivery
   chain (§10).
2. **Recurrence materialisation.** A daily worker job
   (`materialise_bookings`) walks each active
   `property_work_role_assignment` whose `schedule_ruleset` slots
   fall within the rolling horizon (workspace-configurable, default
   28 days) and inserts `booking` rows for each (user × property ×
   slot) occurrence not yet materialised. Mirrors the existing
   `generate_task_occurrences` pattern (§06). Holidays and approved
   leaves are honoured via the §06 availability precedence stack.
3. **Worker**, via the ad-hoc path (see "Ad-hoc bookings" below) —
   creates with `status = pending_approval` until the manager
   confirms.

A booking does **not** require any tasks to exist. Tasks generated
from §06 schedules / stay lifecycle rules at the same property and
time window naturally surface inside the booking on the worker's
PWA, but they are not foreign-key-linked: the worker's day is
"the union of bookings I have + tasks at those bookings". If a
booking exists with no tasks (a 4h chore-time at Villa A with
nothing pre-planned), the worker can add ad-hoc tasks during the
booking, or just do their thing.

### Amend operation

A single mechanism handles overruns, underruns, extensions, and
manager corrections. Whether the amend happens **before**, **during**,
or **after** the booking is just a timestamp — the data shape and
authorisation are identical.

`POST /bookings/{id}/amend` accepts a partial body of:

- `scheduled_start` / `scheduled_end` — change the contract window.
- `actual_minutes` — record what really happened (overrun/underrun).
- `break_seconds` — adjust unpaid break time.
- `kind` — switch between `work` / `travel`.
- `reason` — required string; mandatory whenever any time field
  moves. 422 `amend_reason_required` otherwise.

**Authorisation + auto-approve threshold.**

- The worker assigned to the booking holds `bookings.amend_self`. A
  self-amend that increases time by **at most** the engagement-level
  `bookings.auto_approve_overrun_minutes` (default `30`) **and**
  decreases time by any amount is auto-approved: `actual_minutes`
  and `actual_minutes_paid` move together, `adjusted = true`,
  `adjustment_reason` is set, audit log records the change, manager
  sees it in the daily digest. No queue.
- A self-amend that exceeds the threshold writes
  `pending_amend_minutes` and `pending_amend_reason` but leaves
  `actual_minutes_paid` at the scheduled value. The booking's
  `status` does *not* change; the row appears on the manager's
  amend queue (`/bookings?pending_amend=true`). On approve,
  `actual_minutes_paid` advances to the requested value;
  `pending_*` fields clear. The pending value is recorded for
  labour-law compliance — the worker's claim is on record from
  the moment they submit, even if pay is held back.
- Owners, managers, and any user with `bookings.amend_other` can
  amend any booking unconditionally, with the same audit hook. No
  threshold applies — the manager *is* the approval.

The same endpoint serves all four shapes: pre-booking window
extension ("the prep is bigger than I thought, please move my
end time to 14:00 instead of 13:00"), in-flight extension ("I'm
running long"), post-booking submission ("I stayed until 13:42
because of X"), and manager correction ("Maria's break was 30 min
not 60"). Workers do not have to learn two flows — there is one
amend.

**Re-deriving billing.** Amending time fields on a `completed`
booking re-derives its `booking_billing` row inside the same
transaction (§22).

### Pay basis (per-engagement)

The setting `bookings.pay_basis` is resolved through the
`workspace → work_engagement` cascade (§02), enum
`scheduled | actual`. Default `scheduled`.

- **`scheduled`** (default): payroll multiplies
  `scheduled_end - scheduled_start - break_seconds/60` (or, when
  amended, `actual_minutes_paid`). The norm for cleaning agencies
  and most household contracts. Underruns stay with the agency
  (productivity gain); overruns go through amend.
- **`actual`**: payroll multiplies `actual_minutes_paid`, defaulting
  to the scheduled total only if no amend was made. The norm for
  direct-employed staff whose owner pays for what was actually
  worked, not for what was booked. Workers must close the booking
  with an amend to enable underruns to stick — the default
  derivation is still scheduled.

The setting affects payroll only. Client billing always reads
`actual_minutes_paid` (defaults to scheduled), regardless of the
worker's pay basis — the agency's commercial promise to the client
is independent of the agency's contract with its workers.

### Cancellation policy

Two scopes:

- **Per-client** (lives on `organization`, §22): two columns
  `cancellation_window_hours` and `cancellation_fee_pct` on rows
  with `is_client = true`. Null on either falls through to the
  workspace defaults below.
- **Workspace defaults**, settings-cascade keys
  `bookings.cancellation_window_hours` (default `24`) and
  `bookings.cancellation_fee_pct` (default `50`).

When a booking is cancelled with status `cancelled_by_client`:

1. Resolve the policy (per-client first, then workspace default).
2. Compute `lead_hours = booking.scheduled_start - cancelled_at` in
   property-local hours.
3. If `lead_hours >= cancellation_window_hours` → no client fee, no
   worker pay. Booking row remains for history; `booking_billing`
   is not written.
4. If `lead_hours < cancellation_window_hours` → bill the client
   `subtotal_cents = scheduled_minutes * client_hourly_cents *
   cancellation_fee_pct / 100` (rounded half-to-even). Worker is
   paid in full per the engagement's
   `bookings.cancellation_pay_to_worker` setting (default `true`
   — the worker reserved the slot and may have lost other work).
5. Audit log records `lead_hours`, the resolved policy, and which
   layer it came from (per-client / workspace-default).

`cancelled_by_agency` follows the inverse default: worker is paid
in full only if `lead_hours < cancellation_window_hours`, otherwise
unpaid (the agency had time to redeploy them). Client is **not**
billed; if the workspace was billing through to a client, the
manager has to negotiate the credit out of band.

`no_show_worker` cancels the booking with no client bill and no
worker pay. The worker's audit row records the auto-detection
threshold; the manager can override to `completed` from the
booking detail screen.

**Recurring bookings.** Each materialised occurrence is independently
cancellable. A client cancelling their Tuesday slot for the next two
weeks issues two `cancel` calls (or selects a date range in the UI).
The `schedule_ruleset_slot` row is untouched — only the materialised
bookings cancel.

> **Per-property cancellation override.** Deliberately not modelled
> in v1. A single client with multiple villas of varying difficulty
> can override the policy by issuing two `client_user_rate` rows or
> two organizations; once a real customer asks for per-property
> cancellation policy, we add a `property.cancellation_window_hours`
> override layer to the cascade. See §19.

### Salaried engagements

Engagements with `pay_rule.kind = monthly_salary` do **not** get
pay-bearing booking rows. Their `/schedule` view still shows the
recurring weekly pattern (`schedule_ruleset_slot`), approved leaves,
and any tasks assigned to them — the surface is *informational*,
showing where they're expected to be without producing a payroll
ledger entry.

In data terms: the `materialise_bookings` worker job skips
engagements whose active `pay_rule.kind = monthly_salary`. If the
engagement's active rule is `per_task`, bookings are materialised
only for explicit per-task assignments — the worker is paid per
completed `task` rather than per booked time. If `hourly`, bookings
are materialised normally.

A salaried worker can still appear on `/scheduler` via their rota
slots and tasks; the manager can still assign ad-hoc tasks; the
worker still gets the daily digest. The only thing that does not
happen is the creation of a `booking` row that would multiply to
pay.

### Worker decline

A worker may **decline** a `scheduled` booking via
`POST /bookings/{id}/decline` (verb `bookings.decline_self`). The
server stamps `declined_at`, `declined_reason`, returns the row to
`status = pending_approval`, clears the `work_engagement_id`
(unassigned), and notifies the manager via the daily digest +
agent-message chain. The original assignee is excluded from the
auto-reassignment candidate pool for that booking.

Decline is unilateral — no confirmation prompt — but the audit row
makes the act visible. A worker who declines repeatedly will surface
in the manager's people view as a flagged engagement.

### Ad-hoc bookings (worker-created)

A worker may propose a booking via `POST /bookings` with body
`{property_id, scheduled_start, scheduled_end, kind?, notes_md?}`
(verb `bookings.create_pending`). The server forces
`status = pending_approval`, `work_engagement_id` to the worker's
own engagement, and `pay_basis` derives from the engagement.

The manager sees the proposed booking on the amend / pending queue
and either approves (status flips to `scheduled` or, if the booking
is already in the past, directly to `completed`) or rejects (soft
delete with reason; webhook fires).

Use case: the maid swings by Villa A unexpectedly to grab forgotten
laundry; she logs the visit so it shows up on her schedule and rolls
into payroll once the manager confirms. Mirrors the
`expense_claim.submit → approve` shape.

### Coverage / reassignment

Reassigning a booking to a different worker is a
`PATCH /bookings/{id}` writing a new `work_engagement_id`, gated by
`bookings.assign_other` (manager). The server:

1. Validates the new assignee's availability through the §06
   precedence stack; 422 `availability_conflict` if they're on
   approved leave / outside their rota / on another booking that
   overlaps.
2. Re-resolves the pay rule for the new engagement (so Sara's
   hourly rate replaces Maria's, even though the client rate at
   Villa A is unchanged).
3. Fires `booking.reassigned` webhook with `from_user_id` /
   `to_user_id`. Daily digest surfaces the swap to both workers.

The booking row keeps its id; `booking_billing` (if already written
for a completed booking) is recomputed in the same transaction.

### Owner and manager adjustments

Any booking field is patchable via `PATCH /bookings/{id}` with the
same authorisation rules as the amend endpoint. Time fields go
through the amend pipeline above; non-time fields (`notes_md`,
`property_id` for a future booking) update directly. The manager UI
exposes amend as a dialog from the booking detail; the API endpoint
is shared.

### Labour-law compliance

The booking row plus `actual_minutes` (when amended) **is** a
compliant time record under FR / EU rules: it carries
`scheduled_start`, `scheduled_end`, `actual_minutes_paid`, the
worker, the property, and the manager-verifiable audit trail. The
worker is not required to perform minute-by-minute self-reporting;
the booking itself documents the worked period. Jurisdictions that
require per-day signatures (rare, niche) will need a future export
that prints the daily booking list as a signable PDF — flagged in
§19.

### Out of scope (v1)

- **"Arrived" presence beacon.** A one-tap PWA button that stamps
  `arrived_at` on the booking is in the design space but deferred —
  we have task-completion timestamps for the same signal at
  acceptable latency, and adding the column later is purely
  additive. See §19.
- **Door-lock / NFC integration** for external "she was on
  premises" proof. Better than GPS pings, but not v1 — flagged in
  §19 alongside the marketplace.
- **GPS geofencing.** Without clock-in, GPS adds nothing; the v0
  geofence_required setting is removed. PII surface shrinks
  accordingly (§15).
- **Real-time presence dashboards.** Owners who want "is Maria at
  Villa A right now?" see the booking's `scheduled_start`,
  `scheduled_end`, and the most recent task tick; that's enough
  for household management. A live presence map is deferred.

## Pay rules

A `pay_rule` binds a work engagement (or a user_work_role) to a pay
model. Applies only to work engagements with
**`engagement_kind = payroll`** (§05, §22); contractors and
agency-supplied workers use `vendor_invoice` (§22) and never have a
`pay_rule` row. Attempting to write one for a non-payroll engagement
returns 422 `error = "pay_rule_requires_payroll_engagement"`.

| field              | type      | notes                                 |
|--------------------|-----------|---------------------------------------|
| id                 | ULID PK   |                                       |
| work_engagement_id | ULID FK?  | OR user_work_role_id                  |
| user_work_role_id  | ULID FK?  |                                       |
| kind               | enum      | `hourly | monthly_salary | per_task | piecework` |
| effective_from     | date      |                                       |
| effective_to       | date?     | null = ongoing                        |
| currency           | text      | ISO 4217                              |
| hourly_cents       | int?      | kind = hourly                         |
| monthly_cents      | int?      | kind = monthly_salary                 |
| per_task_cents     | int?      | kind = per_task                       |
| piecework_json     | jsonb?    | kind = piecework (units/rates)        |
| overtime_rule_json | jsonb?    | thresholds and multipliers            |
| holiday_rule_json  | jsonb?    | dates + multipliers                   |
| weekly_hours       | int?      | for salary → hourly conversion        |
| notes_md           | text?     |                                       |

Exactly one of `work_engagement_id` / `user_work_role_id` is set.

### Overtime rule shape

```json
{
  "daily_threshold_hours": 8,
  "daily_multiplier": 1.5,
  "weekly_threshold_hours": 40,
  "weekly_multiplier": 1.5,
  "sundays_multiplier": 2.0
}
```

All fields optional; unset fields disable that dimension. **We do not
ship jurisdiction-specific defaults** — that way we do not imply legal
compliance we do not offer. Managers enter what they know.

**Daily + weekly interaction.** If a `weekly_threshold_hours` is set,
weekly overtime is computed and daily thresholds are ignored for that
rule, even if `daily_threshold_hours` is also present. Rationale:
compounding (paying both daily and weekly OT on the same hour) is
rarely legal and the max-only alternative is too surprising for
managers who configured both. If a manager actually needs a
compounding scheme, they encode it in `piecework_json` or file two
rules with disjoint effective dates.

### Piecework shape

`piecework_json` on a `pay_rule` with `kind = piecework`:

```json
{
  "lines": [
    { "unit": "turnover", "label": "Standard turnover", "rate_cents": 2500 },
    { "unit": "deep_clean", "label": "Deep clean", "rate_cents": 6000 }
  ],
  "attribution": "task_template"
}
```

`attribution` is `"task_template"` (count completed tasks whose
template name matches `unit`) or `"manual"` (manager enters counts
when closing the period).

### Pay-rule selection when multiple rules overlap

For a `(work_engagement, period)` pair, the applicable rule is the
one where:

- `effective_from ≤ period.ends_on`, and
- `effective_to IS NULL OR effective_to ≥ period.starts_on`.

If more than one row satisfies both, the rule with the **greatest
`effective_from`** wins; ULID-sort breaks any remaining tie (newer
row). A rule authored with `kind = piecework` never loses to an
earlier `hourly` rule and vice versa — the rank is on
`effective_from` only.

### Holiday rule shape

```json
{
  "dates": ["2026-05-01", "2026-12-25"],
  "multiplier": 2.0,
  "country_codes_for_suggestions": ["FR"]
}
```

**Integration with `public_holidays` (§06).** At period close, the
payroll worker queries `public_holidays` for dates in the period range
and applies each holiday's `payroll_multiplier` to hours worked on
those dates. The pay-rule-level `multiplier` in `holiday_rule_json`
overrides the holiday-table multiplier if both exist for the same
date (pay-rule wins). The `dates` array in `holiday_rule_json` is
optional/deprecated — if present, its dates are merged with
`public_holidays` (union). The `country_codes_for_suggestions` field
remains for the UI suggestion feature.

The UI can suggest public holidays from the `public_holidays` table
(filtered by workspace and country) when the manager configures pay
rules, replacing the older bundled data file approach.

## Pay period

`pay_period {id, workspace_id, work_engagement_id?, starts_on, ends_on,
frequency, status}`. `status` is the canonical `pay_period_status`
enum in §02 (`open | locked | paid`). `work_engagement_id` is
populated when a workspace has divergent per-engagement pay rules;
otherwise null (the period applies to all engagements in the
workspace).

Periods are created per workspace based on the workspace's default
frequency (monthly by default; bi-weekly supported). Periods may
overlap across engagements when their pay rules diverge.

### Period close

An owner or manager closes a period ("Lock"):

1. Validate: no `scheduled` or `pending_approval` bookings remain in
   the period (and no booking with a non-null `pending_amend_minutes`).
   The lock refuses with `bookings_unsettled` listing the offenders so
   the manager can amend or cancel them first.
2. Compute `pay_period_entry` rows: per work_engagement, per day,
   regular hours / overtime / holiday / per-task counts / piecework
   totals. Holiday hours are identified by querying `public_holidays`
   (§06) for dates in the period, applying `payroll_multiplier` from
   the holiday row (overridden by pay-rule-level multiplier if both
   exist).
3. Generate `payslip` rows (`status = draft`).
4. Emit `payroll.period_locked` webhook.

Locked periods cannot be edited; an owner or manager can "reopen"
with an explicit audit event, which also resets the contained
payslips to `draft`.

### Transition to `paid`

A period moves from `locked` to `paid` automatically: when the last
payslip contained in the period transitions to `status = paid`, the
period flips in the same transaction and emits
`payroll.period_paid`. Reopening a period (if legal) flips it back to
`locked`. There is no manual "close" action — payslip state is the
source of truth.

## Payslip

A computed pay document for one (work_engagement, pay_period).

| field                   | type    |
|-------------------------|---------|
| id                      | ULID PK |
| work_engagement_id      | ULID FK |
| pay_period_id           | ULID FK |
| currency                | text    |
| locale                  | text    | BCP-47. Resolved at `draft` creation, immutable. Drives PDF date/number/currency formatting. Resolution: users.preferred_locale (via work_engagement.user_id) -> property.locale -> workspace.default_locale -> `en-US`. |
| jurisdiction            | text    | ISO-3166-1 alpha-2. From the work_engagement's primary property `country` at draft time. Immutable. Selects which payslip template to use. |
| gross_total_cents       | int     |
| components_json         | jsonb   |
| expense_reimbursements_cents | int |
| net_total_cents         | int     |
| pdf_file_id             | ULID FK |
| status                  | enum    | `draft | issued | paid | voided` |
| issued_at / paid_at     | tstz?   |
| email_delivery_id       | ULID FK?|
| payout_snapshot_json    | jsonb?  | immutable snapshot of destinations used; null on `draft`, populated at `draft → issued` transition, never modified thereafter. See "Snapshot on the payslip" below. |

### `components_json` schema

```json
{
  "schema_version": 1,
  "gross_breakdown": [
    {"key": "base_pay",      "cents": 200000},
    {"key": "overtime_150",  "cents": 30000},
    {"key": "holiday_bonus", "cents": 0}
  ],
  "deductions": [
    {"key": "adjustment", "cents": 0, "reason": null}
  ],
  "statutory": [],
  "metadata": {
    "hours_regular": 151.67,
    "hours_overtime_150": 12.0,
    "hourly_rate_cents": 1429
  }
}
```

Design rules:

- `gross_breakdown` keys come from a catalog; **labels are resolved at
  PDF render time** from a locale-aware catalog
  (`payslip_components_{locale}.json`), never stored as final text.
  This lets the same payslip re-render in any locale.
- `statutory` is an empty array in v1. Future country modules populate
  it with lines like
  `{"key": "fr_urssaf_csg", "rate": 0.098, "base_cents": 248000, "cents": 24304}`.
  The PDF template iterates whatever is present.
- `schema_version` allows future shape migration.
- `metadata` carries non-monetary data (hours, rates) needed for the PDF.

### PDF

Rendered with WeasyPrint from a Jinja template.

**Locale/jurisdiction awareness.** Templates are organized as
`payslip_base.html` (shared layout) with optional
`payslip_{jurisdiction}.html` partials for country-specific statutory
sections. v1 ships only the base template. All date/number/currency
formatting in the PDF uses Babel with the payslip's `locale`, never
hardcoded formats.

Line items include:

- Base pay (hours × rate or monthly salary),
- Overtime breakdown (by threshold),
- Holiday bonus,
- Per-task or piecework credits (itemized),
- Expense reimbursements (line per approved claim, linking the
  claim id, grouped by payout destination — see "Payout
  destinations" below).
- Deductions (rare in a workspace context; we leave a line for
  manager-entered adjustments with a mandatory reason).

### Distribution

Email to the worker (via `work_engagement.user_id → users.email`)
with the PDF attached, or a download link (signed URL) if the PDF
is above a configurable size.

## Payout destinations

A user can receive **pay** and **expense reimbursements** at
different destinations. A common case: the workspace opens a small
pre-funded account in the worker's name for operational expenses
so they don't have to front cash; reimbursements land there while
their main paycheque lands in their personal account.

**Payout execution is out of scope for v1** — crew.day does not move
money. Destinations are metadata rendered on the payslip PDF and
returned in API responses so the operator knows where to push funds
from their bank or treasury tool. Even so, routing is
**security-critical**: a tampered destination silently redirects
someone's pay. The rules below are written with that threat in mind.

### `payout_destination`

| field          | type     | notes                                                         |
|----------------|----------|---------------------------------------------------------------|
| id             | ULID PK  |                                                               |
| workspace_id   | ULID FK  | scoping                                                       |
| user_id        | ULID FK? | row belongs to exactly one user **OR** one organization; see "Owner" below |
| organization_id | ULID FK? | row belongs to exactly one organization; see §22              |
| label          | text     | "Personal BNP", "Expense float — Revolut" — display only      |
| kind           | enum     | `bank_account | card_reload | wallet | cash | other`          |
| currency       | text     | ISO 4217; required for all non-`cash` kinds                   |
| display_stub   | text     | public-safe short form: IBAN last-4 + country (`•• FR-12`), card last-4, wallet handle. Never the full number. NULL for `cash`. |
| secret_ref_id  | ULID FK? | pointer to the `secret_envelope` row holding the full account number. Required for `bank_account` and `card_reload`; NULL for `cash`. The full number is never returned by any standard API endpoint — it decrypts only to render a **payout manifest** (§ below), which is streamed and not stored. |
| country        | text?    | ISO-3166; required for `bank_account`                         |
| verified_at    | tstz?    | set when an owner or manager hand-verifies the full number against a paper/photo artifact; `null` means unverified |
| verified_by    | ULID FK? | user id of the verifier                                       |
| notes_md       | text?    | owner/manager-visible, not rendered on PDF                    |
| created_at / updated_at | tstz |                                                         |
| archived_at    | tstz?    | non-null → cannot be selected as a new default; see below     |

### Owner

Exactly one of `user_id` / `organization_id` is set; the
reverse is a 422 `error = "destination_owner_required"`. DB-level
CHECK constraint enforces the exclusivity.

- **User-owned** destinations (the default) serve payslips
  (`work_engagement.pay_destination_id`), expense reimbursements
  (`work_engagement.reimbursement_destination_id`), and — for
  work engagements with `engagement_kind = contractor` — vendor
  invoices where `vendor_invoice.vendor_user_id` points at the
  owning user.
- **Organization-owned** destinations serve vendor invoices where
  `vendor_invoice.vendor_organization_id` points at the owning
  org. They back the `organization.default_pay_destination_id`
  pointer used to route agency-supplied workers' invoices
  automatically. An org-owned destination cannot be used to pay a
  payslip or reimburse a user expense claim — those pipelines are
  work-engagement-oriented.

All downstream rules — per-kind field validation, verification,
approval gates, snapshotting on use, the payout manifest endpoint —
apply identically to both owner kinds. Where the text below says
"the owning employee", read "the owning employee or organization"
unless the context is explicitly payslip-only.

**Validation per `kind`** (server-side, at write time):

- `bank_account`: `country` required; `display_stub` must match the
  country's IBAN format rules; full number is IBAN-checksummed before
  being stored in `secret_envelope`.
- `card_reload`: `display_stub` must be 4 digits; full PAN is Luhn-
  checked before being stored; PAN is **write-only** (never returned).
- `wallet`: `display_stub` is the handle or masked id.
- `cash`: `display_stub` and `secret_ref_id` must be NULL.
- `other`: `display_stub` free-form; `secret_ref_id` optional.

### Where the full number is allowed

- It is supplied only via `POST/PATCH /payout_destinations` body
  field `account_number_plaintext`, which the server encrypts into a
  new `secret_envelope` row in the same transaction and then discards
  from memory.
- The plaintext is **never** echoed back in the response, listed in
  `GET`, returned in webhook payloads, or written to any log. API
  clients never see it again.
- The **stored payslip PDF never contains the full account number**
  — it is rendered from `payslip.payout_snapshot_json`, which holds
  only the `display_stub`. This is deliberate: a stored PDF blob
  outlives most cleanup paths (retention, GDPR purge, S3 backfills)
  and must be safe to keep forever. See "Payout manifest" below for
  how the operator retrieves the full numbers when they actually push
  money.

### Who can mutate destinations

All mutations (`POST`, `PATCH`, archive) write an audit_log row and
fire the `payout_destination.{created,updated,archived,verified}`
webhook. In addition:

- A **worker** (user with a `worker` grant) can create/edit their own
  destinations only if the resolved setting
  `pay.allow_self_manage_destinations = true` (default **off**).
  When off, only users with `owner` or `manager` grants can write.
- **Agent tokens** cannot mutate destinations without manager
  approval. `payout_destination.create`, `.update`,
  `.set_default_pay`, `.set_default_reimbursement`, and
  `expense_claim.set_destination_override` are added to §11's
  approvable-action list unconditionally — no workspace setting
  disables the gate.
- Setting or changing a `work_engagement.pay_destination_id` or
  `work_engagement.reimbursement_destination_id` to a row that does
  not yet have `verified_at` raises a non-fatal warning in the
  owner/manager UI and daily digest until verification is recorded.
  The PDF still renders unverified destinations; the warning is about
  operator hygiene, not a system block.

### Default pointers on `work_engagement`

- `pay_destination_id` — where payslips land.
- `reimbursement_destination_id` — where approved expense
  reimbursements land. If null, falls back to `pay_destination_id`.

Both must reference a non-archived destination whose
`user_id = work_engagement.user_id` and whose `workspace_id` matches
— the FK is enforced with a `CHECK` trigger in SQLite and a
constraint function in Postgres. Attempting to set a pointer to
another user's destination is a 422.

Archiving a destination that is currently referenced as a default
nulls the relevant pointer(s) in the same transaction and emits a
`work_engagement_default_destination.cleared` audit event + webhook.
The next payslip for that engagement renders "Payout: arranged
manually" unless a new default is set first.

Either pointer may be null (cash-in-hand, or not yet configured);
the payslip PDF then renders "Payout: arranged manually" on the
corresponding line — explicit, not silently defaulted to zero.

### Per-claim override

An `expense_claim` carries an optional
`reimbursement_destination_id`. When an owner or manager approves
the claim, the server validates that the referenced destination:

- has `user_id = work_engagement.user_id` for the claim's
  `work_engagement_id`,
- has `workspace_id = claim.workspace_id`,
- is not archived.

The approval UI lets the approver pick any destination satisfying
those rules; there is no separate "blessed" subset. If null, the
engagement's default reimbursement destination applies (which itself
falls back to `pay_destination_id`).

**Currency.** `destination.currency != claim.currency` is fully
supported. On approval the server snaps two related figures
(§ "Amount owed to the employee"): `exchange_rate_to_default`
(claim → workspace default, for reporting) and `owed_amount_cents` /
`owed_exchange_rate` (claim → destination, for payout). The snapped
rates *are* the acknowledgement — no second confirmation step. The
payslip PDF and the payout manifest both show the original amount,
the destination-currency amount, the rate, and the rate source for
transparency.

An agent cannot approve a claim with a non-null
`reimbursement_destination_id` in the approval payload — that field
forces the `expense_claim.set_destination_override` approvable-action
gate (§11) even if the agent also holds `expenses:approve`.

### Snapshot on the payslip

Destinations can change after a period is locked but before a
payslip is issued, or between issue and payment. To keep the pay
record honest, the payslip captures an **immutable snapshot** of the
destinations in use:

```
payslip.payout_snapshot_json = {
  "pay": {
    "destination_id": "pd_…",
    "label": "Personal BNP",
    "kind": "bank_account",
    "display_stub": "•• FR-12",
    "currency": "EUR",
    "verified": true
  },
  "reimbursements": [
    { "claim_id": "exp_…",
      "destination_id": "pd_…",
      "label": "Expense float — Revolut",
      "display_stub": "•• 4499",
      "currency": "EUR",                     // = claim.owed_currency
      "amount_cents": 3412,                  // = claim.owed_amount_cents
      "original_currency": "GBP",            // claim.currency at submission
      "original_amount_cents": 2850,
      "exchange_rate": 1.1972,
      "rate_source": "ecb" }
  ]
}
```

The snapshot is written when the payslip transitions from `draft` to
`issued` (see `payslip_status` §02). Changes to the underlying
`payout_destination` rows after that point do **not** modify the
snapshot. If the destinations referenced in the snapshot are later
archived, the snapshot remains as-is (it is historical evidence).

The PDF is rendered from the snapshot, never from the live pointers,
and contains only `display_stub` for each destination (never the
full account number). Reimbursements on the payslip group by snapshot
`destination_id` so the employee and operator both see what is going
where.

### Payout manifest

The operator needs the full account numbers only at the moment they
actually push funds from their bank/treasury tool. That's a
one-off read, not a stored artifact. crew.day exposes it as:

```
POST /payslips/{id}/payout_manifest
```

- **Manager-session only** (passkey-authenticated human). All bearer
  tokens (scoped and delegated) are refused unconditionally — this
  endpoint is on the "interactive-session-only" list in §11.
  Approval-gating would itself leak, because the approval pipeline
  persists `agent_action.result_json`; so agents cannot reach this
  endpoint even with a manager's approval.
- Response streams a short-TTL artifact (`application/json`) with
  the decrypted account numbers for each destination referenced in
  `payout_snapshot_json`, the corresponding amounts, currency, and
  the `display_stub` for cross-check. The artifact is **not**
  persisted by the server — no `file` row, no blob on disk, no
  webhook payload carrying the plaintext, no entry in the
  idempotency cache (§12).
- Every call writes an `audit_log` row with the caller, IP, and a
  list of destination ids decrypted. Two calls in a 5-minute window
  for the same payslip raise a digest alert ("payout manifest
  fetched twice — confirm the first fetch was legitimate").
- If the underlying `secret_envelope` rows have been erased
  (GDPR purge, see §15), the manifest endpoint returns 410 Gone for
  that payslip with a clear message that routing data is no longer
  available; the operator must arrange payment manually.

The employee-facing payslip (PDF and in-app view) never calls this
endpoint. Only treasury workflows do.

### Currency mismatch

**Payroll (gross).** Issuing a payslip whose computed gross is in
currency `X` with a `pay_destination` whose currency is `Y` is
blocked at the `draft → issued` transition with a 422
`currency_mismatch` error. Multi-currency payroll is deferred
(§ "Out of scope (v1)"): payroll math within a single period is
single-currency, so the mismatch is treated as a configuration
bug. Managers resolve by choosing a same-currency destination or by
explicitly marking the payslip "pay by cash" (clears the pointer
for this payslip only via the snapshot).

**Reimbursements.** A claim in currency `X` attached to a
destination in currency `Y` is **not** a mismatch — expenses are
fully multi-currency (§02, § "Amount owed to the employee"). The
conversion is recorded on the claim at approval time
(`owed_amount_cents` + `owed_exchange_rate` + `owed_rate_source`)
and is itself the acknowledgement; no separate confirmation is
required.

### Audit, approval, and webhook events

- `payout_destination.*`: `created`, `updated`, `archived`, `verified`.
- `work_engagement_default_destination.*`: `set`, `cleared`.
- `payroll.payslip_destination_snapshotted` (fires at issue time).
- `payroll.payout_manifest_accessed` (fires on every manifest fetch).
- `exchange_rate.*`: `refreshed`, `failed`, `overridden` (fired by
  the worker job and the manual-override endpoint — see "Exchange
  rates service" below).
- All of the above are in §10's webhook catalog; the approval gate
  is in §11.

## Expense claims

### Submission flow (worker)

The central user requirement: *submitting should be super easy, with
the LLM auto-populating from a receipt photo.*

1. Worker opens "New expense" on the PWA (or web).
2. Tap **"Add receipt"** → camera opens, takes photo (or picks from
   library). Multiple pages allowed.
3. Upload begins in the background; simultaneously the server calls
   the `expenses.autofill` LLM capability (§11) with the image(s).
4. Within ~3 seconds, the form is pre-populated:
   - `vendor` (e.g. "Monoprix")
   - `purchased_at` (date + approximate time if legible)
   - `total_amount` in the currency from the receipt
   - `currency` (from symbols / locale heuristics)
   - suggested `category` (from vendor type)
   - a set of `expense_line` rows (one per line item) with
     descriptions, quantities, unit prices
   - a suggested `note_md` summary
   - a `confidence` per field
5. Worker reviews; fields with low confidence are highlighted.
6. Submit. State becomes `submitted`, an owner or manager gets a
   notification (email + webhook).

Offline capture: the photo is queued locally; OCR runs on reconnect.

### Model

```
expense_claim
├── id
├── work_engagement_id         # FK → work_engagement.id (submitter's engagement)
├── submitted_at
├── vendor
├── purchased_at               # date (+ time if known)
├── currency                   # ISO-4217; any code, per-claim (see §02 "Multi-currency expenses")
├── exchange_rate_to_default   # snap at approval against workspace.default_currency; immutable
├── total_amount_cents         # in claim currency
├── owed_destination_id        # FK snapshot of payout_destination at approval; immutable
├── owed_currency              # ISO-4217; copy of owed destination's currency
├── owed_amount_cents          # claim total converted into owed_currency via snapped rate; authoritative "owed"
├── owed_exchange_rate         # claim.currency → owed_currency rate (may be cross via EUR)
├── owed_rate_source           # ecb | manual | stale_carryover — copied from exchange_rate.source
├── category                   # supplies|fuel|food|transport|maintenance|other
├── property_id                # optional
├── note_md
├── llm_autofill_json          # full JSON returned by autofill (shape below)
├── autofill_confidence_overall # 0..1, derived min() of per-field scores
├── state                      # draft|submitted|approved|rejected|reimbursed
├── decided_by_user_id
├── decided_at
├── decision_note_md
├── reimbursement_destination_id # ULID FK? override; null → use work_engagement default
└── deleted_at
```

```
expense_line
├── id
├── claim_id
├── description
├── quantity
├── unit_price_cents
├── line_total_cents           # derived
├── asset_id                   # ULID FK?; links to asset for TCO tracking (§21)
├── source                     # ocr | manual (§02)
└── edited_by_user             # bool; set when a user mutates an ocr row
```

```
expense_attachment
├── id
├── claim_id
├── file_id                    # photo / pdf
├── kind                       # receipt | invoice | other
├── pages                      # int (for multi-page PDFs)
```

### Approval (owner or manager)

- Review claim, edit any field, approve or reject with reason.
- Approving snaps the exchange rate against the workspace default
  currency. Source of truth is the `exchange_rate` table (§02),
  populated daily by the `refresh_exchange_rates` worker job and
  — as fallback — by an on-demand fetch at approval time if the
  needed row is missing. The resolved rate is copied to the claim's
  `exchange_rate_to_default` for reproducibility, and the
  destination-currency amount owed is computed and cached (see
  "Amount owed to the employee" below). If both the table and the
  on-demand fetch fail (ECB outage + no prior carryover), the UI
  blocks approval with `no_rate_available` and the approver may
  type the rate by hand (`source = 'manual'`, audit-logged). See
  "Exchange rates service" below for the full rules.
- The claim attaches to the pay period whose `[starts_on, ends_on]`
  contains `purchased_at`. If that period is already `locked` for the
  work_engagement, it attaches to the next open period; a note is
  added to the claim so the worker can see why reimbursement is
  delayed.
- Webhook `expense.approved` / `expense.rejected` fires.

### Reimbursement

A claim becomes `reimbursed` when the containing payslip moves to
`paid`. No separate payment integration.

### Amount owed to the employee

A reimbursement is always ultimately paid from one account into one
account. The **authoritative payment amount** is expressed in the
**reimbursement destination's currency** — that is the number the
employee will actually see land in their account. All other numbers
on the claim (original purchase amount in `claim.currency`,
workspace-default equivalent from `exchange_rate_to_default`) are
informational.

On approval, in addition to
`exchange_rate_to_default`, the server computes and stores:

| field                          | notes                                                                 |
|--------------------------------|-----------------------------------------------------------------------|
| `owed_destination_id`          | snapshot of the destination that was the active default (or override) at approval time; FK to `payout_destination` |
| `owed_currency`                | copy of `payout_destination.currency` at that moment                  |
| `owed_amount_cents`            | `total_amount_cents` converted from `claim.currency` to `owed_currency` using the snapped rate; minor units of `owed_currency` |
| `owed_exchange_rate`           | `1 {claim.currency} = rate {owed_currency}`, derived from the workspace-default rate and the destination-currency rate via EUR cross (see "Exchange rates service" below) |
| `owed_rate_source`             | `ecb | manual | stale_carryover` — copied from the underlying `exchange_rate` row |

Rounding: half-to-even at the destination currency's minor-unit
precision. Storing the rate alongside the amount makes the rounding
reproducible for audit.

**Display & surfaces.**

- **Worker "My pay" (PWA + web).** Shows the running total of
  approved-but-not-yet-reimbursed claims grouped by `owed_currency`
  plus a "due on YYYY-MM-DD" stamp from the next open pay period
  that will roll them up. Driven by
  `GET /expense_claims/pending_reimbursement?user_id=me`.
- **Payslip PDF (§ "PDF" above).** The existing "Expense
  reimbursements" line itemises each included claim as
  `{original_amount} {claim.currency} → {owed_amount} {owed_currency}
  @ {owed_exchange_rate} ({owed_rate_source})` when
  `claim.currency ≠ owed_currency`; otherwise just the single
  amount. Subtotals group by `payout_snapshot_json.destination_id`.
- **Manager "Pay" page.** Per-employee pending total and a
  workspace-wide aggregate, each grouped by destination currency.
  A manager who wants to split-pay early (before period close)
  issues one-off payments out of band — crew.day records that the
  claim is `reimbursed` when the operator marks the containing
  payslip `paid`.

**Currency alignment rule.** Because `owed_currency` is pinned to the
destination at approval time, later changes to the destination's
currency or archival have no effect on the amount owed (the snapshot
on the claim is immutable, same as `exchange_rate_to_default`). If
the destination is archived before reimbursement, the manager must
route the payment manually — the payslip will still render the
snapshotted figure.

**Agent authority.** An agent cannot change `owed_destination_id`
independently: the `expense_claim.set_destination_override`
approval gate already covers this (§11). Approving an agent's
override also re-snaps `owed_currency` / `owed_amount_cents` at
the approval moment (same transaction).

### Exchange rates service

#### Worker job: `refresh_exchange_rates`

A daily APScheduler job (§01) fetches foreign-exchange reference
rates and populates the `exchange_rate` table (§02).

- **Cadence.** Runs at **17:00 CET** on every calendar day. The ECB
  reference rate is published on working days at ~16:00 CET; running
  at 17:00 leaves a buffer. On weekends and TARGET holidays the job
  still runs (and writes `stale_carryover` rows forward from the
  last working day's rate).
- **Currencies refreshed.** For every workspace, the job computes
  the **active currency set**: the union of
  - `workspace.default_currency`,
  - every distinct `property.default_currency` in the workspace,
  - every distinct `currency` on open `expense_claim` rows in the
    last 180 days,
  - every distinct `currency` on `payout_destination` rows not
    archived,
  - every distinct `currency` on `pay_rule` rows with
    `effective_to IS NULL OR effective_to ≥ today - 180 days`.
  The active set is the quote currencies; the base is
  `workspace.default_currency`. For workspaces whose default is
  not `EUR`, the job fetches ECB's EUR-based table and **cross-
  computes** the non-EUR base rate via EUR pivot.
- **Provider.** ECB daily reference rates
  (`https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml`).
  No other provider in v1. A workspace-scoped manual override is the
  escape hatch for currencies ECB doesn't publish (e.g. XOF peg);
  see "Manual override" below.
- **Idempotency.** Upsert on `(workspace_id, base, quote,
  as_of_date)`. A retry after partial success is safe.
- **Failure handling.** If the ECB endpoint is unreachable, the job
  logs `llm_call`-shaped diagnostics (source = `ecb`, latency, HTTP
  status), emits an `exchange_rate.failed` webhook and a daily-
  digest warning, and re-queues itself at 18:00, 19:00, 20:00 CET.
  Three consecutive failures page the deployment operator.
- **Events.** One `exchange_rate.refreshed` webhook per successful
  run carrying `{workspace_id, as_of_date, currencies_refreshed: n}`;
  `exchange_rate.failed` on each failed attempt.

#### On-demand fallback

If an approval needs a `(base, quote, as_of_date)` row and the table
does not have one (e.g. first-ever use of `GBP` in the workspace,
worker hasn't caught up yet), the server issues a one-shot ECB fetch
inside the approval transaction and upserts the missing row with
`source = 'ecb'`, `fetched_by_job = 'on_demand_fallback'`. The
snapshot fields on the claim are then copied from that row. A
process-wide in-memory cache keyed by `(base, quote, as_of_date)`
keeps a batch of consecutive approvals to one fetch.

#### Manual override

When neither the worker nor the fallback can produce a rate (ECB
outage beyond the retry window, or a currency ECB doesn't publish),
the approval UI prompts the manager for a rate. The manager-entered
row is written with `source = 'manual'`, `source_ref = user_id`, and
an `exchange_rate.overridden` webhook fires. Subsequent approvals on
the same day reuse that row; the worker job will not overwrite a
`manual` row.

#### Staleness policy

At approval time the server checks `(as_of_date, today())`:

- `as_of_date == today` → green, no warning.
- `today - as_of_date ≤ 3 days` and `source = 'stale_carryover'` →
  amber, inline warning "Using rate from {as_of_date}; ECB has not
  published since."
- `today - as_of_date > 3 days` → red, approval blocked with
  `rate_too_stale`; manager must refresh or enter a manual rate.

#### Why rates are workspace-scoped

Rates could have been a deployment-wide singleton. Scoping them per
workspace keeps four properties straight:

1. The base currency in the row always matches that workspace's
   `default_currency`, so reads need no conversion.
2. Manual overrides are tenant-local — one client's manual XAF rate
   does not leak into another's approvals.
3. Multi-tenant deployments (§01) can purge one tenant's rates on
   offboarding without untangling a global table.
4. The rate-refresh job's active-set logic can run per workspace in
   parallel without a global coordination lock.

The storage cost is tiny (N workspaces × ~30 currencies × 365 days).

#### Surfaces

- `GET /exchange_rates` — list rates for the workspace; filters
  `as_of_date`, `quote`, `source`.
- `GET /exchange_rates/{base}/{quote}?as_of=YYYY-MM-DD` — single row;
  `as_of` defaults to today.
- `POST /exchange_rates/refresh` — manager-only; force a run of
  `refresh_exchange_rates` for this workspace. Returns the job
  correlation id.
- `POST /exchange_rates/manual` — manager-only; body `{base, quote,
  as_of_date, rate}`; fails 409 if an `ecb` row exists for that key.
- CLI: `crewday rates show`, `crewday rates refresh`, `crewday rates
  set-manual` (§13).

### LLM accuracy & guardrails

- The autofill call is bounded (max 2 images, 5 MB total); oversized
  uploads split into multiple calls.
- Confidence is **per-field**, not aggregate. `llm_autofill_json`
  shape:

  ```json
  {
    "vendor":        { "value": "Monoprix",    "confidence": 0.94 },
    "purchased_at":  { "value": "2026-04-15",  "confidence": 0.88 },
    "currency":      { "value": "EUR",         "confidence": 0.99 },
    "total_amount_cents": { "value": 3412,     "confidence": 0.72 },
    "category":      { "value": "supplies",    "confidence": 0.55 },
    "lines": [
      { "description": {"value": "Detergent 2L", "confidence": 0.9},
        "quantity":    {"value": 1,              "confidence": 0.95},
        "unit_price_cents": {"value": 899,       "confidence": 0.8} }
    ],
    "note_md":       { "value": "2-item grocery receipt", "confidence": 0.7 }
  }
  ```

  `autofill_confidence_overall` on `expense_claim` is derived as the
  minimum confidence across all populated top-level fields.

- Per-field UI thresholds:
  - ≥0.9 autofilled, quiet.
  - 0.6–0.9 autofilled, slight yellow border, focus on click.
  - <0.6 left blank with "review" placeholder, never pre-filled.
- All extractions recorded in `llm_call` (§11). Cost is attributed to
  `expenses.autofill` capability.
- If the resolved setting `expenses.autofill_receipts = false` for the
  worker / workspace, the photo is attached but no extraction runs.
- When a user edits an OCR line, `expense_line.source` stays `ocr`
  and `edited_by_user` flips to `true`. Fully user-created lines have
  `source = manual` from the start.

## Reports and exports

- **Timesheets** — CSV per pay period: user, date, property, hours
  (scheduled / actual_paid), overtime, holiday, notes. Includes
  bookings from all engagement kinds; a column marks whether the
  hours roll into a payslip or a `vendor_invoice` pipeline.
- **Payroll register** — CSV per pay period: user, gross, net,
  expenses, currency. Payroll work engagements only.
- **Expense ledger** — CSV by date range: claim id, user, vendor,
  category, amount (claim + base currency), state.
- **Hours by property** — rollup useful for owners: hours consumed at
  each property for budgeting.
- **Billable hours by client** — see §22 "Billable-hour rollup and
  exports". Per-client CSV driven by `booking_billing` rows.
- **Work-order ledger** — see §22. Per-client work orders with
  aggregate quote and invoice totals.

Exports: `GET /api/v1/exports/...csv` (streamed) or via CLI
`crewday export ...`.

## Out of scope (v1)

- Tax withholding, social contributions, statutory filings.
- Tip pooling, booking differentials (e.g. night-rate uplifts)
  beyond the overtime / holiday rules.
- Direct bank transfers or payment execution.
- **Multi-currency *payroll*** is not implemented in v1.
  `pay_rule.currency` and `payslip.currency` carry per-entity codes,
  and `property.default_currency` allows per-property overrides, but
  v1 enforces that all pay rules for one work_engagement within a
  single pay period share one currency. Lifting that constraint
  requires conversion logic at period-close time.
  **Multi-currency *expenses*, on the other hand, are fully
  supported in v1** — any claim in any ISO-4217 currency, snapped
  against the workspace default on approval, paid in the
  reimbursement destination's currency (see "Amount owed to the
  employee" above and §02 "Multi-currency expenses").
