// crewday — JSON API types.
// Shapes mirror the dataclasses in mocks/app/mock_data.py. The FastAPI
// layer serializes via dataclasses.asdict, so dates arrive as ISO-8601
// strings and enums as their literal string values.

export type Role = "employee" | "manager" | "client" | "admin";
export type Theme = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

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
  /** §22 — when set, the property is billed to that organization. */
  client_org_id: string | null;
  /** §22 — owner-of-record (a real human, not just a workspace). */
  owner_user_id: string | null;
}

// §02 — `property_workspace` junction. A property can belong to many
// workspaces; `membership_role` says how the workspace relates to it.
export type MembershipRole =
  | "owner_workspace"
  | "managed_workspace"
  | "observer_workspace";

export interface PropertyWorkspace {
  property_id: string;
  workspace_id: string;
  membership_role: MembershipRole;
  share_guest_identity: boolean;
  invite_id: string | null;
  added_at: string;
  added_by_user_id: string | null;
  added_via: "invite_accept" | "system" | "seed";
}

export type PropertyWorkspaceInviteState =
  | "pending"
  | "accepted"
  | "rejected"
  | "revoked"
  | "expired";

export interface PropertyWorkspaceInvite {
  id: string;
  token: string;
  from_workspace_id: string;
  property_id: string;
  to_workspace_id: string | null;
  proposed_membership_role: "managed_workspace" | "observer_workspace";
  initial_share_settings: { share_guest_identity: boolean };
  state: PropertyWorkspaceInviteState;
  created_by_user_id: string;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  decided_by_user_id: string | null;
  decision_note_md: string | null;
}

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

export type TaskStatus = "scheduled" | "pending" | "in_progress" | "completed" | "skipped" | "cancelled" | "overdue";
export type TaskPriority = "low" | "normal" | "high" | "urgent";
export type PhotoEvidence = "disabled" | "optional" | "required";

export interface ChecklistItem {
  label: string;
  done?: boolean;
  guest_visible?: boolean;
  // §06 "Checklist template shape": per-item RRULE filter.
  // Populated when the item is a materialised instance of a template
  // item that carried an RRULE.
  key?: string;
  required?: boolean;
  rrule?: string | null;
  dtstart_local?: string | null;
}

// §06 Task template checklist shape — authoring-side shape carried on
// task_template.checklist_template_json items. Distinct from the
// runtime per-task row: this is the template rule, including the
// optional RRULE filter.
export interface ChecklistTemplateItem {
  key: string;
  text: string;
  required: boolean;
  guest_visible?: boolean;
  rrule?: string | null;
  dtstart_local?: string | null;
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
  assigned_user_id: string;
  workspace_id: string;
  // §06 "Self-created and personal tasks". Private to creator +
  // workspace owners when `is_personal` (see §15 RLS).
  created_by: string;
  is_personal: boolean;
}

// ── Time / payroll ────────────────────────────────────────────────

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

// §11 — which layer of the gate fired and where its confirmation
// lands. `desk` rows live on /approvals only; `inline_chat` rows
// also render in the user's chat sidebar / PWA chat tab.
export type GateSource =
  | "workspace_always"
  | "workspace_configurable"
  | "user_auto_annotation"
  | "user_strict_mutation";
export type GateDestination = "desk" | "inline_chat";
export type InlineChannel =
  | "desk_only"
  | "web_owner_sidebar"
  | "web_worker_chat"
  | "offapp_whatsapp";

export interface ApprovalRequest {
  id: string;
  agent: string;
  action: string;
  target: string;
  reason: string;
  requested_at: string;
  risk: "low" | "medium" | "high";
  diff: string[];
  gate_source: GateSource;
  gate_destination: GateDestination;
  inline_channel: InlineChannel;
  card_summary: string;
  card_fields: [string, string][];
  for_user_id: string | null;
  resolved_user_mode: AgentApprovalMode | null;
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
  backup_assignee_user_ids: string[];
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
  // §11 "Cost tracking" — chain metadata; nullable on legacy rows.
  assignment_id?: string | null;
  provider_model_id?: string | null;
  prompt_template_id?: string | null;
  prompt_version?: number | null;
  fallback_attempts?: number;
  raw_response_available?: boolean;
}

// §11 — provider / model / provider-model graph shapes.

export type LlmProviderType = "openrouter" | "openai_compatible" | "fake";
export type LlmApiKeyStatus = "present" | "missing" | "rotating";
export type LlmPriceSource = "openrouter" | "manual" | "";
export type LlmPriceSourceOverride = "" | "none" | "openrouter";
export type LlmReasoningEffort = "" | "low" | "medium" | "high";

export interface LlmProvider {
  id: string;
  name: string;
  provider_type: LlmProviderType;
  endpoint: string;
  api_key_ref: string | null;
  api_key_status: LlmApiKeyStatus;
  default_model: string | null;
  requests_per_minute: number;
  timeout_s: number;
  priority: number;
  is_enabled: boolean;
  provider_model_count: number;
}

export interface LlmModel {
  id: string;
  canonical_name: string;
  display_name: string;
  vendor: string;
  capabilities: string[];
  context_window: number | null;
  max_output_tokens: number | null;
  price_source: LlmPriceSource;
  price_source_model_id: string | null;
  is_active: boolean;
  notes: string | null;
  provider_model_count: number;
}

export interface LlmProviderModel {
  id: string;
  provider_id: string;
  model_id: string;
  api_model_id: string;
  input_cost_per_million: number;
  output_cost_per_million: number;
  max_tokens_override: number | null;
  temperature_override: number | null;
  supports_system_prompt: boolean;
  supports_temperature: boolean;
  reasoning_effort: LlmReasoningEffort;
  price_source_override: LlmPriceSourceOverride;
  price_last_synced_at: string | null;
  is_enabled: boolean;
}

export interface LlmAssignment {
  id: string;
  capability: string;
  description: string;
  priority: number;
  provider_model_id: string;
  max_tokens: number | null;
  temperature: number | null;
  extra_api_params: Record<string, unknown>;
  required_capabilities: string[];
  is_enabled: boolean;
  last_used_at: string | null;
  spend_usd_30d: number;
  calls_30d: number;
}

export interface LlmCapabilityEntry {
  key: string;
  description: string;
  required_capabilities: string[];
}

export interface LlmCapabilityInheritance {
  capability: string;
  inherits_from: string;
}

export interface LlmAssignmentIssue {
  assignment_id: string;
  capability: string;
  missing_capabilities: string[];
}

export interface LlmPromptTemplate {
  id: string;
  capability: string;
  name: string;
  version: number;
  is_active: boolean;
  is_customised: boolean;
  default_hash: string;
  updated_at: string;
  revisions_count: number;
  preview: string;
}

export interface LlmGraphPayload {
  providers: LlmProvider[];
  models: LlmModel[];
  provider_models: LlmProviderModel[];
  capabilities: LlmCapabilityEntry[];
  inheritance: LlmCapabilityInheritance[];
  assignments: LlmAssignment[];
  assignment_issues: LlmAssignmentIssue[];
  totals: {
    spend_usd_30d: number;
    calls_30d: number;
    provider_count: number;
    model_count: number;
    capability_count: number;
    unassigned_capabilities: string[];
  };
}

export interface LlmSyncPricingResult {
  started_at: string;
  deltas: {
    provider_model_id: string;
    api_model_id: string;
    input_before: number;
    input_after: number;
    output_before: number;
    output_after: number;
    status: "updated" | "unchanged" | "pinned" | "error";
  }[];
  updated: number;
  skipped: number;
  errors: number;
}

// §11 — Workspace usage budget (manager-visible shape).
// Deliberately percent-only: no dollars, no tokens, no reset date.
// Dollars live on the LLM settings page for the operator audience;
// workers and managers only see the envelope usage here.
export interface WorkspaceUsage {
  percent: number;
  paused: boolean;
  window_label: string;
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
  actor_grant_role: "manager" | "worker" | "client" | "guest" | "admin" | null;
  actor_was_owner_member: boolean | null;
  actor_action_key: string | null;
  actor_id: string | null;
  agent_label: string | null;
}

// ── Permission model (§02, §05) ───────────────────────────────────

export type ScopeKind = "workspace" | "property" | "organization" | "deployment";
export type GroupScopeKind = "workspace" | "organization" | "deployment";
export type RuleEffect = "allow" | "deny";
export type GrantRole = "manager" | "worker" | "client" | "guest" | "admin";

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

// §02 — workspaces the current user has access to, with the
// highest-privilege grant role they hold there. Returned by /me so
// the workspace switcher can render without a second call.
export interface AvailableWorkspace {
  workspace: Workspace;
  grant_role: GrantRole | null;
  binding_org_id: string | null;
  source: "workspace_grant" | "property_grant" | "org_grant" | "work_engagement";
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

// §03 API tokens — three kinds. The wire shape is a single type
// because the list endpoint mixes them (for managers) and the /me
// endpoint filters to `personal` only.
export type ApiTokenKind = "scoped" | "delegated" | "personal";

export interface ApiToken {
  id: string;
  name: string;
  kind: ApiTokenKind;
  /** `mip_<key_id>` — the public half of the token. Full secret
   *  only returned once at creation time via `ApiTokenCreated`. */
  prefix: string;
  /** Scopes requested. Empty for delegated tokens. */
  scopes: string[];
  /** Creator for scoped; subject for personal; delegator for
   *  delegated. Same column, populated from the session. */
  created_by_user_id: string;
  created_by_display: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  /** Truncated to /24 (v4) or /64 (v6) per §15. */
  last_used_ip: string | null;
  last_used_path: string | null;
  revoked_at: string | null;
  note: string | null;
  ip_allowlist: string[];
}

export interface ApiTokenCreated {
  token: ApiToken;
  /** The `mip_<key_id>_<secret>` plaintext. Shown once. */
  plaintext: string;
  /** Example curl for the first scope granted. */
  curl_example: string;
}

export interface ApiTokenAuditEntry {
  at: string;
  method: string;
  path: string;
  status: number;
  ip: string;
  user_agent: string;
  correlation_id: string;
}

export type ChatChannelKind = "offapp_whatsapp" | "offapp_telegram";

export interface AgentMessage {
  at: string;
  kind: "agent" | "user" | "action";
  body: string;
  /** §23 chat gateway — channel the turn traversed; null/undefined = web. */
  channel_kind?: ChatChannelKind | null;
}

export interface ChatChannelBinding {
  id: string;
  user_id: string;
  user_display_name: string;
  channel_kind: ChatChannelKind;
  address: string;
  display_label: string;
  state: "pending" | "active" | "revoked";
  verified_at: string | null;
  last_message_at: string | null;
  revoked_at: string | null;
  revoke_reason: "user" | "stop_keyword" | "user_archived" | "admin" | "provider_error" | null;
}

export interface ChatGatewayProvider {
  channel_kind: ChatChannelKind;
  provider: string;
  status: "connected" | "pending" | "error" | "not_configured";
  display_stub: string;
  last_webhook_at: string | null;
  templates: string[];
}

export interface AgentAction {
  id: string;
  title: string;
  detail: string;
  risk: "low" | "medium" | "high";
  card_summary: string;
  card_fields: [string, string][];
  gate_source: GateSource;
  inline_channel: "web_owner_sidebar" | "web_worker_chat" | "web_admin_sidebar";
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


// §11 — per-user setting governing when the user's embedded chat
// agent pauses for an inline confirmation card before executing.
export type AgentApprovalMode = "bypass" | "auto" | "strict";

// §11 — Agent preferences. Free-form Markdown stacked into the LLM
// system prompt; three layers (workspace / property / user).
export type AgentPreferenceScope = "workspace" | "property" | "user";

export interface AgentPreference {
  scope_kind: AgentPreferenceScope;
  scope_id: string;
  body_md: string;
  token_count: number;
  updated_by_user_id: string | null;
  updated_at: string | null;
  writable: boolean;
  soft_cap: number;
  hard_cap: number;
}

export interface AgentPreferenceRevision {
  revision_number: number;
  body_md: string;
  saved_by_user_id: string;
  saved_at: string;
  save_note: string | null;
}

export interface AgentPreferenceRevisionsPayload {
  scope_kind: AgentPreferenceScope;
  scope_id: string;
  revisions: AgentPreferenceRevision[];
}

export interface Me {
  role: Role;
  theme: Theme;
  agent_sidebar_collapsed: boolean;
  employee: Employee;
  manager_name: string;
  today: string;
  now: string;
  user_id: string | null;
  agent_approval_mode: AgentApprovalMode;
  /** §02 — active workspace context for the current request. */
  current_workspace_id: string;
  /** §02 — workspaces the user can switch into. */
  available_workspaces: AvailableWorkspace[];
  /** §22 — when the active grant on `current_workspace_id` is a
   *  client grant, the org(s) the user is bound to. Drives the
   *  client portal's "billed to me" filter. */
  client_binding_org_ids: string[];
  /** §05 — true iff the caller holds any active role_grants row with
   *  scope_kind='deployment'. Gates the "Administration" link in the
   *  manager nav and the 404 on /admin/api/v1/* for non-admins. */
  is_deployment_admin: boolean;
  /** §11 — convenience flag: true iff the caller is in owners@deployment. */
  is_deployment_owner: boolean;
}

// §14 — /admin shell.

export interface AdminMe {
  user_id: string;
  display_name: string;
  email: string;
  is_owner: boolean;
  capabilities: Record<string, boolean>;
}

export interface AdminWorkspaceRow {
  id: string;
  slug: string;
  name: string;
  plan: "free" | "pro" | "trial";
  verification_state:
    | "unverified"
    | "email_verified"
    | "human_verified"
    | "trusted";
  properties_count: number;
  members_count: number;
  cap_usd_30d: number;
  spent_usd_30d: number;
  usage_percent: number;
  paused: boolean;
  archived_at: string | null;
  created_at: string;
}

export interface AdminUsageSummary {
  window_label: string;
  deployment_spend_usd_30d: number;
  deployment_call_count_30d: number;
  workspace_count: number;
  paused_workspaces: number;
  per_capability: { capability: string; spend_usd_30d: number; calls_30d: number }[];
}

export interface AdminChatProviderCredential {
  field: string;
  label: string;
  display_stub: string;
  set: boolean;
  updated_at: string | null;
  updated_by: string | null;
}

export interface AdminChatProviderTemplate {
  name: string;
  purpose: string;
  status: "approved" | "pending" | "rejected" | "paused";
  last_sync_at: string | null;
  rejection_reason: string | null;
}

export interface AdminChatProvider {
  channel_kind: "offapp_whatsapp" | "offapp_telegram";
  label: string;
  phone_display: string;
  status: "connected" | "error" | "not_configured";
  last_webhook_at: string | null;
  last_webhook_error: string | null;
  webhook_url: string;
  verify_token_stub: string;
  credentials: AdminChatProviderCredential[];
  templates: AdminChatProviderTemplate[];
  per_workspace_soft_cap: number;
  daily_outbound_cap: number;
  outbound_24h: number;
  delivery_error_rate_pct: number;
}

export interface AdminChatOverrideRow {
  workspace_id: string;
  workspace_name: string;
  channel_kind: "offapp_whatsapp" | "offapp_telegram";
  phone_display: string;
  status: "connected" | "error" | "not_configured";
  created_at: string;
  reason: string | null;
}

export interface AdminSignupSettings {
  enabled: boolean;
  disposable_domains_count: number;
  throttle_per_ip_hour: number;
  throttle_per_email_lifetime: number;
  pre_verified_upload_mb_cap: number;
  pre_verified_llm_percent_cap: number;
  updated_at: string;
  updated_by: string;
}

export interface AdminDeploymentSetting {
  key: string;
  value: string | number | boolean;
  kind: "bool" | "int" | "string";
  description: string;
  root_only: boolean;
  updated_at: string;
  updated_by: string;
}

export interface AdminTeamMember {
  id: string;
  user_id: string;
  display_name: string;
  email: string;
  is_owner: boolean;
  granted_at: string;
  granted_by: string;
}

export interface HistoryPayload {
  tab: "tasks" | "chats" | "expenses" | "leaves";
  tasks: Task[];
  expenses: Expense[];
  leaves: Leave[];
  chats: { id: string; title: string; last_at: string; summary: string }[];
}

export interface DashboardPayload {
  on_booking: Employee[];
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
  icon_name: string;
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

export type FileExtractionStatus =
  | "pending"
  | "extracting"
  | "succeeded"
  | "failed"
  | "unsupported"
  | "empty";

export type FileExtractor =
  | "pypdf"
  | "pdfminer"
  | "python_docx"
  | "openpyxl"
  | "tesseract"
  | "llm_vision"
  | "passthrough";

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
  extraction_status: FileExtractionStatus;
  extracted_at: string | null;
}

export interface DocumentExtraction {
  document_id: string;
  status: FileExtractionStatus;
  extractor: FileExtractor | null;
  body_preview: string;
  page_count: number;
  token_count: number;
  has_secret_marker: boolean;
  last_error: string | null;
  extracted_at: string | null;
}

export interface KbHit {
  kind: "instruction" | "document";
  id: string;
  title: string;
  snippet: string;
  score: number;
  why: string;
}

export interface KbSearchResponse {
  results: KbHit[];
  total: number;
}

export interface KbDocPayload {
  kind: "instruction" | "document";
  id: string;
  title?: string;
  body?: string;
  page?: number;
  page_count?: number;
  more_pages?: boolean;
  source_ref?: Record<string, string | null>;
  extraction_status?: FileExtractionStatus;
  hint?: string;
}

export interface AgentDocSummary {
  slug: string;
  title: string;
  summary: string;
  roles: string[];
  updated_at: string;
}

export interface AgentDoc extends AgentDocSummary {
  body_md: string;
  capabilities: string[];
  version: number;
  is_customised: boolean;
  default_hash: string;
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
export type AgentTurnScope = "employee" | "manager" | "admin" | "task";

export type SseEvent =
  | { event: "tick"; data: { now: string } }
  | {
      event: "agent.message.appended";
      data: {
        scope: "employee" | "manager" | "admin" | "task";
        /** Present when `scope === "task"`; identifies which task the message belongs to. */
        task_id?: string;
        message: AgentMessage;
      };
    }
  | {
      // §11 "Agent turn lifecycle" — bracket the server-side agent turn
      // so clients can render the typing indicator (§14).
      event: "agent.turn.started";
      data: {
        scope: AgentTurnScope;
        task_id?: string;
        started_at: string;
      };
    }
  | {
      event: "agent.turn.finished";
      data: {
        scope: AgentTurnScope;
        task_id?: string;
        finished_at: string;
        outcome: "replied" | "action" | "error" | "timeout";
      };
    }
  | { event: "task.updated"; data: { task: Task } }
  | { event: "task.completed"; data: { task: Task } }
  | { event: "task.skipped"; data: { task: Task; reason: string | null } }
  | { event: "approval.decided"; data: { id: string; decision: "approve" | "reject" } }
  | { event: "expense.approved"; data: { id: string; status: ExpenseStatus } }
  | { event: "expense.rejected"; data: { id: string; status: ExpenseStatus } }
  | { event: "expense.reimbursed"; data: { id: string; status: ExpenseStatus } }
  | { event: "asset_action.performed"; data: { asset_id: string; action: AssetAction } }
  | { event: "schedule_ruleset.upserted"; data: { ruleset: ScheduleRuleset } }
  | { event: "schedule_ruleset.deleted"; data: { id: string } }
  // §09 booking lifecycle — every mutation from any tab (including
  // another manager approving an amend) fans out to every connected
  // client so `/schedule` and `/dashboard` stay coherent without a
  // poll. One dispatch case invalidates the shared roots.
  | { event: "booking.created"; data: { booking: Booking } }
  | { event: "booking.amended"; data: { booking: Booking } }
  | { event: "booking.declined"; data: { booking: Booking } }
  | { event: "booking.approved"; data: { booking: Booking } }
  | { event: "booking.rejected"; data: { booking: Booking } }
  | { event: "booking.cancelled"; data: { booking: Booking } }
  | { event: "booking.reassigned"; data: { booking: Booking } };

// §06 — per-property recurring rota (Schedule ruleset).
export interface ScheduleRuleset {
  id: string;
  workspace_id: string;
  name: string;
}

export interface ScheduleRulesetSlot {
  id: string;
  schedule_ruleset_id: string;
  weekday: number; // 0..6 (Mon..Sun, ISO)
  starts_local: string; // "HH:MM"
  ends_local: string;
}

export interface ScheduleAssignment {
  id: string;
  user_id: string | null;
  work_role_id: string | null;
  property_id: string;
  schedule_ruleset_id: string | null;
}

export interface SchedulerUserView {
  id: string;
  first_name: string;
  display_name?: string;
}

export interface SchedulerTaskView {
  id: string;
  title: string;
  property_id: string;
  user_id: string;
  scheduled_start: string;
  estimated_minutes: number;
  priority: TaskPriority;
  status: TaskStatus;
}

export interface SchedulerCalendarPayload {
  window: { from: string; to: string };
  rulesets: ScheduleRuleset[];
  slots: ScheduleRulesetSlot[];
  assignments: ScheduleAssignment[];
  tasks: SchedulerTaskView[];
  users: SchedulerUserView[];
  properties: { id: string; name: string; timezone: string }[];
}

// §06 — per-date override of the user's weekly availability.
export type AvailabilityOverrideCategory =
  | "off"
  | "custom_hours"
  | "extend"
  | "extra_day";

export interface AvailabilityOverride {
  id: string;
  user_id: string;
  workspace_id: string;
  date: string; // ISO date
  available: boolean;
  starts_local: string | null; // "HH:MM" when available with custom hours
  ends_local: string | null;
  reason: string | null;
  approval_required: boolean;
  approved_at: string | null;
  approved_by: string | null;
  created_at: string;
}

export interface SelfWeeklyAvailabilitySlot {
  weekday: number; // 0..6 ISO
  starts_local: string | null;
  ends_local: string | null;
}

// §12 GET /api/v1/me/schedule — self-only calendar feed for /schedule (§14).
export interface MySchedulePayload {
  window: { from: string; to: string };
  user_id: string;
  weekly_availability: SelfWeeklyAvailabilitySlot[];
  rulesets: ScheduleRuleset[];
  slots: ScheduleRulesetSlot[];
  assignments: ScheduleAssignment[];
  tasks: SchedulerTaskView[];
  properties: { id: string; name: string; timezone: string }[];
  leaves: Leave[];
  overrides: AvailabilityOverride[];
  // §09 — worker's booking rows covering the window, any status. The
  // day drawer renders them inline with amend / decline actions; the
  // page header surfaces a banner counting pending_approval +
  // pending_amend rows.
  bookings: Booking[];
}
