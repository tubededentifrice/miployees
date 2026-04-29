// crewday — JSON API types: §14 /admin shell. Deployment-level
// rows for workspaces, usage rollups, chat providers, deployment
// settings, and owner team membership.

export interface AdminMe {
  user_id: string;
  display_name: string;
  email: string;
  is_owner: boolean;
  capabilities: Record<string, boolean>;
}

export type AdminWorkspaceVerificationState =
  | "trusted"
  | "human_verified"
  | "email_verified"
  | "unverified"
  | "archived";

export interface AdminWorkspaceRow {
  id: string;
  slug: string;
  name: string;
  plan: string;
  verification_state: AdminWorkspaceVerificationState;
  properties_count: number;
  members_count: number;
  spent_cents_30d: number;
  cap_cents_30d: number;
  archived_at: string | null;
  created_at: string;
}

export interface AdminWorkspacesResponse {
  workspaces: AdminWorkspaceRow[];
}

export interface AdminUsageWorkspaceRow {
  workspace_id: string;
  slug: string;
  name: string;
  cap_cents_30d: number;
  spent_cents_30d: number;
  percent: number;
  paused: boolean;
}

export interface AdminUsageWorkspacesResponse {
  workspaces: AdminUsageWorkspaceRow[];
}

export interface AdminUsageSummary {
  window_label: string;
  deployment_spend_cents_30d: number;
  deployment_calls_30d: number;
  workspace_count: number;
  paused_workspace_count: number;
  per_capability: { capability: string; spend_cents_30d: number; calls_30d: number }[];
}

export interface AdminAuditEntry {
  id: string;
  actor_id: string;
  actor_kind: "user" | "agent" | "system";
  actor_grant_role: string;
  actor_was_owner_member: boolean;
  entity_kind: string;
  entity_id: string;
  action: string;
  diff: JsonValue;
  correlation_id: string;
  created_at: string;
}

export interface AdminAuditListResponse {
  data: AdminAuditEntry[];
  next_cursor: string | null;
  has_more: boolean;
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
  signup_enabled: boolean;
  signup_throttle_overrides: Record<string, number>;
  signup_disposable_domains_path: string;
}

export interface AdminDeploymentSettingsResponse {
  settings: AdminDeploymentSetting[];
}

export interface AdminDeploymentSetting {
  key: string;
  value: string | number | boolean | JsonValue;
  kind: "bool" | "int" | "string" | "json";
  description: string;
  root_only: boolean;
  updated_at: string;
  updated_by: string;
}

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface AdminTeamMember {
  id: string;
  user_id: string;
  display_name: string;
  email: string;
  is_owner: boolean;
  granted_at: string;
  granted_by: string;
}
