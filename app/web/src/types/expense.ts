// crewday — JSON API types: expenses, receipts, FX rates, pending
// reimbursements, and scan-assisted autofill.

export type ExpenseStatus = "draft" | "submitted" | "approved" | "rejected" | "reimbursed";
export type ExpenseCategory = "supplies" | "fuel" | "food" | "transport" | "maintenance" | "other";

export type ExchangeRateSource = "ecb" | "manual" | "stale_carryover";

export interface Expense {
  id: string;
  employee_id: string;
  user_id: string;
  work_engagement_id: string;
  amount_cents: number;
  currency: string;
  merchant: string;
  submitted_at: string;
  status: ExpenseStatus;
  note: string;
  ocr_confidence: number | null;
  category: ExpenseCategory | null;
  /** Snap of claim→workspace-default FX at approval time. §09 */
  exchange_rate_to_default: number | null;
  /** Destination currency at approval time — authoritative "owed" currency. §09 */
  owed_currency: string | null;
  /** Claim total converted into owed_currency using the snapped rate. §09 */
  owed_amount_cents: number | null;
  owed_exchange_rate: number | null;
  owed_rate_source: ExchangeRateSource | null;
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
  employee_id: string;
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
