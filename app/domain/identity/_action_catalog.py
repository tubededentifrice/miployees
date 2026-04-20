"""Canonical action-key catalog for authority checks (v1).

The catalog lists every ``action_key`` the permission resolver knows
about. Writes to ``permission_rule`` and ``permission_group.capabilities_json``
that reference a key missing from this set are rejected with
:class:`UnknownCapability` / 422 ``unknown_action_key`` — unknown
actions never fall through silently (§02 "Permission resolution" #1).

The values here mirror column 1 of the two tables in
``docs/specs/05-employees-and-roles.md`` §"Action catalog":

* **Root-only actions** (governance, always ``owners``-only).
* **Rule-driven actions** (ship with sane defaults, fully
  rule-configurable).

Sorted tuple literal so the order is stable for diffing; the
frozenset at the bottom is what callers compare against and gives
O(1) membership checks. New actions require a spec edit first — add
the key here in the same PR.
"""

from __future__ import annotations

__all__ = ["ACTION_CATALOG", "ACTION_KEYS"]


# Keep the literal sorted. When adding a key, keep the alphabetical
# order within its section so diffs stay readable.
_ROOT_ONLY_ACTIONS: tuple[str, ...] = (
    "admin.purge",
    "deployment.rotate_root_key",
    "groups.manage_owners_membership",
    "organization.archive",
    "permissions.edit_rules",
    "scope.transfer",
    "workspace.archive",
)


_RULE_DRIVEN_ACTIONS: tuple[str, ...] = (
    "agent_prefs.edit_property",
    "agent_prefs.edit_workspace",
    "api_tokens.manage",
    "assets.edit",
    "audit_log.view",
    "bookings.amend_other",
    "bookings.assign_other",
    "bookings.cancel",
    "bookings.create_pending",
    "bookings.view_other",
    "deployment.audit.view",
    "deployment.budget.edit",
    "deployment.llm.edit",
    "deployment.llm.view",
    "deployment.settings.edit",
    "deployment.signup.edit",
    "deployment.usage.view",
    "deployment.view",
    "deployment.workspaces.archive",
    "deployment.workspaces.trust",
    "deployment.workspaces.view",
    "expenses.approve",
    "expenses.reimburse",
    "expenses.submit",
    "groups.create",
    "groups.edit",
    "groups.manage_members",
    "instructions.edit",
    "inventory.adjust",
    "messaging.comments.author_global",
    "messaging.report_issue.triage",
    "organizations.create",
    "organizations.edit",
    "organizations.edit_pay_destination",
    "pay_rules.edit",
    "payroll.issue_payslip",
    "payroll.lock_period",
    "payroll.view_other",
    "properties.archive",
    "properties.create",
    "properties.edit",
    "properties.view_access_codes",
    "property_workspace.revoke",
    "property_workspace_invite.accept",
    "property_workspace_invite.create",
    "property_workspace_invite.reject",
    "property_workspace_invite.revoke",
    "quotes.accept",
    "quotes.submit",
    "role_grants.create",
    "role_grants.revoke",
    "scope.edit_settings",
    "scope.view",
    "tasks.assign_other",
    "tasks.complete_other",
    "tasks.create",
    "tasks.skip_other",
    "users.archive",
    "users.edit_profile_other",
    "users.invite",
    "vendor_invoices.approve",
    "vendor_invoices.approve_as_client",
    "vendor_invoices.remove_proof",
    "vendor_invoices.submit",
    "vendor_invoices.upload_proof",
    "work_orders.assign_contractor",
    "work_orders.create",
    "work_orders.view",
    "work_roles.manage",
)


#: Full ordered tuple of every v1 action key. Preserves section order
#: (root-only first, then rule-driven) for documentation / debugging;
#: :data:`ACTION_CATALOG` is what runtime checks use.
ACTION_KEYS: tuple[str, ...] = _ROOT_ONLY_ACTIONS + _RULE_DRIVEN_ACTIONS


#: O(1)-lookup view over every registered action key. Domain services
#: compare capability payloads against this set.
ACTION_CATALOG: frozenset[str] = frozenset(ACTION_KEYS)
