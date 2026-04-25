"""Canonical action-key catalog for authority checks (v1).

The catalog lists every ``action_key`` the permission resolver knows
about. Writes to ``permission_rule`` and ``permission_group.capabilities_json``
that reference a key missing from this set are rejected with
:class:`UnknownCapability` / 422 ``unknown_action_key`` — unknown
actions never fall through silently (§02 "Permission resolution" #1).

Entries mirror the two tables in
``docs/specs/05-employees-and-roles.md`` §"Action catalog":

* **Root-only actions** (governance, always ``owners``-only).
* **Rule-driven actions** (ship with sane defaults, fully
  rule-configurable).

Each :class:`ActionSpec` records the metadata the resolver needs:

* ``valid_scope_kinds`` — which ``permission_rule.scope_kind`` values
  the key accepts (§05 table column).
* ``default_allow`` — ordered tuple of system-group slugs granted the
  action when no rule matches (§05 table column). Root-only entries
  carry an empty tuple — the root-only gate decides without consulting
  defaults.
* ``root_only`` — ``True`` means only ``owners`` members decide
  (§05 "Root-only actions").
* ``root_protected_deny`` — ``True`` means owners cannot be denied by
  a deny rule (§05 "root_protected_deny" column, shown as ✅).

``ACTION_CATALOG`` is a ``Mapping[str, ActionSpec]`` — the primary
surface for the resolver. ``ACTION_KEYS`` is a ``frozenset[str]`` view
derived from the same table so existing callers that only need
membership tests keep working.

New actions require a spec edit first — add the entry below in the
same PR that edits ``docs/specs/05-employees-and-roles.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = ["ACTION_CATALOG", "ACTION_KEYS", "VALID_SCOPE_KINDS", "ActionSpec"]


#: Every ``scope_kind`` the v1 permission model recognises. Used at
#: catalog-consistency time (cf. :func:`validate_catalog_integrity`)
#: and at write time to reject malformed rules with 422.
VALID_SCOPE_KINDS: frozenset[str] = frozenset(
    {"workspace", "property", "organization", "deployment"}
)


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """Structured catalog entry for a single ``action_key``.

    Immutable: equality compares every field. Callers never need to
    build these — the module owns every instance — but keeping the
    dataclass public lets tests pin the shape.

    ``default_allow`` is a tuple (not a set) so the ordered,
    human-readable section of §05 round-trips verbatim and diffs stay
    readable.
    """

    key: str
    valid_scope_kinds: tuple[str, ...]
    default_allow: tuple[str, ...]
    root_only: bool
    root_protected_deny: bool


# ---------------------------------------------------------------------------
# Root-only actions (§05 "Root-only actions (governance)"). Ordered
# alphabetically within the section. ``default_allow`` is empty — the
# resolver short-circuits on ``root_only`` before consulting defaults.
# ``root_protected_deny`` is irrelevant because deny rules don't fire
# on root-only actions at all; left ``False`` to mirror the spec
# (§02 "Permission resolution" #2 notes that allow/deny rules on
# root-only keys are accepted at write time "but have no effect").
# ---------------------------------------------------------------------------
_ROOT_ONLY: tuple[ActionSpec, ...] = (
    ActionSpec(
        key="admin.purge",
        valid_scope_kinds=("workspace",),
        default_allow=(),
        root_only=True,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="deployment.rotate_root_key",
        valid_scope_kinds=("deployment",),
        default_allow=(),
        root_only=True,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="groups.manage_owners_membership",
        valid_scope_kinds=("workspace", "organization", "deployment"),
        default_allow=(),
        root_only=True,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="organization.archive",
        valid_scope_kinds=("organization",),
        default_allow=(),
        root_only=True,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="permissions.edit_rules",
        valid_scope_kinds=("workspace", "property", "organization", "deployment"),
        default_allow=(),
        root_only=True,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="scope.transfer",
        valid_scope_kinds=("workspace", "organization", "deployment"),
        default_allow=(),
        root_only=True,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="workspace.archive",
        valid_scope_kinds=("workspace",),
        default_allow=(),
        root_only=True,
        root_protected_deny=False,
    ),
)


# ---------------------------------------------------------------------------
# Rule-driven actions (§05 "Rule-driven actions"). Ordered
# alphabetically within the section. The ✅ column in the spec maps
# to ``root_protected_deny=True``; a bare em-dash maps to ``False``.
# ---------------------------------------------------------------------------
_RULE_DRIVEN: tuple[ActionSpec, ...] = (
    ActionSpec(
        key="agent_prefs.edit_property",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="agent_prefs.edit_workspace",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="api_tokens.manage",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="assets.edit",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="audit_log.view",
        valid_scope_kinds=("workspace", "property", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="availability_overrides.create_self",
        # cd-uqw1 — workers self-submit `user_availability_override` rows
        # (date-specific tweaks of the weekly availability pattern).
        # Mirrors :data:`leaves.create_self`: managers + owners hold the
        # capability so a manager creating an override on their own
        # account takes the same code path; cross-user creation is
        # gated on ``availability_overrides.edit_others``. Listed in
        # ``docs/specs/05-employees-and-roles.md`` §"Rule-driven actions".
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers", "all_workers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="availability_overrides.edit_others",
        # cd-uqw1 — manager / owner retroactive edits on someone else's
        # override (create-on-behalf-of, approve, reject, delete). Same
        # shape as :data:`leaves.edit_others`: a single capability
        # covers approve / reject / cross-user edit so the catalog
        # doesn't drift toward one key per verb. Listed in
        # ``docs/specs/05-employees-and-roles.md`` §"Rule-driven actions".
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="availability_overrides.view_others",
        # cd-uqw1 — manager / owner inbox view "every override in this
        # workspace". A worker reads their own overrides via the self
        # filter (``?user_id=ctx.actor_id``); cross-user visibility
        # requires this capability. Listed in
        # ``docs/specs/05-employees-and-roles.md`` §"Rule-driven actions".
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="bookings.amend_other",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="bookings.assign_other",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="bookings.cancel",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="bookings.create_pending",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers", "all_workers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="bookings.view_other",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="deployment.audit.view",
        valid_scope_kinds=("deployment",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="deployment.budget.edit",
        valid_scope_kinds=("deployment",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="deployment.llm.edit",
        valid_scope_kinds=("deployment",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="deployment.llm.view",
        valid_scope_kinds=("deployment",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="deployment.settings.edit",
        valid_scope_kinds=("deployment",),
        default_allow=("owners",),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="deployment.signup.edit",
        valid_scope_kinds=("deployment",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="deployment.usage.view",
        valid_scope_kinds=("deployment",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="deployment.view",
        valid_scope_kinds=("deployment",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="deployment.workspaces.archive",
        valid_scope_kinds=("deployment",),
        default_allow=("owners",),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="deployment.workspaces.trust",
        valid_scope_kinds=("deployment",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="deployment.workspaces.view",
        valid_scope_kinds=("deployment",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="employees.read",
        # cd-g6nf — SPA roster endpoint. Manager-only (owners + managers)
        # because the projection collates identity-level fields
        # (display_name, email, locale, timezone) with the workspace-
        # scoped engagement + role-grant + property assignments. Workers
        # see their own profile via :func:`/auth/me` + the worker
        # surfaces; the roster is a manager view by design (§05 "User
        # (as worker)" — workers do not see other workers' rosters).
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="expenses.approve",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="expenses.reimburse",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="expenses.submit",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers", "all_workers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="groups.create",
        valid_scope_kinds=("workspace", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="groups.edit",
        valid_scope_kinds=("workspace", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="groups.manage_members",
        valid_scope_kinds=("workspace", "organization"),
        default_allow=("owners",),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="instructions.edit",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="leaves.create_self",
        # Workers self-request leave; managers + owners also hold the
        # capability so a manager creating a leave on their own account
        # takes the same code path. Cross-user creation is gated on
        # ``leaves.edit_others`` — see :mod:`app.services.leave.service`
        # (cd-31c). Listed in ``docs/specs/05-employees-and-roles.md``
        # §"Rule-driven actions".
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers", "all_workers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="leaves.edit_others",
        # Manager / owner retroactive edits on someone else's leave —
        # create-on-behalf-of, cancel, amend dates. Listed in
        # ``docs/specs/05-employees-and-roles.md`` §"Rule-driven actions".
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="leaves.view_others",
        # Manager / owner inbox view — "every leave in this workspace".
        # A worker reads their own leaves via :func:`list_for_user`
        # (defaults ``user_id=ctx.actor_id``); only cross-user visibility
        # requires this capability. Listed in
        # ``docs/specs/05-employees-and-roles.md`` §"Rule-driven actions".
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="inventory.adjust",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="messaging.comments.author_global",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers", "all_workers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="messaging.report_issue.triage",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="organizations.create",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="organizations.edit",
        valid_scope_kinds=("workspace", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="organizations.edit_pay_destination",
        valid_scope_kinds=("workspace", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="pay_rules.edit",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="payroll.issue_payslip",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="payroll.lock_period",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="payroll.view_other",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="properties.archive",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="properties.create",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="properties.edit",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="properties.read",
        # cd-lzh1, cd-yjw5 — SPA properties roster endpoint
        # (``GET /properties``). Manager-only (owners + managers) by
        # ``default_allow``: holding the action grants the **full**
        # projection, including the §22 governance-adjacent fields
        # (``client_org_id`` / ``owner_user_id``) and the per-property
        # ``settings_override`` blob.
        #
        # The endpoint itself accepts every authenticated workspace
        # member (no action-key gate — see ``app/api/v1/places.py``
        # for why ``scope.view@workspace`` would be too narrow) and
        # falls through to a worker-narrowed projection when
        # ``properties.read`` resolves deny: only the properties the
        # worker holds a ``role_grant`` on, with the three governance
        # fields masked to safe defaults. Worker pages
        # (``HistoryPage``, ``NewTaskModal``, ``SubmitExpenseForm``)
        # need the name + city + timezone of properties they already
        # see in property-pinned data; the cross-roster listing is the
        # cheapest place to serve that without N+1
        # ``/properties/{id}`` calls.
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="properties.view_access_codes",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="property_workspace.revoke",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="property_workspace_invite.accept",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="property_workspace_invite.create",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="property_workspace_invite.reject",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="property_workspace_invite.revoke",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="quotes.accept",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers", "all_clients"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="quotes.submit",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="role_grants.create",
        valid_scope_kinds=("workspace", "property", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="role_grants.revoke",
        valid_scope_kinds=("workspace", "property", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="scope.edit_settings",
        valid_scope_kinds=("workspace", "property", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="scope.view",
        valid_scope_kinds=("workspace", "property", "organization"),
        default_allow=("owners", "managers", "all_workers", "all_clients"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="tasks.assign_other",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="tasks.comment",
        # cd-cfe4 — post a ``kind='user'`` comment on a task
        # occurrence. Workers need the capability by default (the
        # agent inbox is where they actually report progress), so
        # ``all_workers`` joins owners + managers in ``default_allow``.
        # Gated at the domain layer by
        # :func:`app.domain.tasks.comments.post_comment`.
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers", "all_workers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="tasks.comment_moderate",
        # cd-cfe4 — delete another user's comment, or edit a
        # ``kind='user'`` comment after the 5-minute author grace
        # window. Owners / managers only; workers cannot moderate
        # someone else's chat history. Listed in
        # ``docs/specs/05-employees-and-roles.md`` §"Rule-driven
        # actions".
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="tasks.complete_other",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="tasks.create",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers", "all_workers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="tasks.skip_other",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="time.clock_self",
        # §09's shift model is property-anchored (``shift.property_id``
        # optional but typically set), so property + workspace scope
        # both make sense. ``all_workers`` gets the default so a worker
        # can clock in without an explicit rule. Listed in
        # ``docs/specs/05-employees-and-roles.md`` §"Rule-driven actions".
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers", "all_workers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="time.edit_others",
        # Manager-only: amend someone else's shift (add a retroactive
        # entry, correct a misclicked clock-out, etc.). Listed in
        # ``docs/specs/05-employees-and-roles.md`` §"Rule-driven actions".
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="users.archive",
        valid_scope_kinds=("workspace", "property", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="users.edit_profile_other",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="users.invite",
        valid_scope_kinds=("workspace", "property", "organization"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        # cd-y5z3 — owner-initiated worker passkey reset
        # (``POST /users/{id}/reset_passkey``). §03 "Owner-initiated
        # worker passkey reset" pins the gate to **owners only**:
        # mailing the worker a fresh enrolment link plus a non-
        # consumable notification copy to the owner is a sensitive
        # break-glass path — managers are intentionally excluded so a
        # manager-tier compromise cannot pivot to wholesale credential
        # rotation without an owner's hand on the trigger.
        # ``root_protected_deny=True`` so a deny rule cannot strip the
        # capability from owners (mirrors :data:`groups.manage_members`).
        key="users.reset_passkey",
        valid_scope_kinds=("workspace",),
        default_allow=("owners",),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="vendor_invoices.approve",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=True,
    ),
    ActionSpec(
        key="vendor_invoices.approve_as_client",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("all_clients",),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="vendor_invoices.remove_proof",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="vendor_invoices.submit",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="vendor_invoices.upload_proof",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers", "all_clients"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="work_orders.assign_contractor",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="work_orders.create",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="work_orders.view",
        valid_scope_kinds=("workspace", "property"),
        default_allow=("owners", "managers", "all_workers", "all_clients"),
        root_only=False,
        root_protected_deny=False,
    ),
    ActionSpec(
        key="work_roles.manage",
        valid_scope_kinds=("workspace",),
        default_allow=("owners", "managers"),
        root_only=False,
        root_protected_deny=False,
    ),
)


_ALL_ENTRIES: tuple[ActionSpec, ...] = _ROOT_ONLY + _RULE_DRIVEN


#: Primary catalog surface — ``{action_key: ActionSpec}``. Wrapped in
#: :class:`MappingProxyType` so callers can't mutate it in place; the
#: resolver expects the mapping to be immutable across the process
#: lifetime. Section order (root-only first) is preserved as a
#: diff-readability aid; callers that need a deterministic iteration
#: should sort explicitly.
ACTION_CATALOG: Mapping[str, ActionSpec] = MappingProxyType(
    {spec.key: spec for spec in _ALL_ENTRIES}
)


#: O(1)-lookup view over every registered action key. Domain services
#: validating free-form capability payloads use ``key in ACTION_KEYS``
#: — the membership test is what matters, not the spec metadata.
ACTION_KEYS: frozenset[str] = frozenset(ACTION_CATALOG.keys())
