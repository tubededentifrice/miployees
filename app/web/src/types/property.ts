// crewday — JSON API types: properties, workspace membership, stays.

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
  /**
   * §05 per-property settings cascade override blob. Always a real
   * mapping for owner / manager callers; cd-yjw5 masks it to `{}`
   * for workers — the empty mapping is the SPA's "inherit workspace
   * default" signal, so worker UIs must keep treating an empty
   * `settings_override` as "no override" rather than synthesising
   * one from a missing field.
   */
  settings_override: Record<string, unknown>;
  /**
   * §22 — when set, the property is billed to that organization.
   * cd-yjw5 masks this to `null` for workers (it's a manager-only
   * governance field). The type already permits `null` for the
   * pre-§22 rows that legitimately have no client_org pinned, so
   * the masking is a value change only — no type narrowing needed.
   */
  client_org_id: string | null;
  /**
   * §22 — owner-of-record (a real human, not just a workspace).
   * cd-yjw5 masks this to `null` for workers for the same reason
   * as `client_org_id` above.
   */
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

export interface PropertyClosure {
  id: string;
  property_id: string;
  starts_on: string;
  ends_on: string;
  reason: "renovation" | "owner_stay" | "seasonal" | "ical_unavailable" | "other";
  note: string;
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
