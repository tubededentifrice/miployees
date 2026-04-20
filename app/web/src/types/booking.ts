// crewday — JSON API types: bookings, stay lifecycle, booking billing.

// §02, §09 — booking_status. Replaces the v0 shift_status enum.
export type BookingStatus =
  | "pending_approval"
  | "scheduled"
  | "completed"
  | "cancelled_by_client"
  | "cancelled_by_agency"
  | "no_show_worker"
  | "adjusted";

export type BookingKind = "work" | "travel";

export interface Booking {
  id: string;
  employee_id: string;
  property_id: string;
  scheduled_start: string;
  scheduled_end: string;
  status: BookingStatus;
  kind: BookingKind;
  actual_minutes: number | null;
  actual_minutes_paid: number | null;
  break_seconds: number;
  pending_amend_minutes: number | null;
  pending_amend_reason: string | null;
  declined_at: string | null;
  declined_reason: string | null;
  notes_md: string;
  adjusted: boolean;
  adjustment_reason: string | null;
  client_org_id: string | null;
  work_engagement_id: string;
  user_id: string;
}

// ── Stay lifecycle ────────────────────────────────────────────────

export type LifecycleTrigger = "before_checkin" | "after_checkout" | "during_stay";

export interface StayLifecycleRule {
  id: string;
  property_id: string;
  trigger: LifecycleTrigger;
  template_id: string;
  offset_hours: number;
  enabled: boolean;
}

export interface BookingBilling {
  id: string;
  booking_id: string;
  client_org_id: string;
  user_id: string;
  currency: string;
  billable_minutes: number;
  hourly_cents: number;
  subtotal_cents: number;
  rate_source: "client_user_rate" | "client_rate" | "unpriced";
  rate_source_id: string | null;
  work_engagement_id: string;
  is_cancellation_fee: boolean;
}
