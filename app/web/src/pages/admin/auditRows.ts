import type { AdminAuditEntry, AuditEntry } from "@/types/api";

export function displayAuditRow(row: AdminAuditEntry): AuditEntry {
  return {
    at: row.created_at,
    actor_kind: row.actor_kind,
    actor: row.actor_id,
    action: row.action,
    target: row.entity_kind + ":" + row.entity_id,
    via: "api",
    reason: null,
    actor_grant_role: row.actor_grant_role as AuditEntry["actor_grant_role"],
    actor_was_owner_member: row.actor_was_owner_member,
    actor_action_key: null,
    actor_id: row.actor_id,
    agent_label: null,
  };
}
