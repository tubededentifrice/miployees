// crewday — JSON API types: core primitives.
// Shapes mirror the dataclasses in mocks/app/mock_data.py. The FastAPI
// layer serializes via dataclasses.asdict, so dates arrive as ISO-8601
// strings and enums as their literal string values.

export type Role = "employee" | "manager" | "client" | "admin";
export type Theme = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

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

export interface Webhook {
  id: string;
  url: string;
  events: string[];
  active: boolean;
  last_delivery_status: number;
  last_delivery_at: string;
}
