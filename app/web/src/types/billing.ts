// crewday — JSON API types: client organizations, rates, work orders,
// quotes, and vendor invoices (§22 client and supplier accounting).

// §22 — counterparty of the workspace. Either a paying client, a
// supplier of workers, or both (one row, role flags).
export interface Organization {
  id: string;
  name: string;
  workspace_id: string;
  is_client: boolean;
  is_supplier: boolean;
  legal_name: string | null;
  default_currency: string;
  tax_id: string | null;
  contacts: { label: string; name: string; email: string; phone_e164: string; role: string }[];
  notes: string | null;
  default_pay_destination_stub: string | null;
  portal_user_id: string | null;
  /** §22 cancellation policy. Null falls through to workspace defaults
   *  `bookings.cancellation_window_hours` / `bookings.cancellation_fee_pct`. */
  cancellation_window_hours: number | null;
  cancellation_fee_pct: number | null;
}

export interface ClientRate {
  id: string;
  client_org_id: string;
  work_role_id: string;
  hourly_cents: number;
  currency: string;
  effective_from: string;
  effective_to: string | null;
}

export interface ClientUserRate {
  id: string;
  client_org_id: string;
  user_id: string;
  hourly_cents: number;
  currency: string;
  effective_from: string;
  effective_to: string | null;
}

export type WorkOrderState =
  | "draft" | "quoted" | "accepted" | "in_progress"
  | "completed" | "cancelled" | "invoiced" | "paid";

export interface WorkOrder {
  id: string;
  property_id: string;
  title: string;
  state: WorkOrderState;
  assigned_user_id: string | null;
  currency: string;
  client_org_id: string | null;
  asset_id: string | null;
  description: string | null;
  accepted_quote_id: string | null;
  created_at: string | null;
  requested_by_user_id: string | null;
}

export interface QuoteLine {
  kind: string;
  description: string;
  quantity: number;
  unit: string;
  unit_price_cents: number;
  total_cents: number;
}

export interface Quote {
  id: string;
  work_order_id: string;
  submitted_by_user_id: string;
  currency: string;
  subtotal_cents: number;
  tax_cents: number;
  total_cents: number;
  status: "draft" | "submitted" | "accepted" | "rejected" | "superseded" | "expired";
  lines: QuoteLine[];
  valid_until: string | null;
  submitted_at: string | null;
  decided_at: string | null;
  decided_by_user_id: string | null;
  decision_note: string | null;
  work_engagement_id: string | null;
}

export interface VendorInvoice {
  id: string;
  currency: string;
  subtotal_cents: number;
  tax_cents: number;
  total_cents: number;
  billed_at: string;
  status: "draft" | "submitted" | "approved" | "rejected" | "paid" | "voided";
  work_order_id: string | null;
  property_id: string | null;
  vendor_user_id: string | null;
  vendor_work_engagement_id: string | null;
  vendor_organization_id: string | null;
  due_on: string | null;
  payout_destination_stub: string | null;
  lines: QuoteLine[];
  submitted_at: string | null;
  approved_at: string | null;
  decided_by_user_id: string | null;
  paid_at: string | null;
  paid_by_user_id: string | null;
  decision_note: string | null;
  // §22 Proof of payment + reminders
  proof_of_payment_file_ids: string[];
  reminder_last_sent_at: string | null;
  reminder_next_due_at: string | null;
}
