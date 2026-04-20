// crewday — JSON API types: employees, leave, payroll.

export interface Employee {
  id: string;
  name: string;
  roles: string[];
  properties: string[];
  avatar_initials: string;
  avatar_file_id: string | null;
  avatar_url: string | null;
  phone: string;
  email: string;
  started_on: string;
  capabilities: Record<string, boolean | null>;
  workspaces: string[];
  villas: string[];
  language: string;
  weekly_availability: Record<string, [string, string] | null>;
  evidence_policy: "inherit" | "require" | "optional" | "forbid";
  preferred_locale: string | null;
  settings_override: Record<string, unknown>;
}

export interface Leave {
  id: string;
  employee_id: string;
  starts_on: string;
  ends_on: string;
  category: "vacation" | "sick" | "personal" | "bereavement" | "other";
  note: string;
  approved_at: string | null;
}

export type PayRuleKind = "hourly" | "monthly_salary" | "per_task" | "piecework";

export interface PayRule {
  id: string;
  employee_id: string;
  property_id: string | null;
  kind: PayRuleKind;
  rate_cents: number;
  currency: string;
  effective_from: string;
  effective_until: string | null;
}

export type PayPeriodStatus = "open" | "locked" | "paid";

export interface PayPeriod {
  id: string;
  starts_on: string;
  ends_on: string;
  status: PayPeriodStatus;
  locked_at: string | null;
}

export interface PaySlip {
  id: string;
  employee_id: string;
  period_starts: string;
  period_ends: string;
  gross_cents: number;
  reimbursements_cents: number;
  net_cents: number;
  status: "draft" | "issued" | "paid" | "voided";
  hours: number;
  overtime: number;
  currency: string;
  locale: string;
  jurisdiction: string;
}
