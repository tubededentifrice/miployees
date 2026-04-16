// miployees — JSON API types.
// Shapes mirror the dataclasses in mocks/app/mock_data.py. The FastAPI
// layer serializes via dataclasses.asdict, so dates arrive as ISO-8601
// strings and enums as their literal string values.

export type Role = "employee" | "manager";
export type Theme = "light" | "dark";

export type PropertyColor = "moss" | "sky" | "rust";
export type PropertyKind = "str" | "vacation" | "residence" | "mixed";

export interface Property {
  id: string;
  name: string;
  city: string;
  timezone: string;
  color: PropertyColor;
  kind: PropertyKind;
  areas: string[];
  evidence_policy: "inherit" | "require" | "optional" | "forbid";
  country: string;
  locale: string;
}

export interface Employee {
  id: string;
  name: string;
  roles: string[];
  properties: string[];
  avatar_initials: string;
  phone: string;
  email: string;
  started_on: string;
  clocked_in_at: string | null;
  capabilities: Record<string, boolean | null>;
  workspaces: string[];
  villas: string[];
  clock_mode: "manual" | "auto" | "disabled";
  auto_clock_idle_minutes: number;
  language: string;
  weekly_availability: Record<string, [string, string] | null>;
  evidence_policy: "inherit" | "require" | "optional" | "forbid";
  preferred_locale: string | null;
}

export type TaskStatus = "pending" | "in_progress" | "completed" | "skipped";
export type TaskPriority = "low" | "normal" | "high" | "urgent";
export type PhotoEvidence = "disabled" | "optional" | "required";

export interface ChecklistItem {
  label: string;
  done?: boolean;
  guest_visible?: boolean;
}

export interface Task {
  id: string;
  title: string;
  property_id: string;
  area: string;
  assignee_id: string;
  scheduled_start: string;
  estimated_minutes: number;
  priority: TaskPriority;
  status: TaskStatus;
  checklist: ChecklistItem[];
  photo_evidence: PhotoEvidence;
  evidence_policy: "inherit" | "require" | "optional" | "forbid";
  instructions_ids: string[];
  template_id: string | null;
  schedule_id: string | null;
  turnover_bundle_id: string | null;
}

export type ExpenseStatus = "pending" | "approved" | "rejected" | "reimbursed";

export interface Expense {
  id: string;
  employee_id: string;
  amount_cents: number;
  currency: string;
  merchant: string;
  submitted_at: string;
  status: ExpenseStatus;
  note: string;
  ocr_confidence: number | null;
}

export interface Stay {
  id: string;
  property_id: string;
  guest: string;
  source: "Airbnb" | "VRBO" | "Booking.com" | "Direct";
  check_in: string;
  check_out: string;
  guests: number;
  status: "booked" | "in_house" | "checked_out" | "cancelled";
}

export interface ApprovalRequest {
  id: string;
  agent: string;
  action: string;
  target: string;
  reason: string;
  requested_at: string;
  risk: "low" | "medium" | "high";
  diff: string[];
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

export interface PropertyClosure {
  id: string;
  property_id: string;
  starts_on: string;
  ends_on: string;
  reason: "renovation" | "owner_stay" | "seasonal" | "ical_unavailable" | "other";
  note: string;
}

export interface TaskTemplate {
  id: string;
  name: string;
  description: string;
  role: string;
  duration_minutes: number;
  property_scope: "any" | "one" | "listed";
  photo_evidence: PhotoEvidence;
  priority: TaskPriority;
  checklist: ChecklistItem[];
}

export interface Schedule {
  id: string;
  name: string;
  template_id: string;
  property_id: string;
  rrule_human: string;
  default_assignee_id: string | null;
  duration_minutes: number;
  active_from: string;
  paused: boolean;
}

export interface Instruction {
  id: string;
  title: string;
  scope: "global" | "property" | "area";
  property_id: string | null;
  area: string | null;
  tags: string[];
  body_md: string;
  version: number;
  updated_at: string;
}

export interface InventoryItem {
  id: string;
  property_id: string;
  name: string;
  sku: string;
  on_hand: number;
  par: number;
  unit: string;
  area: string;
}

export interface Issue {
  id: string;
  reported_by: string;
  property_id: string;
  area: string;
  severity: "low" | "medium" | "high";
  category: "damage" | "broken" | "supplies" | "safety" | "other";
  title: string;
  body: string;
  reported_at: string;
  status: "open" | "in_progress" | "resolved";
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

export interface ModelAssignment {
  capability: string;
  description: string;
  provider: string;
  model_id: string;
  enabled: boolean;
  daily_budget_usd: number;
  spent_24h_usd: number;
  calls_24h: number;
}

export interface LLMCall {
  at: string;
  capability: string;
  model_id: string;
  input_tokens: number;
  output_tokens: number;
  cost_cents: number;
  latency_ms: number;
  status: "ok" | "error" | "redacted_block";
}

export interface AuditEntry {
  at: string;
  actor_kind: "human" | "agent" | "system";
  actor: string;
  action: string;
  target: string;
  via: "web" | "api" | "cli" | "system";
  reason: string | null;
}

export interface Webhook {
  id: string;
  url: string;
  events: string[];
  active: boolean;
  last_delivery_status: number;
  last_delivery_at: string;
}

export interface AgentMessage {
  at: string;
  kind: "agent" | "user" | "action";
  body: string;
}

export interface AgentAction {
  id: string;
  title: string;
  detail: string;
  risk: "low" | "medium" | "high";
}

export interface HouseholdSettings {
  name: string;
  timezone: string;
  currency: string;
  week_start: string;
  pay_frequency: string;
  default_photo_evidence: PhotoEvidence;
  geofence_radius_m: number;
  retention_days: Record<string, number>;
  approvals: { always_gated: string[]; configurable: string[] };
  danger_zone: string[];
  country: string;
  default_locale: string;
}

export interface Me {
  role: Role;
  theme: Theme;
  agent_sidebar_collapsed: boolean;
  employee: Employee;
  manager_name: string;
  today: string;
  now: string;
}

export interface HistoryPayload {
  tab: "tasks" | "chats" | "expenses" | "leaves";
  tasks: Task[];
  expenses: Expense[];
  leaves: Leave[];
  chats: { id: string; title: string; last_at: string; summary: string }[];
}

export interface DashboardPayload {
  on_shift: Employee[];
  by_status: { completed: Task[]; in_progress: Task[]; pending: Task[] };
  pending_approvals: ApprovalRequest[];
  pending_expenses: Expense[];
  pending_leaves: Leave[];
  open_issues: Issue[];
  stays_today: Stay[];
}

// SSE event shapes. The server emits JSON-serialised payloads under
// different event names; the SseContext dispatches on `event` and
// feeds `data` into TanStack Query invalidations.
export type SseEvent =
  | { event: "tick"; data: { now: string } }
  | { event: "agent.message.appended"; data: { scope: "employee" | "manager"; message: AgentMessage } }
  | { event: "task.updated"; data: { task: Task } }
  | { event: "approval.resolved"; data: { id: string; decision: "approve" | "reject" } }
  | { event: "expense.decided"; data: { id: string; status: ExpenseStatus } };
