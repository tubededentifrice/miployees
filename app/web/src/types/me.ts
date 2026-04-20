// crewday — JSON API type: /me envelope returned by GET /api/v1/me.

import type { Role, Theme } from "./core";
import type { Employee } from "./employee";
import type { AgentApprovalMode } from "./approval";
import type { AvailableWorkspace } from "./auth";

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
