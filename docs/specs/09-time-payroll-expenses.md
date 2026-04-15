# 09 — Time, payroll, expenses

Three tightly-linked features for staff who expect to get paid
correctly and for managers who want to stop keeping shift notes in a
phone's notes app.

## Time tracking (shifts)

### Model

```
shift
├── id
├── household_id
├── employee_id
├── property_id              # optional; unassigned shifts for remote drivers, etc.
├── status                   # enum: open | closed | disputed (§02)
├── started_at               # utc
├── ended_at                 # utc, nullable while status = open
├── expected_started_at      # nullable; set when clock-in is delayed
├── method_in                # enum: pwa | web | manager | agent | qr_kiosk
├── method_out               # same
├── geo_in_lat/lon/accuracy  # nullable, only if capability + consent
├── geo_out_lat/lon/accuracy
├── break_seconds            # manager-entered or self-entered
├── notes_md                 # optional
├── adjusted                 # bool
├── adjustment_reason        # text? when adjusted == true
├── created_by_actor_kind/id
└── deleted_at
```

`status` transitions: `open` on clock-in; `closed` on clock-out or
manager close; `disputed` when the worker auto-closes an orphan open
shift (see "Open shift recovery").

### Clock-in / clock-out

Capabilities (`time.clock_in`, `time.geofence_required`) gate UI.

- **Clock-in.** Employee taps a big green button on the PWA "home"
  screen; the server records `started_at = now()`, property defaulted
  from today's assigned task (or manually picked). If geofence
  required, the browser's Geolocation API is consulted with a
  configured accuracy threshold; if the user denies or GPS is poor,
  clock-in fails with a clear explanation and an option to request
  a manager override.
- **Clock-out.** Green button flips to red "Clock out". Prompts for
  break time if shift > 6h (configurable). Saves `ended_at`.
- **QR kiosk.** Managers can print a property-specific QR that opens
  a simple clock-in/out page with passkey assertion. Useful when
  staff share a family phone.

### Manager adjustments

Any shift can be adjusted via `PATCH /shifts/{id}` (§12). The server
computes whether the patch touches time fields (`started_at`,
`ended_at`, `break_seconds`, `expected_started_at`):

- If yes: sets `adjusted = true` and requires a non-empty
  `adjustment_reason` in the body; returns 422 otherwise.
- If the patch only touches `notes_md` / `property_id`: does **not**
  set `adjusted`; `adjustment_reason` is optional.

Original values are preserved in `audit_log.before_json` either way.
Employees see "(edited by manager)" on shifts with `adjusted = true`.

### Open shift recovery

If a shift stays open > 16h, the worker emails a reminder to the
employee; if still open at 24h, manager is notified and the system
auto-closes at `started_at + 8h` with `status = disputed` so it shows
up in review.

## Pay rules

A `pay_rule` binds an employee (or an employee_role) to a pay model.

| field              | type      | notes                                 |
|--------------------|-----------|---------------------------------------|
| id                 | ULID PK   |                                       |
| employee_id        | ULID FK?  | OR employee_role_id                   |
| employee_role_id   | ULID FK?  |                                       |
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

Exactly one of `employee_id` / `employee_role_id` is set.

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

For a `(employee, period)` pair, the applicable rule is the one where:

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

The UI can suggest public holidays from a bundled data file per
country, but the manager copies them into the rule — nothing is
auto-picked, to avoid surprise.

## Pay period

`pay_period {id, household_id, employee_id?, starts_on, ends_on,
frequency, status}`. `status` is the canonical `pay_period_status`
enum in §02 (`open | locked | paid`). `employee_id` is populated when
a household has divergent per-employee pay rules; otherwise null
(the period applies to all employees).

Periods are created per household based on the household's default
frequency (monthly by default; bi-weekly supported). Periods may
overlap across employees when their pay rules diverge.

### Period close

A manager closes a period ("Lock"):

1. Validate: no open shifts remain in the period.
2. Compute `pay_period_entry` rows: per employee, per day, regular
   hours / overtime / holiday / per-task counts / piecework totals.
3. Generate `payslip` rows (`status = draft`).
4. Emit `payroll.period_locked` webhook.

Locked periods cannot be edited; manager can "reopen" with an explicit
audit event, which also resets the contained payslips to `draft`.

### Transition to `paid`

A period moves from `locked` to `paid` automatically: when the last
payslip contained in the period transitions to `status = paid`, the
period flips in the same transaction and emits
`payroll.period_paid`. Reopening a period (if legal) flips it back to
`locked`. There is no manual "close" action — payslip state is the
source of truth.

## Payslip

A computed pay document for one (employee, pay_period).

| field                   | type    |
|-------------------------|---------|
| id                      | ULID PK |
| employee_id             | ULID FK |
| pay_period_id           | ULID FK |
| currency                | text    |
| gross_total_cents       | int     |
| components_json         | jsonb   |
| expense_reimbursements_cents | int |
| net_total_cents         | int     |
| pdf_file_id             | ULID FK |
| status                  | enum    | `draft | issued | paid | voided` |
| issued_at / paid_at     | tstz?   |
| email_delivery_id       | ULID FK?|

### PDF

Rendered with WeasyPrint from a Jinja template. Line items include:

- Base pay (hours × rate or monthly salary),
- Overtime breakdown (by threshold),
- Holiday bonus,
- Per-task or piecework credits (itemized),
- Expense reimbursements (line per approved claim, linking the
  claim id, grouped by payout destination — see "Payout
  destinations" below).
- Deductions (rare in a household context; we leave a line for
  manager-entered adjustments with a mandatory reason).

### Distribution

Email to the employee with the PDF attached, or a download link
(signed URL) if the PDF is above a configurable size.

## Payout destinations

An employee can receive **pay** and **expense reimbursements** at
different destinations. A common case: the household opens a small
pre-funded account in the employee's name for operational expenses
so they don't have to front cash; reimbursements land there while
their main paycheque lands in their personal account.

### `payout_destination`

| field         | type     | notes                                  |
|---------------|----------|----------------------------------------|
| id            | ULID PK  |                                        |
| household_id  | ULID FK  |                                        |
| employee_id   | ULID FK  |                                        |
| label         | text     | "Personal BNP", "Expense float — Revolut" |
| kind          | enum     | `bank_account | card_reload | cash | other` |
| account_ref   | text     | IBAN / last4 / wallet handle; validated per `kind` |
| account_ref_encrypted | bool | set `true` for full IBAN / account number; stored via `secret_envelope` (§15) |
| currency      | text     | ISO 4217                               |
| notes_md      | text?    |                                        |
| archived_at   | tstz?    |                                        |

The employee can hold multiple destinations; each one is a separate
row. Archiving preserves history.

### `employee.pay_destination_id` and `employee.reimbursement_destination_id`

Two nullable pointers on the employee row name the **defaults**:

- `pay_destination_id` — where payslips land.
- `reimbursement_destination_id` — where approved expense
  reimbursements land. If null, falls back to `pay_destination_id`.

Either pointer may be null (cash-in-hand, or not yet configured);
the payslip PDF then renders "Payout: arranged manually" on the
corresponding line.

### Per-claim override

An `expense_claim` carries an optional
`reimbursement_destination_id` that overrides the employee default
when the manager wants to send a specific claim elsewhere (e.g. an
unusually large fuel claim reimbursed directly to a card). The
payslip PDF groups reimbursements by destination so the employee
sees what is going where.

### Out of scope (v1)

Payout execution itself is still out of scope — miployees does not
move money. Destinations are metadata on the payslip PDF and the
reimbursement record so operators know where to push funds from
their bank or treasury tool. Integrations with Wise, Revolut
Business, bank APIs, etc., are post-v1 (see §19).

## Expense claims

### Submission flow (employee)

The central user requirement: *submitting should be super easy, with
the LLM auto-populating from a receipt photo.*

1. Employee opens "New expense" on the PWA (or web).
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
5. Employee reviews; fields with low confidence are highlighted.
6. Submit. State becomes `submitted`, manager gets a notification
   (email + webhook).

Offline capture: the photo is queued locally; OCR runs on reconnect.

### Model

```
expense_claim
├── id
├── employee_id
├── submitted_at
├── vendor
├── purchased_at               # date (+ time if known)
├── currency
├── exchange_rate_to_default   # snapshot at submission; editable by manager
├── total_amount_cents         # in claim currency
├── category                   # supplies|fuel|food|transport|maintenance|other
├── property_id                # optional
├── note_md
├── llm_autofill_json          # full JSON returned by autofill (shape below)
├── autofill_confidence_overall # 0..1, derived min() of per-field scores
├── state                      # draft|submitted|approved|rejected|reimbursed
├── decided_by_manager_id
├── decided_at
├── decision_note_md
├── reimbursement_destination_id # ULID FK? override; null → use employee default
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

### Approval (manager)

- Review claim, edit any field, approve or reject with reason.
- Approving snaps the exchange rate (ECB daily fix fetched at
  approval time; cached in memory per currency/day by the worker,
  and stored on the claim in `exchange_rate_to_default` for
  reproducibility). If the ECB fetch fails, the UI blocks approval
  with a "no exchange rate available, try again or enter manually"
  error and the manager may type the rate by hand.
- The claim attaches to the pay period whose `[starts_on, ends_on]`
  contains `purchased_at`. If that period is already `locked` for the
  employee, it attaches to the next open period; a note is added to
  the claim so the employee can see why reimbursement is delayed.
- Webhook `expense.approved` / `expense.rejected` fires.

### Reimbursement

A claim becomes `reimbursed` when the containing payslip moves to
`paid`. No separate payment integration.

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
- If the household disabled `expenses.autofill_llm` for the employee
  or the capability globally, the photo is attached but no extraction
  runs.
- When a user edits an OCR line, `expense_line.source` stays `ocr`
  and `edited_by_user` flips to `true`. Fully user-created lines have
  `source = manual` from the start.

## Reports and exports

- **Timesheets** — CSV per pay period: employee, date, property,
  hours, overtime, holiday, notes.
- **Payroll register** — CSV per pay period: employee, gross, net,
  expenses, currency.
- **Expense ledger** — CSV by date range: claim id, employee, vendor,
  category, amount (claim + base currency), state.
- **Hours by property** — rollup useful for owners: hours consumed at
  each property for budgeting.

Exports: `GET /api/v1/exports/...csv` (streamed) or via CLI
`miployees export ...`.

## Out of scope (v1)

- Tax withholding, social contributions, statutory filings.
- Tip pooling, shift differentials beyond the overtime/holiday rules.
- Direct bank transfers or payment execution.
- Multi-currency payroll (claims can be multi-currency; payslips are
  per-currency, one per employee).
