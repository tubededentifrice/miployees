// crewday — JSON API types: permission model (§02, §05) and API
// tokens (§03). Kept together because role grants, permission rules,
// and API tokens all turn on the same ScopeKind / GrantRole taxonomy.

import type { Workspace } from "./core";

// ── Permission model (§02, §05) ───────────────────────────────────

export type ScopeKind = "workspace" | "property" | "organization" | "deployment";
export type GroupScopeKind = "workspace" | "organization" | "deployment";
export type RuleEffect = "allow" | "deny";
export type GrantRole = "manager" | "worker" | "client" | "guest" | "admin";

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
