"""Static mock data for the crewday UI preview.

Shapes and vocabulary follow the specs in docs/specs/. The point is to
make the eventual product feel real — not to simulate it.

v1 identity + permission model (per §02, §03, §05, §22):

- One ``users`` row per human login. The **surface** a user sees
  comes from ``role_grants`` (``grant_role ∈ {manager, worker,
  client, guest}``). The **authority** to perform specific
  actions comes from membership in ``permission_group`` rows
  plus ``permission_rule`` rows keyed to an ``action_key`` from
  the §05 action catalog. The ``owner`` grant_role and the
  per-grant ``capability_override`` column from earlier drafts
  have been retired.
- Each workspace and organization is seeded with four system
  groups: ``owners`` (explicit membership, governance anchor,
  ≥1 active member at all times), ``managers``,
  ``all_workers``, ``all_clients`` (membership derived from
  ``role_grants``).
- ``work_engagement`` per (user, workspace) carries pay-pipeline data;
  pay rules / shifts / payslips / expense claims key off
  ``work_engagement_id`` rather than ``user_id`` directly.
- ``work_role`` (previously ``role``) lists the jobs a workspace knows
  about. ``user_work_role`` (previously ``employee_role``) binds a
  user to a role within a workspace.

For UI continuity the legacy ``Employee``/``Role`` dataclasses and
``/api/v1/employees`` endpoints remain; they are now compatibility
views derived from the canonical tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal


@dataclass
class SettingDefinition:
    key: str
    label: str
    type: Literal["enum", "int", "bool"]
    catalog_default: Any
    enum_values: list[str] | None = None
    override_scope: str = "W"
    description: str = ""
    spec: str = ""


TODAY: date = date(2026, 4, 15)
NOW: datetime = datetime(2026, 4, 15, 10, 12)


# ── Core entities ────────────────────────────────────────────────────

@dataclass
class Property:
    id: str
    name: str
    city: str
    timezone: str
    color: str
    kind: Literal["str", "vacation", "residence", "mixed"]
    areas: list[str] = field(default_factory=list)
    evidence_policy: Literal["inherit", "require", "optional", "forbid"] = "inherit"
    country: str = "FR"
    locale: str = "fr-FR"
    settings_override: dict[str, Any] = field(default_factory=dict)
    client_org_id: str | None = None  # §22 — null = self-managed
    owner_user_id: str | None = None  # §22 — nullable FK to users.id


# ── v1 identity model (§02, §03, §05) ────────────────────────────────


@dataclass
class Workspace:
    """A tenancy boundary. v1 ships single-workspace per deployment,
    but the mock seeds more than one to exercise the shared-property /
    client-grant flows from §05 and §22."""

    id: str
    name: str
    timezone: str = "Europe/Paris"
    default_currency: str = "EUR"
    default_country: str = "FR"
    default_locale: str = "fr-FR"


@dataclass
class User:
    """A single human login identity (§02 `users` table)."""

    id: str
    email: str
    display_name: str
    timezone: str = "Europe/Paris"
    languages: list[str] = field(default_factory=list)
    preferred_locale: str | None = None
    avatar_file_id: str | None = None
    primary_workspace_id: str | None = None
    phone_e164: str | None = None
    notes_md: str = ""
    # §11 — governs when the user's own embedded chat agent pauses for
    # an inline confirmation card before executing a delegated-token
    # mutation. Self-writable only; default `strict`.
    agent_approval_mode: Literal["bypass", "auto", "strict"] = "strict"
    archived_at: datetime | None = None


@dataclass
class RoleGrant:
    """A surface / persona row: (user, scope_kind, scope_id, grant_role).

    In v1 ``role_grants`` no longer carries authority — it carries
    the UI shell (worker PWA / client portal / manager dashboard)
    and the RLS filter. Authority lives in
    ``permission_group`` + ``permission_rule`` (§02).

    The ``owner`` grant_role from earlier drafts is retired;
    governance is held by membership in the ``owners``
    permission group instead.
    """

    id: str
    user_id: str
    scope_kind: Literal["workspace", "property", "organization"]
    scope_id: str
    grant_role: Literal["manager", "worker", "client", "guest"]
    binding_org_id: str | None = None
    started_on: date | None = None
    ended_on: date | None = None
    granted_by_user_id: str | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None


@dataclass
class PermissionGroup:
    """A named set of users that can be the subject of a
    ``permission_rule`` (§02).

    Scope is ``workspace`` or ``organization``. Four system
    groups are seeded per scope: ``owners`` (explicit members,
    governance anchor), ``managers`` / ``all_workers`` /
    ``all_clients`` (derived from role_grants on the scope).
    User-defined groups carry explicit members.
    """

    id: str
    scope_kind: Literal["workspace", "organization"]
    scope_id: str
    key: str  # e.g. "owners", "managers", "family"
    name: str
    description_md: str = ""
    group_kind: Literal["system", "user"] = "user"
    is_derived: bool = False  # true for managers / all_workers / all_clients
    deleted_at: datetime | None = None


@dataclass
class PermissionGroupMember:
    """Explicit membership for non-derived groups (``owners`` +
    user-defined). Derived groups compute membership from
    ``role_grants`` at query time and have no rows here."""

    group_id: str
    user_id: str
    added_by_user_id: str | None = None
    added_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass
class PermissionRule:
    """Authority on a scope: (scope_kind, scope_id, action_key,
    subject_kind, subject_id, effect).

    Resolution (§02 "Permission resolution"): most-specific scope
    first; within a scope ``deny`` beats ``allow``; catalog
    default fires when no rule matches. Root-only actions
    short-circuit to owners-only regardless of rules.
    """

    id: str
    scope_kind: Literal["workspace", "property", "organization"]
    scope_id: str
    action_key: str
    subject_kind: Literal["user", "group"]
    subject_id: str
    effect: Literal["allow", "deny"]
    created_by_user_id: str | None = None
    created_at: datetime | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None


@dataclass
class ActionCatalogEntry:
    """Compile-time action catalog entry (§05 action catalog).

    Mirrors the JSON the backend would expose from
    ``GET /permissions/action_catalog``.
    """

    key: str
    description: str
    valid_scope_kinds: list[str]
    default_allow: list[str]  # system-group keys
    root_only: bool = False
    root_protected_deny: bool = False
    spec: str = ""


@dataclass
class WorkRole:
    """v0 `role` renamed to `work_role` (§05)."""

    id: str
    workspace_id: str
    key: str
    name: str
    description_md: str = ""
    icon_glyph: str = ""
    default_capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass
class UserWorkRole:
    """v0 `employee_role` renamed (§05). Scoped per (user, workspace)."""

    id: str
    user_id: str
    workspace_id: str
    work_role_id: str
    started_on: date
    ended_on: date | None = None
    pay_rule_id: str | None = None
    capability_override: dict[str, Any] = field(default_factory=dict)


@dataclass
class PropertyWorkRoleAssignment:
    """v0 `property_role_assignment` renamed (§05)."""

    id: str
    user_work_role_id: str
    property_id: str
    schedule_ruleset_id: str | None = None
    property_pay_rule_id: str | None = None
    capability_override: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkEngagement:
    """The per-(user, workspace) pay pipeline (§02, §22).

    Carries engagement_kind which drives whether the user is paid by
    payslip (`payroll`), via vendor invoice from themselves
    (`contractor`), or via vendor invoice from a supplier
    (`agency_supplied`).
    """

    id: str
    user_id: str
    workspace_id: str
    engagement_kind: Literal["payroll", "contractor", "agency_supplied"] = "payroll"
    supplier_org_id: str | None = None
    pay_destination_id: str | None = None
    reimbursement_destination_id: str | None = None
    started_on: date = field(default_factory=lambda: date(2024, 1, 1))
    archived_on: date | None = None
    notes_md: str = ""


@dataclass
class UserWorkspace:
    """Derived junction (§02). A user is materialised in every workspace
    where they hold at least one active grant.
    """

    user_id: str
    workspace_id: str
    source: Literal["workspace_grant", "property_grant", "org_grant", "work_engagement"]
    added_at: datetime = field(default_factory=lambda: datetime(2026, 1, 1))


@dataclass
class PropertyWorkspace:
    """A property can belong to more than one workspace (§02).

    `membership_role` says how the workspace relates to the property.
    """

    property_id: str
    workspace_id: str
    membership_role: Literal["owner_workspace", "managed_workspace", "observer_workspace"]
    added_at: datetime = field(default_factory=lambda: datetime(2026, 1, 1))
    added_by_user_id: str | None = None
    added_via: Literal["user", "agent", "system"] = "user"


# Legacy alias kept for the web UI's "/api/v1/roles" shape (WorkRole
# serialises with the same fields; `Role` carries only (id, name) for
# the starter-list rendering).
@dataclass
class Role:
    id: str
    name: str


@dataclass
class Employee:
    """UI-facing compatibility view of a user × workspace × work_engagement.

    In the v1 domain model (§02, §05) this is not a storage entity —
    it's a composite projection of:
        users × work_engagement × user_work_role × property_work_role_assignment
    The mock keeps this dataclass so the existing Jinja/React UI and
    the ``/api/v1/employees`` alias endpoint stay stable. ``id`` here
    re-uses the old ``e-...`` slug; ``user_id``, ``work_engagement_id``
    and ``workspace_id`` point at the canonical rows.
    """

    id: str
    name: str
    roles: list[str]
    properties: list[str]
    avatar_initials: str
    phone: str
    email: str
    started_on: date
    clocked_in_at: datetime | None = None
    capabilities: dict[str, bool | None] = field(default_factory=dict)
    workspaces: list[str] = field(default_factory=list)
    villas: list[str] = field(default_factory=list)
    clock_mode: Literal["manual", "auto", "disabled"] = "manual"
    auto_clock_idle_minutes: int = 30
    language: str = "fr"
    weekly_availability: dict[str, tuple[str, str] | None] = field(default_factory=dict)
    evidence_policy: Literal["inherit", "require", "optional", "forbid"] = "inherit"
    preferred_locale: str | None = None
    settings_override: dict[str, Any] = field(default_factory=dict)
    engagement_kind: Literal["payroll", "contractor", "agency_supplied"] = "payroll"  # §05, §22
    supplier_org_id: str | None = None  # required iff engagement_kind = agency_supplied
    user_id: str = ""  # v1 — FK into USERS
    work_engagement_id: str = ""  # v1 — FK into WORK_ENGAGEMENTS
    workspace_id: str = ""  # v1 — home workspace for this engagement


@dataclass
class Stay:
    id: str
    property_id: str
    guest_name: str
    source: Literal["manual", "airbnb", "vrbo", "booking", "google_calendar", "ical"]
    check_in: date
    check_out: date
    guests: int
    status: Literal["tentative", "confirmed", "in_house", "checked_out", "cancelled"] = "confirmed"


@dataclass
class Task:
    id: str
    title: str
    property_id: str
    area: str
    assignee_id: str
    scheduled_start: datetime
    estimated_minutes: int
    priority: Literal["low", "normal", "high", "urgent"]
    status: Literal["scheduled", "pending", "in_progress", "completed", "skipped", "cancelled", "overdue"]
    checklist: list[dict] = field(default_factory=list)
    photo_evidence: Literal["disabled", "optional", "required"] = "disabled"
    # Forward-looking policy used by the agent-first model; kept parallel to
    # photo_evidence so existing templates/checks still work.
    evidence_policy: Literal["inherit", "require", "optional", "forbid"] = "inherit"
    instructions_ids: list[str] = field(default_factory=list)
    template_id: str | None = None
    schedule_id: str | None = None
    turnover_bundle_id: str | None = None
    asset_id: str | None = None
    settings_override: dict[str, Any] = field(default_factory=dict)
    # v1 — canonical FK name per §02/§06. `assignee_id` kept for the
    # UI. These mirror each other in the seeded data and writes set
    # both.
    assigned_user_id: str = ""
    workspace_id: str = ""


@dataclass
class Expense:
    """Per §09 an expense claim belongs to a `work_engagement` (not to
    a user directly). ``employee_id`` is kept for the legacy UI and
    mirrors the employee row's id; ``user_id`` and ``work_engagement_id``
    are the v1 canonical pointers.
    """

    id: str
    employee_id: str
    amount_cents: int
    currency: str
    merchant: str
    submitted_at: datetime
    status: Literal["draft", "submitted", "approved", "rejected", "reimbursed"]
    note: str
    ocr_confidence: float | None = None
    category: str | None = None
    user_id: str = ""
    work_engagement_id: str = ""


@dataclass
class ApprovalRequest:
    id: str
    agent: str
    action: str
    target: str
    reason: str
    requested_at: datetime
    risk: Literal["low", "medium", "high"]
    diff: list[str] = field(default_factory=list)
    # §11 — which layer produced the gate, where it is rendered, and
    # the server-rendered card copy (authoritative for inline chat).
    gate_source: Literal[
        "workspace_always",
        "workspace_configurable",
        "user_auto_annotation",
        "user_strict_mutation",
    ] = "workspace_configurable"
    gate_destination: Literal["desk", "inline_chat"] = "desk"
    inline_channel: Literal[
        "desk_only",
        "web_owner_sidebar",
        "web_worker_chat",
        "offapp_whatsapp",
    ] = "desk_only"
    card_summary: str = ""
    card_fields: list[tuple[str, str]] = field(default_factory=list)
    for_user_id: str | None = None
    resolved_user_mode: Literal["bypass", "auto", "strict"] | None = None


@dataclass
class Leave:
    id: str
    employee_id: str
    starts_on: date
    ends_on: date
    category: Literal["vacation", "sick", "personal", "bereavement", "other"]
    note: str
    approved_at: datetime | None = None
    user_id: str = ""
    decided_by_user_id: str | None = None


@dataclass
class PropertyClosure:
    id: str
    property_id: str
    starts_on: date
    ends_on: date
    reason: Literal["renovation", "owner_stay", "seasonal", "ical_unavailable", "other"]
    note: str = ""


@dataclass
class TaskTemplate:
    id: str
    name: str
    description: str
    role: str
    duration_minutes: int
    property_scope: Literal["any", "one", "listed"]
    photo_evidence: Literal["disabled", "optional", "required"]
    priority: Literal["low", "normal", "high", "urgent"]
    checklist: list[dict] = field(default_factory=list)


@dataclass
class Schedule:
    id: str
    name: str
    template_id: str
    property_id: str
    rrule_human: str
    default_assignee_id: str | None
    duration_minutes: int
    active_from: date
    paused: bool = False


@dataclass
class Instruction:
    id: str
    title: str
    scope: Literal["global", "property", "area"]
    property_id: str | None
    area: str | None
    tags: list[str]
    body_md: str
    version: int
    updated_at: datetime


@dataclass
class InventoryItem:
    id: str
    property_id: str
    name: str
    sku: str
    on_hand: int
    par: int
    unit: str
    area: str


@dataclass
class Issue:
    id: str
    reported_by: str
    property_id: str
    area: str
    severity: Literal["low", "normal", "high", "urgent"]
    category: Literal["damage", "broken", "supplies", "safety", "other"]
    title: str
    body: str
    reported_at: datetime
    status: Literal["open", "in_progress", "resolved", "wont_fix"]


@dataclass
class PaySlip:
    """Pay-pipeline row — belongs to a `work_engagement` (§09, §22).
    ``employee_id`` preserved for UI; ``work_engagement_id`` is the
    canonical FK.
    """

    id: str
    employee_id: str
    period_starts: date
    period_ends: date
    gross_cents: int
    reimbursements_cents: int
    net_cents: int
    status: Literal["draft", "issued", "paid", "voided"]
    hours: float
    overtime: float
    currency: str = "EUR"
    locale: str = "fr-FR"
    jurisdiction: str = "FR"
    work_engagement_id: str = ""
    user_id: str = ""


@dataclass
class ModelAssignment:
    capability: str
    description: str
    provider: str
    model_id: str
    enabled: bool
    daily_budget_usd: float
    spent_24h_usd: float
    calls_24h: int


@dataclass
class LLMCall:
    at: datetime
    capability: str
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_cents: int
    latency_ms: int
    status: Literal["ok", "error", "redacted_block"]


@dataclass
class AuditEntry:
    """v1 `actor_kind` collapses to `user | agent | system` (§02).

    The grant under which a human acted is captured in
    ``actor_grant_role``; it's null when the action is identity-scoped
    (e.g. a user editing their own profile) or when the actor is not
    a user.
    """

    at: datetime
    actor_kind: Literal["user", "agent", "system"]
    actor: str
    action: str
    target: str
    via: Literal["web", "api", "cli", "worker"]
    reason: str | None = None
    # Surface grant the actor was using at the time. v1 drops the
    # ``owner`` value — governance is captured by
    # ``actor_was_owner_member`` below.
    actor_grant_role: Literal["manager", "worker", "client", "guest"] | None = None
    actor_was_owner_member: bool | None = None
    actor_action_key: str | None = None
    actor_id: str | None = None
    agent_label: str | None = None


@dataclass
class Webhook:
    id: str
    url: str
    events: list[str]
    active: bool
    last_delivery_status: int
    last_delivery_at: datetime


@dataclass
class Message:
    id: str
    from_: str
    body: str
    at: datetime


# ── Time / payroll entities ─────────────────────────────────────────

@dataclass
class Shift:
    """Pay-pipeline row — belongs to a `work_engagement` (§09).

    v1 renames ``employee_id`` to ``work_engagement_id``; the
    denormalised ``user_id`` is stored alongside for fast "who was
    working?" queries. The legacy ``employee_id`` alias stays wired
    to the employee compat row id so the UI keeps working.
    """

    id: str
    employee_id: str
    property_id: str
    started_at: datetime
    ended_at: datetime | None
    status: Literal["open", "closed", "disputed"]
    duration_seconds: int | None = None
    break_seconds: int = 0
    method_in: Literal["manual", "auto", "geo"] = "manual"
    method_out: Literal["manual", "auto", "geo"] | None = None
    client_org_id: str | None = None  # §22 — derived cache from property.client_org_id at close
    work_engagement_id: str = ""
    user_id: str = ""


@dataclass
class PayRule:
    """Pay-pipeline row — belongs to a `work_engagement` (§09)."""

    id: str
    employee_id: str
    property_id: str | None
    kind: Literal["hourly", "monthly_salary", "per_task", "piecework"]
    rate_cents: int
    currency: str
    effective_from: date
    effective_until: date | None = None
    work_engagement_id: str = ""


@dataclass
class PayPeriod:
    id: str
    starts_on: date
    ends_on: date
    status: Literal["open", "locked", "paid"]
    locked_at: datetime | None = None


# ── Inventory movement ─────────────────────────────────────────────

@dataclass
class InventoryMovement:
    """`actor_kind` collapses to `user | agent | system` in v1 (§02)."""

    id: str
    item_id: str
    delta: int
    reason: Literal["restock", "consume", "adjust", "waste", "transfer_in", "transfer_out", "audit_correction"]
    actor_kind: Literal["user", "agent", "system"]
    actor_id: str
    occurred_at: datetime
    note: str | None = None


# ── Stay lifecycle ─────────────────────────────────────────────────

@dataclass
class StayLifecycleRule:
    id: str
    property_id: str
    trigger: Literal["before_checkin", "after_checkout", "during_stay"]
    template_id: str
    offset_hours: int = 0
    enabled: bool = True


# ── Task comments ──────────────────────────────────────────────────

@dataclass
class TaskComment:
    """`author_kind` collapses to `user | agent | system` in v1 (§02).

    Manager vs. worker authoring is recovered from the author's
    `role_grants` on the task's workspace.
    """

    id: str
    task_id: str
    author_kind: Literal["user", "agent", "system"]
    author_id: str
    body_md: str
    created_at: datetime


# ── Asset entities ──────────────────────────────────────────────────

@dataclass
class AssetType:
    id: str
    key: str
    name: str
    category: Literal[
        "climate", "appliance", "plumbing", "pool", "heating",
        "outdoor", "safety", "security", "vehicle", "other",
    ]
    icon: str
    default_actions: list[dict[str, Any]]
    default_lifespan_years: int | None = None


@dataclass
class Asset:
    id: str
    property_id: str
    asset_type_id: str | None
    name: str
    area: str | None
    condition: Literal["new", "good", "fair", "poor", "needs_replacement"]
    status: Literal["active", "in_repair", "decommissioned", "disposed"]
    make: str | None = None
    model: str | None = None
    serial_number: str | None = None
    installed_on: date | None = None
    purchased_on: date | None = None
    purchase_price_cents: int | None = None
    purchase_currency: str | None = None
    purchase_vendor: str | None = None
    warranty_expires_on: date | None = None
    expected_lifespan_years: int | None = None
    guest_visible: bool = False
    guest_instructions: str | None = None
    notes: str | None = None
    qr_token: str = ""


@dataclass
class AssetAction:
    id: str
    asset_id: str
    key: str | None
    label: str
    interval_days: int | None = None
    last_performed_at: date | None = None
    linked_task_id: str | None = None
    linked_schedule_id: str | None = None
    description: str | None = None
    estimated_duration_minutes: int | None = None


@dataclass
class AssetDocument:
    id: str
    asset_id: str | None
    property_id: str
    kind: Literal[
        "manual", "warranty", "invoice", "receipt", "photo",
        "certificate", "contract", "permit", "insurance", "other",
    ]
    title: str
    filename: str
    size_kb: int
    uploaded_at: datetime
    expires_on: date | None = None
    amount_cents: int | None = None
    amount_currency: str | None = None


# ── Clients, vendors, work orders (§22) ──────────────────────────────


@dataclass
class Organization:
    id: str
    name: str
    workspace_id: str = ""  # §22 — the workspace this org row belongs to
    is_client: bool = False
    is_supplier: bool = False
    legal_name: str | None = None
    default_currency: str = "EUR"
    tax_id: str | None = None
    contacts: list[dict] = field(default_factory=list)
    notes: str | None = None
    default_pay_destination_stub: str | None = None  # e.g. "•• FR-07" when is_supplier
    portal_user_id: str | None = None  # convenience pointer; canonical login is via role_grants


@dataclass
class ClientRate:
    """Per (client_org, work_role) billable rate (§22).

    v1 split: per-user overrides live on a separate `client_user_rate`
    row (below) rather than overloading this one with a nullable
    `user_id` column.
    """

    id: str
    client_org_id: str
    work_role_id: str
    hourly_cents: int
    currency: str
    effective_from: date
    effective_to: date | None = None


@dataclass
class ClientUserRate:
    """Per (client_org, user) override, previously `client_employee_rate`."""

    id: str
    client_org_id: str
    user_id: str
    hourly_cents: int
    currency: str
    effective_from: date
    effective_to: date | None = None


@dataclass
class ShiftBilling:
    """v1 names (§22): `user_id` + `work_engagement_id`; rate_source
    uses `client_user_rate` instead of the v0 `client_employee_rate`.
    """

    id: str
    shift_id: str
    client_org_id: str
    user_id: str
    currency: str
    billable_minutes: int
    hourly_cents: int
    subtotal_cents: int
    rate_source: Literal["client_user_rate", "client_rate", "unpriced"]
    rate_source_id: str | None = None
    work_engagement_id: str = ""


@dataclass
class WorkOrder:
    id: str
    property_id: str
    title: str
    state: Literal[
        "draft", "quoted", "accepted", "in_progress",
        "completed", "cancelled", "invoiced", "paid",
    ]
    assigned_user_id: str | None
    currency: str
    client_org_id: str | None = None  # derived from property at creation
    asset_id: str | None = None
    description: str | None = None
    accepted_quote_id: str | None = None
    created_at: datetime | None = None
    requested_by_user_id: str | None = None


@dataclass
class Quote:
    id: str
    work_order_id: str
    submitted_by_user_id: str
    currency: str
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    status: Literal["draft", "submitted", "accepted", "rejected", "superseded", "expired"]
    lines: list[dict] = field(default_factory=list)
    valid_until: date | None = None
    submitted_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by_user_id: str | None = None
    decision_note: str | None = None
    work_engagement_id: str | None = None


@dataclass
class VendorInvoice:
    id: str
    currency: str
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    billed_at: date
    status: Literal["draft", "submitted", "approved", "rejected", "paid", "voided"]
    work_order_id: str | None = None
    property_id: str | None = None
    vendor_user_id: str | None = None
    vendor_work_engagement_id: str | None = None
    vendor_organization_id: str | None = None
    due_on: date | None = None
    payout_destination_stub: str | None = None
    lines: list[dict] = field(default_factory=list)
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    decided_by_user_id: str | None = None
    paid_at: datetime | None = None
    paid_by_user_id: str | None = None
    decision_note: str | None = None


# ── Canonical starter data ───────────────────────────────────────────

WORKSPACES: list[Workspace] = [
    Workspace("ws-bernard",  "Bernard workspace"),
    Workspace("ws-vincent",  "VincentOps"),
    Workspace("ws-cleanco",  "AgencyOps — CleanCo"),
]

PROPERTIES: list[Property] = [
    Property("p-villa-sud", "Villa Sud", "Antibes", "Europe/Paris", "moss", "str",
             areas=["Master bedroom", "Kitchen", "Pool", "Garden", "Entryway", "Living room"],
             settings_override={
                 "evidence.policy": "require",
                 "time.geofence_radius_m": 200,
                 "tasks.checklist_required": True,
             }),
    Property("p-apt-3b", "Apt 3B", "Paris", "Europe/Paris", "sky", "str",
             areas=["Full unit", "Kitchen", "Bathroom 1", "Bathroom 2"],
             settings_override={
                 "evidence.policy": "optional",
             },
             client_org_id="org-dupont"),
    Property("p-chalet", "Chalet Cœur", "Megève", "Europe/Paris", "rust", "vacation",
             areas=["Kitchen", "Fireplace room", "Master bedroom", "Ski room"],
             settings_override={
                 "evidence.policy": "forbid",
                 "time.geofence_required": False,
                 "scheduling.horizon_days": 14,
             }),
    # Vincent scenario properties (§05 example).
    Property("p-villa-lac", "Villa du Lac", "Annecy", "Europe/Paris", "moss", "vacation",
             areas=["Master bedroom", "Kitchen", "Living room", "Pool", "Garden"],
             settings_override={
                 "evidence.policy": "optional",
             },
             client_org_id="org-dupont-vincent",
             owner_user_id="u-vincent"),
    Property("p-seaside", "Seaside Apt", "Cassis", "Europe/Paris", "sky", "vacation",
             areas=["Living room", "Kitchen", "Bedroom", "Terrace"],
             client_org_id=None,
             owner_user_id="u-vincent"),
]

# v1: `work_role` (the workspace-defined job bundle). The shorter
# `Role` shape is retained for the UI listings; `WORK_ROLES` carries
# the full v1 row.
ROLES: list[Role] = [
    Role("r-housekeeper", "Housekeeper"),
    Role("r-cook", "Cook"),
    Role("r-driver", "Driver"),
    Role("r-gardener", "Gardener"),
    Role("r-handyman", "Handyman"),
    Role("r-poolcare", "Pool care"),
]

WORK_ROLES: list[WorkRole] = [
    # Bernard workspace catalog (matches existing UI).
    WorkRole("r-housekeeper", "ws-bernard", "maid",        "Housekeeper", icon_glyph="broom"),
    WorkRole("r-cook",        "ws-bernard", "cook",        "Cook",        icon_glyph="flame"),
    WorkRole("r-driver",      "ws-bernard", "driver",      "Driver",      icon_glyph="car"),
    WorkRole("r-gardener",    "ws-bernard", "gardener",    "Gardener",    icon_glyph="sprout"),
    WorkRole("r-handyman",    "ws-bernard", "handyman",    "Handyman",    icon_glyph="wrench"),
    WorkRole("r-poolcare",    "ws-bernard", "pool_tech",   "Pool care",   icon_glyph="pool"),
    # VincentOps — Vincent's own workspace (just a driver for Rachid).
    WorkRole("wr-vincent-driver", "ws-vincent", "driver", "Driver", icon_glyph="car"),
    # CleanCo — serves many clients, exposes a maid role.
    WorkRole("wr-cleanco-maid",   "ws-cleanco", "maid",   "Maid",   icon_glyph="broom"),
]


def _caps(**overrides: bool | None) -> dict[str, bool | None]:
    base = {
        "time.clock_in": True,
        "tasks.photo_evidence": True,
        "tasks.allow_skip_with_reason": True,
        "messaging.comments": True,
        "messaging.report_issue": True,
        "inventory.consume_on_task": True,
        "expenses.submit": True,
        "expenses.photo_upload": True,
        "expenses.autofill_llm": True,
        "chat.assistant": False,
        "voice.assistant": False,
        "pwa.offline_queue": True,
        "notifications.email_digest": True,
    }
    base.update(overrides)
    return base


EMPLOYEES: list[Employee] = [
    Employee(
        "e-maria", "Maria Alvarez", ["Housekeeper"], ["p-villa-sud", "p-apt-3b"],
        "MA", "+33 6 12 34 56 78", "maria@example.com", date(2024, 3, 1),
        clocked_in_at=datetime(2026, 4, 15, 8, 12),
        capabilities=_caps(**{"chat.assistant": True}),
        workspaces=["ws-bernard"],
        villas=["p-villa-sud", "p-apt-3b"],
        clock_mode="auto", auto_clock_idle_minutes=30, language="fr",
        weekly_availability={
            "mon": ("08:00", "17:00"),
            "tue": ("08:00", "17:00"),
            "wed": None,
            "thu": ("08:00", "17:00"),
            "fri": ("08:00", "13:00"),
            "sat": None,
            "sun": None,
        },
        settings_override={
            "time.clock_mode": "auto",
            "time.auto_clock_idle_minutes": 30,
        },
        user_id="u-maria", work_engagement_id="we-maria-bernard", workspace_id="ws-bernard",
    ),
    Employee(
        "e-arun", "Arun Patel", ["Driver"], ["p-villa-sud"],
        "AP", "+33 6 22 45 67 89", "arun@example.com", date(2024, 9, 14),
        capabilities=_caps(**{"time.geofence_required": True}),
        workspaces=["ws-bernard"],
        villas=["p-villa-sud"],
        clock_mode="manual", language="en",
        settings_override={
            "time.geofence_required": True,
        },
        user_id="u-arun", work_engagement_id="we-arun-bernard", workspace_id="ws-bernard",
    ),
    Employee(
        "e-ben", "Ben Traoré", ["Gardener", "Pool care"], ["p-villa-sud"],
        "BT", "+33 6 33 56 78 90", "ben@example.com", date(2023, 5, 20),
        capabilities=_caps(),
        workspaces=["ws-bernard"],
        villas=["p-villa-sud"],
        clock_mode="auto", auto_clock_idle_minutes=20, language="fr",
        settings_override={
            "time.clock_mode": "auto",
            "time.auto_clock_idle_minutes": 20,
        },
        user_id="u-ben", work_engagement_id="we-ben-bernard", workspace_id="ws-bernard",
    ),
    Employee(
        "e-ana", "Ana Rossi", ["Housekeeper", "Cook"], ["p-apt-3b", "p-chalet"],
        "AR", "+33 6 44 67 89 01", "ana@example.com", date(2024, 11, 2),
        capabilities=_caps(**{"chat.assistant": True, "voice.assistant": True}),
        workspaces=["ws-bernard"],
        villas=["p-apt-3b", "p-chalet"],
        clock_mode="auto", auto_clock_idle_minutes=30, language="fr",
        settings_override={
            "time.clock_mode": "auto",
            "tasks.allow_skip_with_reason": False,
        },
        # CleanCo-supplied maid; billed to us by the supplier org, not by Ana.
        engagement_kind="agency_supplied",
        supplier_org_id="org-cleanco",
        user_id="u-ana", work_engagement_id="we-ana-bernard", workspace_id="ws-bernard",
    ),
    Employee(
        "e-sam", "Sam Leclerc", ["Handyman"], ["p-villa-sud", "p-chalet"],
        "SL", "+33 6 55 78 90 12", "sam@example.com", date(2025, 1, 9),
        capabilities=_caps(),
        workspaces=["ws-bernard"],
        villas=["p-villa-sud", "p-chalet"],
        clock_mode="manual", language="fr",
        # Freelance handyman — quotes + vendor invoices, no payslip.
        engagement_kind="contractor",
        user_id="u-sam", work_engagement_id="we-sam-bernard", workspace_id="ws-bernard",
    ),
    # ── Vincent scenario (§05 example) ───────────────────────────────
    # Rachid works only in VincentOps; Joselyn and Julie only in AgencyOps.
    # These rows make the new identity model visible in the existing
    # "Employees" UI while their canonical truth is in
    # USERS / WORK_ENGAGEMENTS / USER_WORK_ROLES below.
    Employee(
        "e-rachid", "Rachid Haddad", ["Driver"], ["p-villa-lac", "p-seaside"],
        "RH", "+33 6 88 11 22 33", "rachid@example.com", date(2024, 6, 1),
        capabilities=_caps(),
        workspaces=["ws-vincent"],
        villas=["p-villa-lac", "p-seaside"],
        clock_mode="manual", language="fr",
        user_id="u-rachid", work_engagement_id="we-rachid-vincent", workspace_id="ws-vincent",
    ),
    Employee(
        "e-joselyn", "Joselyn Rivera", ["Housekeeper"], ["p-villa-lac"],
        "JR", "+33 6 99 22 33 44", "joselyn@example.com", date(2025, 2, 1),
        capabilities=_caps(**{"chat.assistant": True}),
        workspaces=["ws-cleanco"],
        villas=["p-villa-lac"],
        clock_mode="auto", auto_clock_idle_minutes=25, language="es",
        user_id="u-joselyn", work_engagement_id="we-joselyn-cleanco", workspace_id="ws-cleanco",
    ),
    Employee(
        "e-julie", "Julie Moreau", ["Housekeeper"], [],
        "JM", "+33 6 44 33 22 11", "julie@example.com", date(2023, 9, 1),
        capabilities=_caps(**{"chat.assistant": True}),
        workspaces=["ws-cleanco"],
        villas=[],
        clock_mode="manual", language="fr",
        user_id="u-julie", work_engagement_id="we-julie-cleanco", workspace_id="ws-cleanco",
    ),
]


# ── v1 canonical identity rows ──────────────────────────────────────

USERS: list[User] = [
    # Bernard-workspace humans (legacy demo).
    User("u-elodie",  "elodie.bernard@example.com",  "Élodie Bernard",  languages=["fr", "en"],
         preferred_locale="fr-FR", primary_workspace_id="ws-bernard",
         phone_e164="+33 6 11 22 33 44",
         agent_approval_mode="auto"),
    User("u-maria",   "maria@example.com",           "Maria Alvarez",   languages=["fr"],
         preferred_locale="fr-FR", primary_workspace_id="ws-bernard",
         phone_e164="+33 6 12 34 56 78",
         agent_approval_mode="strict"),
    User("u-arun",    "arun@example.com",            "Arun Patel",      languages=["en", "hi"],
         preferred_locale="en-GB", primary_workspace_id="ws-bernard",
         phone_e164="+33 6 22 45 67 89",
         agent_approval_mode="bypass"),
    User("u-ben",     "ben@example.com",             "Ben Traoré",      languages=["fr"],
         preferred_locale="fr-FR", primary_workspace_id="ws-bernard",
         phone_e164="+33 6 33 56 78 90"),
    User("u-ana",     "ana@example.com",             "Ana Rossi",       languages=["fr", "it"],
         preferred_locale="fr-FR", primary_workspace_id="ws-bernard",
         phone_e164="+33 6 44 67 89 01"),
    User("u-sam",     "sam@example.com",             "Sam Leclerc",     languages=["fr"],
         preferred_locale="fr-FR", primary_workspace_id="ws-bernard",
         phone_e164="+33 6 55 78 90 12"),
    # Vincent scenario.
    User("u-vincent", "vincent.dupont@example.com", "Vincent Dupont",   languages=["fr", "en"],
         preferred_locale="fr-FR", primary_workspace_id="ws-vincent",
         phone_e164="+33 6 77 88 99 00"),
    User("u-rachid",  "rachid@example.com",          "Rachid Haddad",   languages=["fr", "ar"],
         preferred_locale="fr-FR", primary_workspace_id="ws-vincent",
         phone_e164="+33 6 88 11 22 33"),
    User("u-joselyn", "joselyn@example.com",         "Joselyn Rivera",  languages=["es", "fr"],
         preferred_locale="es-ES", primary_workspace_id="ws-cleanco",
         phone_e164="+33 6 99 22 33 44"),
    User("u-julie",   "julie@example.com",           "Julie Moreau",    languages=["fr"],
         preferred_locale="fr-FR", primary_workspace_id="ws-cleanco",
         phone_e164="+33 6 44 33 22 11"),
]


ROLE_GRANTS: list[RoleGrant] = [
    # Élodie is the Bernard workspace's governance anchor (on the
    # manager surface + ``owners`` group; see PERMISSION_GROUP_MEMBERS
    # below).
    RoleGrant("rg-elodie-manager-bernard", "u-elodie", "workspace", "ws-bernard",
              "manager", started_on=date(2024, 1, 1)),
    # Maria, Arun, Ben, Ana, Sam hold worker grants on ws-bernard.
    RoleGrant("rg-maria-worker", "u-maria", "workspace", "ws-bernard",
              "worker", started_on=date(2024, 3, 1), granted_by_user_id="u-elodie"),
    RoleGrant("rg-arun-worker",  "u-arun",  "workspace", "ws-bernard",
              "worker", started_on=date(2024, 9, 14), granted_by_user_id="u-elodie"),
    RoleGrant("rg-ben-worker",   "u-ben",   "workspace", "ws-bernard",
              "worker", started_on=date(2023, 5, 20), granted_by_user_id="u-elodie"),
    RoleGrant("rg-ana-worker",   "u-ana",   "workspace", "ws-bernard",
              "worker", started_on=date(2024, 11, 2), granted_by_user_id="u-elodie"),
    RoleGrant("rg-sam-worker",   "u-sam",   "workspace", "ws-bernard",
              "worker", started_on=date(2025, 1, 9), granted_by_user_id="u-elodie"),

    # ── Vincent scenario (§05 example) ───────────────────────────────
    # Vincent runs his own workspace on the manager surface and is
    # the governance anchor for his billing entity (DupontFamily) —
    # placed on ``owners`` via PERMISSION_GROUP_MEMBERS below. He also
    # holds a CLIENT grant on CleanCo's AgencyOps workspace, narrowed
    # by binding_org_id so he only sees data billed to his own org.
    RoleGrant("rg-vincent-manager-vincent", "u-vincent", "workspace", "ws-vincent",
              "manager", started_on=date(2024, 1, 1)),
    RoleGrant("rg-vincent-manager-org", "u-vincent", "organization", "org-dupont-vincent",
              "manager", started_on=date(2024, 1, 1)),
    RoleGrant("rg-vincent-client-cleanco", "u-vincent", "workspace", "ws-cleanco",
              "client", binding_org_id="org-dupont-vincent",
              started_on=date(2024, 6, 1), granted_by_user_id="u-julie"),
    # Rachid is a worker in VincentOps only.
    RoleGrant("rg-rachid-worker", "u-rachid", "workspace", "ws-vincent",
              "worker", started_on=date(2024, 6, 1), granted_by_user_id="u-vincent"),
    # Julie manages CleanCo's AgencyOps workspace — also the
    # governance anchor there (see PERMISSION_GROUP_MEMBERS).
    RoleGrant("rg-julie-manager", "u-julie", "workspace", "ws-cleanco",
              "manager", started_on=date(2023, 9, 1)),
    # Joselyn works at AgencyOps.
    RoleGrant("rg-joselyn-worker", "u-joselyn", "workspace", "ws-cleanco",
              "worker", started_on=date(2025, 2, 1), granted_by_user_id="u-julie"),
]


# ── Permission model seed (§02, §05 action catalog) ─────────────────

PERMISSION_GROUPS: list[PermissionGroup] = [
    # Bernard workspace — system groups.
    PermissionGroup("pg-ws-bernard-owners",       "workspace", "ws-bernard",
                    "owners",       "Owners",        group_kind="system"),
    PermissionGroup("pg-ws-bernard-managers",     "workspace", "ws-bernard",
                    "managers",     "Managers",      group_kind="system", is_derived=True),
    PermissionGroup("pg-ws-bernard-all-workers",  "workspace", "ws-bernard",
                    "all_workers",  "All workers",   group_kind="system", is_derived=True),
    PermissionGroup("pg-ws-bernard-all-clients",  "workspace", "ws-bernard",
                    "all_clients",  "All clients",   group_kind="system", is_derived=True),
    # A user-defined group on Bernard to exercise the UI.
    PermissionGroup("pg-ws-bernard-family",       "workspace", "ws-bernard",
                    "family",       "Family",        group_kind="user",
                    description_md="Household members entitled to approve expenses and accept quotes."),

    # Vincent's workspace — system groups.
    PermissionGroup("pg-ws-vincent-owners",       "workspace", "ws-vincent",
                    "owners",       "Owners",        group_kind="system"),
    PermissionGroup("pg-ws-vincent-managers",     "workspace", "ws-vincent",
                    "managers",     "Managers",      group_kind="system", is_derived=True),
    PermissionGroup("pg-ws-vincent-all-workers",  "workspace", "ws-vincent",
                    "all_workers",  "All workers",   group_kind="system", is_derived=True),
    PermissionGroup("pg-ws-vincent-all-clients",  "workspace", "ws-vincent",
                    "all_clients",  "All clients",   group_kind="system", is_derived=True),

    # CleanCo agency workspace — system groups.
    PermissionGroup("pg-ws-cleanco-owners",       "workspace", "ws-cleanco",
                    "owners",       "Owners",        group_kind="system"),
    PermissionGroup("pg-ws-cleanco-managers",     "workspace", "ws-cleanco",
                    "managers",     "Managers",      group_kind="system", is_derived=True),
    PermissionGroup("pg-ws-cleanco-all-workers",  "workspace", "ws-cleanco",
                    "all_workers",  "All workers",   group_kind="system", is_derived=True),
    PermissionGroup("pg-ws-cleanco-all-clients",  "workspace", "ws-cleanco",
                    "all_clients",  "All clients",   group_kind="system", is_derived=True),
    # User-defined group: "Inspectors" empowered to approve vendor
    # invoices at CleanCo.
    PermissionGroup("pg-ws-cleanco-inspectors",   "workspace", "ws-cleanco",
                    "inspectors",   "Inspectors",    group_kind="user",
                    description_md="Trusted staff who may approve vendor invoices outside the standard manager set."),

    # Organization-scope system groups for DupontFamily.
    PermissionGroup("pg-org-dupont-owners",       "organization", "org-dupont-vincent",
                    "owners",       "Owners",        group_kind="system"),
    PermissionGroup("pg-org-dupont-managers",     "organization", "org-dupont-vincent",
                    "managers",     "Managers",      group_kind="system", is_derived=True),
]


PERMISSION_GROUP_MEMBERS: list[PermissionGroupMember] = [
    # Élodie is Bernard's owner anchor.
    PermissionGroupMember("pg-ws-bernard-owners", "u-elodie",
                          added_by_user_id=None,
                          added_at=datetime(2024, 1, 1, 9, 0)),
    # Élodie also placed a trusted co-manager in ``family``.
    PermissionGroupMember("pg-ws-bernard-family", "u-elodie",
                          added_by_user_id="u-elodie",
                          added_at=datetime(2024, 1, 1, 9, 5)),
    # Vincent anchors his own workspace and his billing organisation.
    PermissionGroupMember("pg-ws-vincent-owners", "u-vincent",
                          added_at=datetime(2024, 1, 1, 9, 0)),
    PermissionGroupMember("pg-org-dupont-owners", "u-vincent",
                          added_at=datetime(2024, 1, 1, 9, 0)),
    # Julie anchors CleanCo.
    PermissionGroupMember("pg-ws-cleanco-owners", "u-julie",
                          added_at=datetime(2023, 9, 1, 9, 0)),
    # Joselyn is an inspector at CleanCo (trusted to approve
    # vendor invoices at some properties).
    PermissionGroupMember("pg-ws-cleanco-inspectors", "u-joselyn",
                          added_by_user_id="u-julie",
                          added_at=datetime(2025, 3, 1, 10, 0)),
]


ACTION_CATALOG: list[ActionCatalogEntry] = [
    # Root-only governance actions.
    ActionCatalogEntry("workspace.archive",               "Archive an entire workspace.",
                       ["workspace"],                [],                           root_only=True,  spec="§15"),
    ActionCatalogEntry("organization.archive",            "Archive an organization.",
                       ["organization"],             [],                           root_only=True,  spec="§22"),
    ActionCatalogEntry("scope.transfer",                  "Transfer governance to another user.",
                       ["workspace", "organization"],[],                           root_only=True,  spec="§15"),
    ActionCatalogEntry("permissions.edit_rules",          "Create, revoke, or edit permission rules on this scope.",
                       ["workspace", "property", "organization"], [],              root_only=True,  spec="§02"),
    ActionCatalogEntry("groups.manage_owners_membership", "Add or remove members of the owners group.",
                       ["workspace", "organization"],[],                           root_only=True,  spec="§02"),
    ActionCatalogEntry("admin.purge",                     "Hard-delete workspace data.",
                       ["workspace"],                [],                           root_only=True,  spec="§13"),

    # Rule-driven actions with sane defaults.
    ActionCatalogEntry("scope.view",                      "See that the scope exists (RLS still filters rows).",
                       ["workspace", "property", "organization"],
                       ["owners", "managers", "all_workers", "all_clients"], root_protected_deny=True, spec="§14"),
    ActionCatalogEntry("scope.edit_settings",             "Edit workspace / property / org settings.",
                       ["workspace", "property", "organization"],
                       ["owners", "managers"], root_protected_deny=True, spec="§02"),
    ActionCatalogEntry("users.invite",                    "Invite a new user to a scope.",
                       ["workspace", "property", "organization"],
                       ["owners", "managers"], root_protected_deny=True, spec="§03"),
    ActionCatalogEntry("users.archive",                   "Archive a user deployment-wide.",
                       ["workspace", "property", "organization"],
                       ["owners", "managers"], root_protected_deny=True, spec="§05"),
    ActionCatalogEntry("users.edit_profile_other",        "Edit another user's profile.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§02"),
    ActionCatalogEntry("users.reissue_magic_link",        "Re-issue a magic link to another user.",
                       ["workspace", "property", "organization"],
                       ["owners", "managers"], root_protected_deny=True, spec="§03"),
    ActionCatalogEntry("role_grants.create",              "Create a surface grant.",
                       ["workspace", "property", "organization"],
                       ["owners", "managers"], root_protected_deny=True, spec="§02"),
    ActionCatalogEntry("role_grants.revoke",              "Revoke a surface grant.",
                       ["workspace", "property", "organization"],
                       ["owners", "managers"], root_protected_deny=True, spec="§02"),
    ActionCatalogEntry("groups.create",                   "Create a user-defined permission group.",
                       ["workspace", "organization"],
                       ["owners", "managers"], root_protected_deny=True, spec="§02"),
    ActionCatalogEntry("groups.edit",                     "Edit or delete a user-defined group.",
                       ["workspace", "organization"],
                       ["owners", "managers"], spec="§02"),
    ActionCatalogEntry("groups.manage_members",           "Add / remove members of a non-owners group.",
                       ["workspace", "organization"],
                       ["owners"], root_protected_deny=True, spec="§02"),
    ActionCatalogEntry("properties.create",               "Create a property.",
                       ["workspace"],
                       ["owners", "managers"], spec="§04"),
    ActionCatalogEntry("properties.archive",              "Archive a property.",
                       ["workspace", "property"],
                       ["owners", "managers"], root_protected_deny=True, spec="§04"),
    ActionCatalogEntry("properties.edit",                 "Edit a property.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§04"),
    ActionCatalogEntry("properties.view_access_codes",    "View access codes / wifi / door codes.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§04"),
    ActionCatalogEntry("work_roles.manage",               "Edit the workspace's work-role catalog.",
                       ["workspace"],
                       ["owners", "managers"], spec="§05"),
    ActionCatalogEntry("tasks.create",                    "Create a task or template.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§06"),
    ActionCatalogEntry("tasks.assign_other",              "Assign a task to someone other than self.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§06"),
    ActionCatalogEntry("tasks.complete_other",            "Complete a task on behalf of another user.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§06"),
    ActionCatalogEntry("tasks.skip_other",                "Skip a task on behalf of another user.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§06"),
    ActionCatalogEntry("shifts.view_other",               "View another user's shifts.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§09"),
    ActionCatalogEntry("shifts.edit_other",               "Edit another user's shifts.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§09"),
    ActionCatalogEntry("payroll.lock_period",             "Lock a pay period.",
                       ["workspace"],
                       ["owners", "managers"], root_protected_deny=True, spec="§09"),
    ActionCatalogEntry("payroll.issue_payslip",           "Issue a payslip.",
                       ["workspace"],
                       ["owners", "managers"], root_protected_deny=True, spec="§09"),
    ActionCatalogEntry("payroll.view_other",              "View another user's payslips.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§09"),
    ActionCatalogEntry("pay_rules.edit",                  "Edit a pay rule.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§09"),
    ActionCatalogEntry("expenses.submit",                 "Submit an expense claim.",
                       ["workspace", "property"],
                       ["owners", "managers", "all_workers"], spec="§09"),
    ActionCatalogEntry("expenses.approve",                "Approve or reject an expense claim.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§09"),
    ActionCatalogEntry("expenses.reimburse",              "Mark an expense claim reimbursed.",
                       ["workspace"],
                       ["owners", "managers"], root_protected_deny=True, spec="§09"),
    ActionCatalogEntry("inventory.adjust",                "Manually adjust stock levels.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§08"),
    ActionCatalogEntry("instructions.edit",               "Edit an SOP / instruction.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§07"),
    ActionCatalogEntry("assets.edit",                     "Create or edit an asset.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§21"),
    ActionCatalogEntry("api_tokens.manage",               "Create, rotate, or revoke API tokens.",
                       ["workspace"],
                       ["owners", "managers"], root_protected_deny=True, spec="§03"),
    ActionCatalogEntry("audit_log.view",                  "View the audit log.",
                       ["workspace", "property", "organization"],
                       ["owners", "managers"], root_protected_deny=True, spec="§02"),
    ActionCatalogEntry("organizations.create",            "Create a client or supplier organization.",
                       ["workspace"],
                       ["owners", "managers"], spec="§22"),
    ActionCatalogEntry("organizations.edit",              "Edit an organization record.",
                       ["workspace", "organization"],
                       ["owners", "managers"], spec="§22"),
    ActionCatalogEntry("organizations.edit_pay_destination",
                       "Edit an organization's default payout destination.",
                       ["workspace", "organization"],
                       ["owners", "managers"], root_protected_deny=True, spec="§22"),
    ActionCatalogEntry("work_orders.view",                "View work orders on this scope.",
                       ["workspace", "property"],
                       ["owners", "managers", "all_workers", "all_clients"], spec="§22"),
    ActionCatalogEntry("work_orders.create",              "Create a work order.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§22"),
    ActionCatalogEntry("work_orders.assign_contractor",   "Assign a contractor to a work order.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§22"),
    ActionCatalogEntry("quotes.submit",                   "Submit a quote on a work order.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§22"),
    ActionCatalogEntry("quotes.accept",                   "Accept a quote.",
                       ["workspace", "property"],
                       ["owners", "managers", "all_clients"], spec="§22"),
    ActionCatalogEntry("vendor_invoices.submit",          "Submit a vendor invoice.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§22"),
    ActionCatalogEntry("vendor_invoices.approve",         "Approve a vendor invoice for payment.",
                       ["workspace", "property"],
                       ["owners", "managers"], root_protected_deny=True, spec="§22"),
    ActionCatalogEntry("vendor_invoices.approve_as_client",
                       "Client-side acceptance of a vendor invoice.",
                       ["workspace", "property"],
                       ["all_clients"], spec="§22"),
    ActionCatalogEntry("messaging.comments.author_global","Comment on tasks outside your assignment.",
                       ["workspace", "property"],
                       ["owners", "managers", "all_workers"], spec="§10"),
    ActionCatalogEntry("messaging.report_issue.triage",   "Triage a reported issue.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§10"),
]


PERMISSION_RULES: list[PermissionRule] = [
    # Bernard workspace: the "family" group is the only one permitted
    # to approve expenses — override the default "owners, managers"
    # with a broader rule plus a deny on the default allow set.
    PermissionRule("pr-bernard-exp-family", "workspace", "ws-bernard",
                   "expenses.approve", "group", "pg-ws-bernard-family", "allow",
                   created_by_user_id="u-elodie",
                   created_at=datetime(2026, 2, 1, 9, 30)),
    # CleanCo: allow a specific inspector (Joselyn) to approve vendor
    # invoices only at Villa du Lac — widens the workspace default
    # for that property.
    PermissionRule("pr-cleanco-prop-vdl-inv-joselyn", "property", "prop-villa-lac",
                   "vendor_invoices.approve", "user", "u-joselyn", "allow",
                   created_by_user_id="u-julie",
                   created_at=datetime(2026, 3, 15, 14, 0)),
    # Vincent: explicitly deny anyone in ``managers`` (no one yet, but
    # forward-compatibility) from touching organization pay
    # destinations on DupontFamily — owners-only even though the
    # catalog default already includes managers.
    PermissionRule("pr-dupont-org-paydest-mgr-deny", "organization", "org-dupont-vincent",
                   "organizations.edit_pay_destination", "group", "pg-org-dupont-managers", "deny",
                   created_by_user_id="u-vincent",
                   created_at=datetime(2026, 4, 1, 10, 0)),
]


WORK_ENGAGEMENTS: list[WorkEngagement] = [
    # Bernard workspace — five engagements (legacy demo).
    WorkEngagement("we-maria-bernard", "u-maria", "ws-bernard",
                   engagement_kind="payroll", started_on=date(2024, 3, 1)),
    WorkEngagement("we-arun-bernard", "u-arun", "ws-bernard",
                   engagement_kind="payroll", started_on=date(2024, 9, 14)),
    WorkEngagement("we-ben-bernard", "u-ben", "ws-bernard",
                   engagement_kind="payroll", started_on=date(2023, 5, 20)),
    WorkEngagement("we-ana-bernard", "u-ana", "ws-bernard",
                   engagement_kind="agency_supplied", supplier_org_id="org-cleanco",
                   started_on=date(2024, 11, 2)),
    WorkEngagement("we-sam-bernard", "u-sam", "ws-bernard",
                   engagement_kind="contractor", started_on=date(2025, 1, 9)),
    # Vincent scenario.
    WorkEngagement("we-rachid-vincent", "u-rachid", "ws-vincent",
                   engagement_kind="payroll", started_on=date(2024, 6, 1)),
    WorkEngagement("we-joselyn-cleanco", "u-joselyn", "ws-cleanco",
                   engagement_kind="payroll", started_on=date(2025, 2, 1)),
    WorkEngagement("we-julie-cleanco", "u-julie", "ws-cleanco",
                   engagement_kind="payroll", started_on=date(2023, 9, 1)),
]


USER_WORK_ROLES: list[UserWorkRole] = [
    # Bernard workspace.
    UserWorkRole("uwr-maria-housekeeper", "u-maria", "ws-bernard",
                 "r-housekeeper", started_on=date(2024, 3, 1)),
    UserWorkRole("uwr-arun-driver", "u-arun", "ws-bernard",
                 "r-driver", started_on=date(2024, 9, 14)),
    UserWorkRole("uwr-ben-gardener", "u-ben", "ws-bernard",
                 "r-gardener", started_on=date(2023, 5, 20)),
    UserWorkRole("uwr-ben-poolcare", "u-ben", "ws-bernard",
                 "r-poolcare", started_on=date(2023, 5, 20)),
    UserWorkRole("uwr-ana-housekeeper", "u-ana", "ws-bernard",
                 "r-housekeeper", started_on=date(2024, 11, 2)),
    UserWorkRole("uwr-ana-cook", "u-ana", "ws-bernard",
                 "r-cook", started_on=date(2024, 11, 2)),
    UserWorkRole("uwr-sam-handyman", "u-sam", "ws-bernard",
                 "r-handyman", started_on=date(2025, 1, 9)),
    # Vincent scenario.
    UserWorkRole("uwr-rachid-driver", "u-rachid", "ws-vincent",
                 "wr-vincent-driver", started_on=date(2024, 6, 1)),
    UserWorkRole("uwr-joselyn-maid", "u-joselyn", "ws-cleanco",
                 "wr-cleanco-maid", started_on=date(2025, 2, 1)),
]


PROPERTY_WORK_ROLE_ASSIGNMENTS: list[PropertyWorkRoleAssignment] = [
    # Rachid drives for both Villa du Lac and Seaside Apt.
    PropertyWorkRoleAssignment("pwra-rachid-lac",     "uwr-rachid-driver", "p-villa-lac"),
    PropertyWorkRoleAssignment("pwra-rachid-seaside", "uwr-rachid-driver", "p-seaside"),
    # Joselyn is CleanCo's maid at Villa du Lac only (she has other
    # CleanCo clients too, but they are out of scope for the mock).
    PropertyWorkRoleAssignment("pwra-joselyn-lac",    "uwr-joselyn-maid",  "p-villa-lac"),
]


PROPERTY_WORKSPACES: list[PropertyWorkspace] = [
    # Bernard — each property is home-scoped to ws-bernard.
    PropertyWorkspace("p-villa-sud", "ws-bernard", "owner_workspace",
                      added_by_user_id="u-elodie"),
    PropertyWorkspace("p-apt-3b",    "ws-bernard", "owner_workspace",
                      added_by_user_id="u-elodie"),
    PropertyWorkspace("p-chalet",    "ws-bernard", "owner_workspace",
                      added_by_user_id="u-elodie"),
    # Vincent scenario — Villa du Lac belongs to both VincentOps (owner)
    # and AgencyOps (managed). Seaside Apt only to VincentOps.
    PropertyWorkspace("p-villa-lac", "ws-vincent", "owner_workspace",
                      added_by_user_id="u-vincent"),
    PropertyWorkspace("p-villa-lac", "ws-cleanco", "managed_workspace",
                      added_by_user_id="u-vincent"),
    PropertyWorkspace("p-seaside",   "ws-vincent", "owner_workspace",
                      added_by_user_id="u-vincent"),
]


USER_WORKSPACES: list[UserWorkspace] = [
    # Derived junction; real system recomputes this from ROLE_GRANTS +
    # WORK_ENGAGEMENTS + PROPERTY_WORKSPACES. Seeded here for the UI.
    UserWorkspace("u-elodie",  "ws-bernard", "workspace_grant"),
    UserWorkspace("u-maria",   "ws-bernard", "workspace_grant"),
    UserWorkspace("u-arun",    "ws-bernard", "workspace_grant"),
    UserWorkspace("u-ben",     "ws-bernard", "workspace_grant"),
    UserWorkspace("u-ana",     "ws-bernard", "workspace_grant"),
    UserWorkspace("u-sam",     "ws-bernard", "workspace_grant"),
    UserWorkspace("u-vincent", "ws-vincent", "workspace_grant"),
    UserWorkspace("u-vincent", "ws-cleanco", "workspace_grant"),  # client grant
    UserWorkspace("u-rachid",  "ws-vincent", "workspace_grant"),
    UserWorkspace("u-joselyn", "ws-cleanco", "workspace_grant"),
    UserWorkspace("u-julie",   "ws-cleanco", "workspace_grant"),
]

STAYS: list[Stay] = [
    Stay("s-1", "p-villa-sud", "Johnson family",   "airbnb",       date(2026, 4, 13), date(2026, 4, 16), 4, "in_house"),
    Stay("s-2", "p-villa-sud", "Park couple",      "vrbo",         date(2026, 4, 17), date(2026, 4, 22), 2),
    Stay("s-3", "p-apt-3b",    "Nakamura",         "airbnb",       date(2026, 4, 15), date(2026, 4, 18), 2, "in_house"),
    Stay("s-4", "p-chalet",    "Müller family",    "manual",       date(2026, 4, 19), date(2026, 4, 26), 6),
    Stay("s-5", "p-apt-3b",    "Svensson",         "booking",      date(2026, 4, 24), date(2026, 4, 28), 3, "tentative"),
]


def _t(h: int, m: int = 0, day: int = 15) -> datetime:
    return datetime.combine(date(2026, 4, day), time(h, m))


TASKS: list[Task] = [
    Task(
        "t-1", "Pool check & chlorine", "p-villa-sud", "Pool",
        "e-ben", _t(9, 0), 30, "normal", "completed",
        photo_evidence="optional",
        instructions_ids=["i-pool-chem"],
        schedule_id="sch-pool-sat",
        asset_id="a-villa-pool-pump",
        checklist=[
            {"label": "Skim surface", "done": True},
            {"label": "Check pH (7.2–7.6)", "done": True},
            {"label": "Check chlorine (1–3 ppm)", "done": True},
            {"label": "Empty skimmer baskets", "done": True},
        ],
    ),
    Task(
        "t-2", "Change linen — master bedroom", "p-villa-sud", "Master bedroom",
        "e-maria", _t(10, 30), 25, "high", "in_progress",
        photo_evidence="required",
        instructions_ids=["i-linen", "i-villa-house"],
        template_id="tpl-linen-change",
        checklist=[
            {"label": "Strip bed", "done": True, "guest_visible": False},
            {"label": "Fresh sheets from cupboard A", "done": False, "guest_visible": False},
            {"label": "Replace towels", "done": False, "guest_visible": False},
            {"label": "Photo of finished bed", "done": False, "guest_visible": False},
        ],
    ),
    Task(
        "t-3", "Kitchen deep clean", "p-villa-sud", "Kitchen",
        "e-maria", _t(11, 30), 45, "normal", "pending",
        photo_evidence="disabled",
        instructions_ids=["i-kitchen-deep"],
        asset_id="a-villa-oven",
        checklist=[
            {"label": "Wipe surfaces", "done": False},
            {"label": "Degrease hood filter", "done": False},
            {"label": "Sort fridge — toss expired", "done": False},
            {"label": "Run dishwasher", "done": False},
        ],
    ),
    Task(
        "t-4", "Airport pickup — Johnson family", "p-villa-sud", "Transport",
        "e-arun", _t(14, 0), 90, "high", "pending",
        photo_evidence="disabled",
        instructions_ids=["i-airport"],
    ),
    Task(
        "t-5", "Turnover — Apt 3B", "p-apt-3b", "Full unit",
        "e-ana", _t(12, 0, day=18), 120, "high", "pending",
        photo_evidence="required",
        instructions_ids=["i-turnover", "i-apt-welcome"],
        template_id="tpl-turnover",
        turnover_bundle_id="tb-apt-3b-18",
        checklist=[
            {"label": "Strip all beds", "done": False, "guest_visible": False},
            {"label": "Bathrooms (2)", "done": False, "guest_visible": False},
            {"label": "Kitchen reset", "done": False, "guest_visible": False},
            {"label": "Restock welcome basket", "done": False, "guest_visible": False},
            {"label": "Close windows, set thermostat 19°C", "done": False, "guest_visible": False},
            {"label": "Run the dishwasher before you leave", "done": False, "guest_visible": True},
            {"label": "Take out any trash", "done": False, "guest_visible": True},
            {"label": "Leave the keys in the lockbox", "done": False, "guest_visible": True},
        ],
    ),
    Task(
        "t-6", "Water the entryway flowers", "p-villa-sud", "Entryway",
        "e-ben", _t(16, 0), 10, "low", "pending",
        photo_evidence="disabled",
    ),
    Task(
        "t-7", "Fix loose cupboard handle (kitchen)", "p-chalet", "Kitchen",
        "e-sam", _t(15, 0, day=16), 20, "normal", "pending",
        photo_evidence="optional",
    ),
    Task(
        "t-8", "Restock welcome basket — Apt 3B", "p-apt-3b", "Kitchen",
        "e-ana", _t(10, 0, day=23), 30, "normal", "scheduled",
        photo_evidence="optional",
        instructions_ids=["i-apt-welcome"],
        template_id="tpl-turnover",
    ),
    Task(
        "t-9", "Garden hedge trimming", "p-villa-sud", "Garden",
        "e-ben", _t(9, 0, day=12), 60, "low", "cancelled",
        photo_evidence="disabled",
    ),
    Task(
        "t-10", "Replace entryway light bulb", "p-villa-sud", "Entryway",
        "e-sam", _t(11, 0, day=14), 15, "normal", "overdue",
        photo_evidence="disabled",
    ),
]


EXPENSES: list[Expense] = [
    Expense("x-1", "e-maria", 4280, "EUR", "Carrefour",   datetime(2026, 4, 14, 17, 32), "submitted", "Cleaning supplies — bleach, sponges, 2× fresh towels", ocr_confidence=0.96, category="supplies",
            user_id="u-maria", work_engagement_id="we-maria-bernard"),
    Expense("x-2", "e-arun",  1890, "EUR", "Total Energies", datetime(2026, 4, 13, 19, 5), "approved", "Fuel — Johnson airport run", ocr_confidence=0.99, category="fuel",
            user_id="u-arun", work_engagement_id="we-arun-bernard"),
    Expense("x-3", "e-ben",  12500, "EUR", "Pool Pro",    datetime(2026, 4, 10, 11, 22), "submitted", "Chlorine tablets (3 month supply) + replacement skimmer basket", ocr_confidence=0.94, category="maintenance",
            user_id="u-ben", work_engagement_id="we-ben-bernard"),
    Expense("x-4", "e-ana",   2210, "EUR", "Marché Provence", datetime(2026, 4, 11, 9, 40), "approved", "Welcome-basket groceries — Apt 3B", category="food",
            user_id="u-ana", work_engagement_id="we-ana-bernard"),
    Expense("x-5", "e-sam",   5780, "EUR", "Brico Dépôt", datetime(2026, 4, 9, 14, 58), "reimbursed", "Door handles, screws, wood filler", category="maintenance",
            user_id="u-sam", work_engagement_id="we-sam-bernard"),
]


APPROVALS: list[ApprovalRequest] = [
    ApprovalRequest(
        "a-1", "digest-agent", "tasks.reassign",
        "Pool check (Villa Sud) → Sam Leclerc",
        "Ben Traoré is on approved leave 18–21 Apr; Sam is the configured backup for pool care.",
        datetime(2026, 4, 15, 9, 47), "low",
        diff=["assignee: e-ben → e-sam", "note appended: 'auto-reassigned: covering leave'"],
        gate_source="user_auto_annotation",
        gate_destination="inline_chat",
        inline_channel="web_owner_sidebar",
        card_summary="Reassign pool check at Villa Sud from Ben to Sam?",
        card_fields=[
            ("task", "Pool check — Villa Sud"),
            ("new assignee", "Sam Leclerc"),
            ("reason", "Ben on leave 18–21 Apr"),
        ],
        for_user_id="u-elodie",
        resolved_user_mode="auto",
    ),
    ApprovalRequest(
        "a-2", "payroll-agent", "payroll.issue",
        "April payslips — 5 employees",
        "Monthly pay run. Totals within 4% of last month. No open shifts.",
        datetime(2026, 4, 15, 8, 2), "medium",
        diff=["5× payslip draft → issued", "period 2026-04: locked → paid (on last payslip pay)"],
        gate_source="workspace_configurable",
        gate_destination="desk",
        inline_channel="desk_only",
        card_summary="Issue April payslips for 5 employees?",
        card_fields=[
            ("period", "2026-04"),
            ("payslip count", "5"),
            ("total gross", "€12 480"),
        ],
    ),
    ApprovalRequest(
        "a-3", "procurement-agent", "expenses.agent_purchase",
        "Dyson V11 vacuum · €449 · delivered to Villa Sud",
        "Current vacuum flagged by Maria with photo; motor burned. Budget remaining €820.",
        datetime(2026, 4, 14, 16, 18), "medium",
        diff=["create expense: €449 EUR to Brico Dépôt", "attach issue #iss-3 as justification"],
        gate_source="user_auto_annotation",
        gate_destination="inline_chat",
        inline_channel="web_owner_sidebar",
        card_summary="Create expense Brico Dépôt for €449.00?",
        card_fields=[
            ("vendor", "Brico Dépôt"),
            ("amount", "€449.00"),
            ("property", "Villa Sud"),
            ("category", "equipment"),
        ],
        for_user_id="u-elodie",
        resolved_user_mode="auto",
    ),
]


LEAVES: list[Leave] = [
    Leave("lv-1", "e-ben",   date(2026, 4, 18), date(2026, 4, 21), "personal",  "Family visit — Bordeaux",
          approved_at=datetime(2026, 4, 3, 11, 0),
          user_id="u-ben", decided_by_user_id="u-elodie"),
    Leave("lv-2", "e-ana",   date(2026, 5, 1),  date(2026, 5, 3),  "vacation",  "Long weekend",
          user_id="u-ana"),
    Leave("lv-3", "e-sam",   date(2026, 4, 22), date(2026, 4, 22), "sick",      "Migraine — will try to make Thursday",
          user_id="u-sam"),
    Leave("lv-4", "e-arun",  date(2026, 6, 15), date(2026, 6, 29), "vacation",  "Annual trip home — India",
          approved_at=datetime(2026, 2, 20, 10, 15),
          user_id="u-arun", decided_by_user_id="u-elodie"),
]


CLOSURES: list[PropertyClosure] = [
    PropertyClosure("cl-1", "p-chalet",    date(2026, 4, 10), date(2026, 4, 18), "seasonal",         "Between ski and summer seasons"),
    PropertyClosure("cl-2", "p-villa-sud", date(2026, 4, 22), date(2026, 4, 23), "renovation",       "Painter in for touch-ups"),
    PropertyClosure("cl-3", "p-apt-3b",    date(2026, 4, 29), date(2026, 4, 30), "ical_unavailable", "Imported from Airbnb — blocked window"),
]


TEMPLATES: list[TaskTemplate] = [
    TaskTemplate("tpl-turnover", "Standard turnover (STR)",
                 "End-of-stay cleaning + reset for a short-term rental unit.", "Housekeeper",
                 120, "listed", "required", "high",
                 checklist=[
                     {"label": "Strip all beds", "guest_visible": False},
                     {"label": "Bathrooms", "guest_visible": False},
                     {"label": "Kitchen reset", "guest_visible": False},
                     {"label": "Restock welcome basket", "guest_visible": False},
                     {"label": "Trash out", "guest_visible": True},
                     {"label": "Dishwasher on", "guest_visible": True},
                 ]),
    TaskTemplate("tpl-linen-change", "Linen change — master bedroom",
                 "Swap bedding and towels, including fitted sheet orientation.", "Housekeeper",
                 25, "any", "required", "normal",
                 checklist=[{"label": "Strip bed"}, {"label": "Fresh sheets"}, {"label": "Replace towels"}, {"label": "Photo of finished bed"}]),
    TaskTemplate("tpl-pool-weekly", "Pool service — weekly",
                 "Skim, test pH and chlorine, check skimmer baskets.", "Pool care",
                 30, "one", "optional", "normal",
                 checklist=[{"label": "Skim"}, {"label": "pH"}, {"label": "Chlorine"}, {"label": "Skimmer"}]),
    TaskTemplate("tpl-airport", "Airport pickup / drop-off",
                 "Standard guest transfer. Sign with family name at arrivals.", "Driver",
                 90, "any", "disabled", "high"),
    TaskTemplate("tpl-garden", "Garden upkeep", "Mow, trim, water — as needed.", "Gardener",
                 60, "one", "optional", "low"),
]


SCHEDULES: list[Schedule] = [
    Schedule("sch-pool-sat", "Villa Sud pool — Saturdays 09:00", "tpl-pool-weekly",
             "p-villa-sud", "Every Saturday at 09:00", "e-ben", 30, date(2024, 4, 1)),
    Schedule("sch-linen-mon-thu", "Villa Sud linen — Mon & Thu 10:30", "tpl-linen-change",
             "p-villa-sud", "Weekly on Mon, Thu at 10:30", "e-maria", 25, date(2024, 3, 1)),
    Schedule("sch-garden-sat", "Villa Sud garden — Saturdays 08:00", "tpl-garden",
             "p-villa-sud", "Every Saturday at 08:00", "e-ben", 60, date(2024, 4, 1), paused=True),
    Schedule("sch-apt-turnover", "Apt 3B turnover (auto from stays)", "tpl-turnover",
             "p-apt-3b", "Triggered by stay check-out", "e-ana", 120, date(2025, 1, 1)),
]


INSTRUCTIONS: list[Instruction] = [
    Instruction("i-villa-house", "Villa Sud — house rules & quirks", "property", "p-villa-sud", None,
                ["house", "quirks"],
                "The front gate sticks; lift it half a centimetre while turning the key. "
                "Alarm panel in the entry closet — code on a sticky note inside the door (yes, really).",
                3, datetime(2026, 2, 14, 18, 22)),
    Instruction("i-linen", "Linen — fitted sheets & folding", "global", None, None,
                ["housekeeping"],
                "Fitted sheet goes stripe-side up. Pillow cases open away from the door. "
                "Match duvet insert to cover by the sewn-in label — they're not interchangeable.",
                2, datetime(2026, 3, 2, 9, 10)),
    Instruction("i-pool-chem", "Pool — chemistry targets", "area", "p-villa-sud", "Pool",
                ["safety", "pool"],
                "Target pH 7.2–7.6; chlorine 1–3 ppm. Shock only at dusk. "
                "Do NOT mix cal-hypo with tri-chlor — separate containers, separate days.",
                1, datetime(2025, 11, 10, 14, 0)),
    Instruction("i-kitchen-deep", "Kitchen deep clean — monthly targets", "area", "p-villa-sud", "Kitchen",
                ["housekeeping"],
                "Pull out the oven every four weeks and wipe behind. Degrease the hood filter "
                "in a bucket of hot water + dish soap; let it drip-dry before reinstalling.",
                1, datetime(2025, 12, 1, 10, 0)),
    Instruction("i-airport", "Airport pickup protocol", "global", None, None,
                ["transport"],
                "Terminal 2F arrivals. Hold a sign with the family name. Bottled water in the "
                "cupholders. Check that the A/C is actually on — not just set.",
                2, datetime(2026, 1, 8, 12, 30)),
    Instruction("i-turnover", "Turnover — STR reset standard", "global", None, None,
                ["turnover"],
                "Three-towel stack per guest. Fresh flowers in the entryway. Thermostat to 19°C "
                "in winter / 22°C in summer. Last thing: test the wifi on your phone.",
                4, datetime(2026, 3, 28, 8, 5)),
    Instruction("i-apt-welcome", "Apt 3B — welcome basket", "property", "p-apt-3b", None,
                ["housekeeping"],
                "A small bottle of Bordeaux, two pâtisseries from the place on rue de Condé, "
                "and a handwritten card (cards are in the drawer of the console).",
                1, datetime(2026, 2, 3, 16, 0)),
]


INVENTORY: list[InventoryItem] = [
    InventoryItem("inv-1",  "p-villa-sud", "Bed sheet set (queen)", "LINEN-Q", 3,  6,  "sets", "Linen cupboard A"),
    InventoryItem("inv-2",  "p-villa-sud", "Bath towels (L)",       "TOWEL-L", 12, 16, "pcs",  "Linen cupboard A"),
    InventoryItem("inv-3",  "p-villa-sud", "Chlorine tablets",      "POOL-CL", 1,  2,  "box",  "Pool shed"),
    InventoryItem("inv-4",  "p-villa-sud", "Toilet paper",          "TP-12",   2,  4,  "pack", "Utility"),
    InventoryItem("inv-5",  "p-apt-3b",    "Bed sheet set (double)", "LINEN-D", 4, 4,  "sets", "Hall closet"),
    InventoryItem("inv-6",  "p-apt-3b",    "Coffee pods",           "COF-NESP", 24, 30, "pcs",  "Kitchen"),
    InventoryItem("inv-7",  "p-apt-3b",    "Welcome-basket wine",   "WINE-RED",  2, 3,  "btl",  "Kitchen"),
    InventoryItem("inv-8",  "p-chalet",    "Firewood",              "FW-STR",   0,  4,  "stère","Ski room"),
]


ISSUES: list[Issue] = [
    Issue("iss-1", "e-maria", "p-villa-sud", "Master bedroom",
          "normal", "broken", "Bedside lamp flickers",
          "The one on the left side. Bulb is fine — I swapped it. Wiring in the base, I think.",
          datetime(2026, 4, 14, 11, 32), "open"),
    Issue("iss-2", "e-arun", "p-villa-sud", "Transport",
          "low", "supplies", "Need a fresh air freshener for the car",
          "The current one's been in there since January. Guests don't say anything but it's past time.",
          datetime(2026, 4, 13, 20, 1), "open"),
    Issue("iss-3", "e-maria", "p-villa-sud", "Living room",
          "high", "broken", "Vacuum motor burnt out",
          "Smelled like hot plastic, then it stopped. I can still sweep today but we need a replacement.",
          datetime(2026, 4, 12, 15, 4), "in_progress"),
]


PAYSLIPS: list[PaySlip] = [
    PaySlip("ps-1", "e-maria", date(2026, 3, 1), date(2026, 3, 31), 240000, 6420, 246420, "paid", 168.5, 4.0,
            work_engagement_id="we-maria-bernard", user_id="u-maria"),
    PaySlip("ps-2", "e-arun",  date(2026, 3, 1), date(2026, 3, 31), 120000, 1890, 121890, "paid", 84.0, 0,
            work_engagement_id="we-arun-bernard", user_id="u-arun"),
    PaySlip("ps-3", "e-ben",   date(2026, 3, 1), date(2026, 3, 31), 180000, 0,    180000, "paid", 104.0, 2.0,
            work_engagement_id="we-ben-bernard", user_id="u-ben"),
    PaySlip("ps-4", "e-ana",   date(2026, 3, 1), date(2026, 3, 31), 210000, 2210, 212210, "paid", 142.0, 0,
            work_engagement_id="we-ana-bernard", user_id="u-ana"),
    PaySlip("ps-5", "e-sam",   date(2026, 3, 1), date(2026, 3, 31), 160000, 5780, 165780, "paid", 96.0, 0,
            work_engagement_id="we-sam-bernard", user_id="u-sam"),
    PaySlip("ps-6", "e-maria", date(2026, 4, 1), date(2026, 4, 30), 248000, 4280, 252280, "draft", 170.0, 6.0,
            work_engagement_id="we-maria-bernard", user_id="u-maria"),
    PaySlip("ps-7", "e-arun",  date(2026, 4, 1), date(2026, 4, 30), 118000, 0,    118000, "draft", 82.0, 0,
            work_engagement_id="we-arun-bernard", user_id="u-arun"),
    PaySlip("ps-8", "e-ben",   date(2026, 4, 1), date(2026, 4, 30), 170000, 12500, 182500, "draft", 98.0, 0,
            work_engagement_id="we-ben-bernard", user_id="u-ben"),
    PaySlip("ps-9", "e-ana",   date(2026, 4, 1), date(2026, 4, 30), 212000, 2210, 214210, "draft", 144.0, 1.0,
            work_engagement_id="we-ana-bernard", user_id="u-ana"),
    PaySlip("ps-10","e-sam",   date(2026, 4, 1), date(2026, 4, 30), 162000, 0,    162000, "draft", 97.0, 0,
            work_engagement_id="we-sam-bernard", user_id="u-sam"),
]


LLM_ASSIGNMENTS: list[ModelAssignment] = [
    ModelAssignment("tasks.nl_intake",    "Parse free-text into task/template/schedule drafts",      "openrouter", "google/gemma-4-31b-it", True,  1.50, 0.22,  18),
    ModelAssignment("tasks.assist",       "Staff chat assistant: explain an instruction, etc.",      "openrouter", "google/gemma-4-31b-it", True,  2.00, 0.41,  32),
    ModelAssignment("digest.manager",     "Morning manager digest composition",                      "openrouter", "anthropic/claude-haiku-4-5", True, 0.50, 0.08, 2),
    ModelAssignment("digest.employee",    "Morning employee digest composition",                     "openrouter", "google/gemma-4-31b-it", True,  0.50, 0.10,  5),
    ModelAssignment("anomaly.detect",     "Compare recent completions to schedule and flag issues",  "openrouter", "google/gemma-4-31b-it", True,  0.75, 0.00,  0),
    ModelAssignment("expenses.autofill",  "OCR + structure a receipt image",                         "openrouter", "google/gemma-4-31b-it", True,  1.00, 0.31,  12),
    ModelAssignment("instructions.draft", "Suggest an instruction from a conversation",              "openrouter", "google/gemma-4-31b-it", True,  0.50, 0.02,  1),
    ModelAssignment("issue.triage",       "Classify severity/category of a reported issue",          "openrouter", "google/gemma-4-31b-it", True,  0.25, 0.01,  3),
    ModelAssignment("stay.summarize",     "Summarize a stay for a guest welcome blurb",              "openrouter", "google/gemma-4-31b-it", True,  0.25, 0.00,  0),
    ModelAssignment("voice.transcribe",   "Turn a voice note into text",                             "—",          "(unassigned)",           False, 0.00, 0.00,  0),
    ModelAssignment("chat.manager",      "Manager-side embedded agent (full manager tool surface)",  "openrouter", "google/gemma-4-31b-it", True,  3.00, 0.55,  14),
    ModelAssignment("chat.employee",     "Employee-side embedded agent (full employee tool surface)","openrouter", "google/gemma-4-31b-it", True,  2.00, 0.38,  28),
    ModelAssignment("chat.compact",      "Summarize resolved chat topics (hourly compaction)",       "openrouter", "google/gemma-4-31b-it", True,  0.50, 0.04,  2),
    ModelAssignment("chat.detect_language","Detect message language for auto-translation",           "openrouter", "google/gemma-4-31b-it", True,  0.25, 0.02,  8),
    ModelAssignment("chat.translate",    "Translate message to workspace default language",          "openrouter", "google/gemma-4-31b-it", True,  0.50, 0.06,  6),
]


LLM_CALLS: list[LLMCall] = [
    LLMCall(datetime(2026, 4, 15, 10, 6, 44), "tasks.assist",      "google/gemma-4-31b-it",        1240, 310, 1, 1820, "ok"),
    LLMCall(datetime(2026, 4, 15, 9, 47, 2),  "anomaly.detect",    "google/gemma-4-31b-it",        3100, 180, 2, 2100, "ok"),
    LLMCall(datetime(2026, 4, 15, 9, 12, 18), "digest.manager",    "anthropic/claude-haiku-4-5",   4800, 720, 3, 3400, "ok"),
    LLMCall(datetime(2026, 4, 15, 8, 54, 1),  "expenses.autofill", "google/gemma-4-31b-it",        980, 410, 1, 1950, "ok"),
    LLMCall(datetime(2026, 4, 15, 8, 41, 12), "expenses.autofill", "google/gemma-4-31b-it",        1100, 390, 1, 1720, "redacted_block"),
    LLMCall(datetime(2026, 4, 15, 8, 6, 30),  "issue.triage",      "google/gemma-4-31b-it",        620, 140, 0, 890,  "ok"),
]


AUDIT: list[AuditEntry] = [
    # v1 `actor_kind` ∈ {user, agent, system}; `actor_grant_role`
    # carries the grant under which the action was authorised.
    AuditEntry(datetime(2026, 4, 15, 10, 8, 12), "user",   "Élodie Bernard", "task.complete",          "t-1",  "web", None,
               actor_grant_role="manager", actor_was_owner_member=True, actor_action_key="tasks.complete_other", actor_id="u-elodie"),
    AuditEntry(datetime(2026, 4, 15, 9, 47, 2),  "agent",  "digest-agent",   "agent_action.requested", "a-1",  "api", "Auto-reassign pool coverage (Ben on leave)",
               agent_label="digest-agent"),
    AuditEntry(datetime(2026, 4, 15, 9, 41, 0),  "user",   "Maria Alvarez",  "shift.clock_in",         "sh-…", "web", None,
               actor_grant_role="worker", actor_id="u-maria"),
    AuditEntry(datetime(2026, 4, 15, 9, 12, 18), "agent",  "digest-agent",   "digest.sent",            "—",    "api", "Morning manager digest",
               agent_label="digest-agent"),
    AuditEntry(datetime(2026, 4, 15, 8, 54, 1),  "agent",  "procurement-agent", "expense.autofill",    "x-1",  "api", None,
               agent_label="procurement-agent"),
    AuditEntry(datetime(2026, 4, 15, 8, 41, 12), "system", "redaction-layer", "llm.call.blocked",      "—",    "worker", "IBAN-like string in receipt text"),
    AuditEntry(datetime(2026, 4, 15, 8, 12, 0),  "user",   "Maria Alvarez",  "shift.clock_in",         "sh-…", "web", None,
               actor_grant_role="worker", actor_id="u-maria"),
    AuditEntry(datetime(2026, 4, 14, 17, 32, 0), "user",   "Maria Alvarez",  "expense.submit",         "x-1",  "web", None,
               actor_grant_role="worker", actor_id="u-maria"),
    AuditEntry(datetime(2026, 4, 14, 16, 18, 0), "agent",  "procurement-agent", "agent_action.requested", "a-3","api", "Vacuum replacement",
               agent_label="procurement-agent"),
    AuditEntry(datetime(2026, 4, 14, 11, 32, 0), "user",   "Maria Alvarez",  "issue.open",             "iss-1","web", None,
               actor_grant_role="worker", actor_id="u-maria"),
    # Vincent scenario — role_grant activity.
    AuditEntry(datetime(2026, 4, 10, 14, 5, 0),  "user",   "Julie Moreau",   "role_grant.create",      "rg-vincent-client-cleanco", "web",
               "Invited Vincent as client (binding DupontFamily)",
               actor_grant_role="manager", actor_id="u-julie"),
    AuditEntry(datetime(2026, 4, 11, 9, 0, 0),   "user",   "Vincent Dupont", "user.first_passkey",     "u-vincent", "web", None,
               actor_id="u-vincent"),
    AuditEntry(datetime(2026, 4, 15, 19, 2, 0),  "user",   "Julie Moreau",   "vendor_invoice.submit",  "vi-3", "web", None,
               actor_grant_role="manager", actor_id="u-julie"),
]


WEBHOOKS: list[Webhook] = [
    Webhook("wh-1", "https://hooks.example.com/crewday/digest", ["digest.manager", "digest.employee"], True, 200, datetime(2026, 4, 15, 9, 12, 22)),
    Webhook("wh-2", "https://n8n.local/webhook/crewday-payroll", ["payroll.period_locked", "payroll.period_paid"], True, 200, datetime(2026, 3, 31, 22, 1, 0)),
    Webhook("wh-3", "https://slack.internal/hooks/T042/.../B08/...", ["approval.pending", "approval.decided"], True, 200, datetime(2026, 4, 15, 9, 47, 3)),
    Webhook("wh-4", "https://legacy.host/webhooks/tasks", ["task.completed"], False, 502, datetime(2026, 3, 20, 14, 55, 0)),
]


# ── Asset type catalog (18 pre-seeded) ──────────────────────────────

ASSET_TYPES: list[AssetType] = [
    AssetType("at-air-conditioner", "air_conditioner", "Air conditioner", "climate", "❄️", [
        {"key": "clean_filters", "label": "Clean filters", "interval_days": 90, "estimated_duration_minutes": 30},
        {"key": "service_unit", "label": "Annual service", "interval_days": 365, "estimated_duration_minutes": 120},
    ], default_lifespan_years=12),
    AssetType("at-oven-range", "oven_range", "Oven / range", "appliance", "🍳", [
        {"key": "deep_clean", "label": "Deep clean", "interval_days": 90, "estimated_duration_minutes": 45},
        {"key": "check_burners", "label": "Check burners", "interval_days": 365, "estimated_duration_minutes": 30},
    ], default_lifespan_years=15),
    AssetType("at-refrigerator", "refrigerator", "Refrigerator", "appliance", "🧊", [
        {"key": "clean_coils", "label": "Clean coils", "interval_days": 180, "estimated_duration_minutes": 30},
        {"key": "replace_water_filter", "label": "Replace water filter", "interval_days": 180, "estimated_duration_minutes": 15},
        {"key": "check_seals", "label": "Check door seals", "interval_days": 365, "estimated_duration_minutes": 15},
    ], default_lifespan_years=15),
    AssetType("at-dishwasher", "dishwasher", "Dishwasher", "appliance", "🍽️", [
        {"key": "clean_filter", "label": "Clean filter", "interval_days": 30, "estimated_duration_minutes": 15},
        {"key": "descale", "label": "Descale", "interval_days": 90, "estimated_duration_minutes": 20},
    ], default_lifespan_years=10),
    AssetType("at-washing-machine", "washing_machine", "Washing machine", "appliance", "👕", [
        {"key": "clean_drum", "label": "Clean drum", "interval_days": 30, "estimated_duration_minutes": 15},
        {"key": "check_hoses", "label": "Check hoses", "interval_days": 365, "estimated_duration_minutes": 20},
    ], default_lifespan_years=10),
    AssetType("at-dryer", "dryer", "Dryer", "appliance", "🌀", [
        {"key": "clean_vent", "label": "Clean vent", "interval_days": 90, "estimated_duration_minutes": 30},
        {"key": "inspect_duct", "label": "Inspect duct", "interval_days": 365, "estimated_duration_minutes": 30},
    ], default_lifespan_years=12),
    AssetType("at-water-heater", "water_heater", "Water heater", "climate", "🔥", [
        {"key": "flush_tank", "label": "Flush tank", "interval_days": 365, "estimated_duration_minutes": 60},
        {"key": "check_anode", "label": "Check anode rod", "interval_days": 730, "estimated_duration_minutes": 45},
    ], default_lifespan_years=12),
    AssetType("at-boiler", "boiler", "Boiler / furnace", "heating", "🏠", [
        {"key": "annual_service", "label": "Annual service", "interval_days": 365, "estimated_duration_minutes": 90},
        {"key": "bleed_radiators", "label": "Bleed radiators", "interval_days": 180, "estimated_duration_minutes": 45},
    ], default_lifespan_years=15),
    AssetType("at-pool-pump", "pool_pump", "Pool pump", "pool", "🏊", [
        {"key": "clean_basket", "label": "Clean basket", "interval_days": 7, "estimated_duration_minutes": 10},
        {"key": "inspect_seals", "label": "Inspect seals", "interval_days": 180, "estimated_duration_minutes": 20},
        {"key": "service_pump", "label": "Full service", "interval_days": 365, "estimated_duration_minutes": 120},
    ], default_lifespan_years=8),
    AssetType("at-pool-heater", "pool_heater", "Pool heater", "pool", "♨️", [
        {"key": "check_thermostat", "label": "Check thermostat", "interval_days": 30, "estimated_duration_minutes": 10},
        {"key": "annual_service", "label": "Annual service", "interval_days": 365, "estimated_duration_minutes": 90},
    ], default_lifespan_years=10),
    AssetType("at-smoke-detector", "smoke_detector", "Smoke detector", "safety", "🔔", [
        {"key": "test", "label": "Test alarm", "interval_days": 30, "estimated_duration_minutes": 5},
        {"key": "replace_battery", "label": "Replace battery", "interval_days": 365, "estimated_duration_minutes": 10},
    ], default_lifespan_years=10),
    AssetType("at-fire-extinguisher", "fire_extinguisher", "Fire extinguisher", "safety", "🧯", [
        {"key": "check_pressure", "label": "Check pressure", "interval_days": 30, "estimated_duration_minutes": 5},
        {"key": "annual_inspection", "label": "Annual inspection", "interval_days": 365, "estimated_duration_minutes": 15},
    ], default_lifespan_years=12),
    AssetType("at-generator", "generator", "Generator", "outdoor", "⚡", [
        {"key": "test_run", "label": "Test run", "interval_days": 30, "estimated_duration_minutes": 15},
        {"key": "oil_change", "label": "Oil change", "interval_days": 180, "estimated_duration_minutes": 30},
        {"key": "annual_service", "label": "Annual service", "interval_days": 365, "estimated_duration_minutes": 120},
    ], default_lifespan_years=20),
    AssetType("at-solar-panel", "solar_panel", "Solar panels", "outdoor", "☀️", [
        {"key": "clean_panels", "label": "Clean panels", "interval_days": 90, "estimated_duration_minutes": 60},
        {"key": "check_inverter", "label": "Check inverter", "interval_days": 30, "estimated_duration_minutes": 10},
    ], default_lifespan_years=25),
    AssetType("at-septic-tank", "septic_tank", "Septic tank", "plumbing", "🪠", [
        {"key": "pump_tank", "label": "Pump tank", "interval_days": 1095, "estimated_duration_minutes": 120},
        {"key": "inspection", "label": "Inspection", "interval_days": 365, "estimated_duration_minutes": 30},
    ], default_lifespan_years=30),
    AssetType("at-irrigation", "irrigation", "Irrigation system", "outdoor", "💧", [
        {"key": "winterize", "label": "Winterize", "interval_days": 365, "estimated_duration_minutes": 60},
        {"key": "inspect_heads", "label": "Inspect heads", "interval_days": 90, "estimated_duration_minutes": 30},
    ], default_lifespan_years=15),
    AssetType("at-alarm-system", "alarm_system", "Alarm / security", "security", "🔒", [
        {"key": "test_sensors", "label": "Test sensors", "interval_days": 90, "estimated_duration_minutes": 30},
        {"key": "replace_batteries", "label": "Replace batteries", "interval_days": 365, "estimated_duration_minutes": 20},
    ], default_lifespan_years=10),
    AssetType("at-vehicle", "vehicle", "Vehicle", "vehicle", "🚗", [
        {"key": "oil_change", "label": "Oil change", "interval_days": 180, "estimated_duration_minutes": 30},
        {"key": "tire_rotation", "label": "Tire rotation", "interval_days": 180, "estimated_duration_minutes": 30},
        {"key": "annual_inspection", "label": "Annual inspection", "interval_days": 365, "estimated_duration_minutes": 60},
    ], default_lifespan_years=10),
]


# ── Assets (11 across 3 properties) ────────────────────────────────

ASSETS: list[Asset] = [
    # Villa Sud (5)
    Asset("a-villa-ac-bed", "p-villa-sud", "at-air-conditioner", "Bedroom 2 AC", "Master bedroom",
          "good", "active", make="Mitsubishi", model="MSZ-AP25VGK", serial_number="MZ25-2024-7841",
          installed_on=date(2024, 6, 15), purchased_on=date(2024, 5, 20),
          purchase_price_cents=189900, purchase_currency="EUR", purchase_vendor="Leroy Merlin",
          warranty_expires_on=date(2028, 6, 15), expected_lifespan_years=12,
          guest_visible=True, guest_instructions="Remote is on the nightstand. Set to 22-24 C for comfortable sleep.",
          qr_token="ac1bed2villa"),
    Asset("a-villa-pool-pump", "p-villa-sud", "at-pool-pump", "Main pool pump", "Pool",
          "good", "active", make="Hayward", model="Max-Flo XL", serial_number="HW-MF-2022-3319",
          installed_on=date(2022, 3, 10), purchased_on=date(2022, 2, 28),
          purchase_price_cents=85000, purchase_currency="EUR", purchase_vendor="Pool Pro",
          warranty_expires_on=date(2025, 3, 10), expected_lifespan_years=8,
          qr_token="poolpmp1vsud"),
    Asset("a-villa-oven", "p-villa-sud", "at-oven-range", "Kitchen oven", "Kitchen",
          "good", "active", make="De'Longhi", model="DEFX9P", serial_number="DL-FX9-21-4456",
          installed_on=date(2021, 9, 1), purchased_on=date(2021, 8, 15),
          purchase_price_cents=129900, purchase_currency="EUR", purchase_vendor="Darty",
          warranty_expires_on=date(2024, 9, 1), expected_lifespan_years=15,
          guest_visible=True, guest_instructions="Fan-forced is the middle knob setting. Grill is top element only.",
          qr_token="oven1kitchen"),
    Asset("a-villa-smoke-1", "p-villa-sud", "at-smoke-detector", "Hallway smoke detector", "Entryway",
          "new", "active", make="Kidde", model="10SCO",
          installed_on=date(2026, 1, 10), purchased_on=date(2025, 12, 20),
          purchase_price_cents=3490, purchase_currency="EUR", purchase_vendor="Amazon",
          warranty_expires_on=date(2036, 1, 10), expected_lifespan_years=10,
          qr_token="smk1hallvsud"),
    Asset("a-villa-water-heater", "p-villa-sud", "at-water-heater", "Main water heater", "Kitchen",
          "fair", "active", make="Atlantic", model="O'Pro 200L", serial_number="ATL-OP200-19-1128",
          installed_on=date(2019, 11, 1), purchased_on=date(2019, 10, 15),
          purchase_price_cents=62000, purchase_currency="EUR", purchase_vendor="Brico Depot",
          warranty_expires_on=date(2024, 11, 1), expected_lifespan_years=12,
          notes="Getting old. Consider replacement in 2027.",
          qr_token="wh200lvillsd"),
    # Apt 3B (3)
    Asset("a-apt-dishwasher", "p-apt-3b", "at-dishwasher", "Kitchen dishwasher", "Kitchen",
          "good", "active", make="Bosch", model="Serie 4 SMS4HVW33E",
          installed_on=date(2023, 7, 1), purchased_on=date(2023, 6, 15),
          purchase_price_cents=54900, purchase_currency="EUR", purchase_vendor="Darty",
          warranty_expires_on=date(2025, 7, 1), expected_lifespan_years=10,
          guest_visible=True, guest_instructions="Tablets are under the sink. Use the Eco cycle for daily loads.",
          qr_token="dw1kitapt3b"),
    Asset("a-apt-washing", "p-apt-3b", "at-washing-machine", "Bathroom washing machine", "Bathroom 1",
          "good", "active", make="LG", model="F4WV509S0E", serial_number="LG-WM-23-8812",
          installed_on=date(2023, 7, 1), purchased_on=date(2023, 6, 15),
          purchase_price_cents=64900, purchase_currency="EUR", purchase_vendor="Boulanger",
          warranty_expires_on=date(2026, 7, 1), expected_lifespan_years=10,
          guest_visible=True, guest_instructions="Detergent pods in the cupboard above. 40 C for most loads.",
          qr_token="wm1bthapt3b"),
    Asset("a-apt-oven", "p-apt-3b", "at-oven-range", "Kitchen oven", "Kitchen",
          "good", "active", make="SMEG", model="SF6101TVN",
          installed_on=date(2022, 1, 15), purchased_on=date(2021, 12, 20),
          purchase_price_cents=79900, purchase_currency="EUR", purchase_vendor="Darty",
          warranty_expires_on=date(2025, 1, 15), expected_lifespan_years=15,
          guest_visible=True, guest_instructions="Turn the knob right for conventional, left for fan.",
          qr_token="oven1apt3bkn"),
    # Chalet (3)
    Asset("a-chalet-boiler", "p-chalet", "at-boiler", "Main boiler", "Kitchen",
          "good", "active", make="Vaillant", model="ecoFIT pure 425", serial_number="VAI-425-20-6678",
          installed_on=date(2020, 10, 1), purchased_on=date(2020, 9, 15),
          purchase_price_cents=320000, purchase_currency="EUR", purchase_vendor="Plombier Megeve",
          warranty_expires_on=date(2025, 10, 1), expected_lifespan_years=15,
          qr_token="boil1chalet1"),
    Asset("a-chalet-fireplace", "p-chalet", "at-boiler", "Living room fireplace", "Fireplace room",
          "good", "active", make="Morso", model="6148",
          installed_on=date(2018, 11, 1), purchased_on=date(2018, 10, 1),
          purchase_price_cents=245000, purchase_currency="EUR", purchase_vendor="Cheminee Savoyarde",
          expected_lifespan_years=30,
          guest_visible=True, guest_instructions="Firewood in the ski room. Open the damper fully before lighting.",
          qr_token="fire1chaletf"),
    Asset("a-chalet-snowblower", "p-chalet", "at-vehicle", "Honda snowblower", "Ski room",
          "good", "active", make="Honda", model="HSS760E", serial_number="HON-SB-22-1144",
          installed_on=date(2022, 11, 15), purchased_on=date(2022, 10, 30),
          purchase_price_cents=189000, purchase_currency="EUR", purchase_vendor="Honda Megeve",
          warranty_expires_on=date(2025, 11, 15), expected_lifespan_years=10,
          qr_token="snow1chalet1"),
]


# ── Asset actions (15 across assets) ───────────────────────────────

ASSET_ACTIONS: list[AssetAction] = [
    # Villa AC
    AssetAction("aa-1", "a-villa-ac-bed", "clean_filters", "Clean filters",
                interval_days=90, last_performed_at=date(2026, 2, 10),
                estimated_duration_minutes=30),
    AssetAction("aa-2", "a-villa-ac-bed", "service_unit", "Annual service",
                interval_days=365, last_performed_at=date(2025, 6, 20),
                estimated_duration_minutes=120),
    # Villa pool pump
    AssetAction("aa-3", "a-villa-pool-pump", "clean_basket", "Clean basket",
                interval_days=7, last_performed_at=date(2026, 4, 12),
                linked_task_id="t-1", estimated_duration_minutes=10),
    AssetAction("aa-4", "a-villa-pool-pump", "inspect_seals", "Inspect seals",
                interval_days=180, last_performed_at=date(2025, 11, 1),
                estimated_duration_minutes=20),
    AssetAction("aa-5", "a-villa-pool-pump", "service_pump", "Full service",
                interval_days=365, last_performed_at=date(2025, 4, 10),
                description="Overdue by 5 days", estimated_duration_minutes=120),
    # Villa oven
    AssetAction("aa-6", "a-villa-oven", "deep_clean", "Deep clean",
                interval_days=90, last_performed_at=date(2026, 1, 20),
                estimated_duration_minutes=45),
    # Villa smoke detector
    AssetAction("aa-7", "a-villa-smoke-1", "test", "Test alarm",
                interval_days=30, last_performed_at=date(2026, 3, 15),
                description="Overdue by 1 day", estimated_duration_minutes=5),
    # Villa water heater
    AssetAction("aa-8", "a-villa-water-heater", "flush_tank", "Flush tank",
                interval_days=365, last_performed_at=date(2025, 5, 1),
                estimated_duration_minutes=60),
    # Apt dishwasher
    AssetAction("aa-9", "a-apt-dishwasher", "clean_filter", "Clean filter",
                interval_days=30, last_performed_at=date(2026, 4, 1),
                estimated_duration_minutes=15),
    AssetAction("aa-10", "a-apt-dishwasher", "descale", "Descale",
                interval_days=90, last_performed_at=date(2026, 1, 15),
                description="Due today", estimated_duration_minutes=20),
    # Apt washing machine
    AssetAction("aa-11", "a-apt-washing", "clean_drum", "Clean drum",
                interval_days=30, last_performed_at=date(2026, 3, 20),
                estimated_duration_minutes=15),
    # Chalet boiler
    AssetAction("aa-12", "a-chalet-boiler", "annual_service", "Annual service",
                interval_days=365, last_performed_at=date(2025, 10, 5),
                estimated_duration_minutes=90),
    AssetAction("aa-13", "a-chalet-boiler", "bleed_radiators", "Bleed radiators",
                interval_days=180, last_performed_at=date(2025, 11, 1),
                estimated_duration_minutes=45),
    # Chalet snowblower
    AssetAction("aa-14", "a-chalet-snowblower", "oil_change", "Oil change",
                interval_days=180, last_performed_at=date(2025, 10, 15),
                description="Overdue by 2 days", estimated_duration_minutes=30),
    AssetAction("aa-15", "a-chalet-snowblower", "annual_inspection", "Annual inspection",
                interval_days=365, last_performed_at=date(2025, 11, 20),
                estimated_duration_minutes=60),
]


# ── Asset documents (8 + 2 property-level) ─────────────────────────

ASSET_DOCUMENTS: list[AssetDocument] = [
    AssetDocument("ad-1", "a-villa-ac-bed", "p-villa-sud", "manual", "Mitsubishi MSZ-AP25VGK manual",
                  "msz-ap25vgk-manual.pdf", 4200, datetime(2024, 6, 15, 10, 0)),
    AssetDocument("ad-2", "a-villa-ac-bed", "p-villa-sud", "warranty", "AC warranty certificate",
                  "ac-warranty-2024.pdf", 180, datetime(2024, 6, 15, 10, 5),
                  expires_on=date(2028, 6, 15)),
    AssetDocument("ad-3", "a-villa-pool-pump", "p-villa-sud", "invoice", "Pool pump purchase invoice",
                  "hayward-invoice-2022.pdf", 320, datetime(2022, 3, 10, 14, 0),
                  amount_cents=85000, amount_currency="EUR"),
    AssetDocument("ad-4", "a-villa-water-heater", "p-villa-sud", "manual", "Atlantic O'Pro 200L manual",
                  "atlantic-opro-manual.pdf", 3800, datetime(2019, 11, 5, 9, 0)),
    AssetDocument("ad-5", "a-apt-dishwasher", "p-apt-3b", "warranty", "Bosch Serie 4 warranty",
                  "bosch-warranty-2023.pdf", 150, datetime(2023, 7, 1, 12, 0),
                  expires_on=date(2025, 7, 1)),
    AssetDocument("ad-6", "a-chalet-boiler", "p-chalet", "invoice", "Boiler installation invoice",
                  "vaillant-install-invoice.pdf", 280, datetime(2020, 10, 5, 15, 0),
                  amount_cents=320000, amount_currency="EUR"),
    # Property-level documents (asset_id = None)
    AssetDocument("ad-7", None, "p-villa-sud", "insurance", "Villa Sud insurance policy 2026",
                  "villa-sud-insurance-2026.pdf", 1200, datetime(2026, 1, 5, 9, 30),
                  expires_on=date(2027, 1, 5), amount_cents=285000, amount_currency="EUR"),
    AssetDocument("ad-8", None, "p-villa-sud", "permit", "Pool safety compliance certificate",
                  "pool-safety-cert-2025.pdf", 450, datetime(2025, 6, 20, 11, 0),
                  expires_on=date(2026, 6, 20)),
]


SHIFTS: list[Shift] = [
    Shift("sh-1", "e-maria", "p-villa-sud", datetime(2026, 4, 15, 8, 12), None, "open",
          method_in="auto",
          work_engagement_id="we-maria-bernard", user_id="u-maria"),
    Shift("sh-2", "e-ben", "p-villa-sud", datetime(2026, 4, 15, 8, 45),
          datetime(2026, 4, 15, 12, 30), "closed", duration_seconds=13500,
          method_in="auto", method_out="auto",
          work_engagement_id="we-ben-bernard", user_id="u-ben"),
    Shift("sh-3", "e-arun", "p-villa-sud", datetime(2026, 4, 14, 13, 0),
          datetime(2026, 4, 14, 18, 30), "closed", duration_seconds=19800,
          method_in="manual", method_out="manual",
          work_engagement_id="we-arun-bernard", user_id="u-arun"),
    Shift("sh-4", "e-ana", "p-apt-3b", datetime(2026, 4, 14, 9, 0),
          datetime(2026, 4, 14, 14, 0), "closed", duration_seconds=18000,
          method_in="auto", method_out="auto",
          client_org_id="org-dupont",  # billable to Dupont
          work_engagement_id="we-ana-bernard", user_id="u-ana"),
    Shift("sh-5", "e-sam", "p-villa-sud", datetime(2026, 4, 14, 10, 0),
          datetime(2026, 4, 14, 12, 30), "disputed", duration_seconds=9000,
          method_in="manual", method_out="auto",
          work_engagement_id="we-sam-bernard", user_id="u-sam"),
    # Vincent scenario — same day, same place, two workspaces.
    # Joselyn (CleanCo / AgencyOps) cleans Villa du Lac.
    Shift("sh-6", "e-joselyn", "p-villa-lac", datetime(2026, 4, 15, 9, 0),
          datetime(2026, 4, 15, 13, 0), "closed", duration_seconds=14400,
          method_in="auto", method_out="auto",
          client_org_id="org-dupont-vincent",
          work_engagement_id="we-joselyn-cleanco", user_id="u-joselyn"),
    # Rachid (VincentOps) drives Vincent around that afternoon.
    Shift("sh-7", "e-rachid", "p-villa-lac", datetime(2026, 4, 15, 14, 30),
          datetime(2026, 4, 15, 17, 0), "closed", duration_seconds=9000,
          method_in="manual", method_out="manual",
          work_engagement_id="we-rachid-vincent", user_id="u-rachid"),
]

PAY_RULES: list[PayRule] = [
    # Only payroll-engagement employees have pay rules. e-ana (agency_supplied)
    # and e-sam (contractor) are paid through vendor_invoice (§22) instead.
    PayRule("pr-1", "e-maria", None, "monthly_salary", 240000, "EUR", date(2024, 3, 1),
            work_engagement_id="we-maria-bernard"),
    PayRule("pr-2", "e-arun", None, "hourly", 1429, "EUR", date(2024, 9, 14),
            work_engagement_id="we-arun-bernard"),
    PayRule("pr-3", "e-ben", None, "monthly_salary", 180000, "EUR", date(2023, 5, 20),
            work_engagement_id="we-ben-bernard"),
    # Vincent's worker Rachid earns €12/h.
    PayRule("pr-4", "e-rachid", None, "hourly", 1200, "EUR", date(2024, 6, 1),
            work_engagement_id="we-rachid-vincent"),
    # Joselyn at CleanCo.
    PayRule("pr-5", "e-joselyn", None, "hourly", 1500, "EUR", date(2025, 2, 1),
            work_engagement_id="we-joselyn-cleanco"),
]

PAY_PERIODS: list[PayPeriod] = [
    PayPeriod("pp-mar-26", date(2026, 3, 1), date(2026, 3, 31), "paid",
              locked_at=datetime(2026, 3, 31, 22, 0)),
    PayPeriod("pp-apr-26", date(2026, 4, 1), date(2026, 4, 30), "open"),
]


# ── Clients, vendors, work orders (§22) ──────────────────────────────

ORGANIZATIONS: list[Organization] = [
    Organization(
        "org-dupont", "Dupont family",
        workspace_id="ws-bernard",
        is_client=True,
        legal_name="SCI Dupont",
        default_currency="EUR",
        tax_id="FR12 345 678 901",
        contacts=[
            {"label": "Primary", "name": "Hélène Dupont",
             "email": "helene.dupont@example.com", "phone_e164": "+33 6 77 89 01 23",
             "role": "owner"},
        ],
        notes="Owners of Apt 3B. Invoiced monthly, NET-30.",
    ),
    Organization(
        "org-cleanco", "CleanCo SARL",
        workspace_id="ws-bernard",
        is_supplier=True,
        legal_name="CleanCo SARL",
        default_currency="EUR",
        tax_id="FR98 765 432 109",
        contacts=[
            {"label": "Ops", "name": "Marc Girard",
             "email": "ops@cleanco.example", "phone_e164": "+33 1 23 45 67 89",
             "role": "account_manager"},
        ],
        default_pay_destination_stub="•• FR-07",
        notes="Supplies Ana Rossi. Invoices weekly, NET-15.",
    ),
    # Vincent scenario — DupontFamily (Vincent's billing legal entity).
    # Lives in AgencyOps's scope as a client; Vincent holds a workspace
    # role_grant(client, binding_org_id=this.id) on AgencyOps.
    Organization(
        "org-dupont-vincent", "DupontFamily",
        workspace_id="ws-cleanco",
        is_client=True,
        legal_name="SCI Dupont Vincent",
        default_currency="EUR",
        tax_id="FR34 567 890 123",
        contacts=[
            {"label": "Primary", "name": "Vincent Dupont",
             "email": "vincent.dupont@example.com",
             "phone_e164": "+33 6 77 88 99 00",
             "role": "owner"},
        ],
        notes="Billing entity for Vincent Dupont. NET-30. Villa du Lac + Seaside Apt.",
        portal_user_id="u-vincent",
    ),
]

CLIENT_RATES: list[ClientRate] = [
    # Role-based rate card for Dupont (Bernard workspace demo): housekeepers €32/h, handymen €55/h.
    ClientRate("cr-1", "org-dupont", "r-housekeeper",
               3200, "EUR", date(2026, 1, 1)),
    ClientRate("cr-2", "org-dupont", "r-handyman",
               5500, "EUR", date(2026, 1, 1)),
    # Vincent scenario — CleanCo charges Vincent's org €34/h for maid work.
    ClientRate("cr-4", "org-dupont-vincent", "wr-cleanco-maid",
               3400, "EUR", date(2026, 1, 1)),
]

CLIENT_USER_RATES: list[ClientUserRate] = [
    # Per-user override: Ana is billed at a premium rate to Dupont.
    ClientUserRate("cur-1", "org-dupont", "u-ana",
                   3600, "EUR", date(2026, 1, 1)),
]

SHIFT_BILLINGS: list[ShiftBilling] = [
    ShiftBilling("sb-1", "sh-4", "org-dupont", "u-ana",
                 "EUR", billable_minutes=300, hourly_cents=3600,
                 subtotal_cents=18000,
                 rate_source="client_user_rate", rate_source_id="cur-1",
                 work_engagement_id="we-ana-bernard"),
    # Vincent scenario — Joselyn's shift at Villa du Lac billable to
    # DupontFamily at the CleanCo maid rate.
    ShiftBilling("sb-2", "sh-6", "org-dupont-vincent", "u-joselyn",
                 "EUR", billable_minutes=240, hourly_cents=3400,
                 subtotal_cents=13600,
                 rate_source="client_rate", rate_source_id="cr-4",
                 work_engagement_id="we-joselyn-cleanco"),
]

WORK_ORDERS: list[WorkOrder] = [
    WorkOrder(
        "wo-1", "p-villa-sud", "Replace leaking shower mixer — master bath",
        state="accepted",
        assigned_user_id="u-sam",
        currency="EUR",
        asset_id=None,
        description="Cold-side mixer dripping; requires cartridge replacement.",
        accepted_quote_id="q-1",
        created_at=datetime(2026, 4, 10, 14, 30),
        requested_by_user_id="u-elodie",
    ),
    WorkOrder(
        "wo-2", "p-apt-3b", "Deep clean + linens turnover (Dupont)",
        state="invoiced",
        assigned_user_id="u-ana",
        currency="EUR",
        client_org_id="org-dupont",
        description="Standing weekly engagement billed through CleanCo.",
        created_at=datetime(2026, 4, 14, 7, 0),
        requested_by_user_id="u-elodie",
    ),
]

QUOTES: list[Quote] = [
    Quote(
        "q-1", "wo-1", "u-sam",
        currency="EUR", subtotal_cents=24000, tax_cents=4800, total_cents=28800,
        status="accepted",
        lines=[
            {"kind": "labor", "description": "Diagnosis + swap (2h)",
             "quantity": 2, "unit": "hour", "unit_price_cents": 6000, "total_cents": 12000},
            {"kind": "material", "description": "OEM mixer cartridge",
             "quantity": 1, "unit": "unit", "unit_price_cents": 8000, "total_cents": 8000},
            {"kind": "travel", "description": "Call-out fee",
             "quantity": 1, "unit": "unit", "unit_price_cents": 4000, "total_cents": 4000},
        ],
        valid_until=date(2026, 5, 10),
        submitted_at=datetime(2026, 4, 11, 9, 15),
        decided_at=datetime(2026, 4, 11, 16, 40),
        decided_by_user_id="u-elodie",
        decision_note="Accepted — proceed after Friday.",
        work_engagement_id="we-sam-bernard",
    ),
]

VENDOR_INVOICES: list[VendorInvoice] = [
    # Invoice for the pending Sam job — draft, still to be submitted after work is done.
    VendorInvoice(
        "vi-1", currency="EUR",
        subtotal_cents=24000, tax_cents=4800, total_cents=28800,
        billed_at=date(2026, 4, 16),
        status="draft",
        work_order_id="wo-1",
        vendor_user_id="u-sam",
        vendor_work_engagement_id="we-sam-bernard",
        lines=[
            {"kind": "labor", "description": "Diagnosis + swap (2h)",
             "quantity": 2, "unit": "hour", "unit_price_cents": 6000, "total_cents": 12000},
            {"kind": "material", "description": "OEM mixer cartridge",
             "quantity": 1, "unit": "unit", "unit_price_cents": 8000, "total_cents": 8000},
            {"kind": "travel", "description": "Call-out fee",
             "quantity": 1, "unit": "unit", "unit_price_cents": 4000, "total_cents": 4000},
        ],
    ),
    # CleanCo invoice for Ana's week — billed by the supplier org, not Ana herself.
    VendorInvoice(
        "vi-2", currency="EUR",
        subtotal_cents=18000, tax_cents=3600, total_cents=21600,
        billed_at=date(2026, 4, 15),
        due_on=date(2026, 4, 30),
        status="submitted",
        work_order_id="wo-2",
        vendor_organization_id="org-cleanco",
        payout_destination_stub="•• FR-07",
        lines=[
            {"kind": "labor", "description": "Ana Rossi — Apt 3B turnover (5h)",
             "quantity": 5, "unit": "hour", "unit_price_cents": 3600, "total_cents": 18000},
        ],
        submitted_at=datetime(2026, 4, 15, 18, 0),
    ),
    # Vincent scenario — CleanCo invoices Vincent's DupontFamily org for
    # Joselyn's maid work at Villa du Lac.
    VendorInvoice(
        "vi-3", currency="EUR",
        subtotal_cents=13600, tax_cents=2720, total_cents=16320,
        billed_at=date(2026, 4, 15),
        due_on=date(2026, 4, 30),
        status="submitted",
        property_id="p-villa-lac",
        vendor_organization_id="org-cleanco",
        payout_destination_stub="•• FR-07",
        lines=[
            {"kind": "labor", "description": "Joselyn Rivera — Villa du Lac maid service (4h)",
             "quantity": 4, "unit": "hour", "unit_price_cents": 3400, "total_cents": 13600},
        ],
        submitted_at=datetime(2026, 4, 15, 19, 0),
    ),
]

INVENTORY_MOVEMENTS: list[InventoryMovement] = [
    # `actor_kind` is the v1 collapsed enum: user | agent | system (§02).
    InventoryMovement("im-1", "inv-3", -1, "consume", "user", "u-ben",
                      datetime(2026, 4, 12, 9, 30), "Used for weekly pool service"),
    InventoryMovement("im-2", "inv-6", -4, "consume", "user", "u-ana",
                      datetime(2026, 4, 14, 8, 0), "Turnover prep — Apt 3B"),
    InventoryMovement("im-3", "inv-4", 6, "restock", "user", "u-maria",
                      datetime(2026, 4, 13, 17, 0), "Carrefour run"),
    InventoryMovement("im-4", "inv-8", -2, "consume", "user", "u-sam",
                      datetime(2026, 4, 10, 16, 0)),
]

LIFECYCLE_RULES: list[StayLifecycleRule] = [
    StayLifecycleRule("lr-1", "p-villa-sud", "after_checkout", "tpl-turnover", offset_hours=2),
    StayLifecycleRule("lr-2", "p-apt-3b", "after_checkout", "tpl-turnover", offset_hours=1),
    StayLifecycleRule("lr-3", "p-villa-sud", "before_checkin", "tpl-linen-change", offset_hours=-4),
]

TASK_COMMENTS: list[TaskComment] = [
    # v1 `author_kind` ∈ {user, agent, system} (§02).
    TaskComment("tc-1", "t-1", "user", "u-ben", "pH was 7.4 — in range. Skimmer baskets clean.",
                datetime(2026, 4, 15, 9, 25)),
    TaskComment("tc-2", "t-2", "user", "u-maria", "Bed stripped, starting fresh sheets.",
                datetime(2026, 4, 15, 10, 35)),
    TaskComment("tc-3", "t-2", "agent", "digest-agent",
                "Lavender sheets are on shelf 2 of linen cupboard A.",
                datetime(2026, 4, 15, 10, 36)),
    TaskComment("tc-4", "t-3", "system", "system", "Task auto-assigned to Maria (housekeeper, available).",
                datetime(2026, 4, 15, 6, 0)),
]


WORKSPACE_SETTINGS: dict[str, Any] = {
    "evidence.policy": "optional",
    "time.clock_mode": "manual",
    "time.auto_clock_idle_minutes": 30,
    "time.geofence_radius_m": 150,
    "time.geofence_required": False,
    "pay.frequency": "monthly",
    "pay.week_start": "monday",
    "retention.audit_days": 730,
    "retention.llm_calls_days": 90,
    "retention.task_photos_days": 365,
    "scheduling.horizon_days": 30,
    "tasks.checklist_required": False,
    "tasks.allow_skip_with_reason": True,
}

WORKSPACE_POLICY: dict[str, Any] = {
    "approvals": {
        "always_gated": ["payout_destination.*", "work_engagement.set_default_pay_destination"],
        "configurable": ["tasks.bulk_reassign>50", "broadcast.email_many"],
    },
    "danger_zone": ["Rotate envelope key (host CLI only)", "Purge user (host CLI only)", "Export workspace backup"],
}

WORKSPACE_META: dict[str, str] = {
    "name": "Bernard workspace",
    "timezone": "Europe/Paris",
    "currency": "EUR",
    "country": "FR",
    "default_locale": "fr-FR",
}


# ── Agent preferences (§11) ──────────────────────────────────────────
# CLAUDE.md-style free-form guidance stacked into the LLM system prompt.
# Keyed as (scope_kind, scope_id). Empty strings are legal — an empty
# layer still carries its labelled section with body "(none)".

AGENT_PREFERENCES: dict[tuple[str, str], dict[str, Any]] = {
    ("workspace", "ws-bernard"): {
        "body_md": (
            "# Voice\n"
            "- Reply in French unless the user writes in English.\n"
            "- Keep answers under two paragraphs unless asked to expand.\n"
            "- Prices as `€1 234,56`, even when a property's currency differs.\n"
            "\n"
            "# Habits\n"
            "- Summarise long task lists — never dump more than 8 rows in chat.\n"
            "- On Sundays, do not propose schedule changes without prompting.\n"
        ),
        "token_count": 74,
        "updated_by_user_id": "u-elodie",
        "updated_at": "2026-04-11T09:32:00Z",
    },
    ("property", "p-villa-sud"): {
        "body_md": (
            "- Gardener comes Tuesday mornings. Don't propose outdoor tasks that day.\n"
            "- Photo evidence is required here; if I ask to skip it, remind me gently.\n"
            "- Maria knows the pool routine best — prefer her for pool-related work.\n"
        ),
        "token_count": 46,
        "updated_by_user_id": "u-elodie",
        "updated_at": "2026-04-08T16:05:00Z",
    },
    ("property", "p-apt-3b"): {
        "body_md": "",
        "token_count": 0,
        "updated_by_user_id": "u-elodie",
        "updated_at": "2026-03-22T10:11:00Z",
    },
    ("property", "p-chalet"): {
        "body_md": (
            "- Winter only: check heating before every check-in task.\n"
            "- No flash photography inside (artwork). Camera evidence disabled already.\n"
        ),
        "token_count": 28,
        "updated_by_user_id": "u-elodie",
        "updated_at": "2026-02-18T14:20:00Z",
    },
    ("user", "u-elodie"): {
        "body_md": (
            "- Don't request a photo on my tasks unless I ask.\n"
            "- When I'm chatting in the evening, keep it to one paragraph.\n"
            "- I prefer concrete numbers over vague trends in the daily digest.\n"
        ),
        "token_count": 42,
        "updated_by_user_id": "u-elodie",
        "updated_at": "2026-04-14T21:03:00Z",
    },
    ("user", "u-maria"): {
        "body_md": (
            "- Je préfère le français.\n"
            "- Dis-moi seulement ce qui est en retard, pas la liste complète.\n"
        ),
        "token_count": 22,
        "updated_by_user_id": "u-maria",
        "updated_at": "2026-04-10T07:45:00Z",
    },
    ("user", "u-arun"): {
        "body_md": "",
        "token_count": 0,
        "updated_by_user_id": "u-arun",
        "updated_at": "2026-03-30T12:00:00Z",
    },
}

AGENT_PREFERENCE_REVISIONS: dict[tuple[str, str], list[dict[str, Any]]] = {
    ("workspace", "ws-bernard"): [
        {
            "revision_number": 1,
            "body_md": "# Voice\n- Reply in French unless the user writes in English.\n",
            "saved_by_user_id": "u-elodie",
            "saved_at": "2026-03-01T10:00:00Z",
            "save_note": "initial",
        },
        {
            "revision_number": 2,
            "body_md": (
                "# Voice\n- Reply in French unless the user writes in English.\n"
                "- Keep answers under two paragraphs unless asked to expand.\n"
            ),
            "saved_by_user_id": "u-elodie",
            "saved_at": "2026-03-24T15:12:00Z",
            "save_note": "tighten chat length",
        },
        {
            "revision_number": 3,
            "body_md": (
                "# Voice\n- Reply in French unless the user writes in English.\n"
                "- Keep answers under two paragraphs unless asked to expand.\n"
                "- Prices as `€1 234,56`, even when a property's currency differs.\n"
                "\n"
                "# Habits\n"
                "- Summarise long task lists — never dump more than 8 rows in chat.\n"
                "- On Sundays, do not propose schedule changes without prompting.\n"
            ),
            "saved_by_user_id": "u-elodie",
            "saved_at": "2026-04-11T09:32:00Z",
            "save_note": "add habits block",
        },
    ],
    ("user", "u-elodie"): [
        {
            "revision_number": 1,
            "body_md": "- Don't request a photo on my tasks unless I ask.\n",
            "saved_by_user_id": "u-elodie",
            "saved_at": "2026-04-02T18:30:00Z",
            "save_note": None,
        },
        {
            "revision_number": 2,
            "body_md": (
                "- Don't request a photo on my tasks unless I ask.\n"
                "- When I'm chatting in the evening, keep it to one paragraph.\n"
                "- I prefer concrete numbers over vague trends in the daily digest.\n"
            ),
            "saved_by_user_id": "u-elodie",
            "saved_at": "2026-04-14T21:03:00Z",
            "save_note": None,
        },
    ],
}


# Simple heuristic secret pattern set used by the mock save endpoint.
# Matches the §11 "PII posture" spec list — not a real scrubber, just
# enough to show the guard in the UI.
AGENT_PREFERENCE_SECRET_PATTERNS: list[tuple[str, str]] = [
    (r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", "IBAN-shaped token"),
    (r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "card-number-shaped token"),
    (r"\bmip_[a-zA-Z0-9_]{10,}\b", "crewday API token"),
    (r"(?i)\b(wifi|wi-fi|password|passcode|door\s*code|alarm)\s*[:=]\s*\S{4,}", "password / access-code pattern"),
]

SETTINGS_CATALOG: list[SettingDefinition] = [
    SettingDefinition("evidence.policy", "Evidence policy", "enum", "optional",
                      enum_values=["require", "optional", "forbid"],
                      override_scope="W/P/E/T",
                      description="Whether photo evidence is required, optional, or forbidden on task completions.",
                      spec="05, 06"),
    SettingDefinition("time.clock_mode", "Clock mode", "enum", "manual",
                      enum_values=["manual", "auto", "disabled"],
                      override_scope="W/P/E",
                      description="How shift tracking works: manual button, auto from activity, or disabled.",
                      spec="09"),
    SettingDefinition("time.auto_clock_idle_minutes", "Auto-clock idle timeout", "int", 30,
                      override_scope="W/P/E",
                      description="Minutes of inactivity before an auto-clock shift closes.",
                      spec="05"),
    SettingDefinition("time.geofence_radius_m", "Geofence radius (m)", "int", 150,
                      override_scope="W/P",
                      description="Radius in metres for property geofence checks.",
                      spec="09"),
    SettingDefinition("time.geofence_required", "Geofence required", "bool", False,
                      override_scope="W/P/E",
                      description="Whether clock-in requires being within the property geofence.",
                      spec="05"),
    SettingDefinition("pay.frequency", "Pay frequency", "enum", "monthly",
                      enum_values=["monthly", "bi_weekly"],
                      override_scope="W",
                      description="How often pay periods close.",
                      spec="09"),
    SettingDefinition("pay.week_start", "Week start day", "enum", "monday",
                      enum_values=["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
                      override_scope="W",
                      description="First day of the work week for scheduling and pay period calculations."),
    SettingDefinition("retention.audit_days", "Audit log retention (days)", "int", 730,
                      override_scope="W",
                      description="How many days audit log entries are kept before archival.",
                      spec="02"),
    SettingDefinition("retention.llm_calls_days", "LLM call retention (days)", "int", 90,
                      override_scope="W",
                      description="How many days LLM call records are kept.",
                      spec="02, 11"),
    SettingDefinition("retention.task_photos_days", "Task photo retention (days)", "int", 365,
                      override_scope="W",
                      description="How many days task evidence photos are kept."),
    SettingDefinition("scheduling.horizon_days", "Scheduling horizon (days)", "int", 30,
                      override_scope="W/P",
                      description="How far ahead the scheduler materializes task occurrences.",
                      spec="06"),
    SettingDefinition("tasks.checklist_required", "Checklist completion required", "bool", False,
                      override_scope="W/P/E/T",
                      description="All required checklist items must be ticked to complete a task.",
                      spec="05"),
    SettingDefinition("tasks.allow_skip_with_reason", "Allow skip with reason", "bool", True,
                      override_scope="W/P/E",
                      description="Whether employees can skip tasks by providing a reason.",
                      spec="05"),
    SettingDefinition("assets.warranty_alert_days", "Warranty alert window (days)", "int", 30,
                      override_scope="W/P",
                      description="Days before warranty expiry to surface an alert.",
                      spec="21"),
    SettingDefinition("assets.show_guest_assets", "Show assets to guests", "bool", False,
                      override_scope="W/P/U",
                      description="Whether guest-visible assets appear on the guest welcome page.",
                      spec="21"),
]

def resolve_settings(
    workspace_defaults: dict[str, Any],
    property_override: dict[str, Any] | None = None,
    employee_override: dict[str, Any] | None = None,
    task_override: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Walk task -> employee -> property -> workspace; most specific concrete value wins.

    Returns ``{key: {"value": ..., "source": "workspace"|"property"|"employee"|"task"|"catalog"}}``.
    """
    result: dict[str, dict[str, Any]] = {}
    for defn in SETTINGS_CATALOG:
        key = defn.key
        # Walk from most specific to least specific; first non-inherit value wins.
        for layer_name, layer_map in [
            ("task", task_override or {}),
            ("employee", employee_override or {}),
            ("property", property_override or {}),
            ("workspace", workspace_defaults),
        ]:
            val = layer_map.get(key)
            if val is not None and val != "inherit":
                result[key] = {"value": val, "source": layer_name}
                break
        else:
            # Nothing set anywhere; use catalog default.
            result[key] = {"value": defn.catalog_default, "source": "catalog"}
    return result




GUEST_STAY_ID = "s-3"  # the preview guest page renders this stay


# ── Agent sidebar (manager) ─────────────────────────────────────────

@dataclass
class AgentMessage:
    at: datetime
    kind: Literal["agent", "user", "action"]
    body: str
    # §23 — the chat gateway tags each inbound/outbound turn with the
    # channel it arrived on. Null means "web" (sidebar or PWA Chat tab).
    channel_kind: Literal[
        "offapp_whatsapp", "offapp_telegram"
    ] | None = None


@dataclass
class AgentAction:
    """Row rendered in the chat sidebar's pending-actions tray (§14).

    Mirrors the inline fields of `ApprovalRequest` for the subset of
    `agent_action` rows whose `gate_destination = inline_chat` and
    `for_user_id = current_user`.
    """

    id: str
    title: str
    detail: str
    risk: Literal["low", "medium", "high"]
    # §11 — the server-rendered confirmation summary ("Create expense
    # Marché Provence for €22.10?") and a small key/value list of
    # resolved payload fields to show under it.
    card_summary: str = ""
    card_fields: list[tuple[str, str]] = field(default_factory=list)
    gate_source: Literal[
        "workspace_always",
        "workspace_configurable",
        "user_auto_annotation",
        "user_strict_mutation",
    ] = "user_auto_annotation"
    inline_channel: Literal[
        "web_owner_sidebar",
        "web_worker_chat",
    ] = "web_owner_sidebar"


MANAGER_AGENT_LOG: list[AgentMessage] = [
    AgentMessage(datetime(2026, 4, 15, 9, 14), "agent",
        "Morning. Ben is on approved leave 18–21 Apr; pool service on the 18th "
        "needs a cover — Sam is available. Shall I reassign?"),
    AgentMessage(datetime(2026, 4, 15, 9, 15), "user",
        "Yes, go ahead."),
    AgentMessage(datetime(2026, 4, 15, 9, 16), "agent",
        "Reassigned. Also: Maria flagged a burnt-out vacuum on the 12th. I drafted "
        "a €449 replacement (Dyson V11). Review in the actions tray →"),
    AgentMessage(datetime(2026, 4, 15, 10, 2), "user",
        "What's the pending turnover for Apt 3B on the 18th?"),
    AgentMessage(datetime(2026, 4, 15, 10, 2), "agent",
        "Assigned to Ana, starts 12:00, 120 min. Checklist set, welcome basket in "
        "stock. All evidence required. Nothing blocking."),
]

MANAGER_AGENT_ACTIONS: list[AgentAction] = [
    AgentAction(
        "aa-1", "Reassign pool check to Sam",
        "Ben on leave 18–21; Sam is the configured backup.", "low",
        card_summary="Reassign pool check at Villa Sud from Ben to Sam?",
        card_fields=[("task", "Pool check — Villa Sud"), ("new assignee", "Sam Leclerc")],
        gate_source="user_auto_annotation",
    ),
    AgentAction(
        "aa-2", "Create expense Brico Dépôt €449",
        "Vacuum motor burnt out; Maria photo attached; budget remaining €820.", "medium",
        card_summary="Create expense Brico Dépôt for €449.00?",
        card_fields=[
            ("vendor", "Brico Dépôt"),
            ("amount", "€449.00"),
            ("property", "Villa Sud"),
        ],
        gate_source="user_auto_annotation",
    ),
    AgentAction(
        "aa-3", "Draft April payslips",
        "Period closed; all shifts reconciled. Totals within 4% of March.", "medium",
        card_summary="Draft April payslips (5 employees)?",
        card_fields=[("period", "2026-04"), ("count", "5")],
        gate_source="workspace_configurable",
    ),
]


# ── Agent chat (employee) ────────────────────────────────────────────

EMPLOYEE_CHAT_LOG: list[AgentMessage] = [
    AgentMessage(datetime(2026, 4, 15, 8, 10), "agent",
        "Bonjour Maria ! Tu as 4 tâches aujourd'hui. La première : changer le linge "
        "de la chambre principale à 10:30. Les draps lavande sont sur l'étagère 2."),
    AgentMessage(datetime(2026, 4, 15, 8, 11), "user",
        "Ok merci. I bought cleaning supplies yesterday — Carrefour, 12.40 EUR.",
        channel_kind="offapp_whatsapp"),
    AgentMessage(datetime(2026, 4, 15, 8, 12), "action",
        "J'ai enregistré ton reçu Carrefour de 12,40 €. À approuver ?",
        channel_kind="offapp_whatsapp"),
    AgentMessage(datetime(2026, 4, 15, 9, 2), "user",
        "Le vacuum de Villa Sud fait un bruit bizarre, je pense qu'il va lâcher."),
    AgentMessage(datetime(2026, 4, 15, 9, 3), "agent",
        "Noté. Je propose à Élodie de commander un remplacement (Dyson V11, ~449 €) — "
        "elle voit ta photo et décide. Tu peux continuer avec le balai pour la journée."),
    AgentMessage(datetime(2026, 4, 15, 9, 48), "user",
        "Ben est en congé le 18, qui prend la piscine ?"),
    AgentMessage(datetime(2026, 4, 15, 9, 48), "agent",
        "Sam couvre — c'est déjà acté ce matin. Rien à faire de ton côté."),
]


# ── Task-scoped agent chat ──────────────────────────────────────────
#
# §06 "Task notes are the agent inbox": every task carries its own
# thread with the workspace agent. The PWA task detail page
# (§14 /task/<id>) renders the same message components and composer
# as the global /chat tab, pointed at this store.
TASK_CHAT_LOGS: dict[str, list[AgentMessage]] = {
    "t-2": [
        AgentMessage(
            datetime(2026, 4, 15, 8, 32), "agent",
            "Fresh sheets are on shelf 2 of cupboard A — the lavender-scented "
            "ones. Want me to log the finish photo as evidence when you're done?",
        ),
    ],
}


# ── Chat gateway (§23) ───────────────────────────────────────────────

@dataclass
class ChatChannelBinding:
    """§23 — a `(channel_kind, address)` pair tied to one user."""

    id: str
    user_id: str
    channel_kind: Literal[
        "offapp_whatsapp", "offapp_telegram"
    ]
    address: str
    display_label: str
    state: Literal["pending", "active", "revoked"]
    verified_at: datetime | None = None
    last_message_at: datetime | None = None
    revoked_at: datetime | None = None
    revoke_reason: Literal[
        "user", "stop_keyword", "user_archived", "admin", "provider_error"
    ] | None = None


@dataclass
class ChatGatewayProvider:
    """Stub of the per-workspace Meta Cloud config card on /settings."""

    channel_kind: str
    provider: str
    status: Literal["connected", "pending", "error", "not_configured"]
    display_stub: str
    last_webhook_at: datetime | None
    templates: list[str]


CHAT_CHANNEL_BINDINGS: list[ChatChannelBinding] = [
    # Maria (default worker) has WhatsApp linked and actively used —
    # her worker chat log shows an inbound receipt from WhatsApp.
    ChatChannelBinding(
        id="ccb-maria-wa",
        user_id="u-maria",
        channel_kind="offapp_whatsapp",
        address="+33 6 12 34 56 78",
        display_label="Personal phone",
        state="active",
        verified_at=datetime(2026, 3, 4, 9, 21),
        last_message_at=datetime(2026, 4, 15, 8, 11),
    ),
    # Élodie (owner-manager) started a link ceremony this morning; the
    # 6-digit code was sent via WhatsApp template and has not been
    # redeemed yet — state `pending`, 12 minutes left on the TTL.
    ChatChannelBinding(
        id="ccb-elodie-wa",
        user_id="u-elodie",
        channel_kind="offapp_whatsapp",
        address="+33 6 11 22 33 44",
        display_label="Personal phone",
        state="pending",
    ),
    # Arun revoked his binding last month via STOP on WhatsApp.
    # Kept as a historical row to exercise the revoked state in the
    # admin view.
    ChatChannelBinding(
        id="ccb-arun-wa",
        user_id="u-arun",
        channel_kind="offapp_whatsapp",
        address="+33 6 22 45 67 89",
        display_label="Personal phone",
        state="revoked",
        revoked_at=datetime(2026, 3, 22, 18, 40),
        revoke_reason="stop_keyword",
    ),
]


CHAT_GATEWAY_PROVIDERS: list[ChatGatewayProvider] = [
    ChatGatewayProvider(
        channel_kind="offapp_whatsapp",
        provider="Meta Cloud API",
        status="connected",
        display_stub="+33 1 86 65 xx xx · phone_number_id … 4921",
        last_webhook_at=datetime(2026, 4, 15, 8, 11),
        templates=["chat_channel_link_code", "chat_agent_nudge"],
    ),
    ChatGatewayProvider(
        channel_kind="offapp_telegram",
        provider="Telegram Bot API",
        status="not_configured",
        display_stub="—",
        last_webhook_at=None,
        templates=[],
    ),
]


# ── History (archived side views per §14) ────────────────────────────

HISTORY: dict[str, list[dict[str, str]]] = {
    "chats": [
        {
            "id": "h-chat-1",
            "title": "Fuite sous l'évier — Villa Sud",
            "last_at": "Mar 28",
            "summary": "Sam a changé le siphon; classé après vérification le lendemain.",
        },
        {
            "id": "h-chat-2",
            "title": "Welcome basket — Apt 3B",
            "last_at": "Mar 12",
            "summary": "Ajout de deux pâtisseries au panier; standard mis à jour.",
        },
        {
            "id": "h-chat-3",
            "title": "Clés perdues — Chalet Cœur",
            "last_at": "Feb 22",
            "summary": "Clés retrouvées dans la boîte à lettres du voisin. Boîte code changé.",
        },
    ],
}


# ── Helpers ─────────────────────────────────────────────────────────

def property_by_id(pid: str) -> Property:
    return next(p for p in PROPERTIES if p.id == pid)


def employee_by_id(eid: str) -> Employee:
    return next(e for e in EMPLOYEES if e.id == eid)


def tasks_for_employee(eid: str) -> list[Task]:
    return [t for t in TASKS if t.assignee_id == eid]


def task_by_id(tid: str) -> Task | None:
    return next((t for t in TASKS if t.id == tid), None)


def expenses_for_employee(eid: str) -> list[Expense]:
    return [x for x in EXPENSES if x.employee_id == eid]


def stays_for_property(pid: str) -> list[Stay]:
    return [s for s in STAYS if s.property_id == pid]


def leaves_for_employee(eid: str) -> list[Leave]:
    return [lv for lv in LEAVES if lv.employee_id == eid]


def closures_for_property(pid: str) -> list[PropertyClosure]:
    return [c for c in CLOSURES if c.property_id == pid]


def instructions_for_task(t: Task) -> list[Instruction]:
    return [i for i in INSTRUCTIONS if i.id in t.instructions_ids]


def payslips_for_employee(eid: str) -> list[PaySlip]:
    return [p for p in PAYSLIPS if p.employee_id == eid]


def inventory_for_property(pid: str) -> list[InventoryItem]:
    return [i for i in INVENTORY if i.property_id == pid]


def stay_by_id(sid: str) -> Stay | None:
    return next((s for s in STAYS if s.id == sid), None)


# ── Asset helpers ──────────────────────────────────────────────────

def asset_type_by_id(atid: str) -> AssetType | None:
    return next((t for t in ASSET_TYPES if t.id == atid), None)


def asset_by_id(aid: str) -> Asset | None:
    return next((a for a in ASSETS if a.id == aid), None)


def assets_for_property(pid: str) -> list[Asset]:
    return [a for a in ASSETS if a.property_id == pid]


def actions_for_asset(aid: str) -> list[AssetAction]:
    return [a for a in ASSET_ACTIONS if a.asset_id == aid]


def documents_for_asset(aid: str) -> list[AssetDocument]:
    return [d for d in ASSET_DOCUMENTS if d.asset_id == aid]


def documents_for_property(pid: str) -> list[AssetDocument]:
    return [d for d in ASSET_DOCUMENTS if d.property_id == pid]


def shifts_for_employee(eid: str) -> list[Shift]:
    return [s for s in SHIFTS if s.employee_id == eid]


def comments_for_task(tid: str) -> list[TaskComment]:
    return [c for c in TASK_COMMENTS if c.task_id == tid]


def movements_for_item(iid: str) -> list[InventoryMovement]:
    return [m for m in INVENTORY_MOVEMENTS if m.item_id == iid]


def lifecycle_rules_for_property(pid: str) -> list[StayLifecycleRule]:
    return [r for r in LIFECYCLE_RULES if r.property_id == pid]


# The "signed-in" user for each role.
DEFAULT_EMPLOYEE_ID = "e-maria"
DEFAULT_EMPLOYEE_USER_ID = "u-maria"
DEFAULT_MANAGER_NAME = "Élodie Bernard"
DEFAULT_MANAGER_USER_ID = "u-elodie"
DEFAULT_WORKSPACE_ID = "ws-bernard"


# ── v1 helpers ──────────────────────────────────────────────────────

def user_by_id(uid: str) -> User | None:
    return next((u for u in USERS if u.id == uid), None)


def user_by_email(email: str) -> User | None:
    target = email.strip().lower()
    return next((u for u in USERS if u.email.lower() == target), None)


def role_grants_for_user(uid: str) -> list[RoleGrant]:
    return [g for g in ROLE_GRANTS if g.user_id == uid and g.revoked_at is None]


def role_grants_for_scope(scope_kind: str, scope_id: str) -> list[RoleGrant]:
    return [g for g in ROLE_GRANTS
            if g.scope_kind == scope_kind and g.scope_id == scope_id
            and g.revoked_at is None]


def work_engagements_for_user(uid: str) -> list[WorkEngagement]:
    return [w for w in WORK_ENGAGEMENTS if w.user_id == uid and w.archived_on is None]


def work_engagements_for_workspace(wsid: str) -> list[WorkEngagement]:
    return [w for w in WORK_ENGAGEMENTS if w.workspace_id == wsid and w.archived_on is None]


def work_engagement_by_id(weid: str) -> WorkEngagement | None:
    return next((w for w in WORK_ENGAGEMENTS if w.id == weid), None)


def user_work_roles_for_user(uid: str, workspace_id: str | None = None) -> list[UserWorkRole]:
    rows = [r for r in USER_WORK_ROLES if r.user_id == uid and r.ended_on is None]
    if workspace_id:
        rows = [r for r in rows if r.workspace_id == workspace_id]
    return rows


def property_assignments_for_uwr(uwr_id: str) -> list[PropertyWorkRoleAssignment]:
    return [a for a in PROPERTY_WORK_ROLE_ASSIGNMENTS if a.user_work_role_id == uwr_id]


def users_in_workspace(wsid: str) -> list[User]:
    uids = {uw.user_id for uw in USER_WORKSPACES if uw.workspace_id == wsid}
    return [u for u in USERS if u.id in uids]


def workspaces_for_property(pid: str) -> list[PropertyWorkspace]:
    return [pw for pw in PROPERTY_WORKSPACES if pw.property_id == pid]


def workspace_by_id(wsid: str) -> Workspace | None:
    return next((w for w in WORKSPACES if w.id == wsid), None)


# ── Derived field back-fill ─────────────────────────────────────────
# Some of the new v1 columns mirror existing ones (e.g. Task.assignee_id
# ↔ Task.assigned_user_id). Populate the mirrors once at import time so
# writers don't have to worry about drift.

_EMPLOYEE_TO_USER: dict[str, str] = {e.id: e.user_id for e in EMPLOYEES if e.user_id}
_EMPLOYEE_TO_ENGAGEMENT: dict[str, str] = {
    e.id: e.work_engagement_id for e in EMPLOYEES if e.work_engagement_id
}
_EMPLOYEE_TO_WORKSPACE: dict[str, str] = {
    e.id: e.workspace_id for e in EMPLOYEES if e.workspace_id
}


def _backfill_tasks() -> None:
    for t in TASKS:
        if not t.assigned_user_id:
            t.assigned_user_id = _EMPLOYEE_TO_USER.get(t.assignee_id, "")
        if not t.workspace_id:
            t.workspace_id = _EMPLOYEE_TO_WORKSPACE.get(t.assignee_id, "ws-bernard")


_backfill_tasks()
