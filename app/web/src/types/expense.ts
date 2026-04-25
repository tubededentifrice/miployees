// crewday — JSON API types: expenses, receipts, FX rates, pending
// reimbursements, and scan-assisted autofill.
//
// `Expense` mirrors the cd-t6y2 wire shape (`ExpenseClaimPayload` in
// `app/api/v1/expenses.py`). The fetcher in `@/lib/expenses` unwraps
// the cursor envelope and rebuilds each row through
// `mapExpenseClaimPayload`, so consumers can keep importing the
// `Expense` type unchanged once the helper is in place.
//
// Differences vs. the legacy mock-era shape (kept here as a migration
// anchor — every consumer that read these fields is now updated):
// - `merchant` → `vendor`
// - `note` → `note_md`
// - `status` → `state`
// - `submitted_at` is nullable (drafts have not been submitted)
// - `purchased_at` (new) — when the receipt was issued.
// - `amount_cents` → `total_amount_cents`
// - `category` is a non-null string (server enforces the enum)
// - dropped: `employee_id`, `user_id`, `ocr_confidence`,
//   `exchange_rate_to_default`, `owed_*`. None of these are surfaced
//   by the v1 expense payload; reintroduce them only when the server
//   does (each is tracked in its own Beads task).

export type ExpenseStatus = "draft" | "submitted" | "approved" | "rejected" | "reimbursed";
export type ExpenseCategory = "supplies" | "fuel" | "food" | "transport" | "maintenance" | "other";

export type ExchangeRateSource = "ecb" | "manual" | "stale_carryover";

/**
 * Receipt / invoice attachment row inlined on the parent claim.
 *
 * Mirrors `ExpenseAttachmentPayload` server-side. The dedicated
 * `GET /{id}/attachments` endpoint returns the same row shape.
 */
export interface ExpenseAttachment {
  id: string;
  claim_id: string;
  blob_hash: string;
  /** "receipt" | "invoice" | "other" — kept as a string so a server-side
   *  extension lands without a SPA build. */
  kind: string;
  pages: number | null;
  created_at: string;
}

/**
 * Single expense claim row. 1:1 with the server's `ExpenseClaimPayload`.
 */
export interface Expense {
  id: string;
  workspace_id: string;
  work_engagement_id: string;
  vendor: string;
  /** ISO-8601 UTC — date the receipt itself was issued. */
  purchased_at: string;
  currency: string;
  total_amount_cents: number;
  /** Enum value (`ExpenseCategory`); kept as a free string at the
   *  boundary so a server-side allow-list extension lands without a
   *  SPA build. Consumers narrowing to the enum should `as` cast. */
  category: string;
  property_id: string | null;
  note_md: string;
  state: ExpenseStatus;
  /** ISO-8601 UTC; null for draft claims. */
  submitted_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note_md: string | null;
  created_at: string;
  /** ISO-8601 UTC; non-null for soft-deleted (cancelled-from-draft)
   *  rows. The list endpoint already filters these out, so the field
   *  exists for round-tripping detail reads. */
  deleted_at: string | null;
  attachments: ExpenseAttachment[];
}

export interface ExchangeRate {
  id: string;
  workspace_id: string;
  base: string;
  quote: string;
  as_of_date: string;
  rate: number;
  source: ExchangeRateSource;
  fetched_at: string;
  fetched_by_job: string | null;
  source_ref: string | null;
}

export interface PendingReimbursementUserBreakdown {
  user_id: string;
  /** Display name for the manager UI ("Maya G."), pinned at read time
   *  from `user.display_name`. Renames flow through naturally on the
   *  next read of `/expenses/pending_reimbursement`. */
  user_name: string;
  totals_by_currency: { currency: string; amount_cents: number }[];
}

export interface PendingReimbursement {
  user_id: string | null;
  claims: Expense[];
  totals_by_currency: { currency: string; amount_cents: number }[];
  /** Present only on the workspace-wide aggregate response. */
  by_user?: PendingReimbursementUserBreakdown[];
}

export interface AutofillField<T> {
  value: T;
  confidence: number;
}

export interface ExpenseScanResult {
  vendor: AutofillField<string>;
  purchased_at: AutofillField<string>;
  currency: AutofillField<string>;
  total_amount_cents: AutofillField<number>;
  category: AutofillField<ExpenseCategory>;
  note_md: AutofillField<string>;
  agent_question: string | null;
}
