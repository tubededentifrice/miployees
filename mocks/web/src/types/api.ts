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
  settings_override: Record<string, unknown>;
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
  settings_override: Record<string, unknown>;
}

export type TaskStatus = "scheduled" | "pending" | "in_progress" | "completed" | "skipped" | "cancelled" | "overdue";
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
  asset_id: string | null;
  settings_override: Record<string, unknown>;
}

// ── Time / payroll ────────────────────────────────────────────────

export type ShiftStatus = "open" | "closed" | "disputed";

export interface Shift {
  id: string;
  employee_id: string;
  property_id: string;
  started_at: string;
  ended_at: string | null;
  status: ShiftStatus;
  duration_seconds: number | null;
  break_seconds: number;
  method_in: "manual" | "auto" | "geo";
  method_out: "manual" | "auto" | "geo" | null;
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

// ── Inventory movement ────────────────────────────────────────────

export type InventoryMovementReason = "restock" | "consume" | "adjust" | "waste" | "transfer_in" | "transfer_out" | "audit_correction";

export interface InventoryMovement {
  id: string;
  item_id: string;
  delta: number;
  reason: InventoryMovementReason;
  // v1 collapses manager|employee|agent|system to user|agent|system (§02).
  actor_kind: "user" | "agent" | "system";
  actor_id: string;
  note: string | null;
  occurred_at: string;
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

// ── Task comments ─────────────────────────────────────────────────

export interface TaskComment {
  id: string;
  task_id: string;
  // v1 collapses author_kind to user|agent|system (§02).
  author_kind: "user" | "agent" | "system";
  author_id: string;
  body_md: string;
  created_at: string;
}

// ── Expenses ──────────────────────────────────────────────────────

export type ExpenseStatus = "draft" | "submitted" | "approved" | "rejected" | "reimbursed";
export type ExpenseCategory = "supplies" | "fuel" | "food" | "transport" | "maintenance" | "other";

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
  category: ExpenseCategory | null;
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

export type StayStatus = "tentative" | "confirmed" | "in_house" | "checked_out" | "cancelled";

export interface Stay {
  id: string;
  property_id: string;
  guest_name: string;
  source: "manual" | "airbnb" | "vrbo" | "booking" | "google_calendar" | "ical";
  check_in: string;
  check_out: string;
  guests: number;
  status: StayStatus;
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
  severity: "low" | "normal" | "high" | "urgent";
  category: "damage" | "broken" | "supplies" | "safety" | "other";
  title: string;
  body: string;
  reported_at: string;
  status: "open" | "in_progress" | "resolved" | "wont_fix";
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
  // v1 collapses to user|agent|system; the surface grant under
  // which a user acted lives in actor_grant_role (§02). The
  // separate actor_was_owner_member bit captures whether the
  // actor held ``owners`` permission-group membership at the
  // time — so reviewers can tell governance actions apart from
  // ordinary administration.
  actor_kind: "user" | "agent" | "system";
  actor: string;
  action: string;
  target: string;
  via: "web" | "api" | "cli" | "worker";
  reason: string | null;
  actor_grant_role: "manager" | "worker" | "client" | "guest" | null;
  actor_was_owner_member: boolean | null;
  actor_action_key: string | null;
  actor_id: string | null;
  agent_label: string | null;
}

// ── Permission model (§02, §05) ───────────────────────────────────

export type ScopeKind = "workspace" | "property" | "organization";
export type GroupScopeKind = "workspace" | "organization";
export type RuleEffect = "allow" | "deny";
export type GrantRole = "manager" | "worker" | "client" | "guest";

export interface User {
  id: string;
  email: string;
  display_name: string;
  timezone: string;
  languages: string[];
  preferred_locale: string | null;
  avatar_file_id: string | null;
  primary_workspace_id: string | null;
  phone_e164: string | null;
  notes_md: string;
  archived_at: string | null;
}

export interface Workspace {
  id: string;
  name: string;
  timezone: string;
  default_currency: string;
  default_country: string;
  default_locale: string;
}

export interface RoleGrant {
  id: string;
  user_id: string;
  scope_kind: ScopeKind;
  scope_id: string;
  grant_role: GrantRole;
  binding_org_id: string | null;
  started_on: string | null;
  ended_on: string | null;
  granted_by_user_id: string | null;
  revoked_at: string | null;
  revoke_reason: string | null;
}

export interface PermissionGroup {
  id: string;
  scope_kind: GroupScopeKind;
  scope_id: string;
  key: string;
  name: string;
  description_md: string;
  group_kind: "system" | "user";
  is_derived: boolean;
  deleted_at: string | null;
}

export interface PermissionGroupMember {
  group_id: string;
  user_id: string;
  added_by_user_id: string | null;
  added_at: string | null;
  revoked_at: string | null;
}

export interface PermissionGroupMembersResponse {
  group_id: string;
  is_derived: boolean;
  members: { user_id: string; derived: boolean }[];
}

export interface PermissionRule {
  id: string;
  scope_kind: ScopeKind;
  scope_id: string;
  action_key: string;
  subject_kind: "user" | "group";
  subject_id: string;
  effect: RuleEffect;
  created_by_user_id: string | null;
  created_at: string | null;
  revoked_at: string | null;
  revoke_reason: string | null;
}

export interface ActionCatalogEntry {
  key: string;
  description: string;
  valid_scope_kinds: ScopeKind[];
  default_allow: string[];
  root_only: boolean;
  root_protected_deny: boolean;
  spec: string;
}

export interface ResolvedPermission {
  effect: RuleEffect;
  source_layer: string;
  source_rule_id: string | null;
  matched_groups: string[];
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

export interface WorkspaceSettings {
  meta: {
    name: string;
    timezone: string;
    currency: string;
    country: string;
    default_locale: string;
  };
  defaults: Record<string, unknown>;
  policy: {
    approvals: { always_gated: string[]; configurable: string[] };
    danger_zone: string[];
  };
}

export interface SettingDefinition {
  key: string;
  label: string;
  type: "enum" | "int" | "bool";
  catalog_default: unknown;
  enum_values: string[] | null;
  override_scope: string;
  description: string;
  spec: string;
}

export interface ResolvedSetting {
  value: unknown;
  source: "workspace" | "property" | "employee" | "task" | "catalog";
}

export interface ResolvedSettingsPayload {
  entity_kind: string;
  entity_id: string;
  settings: Record<string, ResolvedSetting>;
}

export interface EntitySettingsPayload {
  overrides: Record<string, unknown>;
  resolved: Record<string, ResolvedSetting>;
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
  properties: Property[];
  employees: Employee[];
}

// ── Asset types ────────────────────────────────────────────────────

export type AssetCondition = "new" | "good" | "fair" | "poor" | "needs_replacement";
export type AssetStatus = "active" | "in_repair" | "decommissioned" | "disposed";
export type AssetCategory = "climate" | "appliance" | "plumbing" | "pool" | "heating" | "outdoor" | "safety" | "security" | "vehicle" | "other";
export type DocumentKind = "manual" | "warranty" | "invoice" | "receipt" | "photo" | "certificate" | "contract" | "permit" | "insurance" | "other";

export interface AssetType {
  id: string;
  key: string;
  name: string;
  category: AssetCategory;
  icon: string;
  default_actions: {
    key: string;
    label: string;
    interval_days?: number;
    estimated_duration_minutes?: number;
  }[];
  default_lifespan_years: number | null;
}

export interface Asset {
  id: string;
  property_id: string;
  asset_type_id: string | null;
  name: string;
  area: string | null;
  condition: AssetCondition;
  status: AssetStatus;
  make: string | null;
  model: string | null;
  serial_number: string | null;
  installed_on: string | null;
  purchased_on: string | null;
  purchase_price_cents: number | null;
  purchase_currency: string | null;
  purchase_vendor: string | null;
  warranty_expires_on: string | null;
  expected_lifespan_years: number | null;
  guest_visible: boolean;
  guest_instructions: string | null;
  notes: string | null;
  qr_token: string;
}

export interface AssetAction {
  id: string;
  asset_id: string;
  key: string | null;
  label: string;
  interval_days: number | null;
  last_performed_at: string | null;
  next_due_on: string | null;
  linked_task_id: string | null;
  linked_schedule_id: string | null;
  description: string | null;
  estimated_duration_minutes: number | null;
}

export interface AssetDocument {
  id: string;
  asset_id: string | null;
  property_id: string;
  kind: DocumentKind;
  title: string;
  filename: string;
  size_kb: number;
  uploaded_at: string;
  expires_on: string | null;
  amount_cents: number | null;
  amount_currency: string | null;
}

export interface AssetDetailPayload {
  asset: Asset;
  asset_type: AssetType | null;
  property: Property;
  actions: AssetAction[];
  documents: AssetDocument[];
  linked_tasks: Task[];
}

// SSE event shapes. The server emits JSON-serialised payloads under
// different event names; the SseContext dispatches on `event` and
// feeds `data` into TanStack Query invalidations.
export type SseEvent =
  | { event: "tick"; data: { now: string } }
  | { event: "agent.message.appended"; data: { scope: "employee" | "manager"; message: AgentMessage } }
  | { event: "task.updated"; data: { task: Task } }
  | { event: "task.completed"; data: { task: Task } }
  | { event: "task.skipped"; data: { task: Task; reason: string | null } }
  | { event: "approval.decided"; data: { id: string; decision: "approve" | "reject" } }
  | { event: "expense.approved"; data: { id: string; status: ExpenseStatus } }
  | { event: "expense.rejected"; data: { id: string; status: ExpenseStatus } }
  | { event: "expense.reimbursed"; data: { id: string; status: ExpenseStatus } }
  | { event: "asset_action.performed"; data: { asset_id: string; action: AssetAction } };
