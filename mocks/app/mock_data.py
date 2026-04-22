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
  pay rules / bookings / payslips / expense claims key off
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
from datetime import date, datetime, time, timedelta
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
    icon_name: str = ""
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
class ScheduleRuleset:
    """Reusable recurring weekly rota keyed to a property (§06)."""

    id: str
    workspace_id: str
    name: str


@dataclass
class ScheduleRulesetSlot:
    """One weekday window inside a `ScheduleRuleset` (§06)."""

    id: str
    schedule_ruleset_id: str
    weekday: int  # 0..6 (Mon..Sun, ISO)
    starts_local: str  # HH:MM
    ends_local: str  # HH:MM, > starts_local


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
    `share_guest_identity` widens the §15 cross-workspace PII boundary
    so a managed/observer workspace sees the guest name / contact that
    would otherwise be redacted; defaults to False.
    """

    property_id: str
    workspace_id: str
    membership_role: Literal["owner_workspace", "managed_workspace", "observer_workspace"]
    share_guest_identity: bool = False
    invite_id: str | None = None
    added_at: datetime = field(default_factory=lambda: datetime(2026, 1, 1))
    added_by_user_id: str | None = None
    added_via: Literal["invite_accept", "system", "seed"] = "seed"


@dataclass
class PropertyWorkspaceInvite:
    """§22 two-sided invite used to materialise a non-owner
    `property_workspace` row. Owner workspace creates the invite, the
    target workspace's owners accept via the token URL, and only then
    the `property_workspace` junction row is written.
    """

    id: str
    token: str
    from_workspace_id: str
    property_id: str
    proposed_membership_role: Literal["managed_workspace", "observer_workspace"]
    created_by_user_id: str
    to_workspace_id: str | None = None
    initial_share_settings: dict = field(default_factory=lambda: {"share_guest_identity": False})
    state: Literal["pending", "accepted", "rejected", "revoked", "expired"] = "pending"
    created_at: datetime = field(default_factory=lambda: datetime(2026, 4, 10))
    expires_at: datetime = field(default_factory=lambda: datetime(2026, 4, 24))
    decided_at: datetime | None = None
    decided_by_user_id: str | None = None
    decision_note_md: str | None = None


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
    capabilities: dict[str, bool | None] = field(default_factory=dict)
    workspaces: list[str] = field(default_factory=list)
    villas: list[str] = field(default_factory=list)
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
    avatar_file_id: str | None = None  # §02, §12 — mirrors User.avatar_file_id for the compat projection


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
    # §06 "Self-created and personal tasks". `created_by` is the user
    # who originated the task (empty = seeded / system). Personal tasks
    # are visible only to the creator and workspace owners (§15 RLS).
    created_by: str = ""
    is_personal: bool = False


@dataclass
class Expense:
    """Per §09 an expense claim belongs to a `work_engagement` (not to
    a user directly). ``employee_id`` is kept for the legacy UI and
    mirrors the employee row's id; ``user_id`` and ``work_engagement_id``
    are the v1 canonical pointers.

    Multi-currency fields (§09 "Amount owed to the employee") are
    populated at approval time from the ``exchange_rate`` table:
    ``exchange_rate_to_default`` snaps claim→workspace default for
    reporting, and ``owed_*`` snaps claim→destination for the actual
    payout. Both are immutable once written.
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
    exchange_rate_to_default: float | None = None
    owed_currency: str | None = None
    owed_amount_cents: int | None = None
    owed_exchange_rate: float | None = None
    owed_rate_source: Literal["ecb", "manual", "stale_carryover"] | None = None


@dataclass
class ExchangeRate:
    """A snapped FX rate per §02 ``exchange_rate`` and §09 "Exchange
    rates service". Populated by the daily ``refresh_exchange_rates``
    worker job or the on-demand fallback at approval time.
    ``rate`` is expressed as ``1 {quote} = rate {base}``.
    """

    id: str
    workspace_id: str
    base: str
    quote: str
    as_of_date: date
    rate: float
    source: Literal["ecb", "manual", "stale_carryover"]
    fetched_at: datetime
    fetched_by_job: str | None = None
    source_ref: str | None = None


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
class AvailabilityOverride:
    """§06 — per-date override of a user's weekly availability.

    Mirrors `user_availability_overrides` with the approval-required
    field denormalised from the §06 "hybrid model" table. Server
    computes it on create; mocks seed it directly.
    """

    id: str
    user_id: str
    workspace_id: str
    date: date
    available: bool
    starts_local: str | None
    ends_local: str | None
    reason: str | None
    approval_required: bool
    approved_at: datetime | None
    approved_by: str | None
    created_at: datetime


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
    # §08 Inventory effects — list of `{item_ref, kind, qty}`. A
    # linen-change template declares one `consume` of clean sheets
    # and one `produce` of dirty sheets; the laundry template
    # declares the inverse.
    inventory_effects: list[dict] = field(default_factory=list)


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
    # §06 Ordered fallback list the assignment algorithm tries before
    # the generic candidate pool when the primary is unavailable.
    backup_assignee_user_ids: list[str] = field(default_factory=list)


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
    """Per-property stock row. Quantities are `float` (spec §08):
    a pool service consumes 0.3 bottles; a laundry run uses
    0.05 kg of detergent."""

    id: str
    property_id: str
    name: str
    sku: str
    on_hand: float
    par: float
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
    """Legacy shape kept for downstream pages that still read the flat row.

    The /admin/llm graph uses `LlmAssignment` (see below); this dataclass is
    derived from the same seed data at module load time.
    """

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
    # §11 "Cost tracking": the new FKs and chain depth. Nullable so legacy
    # rows still render.
    assignment_id: str | None = None
    provider_model_id: str | None = None
    prompt_template_id: str | None = None
    prompt_version: int | None = None
    fallback_attempts: int = 0
    raw_response_available: bool = False


# ---------------------------------------------------------------------------
# §11 — provider / model / provider-model registry (deployment-scope).
# The three-column graph on /admin/llm reads these dataclasses directly.
# ---------------------------------------------------------------------------


@dataclass
class LlmProvider:
    id: str
    name: str
    provider_type: Literal["openrouter", "openai_compatible", "fake"]
    endpoint: str
    api_key_ref: str | None
    api_key_status: Literal["present", "missing", "rotating"]
    default_model: str | None
    requests_per_minute: int
    timeout_s: int
    priority: int
    is_enabled: bool


@dataclass
class LlmModel:
    id: str
    canonical_name: str
    display_name: str
    vendor: str
    capabilities: list[str]                    # chat, vision, audio_input, reasoning, function_calling, json_mode, streaming
    context_window: int | None
    max_output_tokens: int | None
    price_source: Literal["openrouter", "manual", ""]
    price_source_model_id: str | None
    is_active: bool
    notes: str | None = None


@dataclass
class LlmProviderModel:
    id: str
    provider_id: str
    model_id: str
    api_model_id: str
    input_cost_per_million: float
    output_cost_per_million: float
    max_tokens_override: int | None
    temperature_override: float | None
    supports_system_prompt: bool
    supports_temperature: bool
    reasoning_effort: Literal["", "low", "medium", "high"]
    price_source_override: Literal["", "none", "openrouter"]
    price_last_synced_at: str | None
    is_enabled: bool


@dataclass
class LlmAssignment:
    id: str
    capability: str
    description: str
    priority: int                              # 0 = primary, 1 = first fallback, ...
    provider_model_id: str
    max_tokens: int | None
    temperature: float | None
    extra_api_params: dict[str, Any]
    required_capabilities: list[str]
    is_enabled: bool
    last_used_at: str | None
    # Observability — denormalised for the admin graph
    spend_usd_30d: float
    calls_30d: int


@dataclass
class LlmCapabilityInheritance:
    capability: str                            # child
    inherits_from: str                         # parent


@dataclass
class LlmPromptTemplate:
    id: str
    capability: str
    name: str
    version: int
    is_active: bool
    is_customised: bool                        # true when body hash ≠ default_hash
    default_hash: str
    updated_at: str
    revisions_count: int
    preview: str                               # first 160 chars of the current body


@dataclass
class WorkspaceUsage:
    """§11 — Workspace agent usage budget (manager-visible shape)."""

    percent: int
    paused: bool
    window_label: str


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
class ApiToken:
    """§03 API token, in all three kinds: scoped / delegated / personal.

    The wire shape mirrors ``web/src/types/api.ts::ApiToken``. This mock
    elides the argon2id hash (real server stores ``secret_hash`` +
    ``hash_params``) and keeps only the public half (``prefix``) plus
    the bookkeeping columns the UI renders.
    """

    id: str
    name: str
    kind: Literal["scoped", "delegated", "personal"]
    prefix: str
    scopes: list[str]
    created_by_user_id: str
    created_by_display: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    last_used_ip: str | None
    last_used_path: str | None
    revoked_at: datetime | None
    note: str | None
    ip_allowlist: list[str]


@dataclass
class ApiTokenAuditEntry:
    at: datetime
    method: str
    path: str
    status: int
    ip: str
    user_agent: str
    correlation_id: str


@dataclass
class Message:
    id: str
    from_: str
    body: str
    at: datetime


# ── Time / payroll entities ─────────────────────────────────────────

@dataclass
class Booking:
    """Worker × property × time-window commitment (§09).

    Replaces the v0 `shift` (clock-in / clock-out) entity. The booking
    is the canonical billable / payable atom: scheduled time IS paid
    time and billed time, by default. `actual_minutes` is set only
    when an amend records a real overrun / underrun.
    """

    id: str
    employee_id: str  # compat alias for the legacy employee row id
    property_id: str
    scheduled_start: datetime
    scheduled_end: datetime
    status: Literal[
        "pending_approval",
        "scheduled",
        "completed",
        "cancelled_by_client",
        "cancelled_by_agency",
        "no_show_worker",
        "adjusted",
    ]
    kind: Literal["work", "travel"] = "work"
    actual_minutes: int | None = None
    actual_minutes_paid: int | None = None  # defaults derive from scheduled if null
    break_seconds: int = 0
    pending_amend_minutes: int | None = None
    pending_amend_reason: str | None = None
    declined_at: datetime | None = None
    declined_reason: str | None = None
    notes_md: str = ""
    adjusted: bool = False
    adjustment_reason: str | None = None
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
    """`actor_kind` collapses to `user | agent | system` in v1 (§02).

    `reason` taxonomy matches §08 — `produce`, `theft`, `loss`,
    `found`, and `returned_to_vendor` were added alongside
    task-driven production and richer reconciliation. `delta` is
    `float` so fractional movements (0.3 L of window-washer) round-
    trip through the ledger.
    """

    id: str
    item_id: str
    delta: float
    reason: Literal[
        "restock",
        "consume",
        "produce",
        "waste",
        "theft",
        "loss",
        "found",
        "returned_to_vendor",
        "transfer_in",
        "transfer_out",
        "audit_correction",
        "adjust",
    ]
    actor_kind: Literal["user", "agent", "system"]
    actor_id: str
    occurred_at: datetime
    note: str | None = None
    source_task_id: str | None = None
    source_stocktake_id: str | None = None


@dataclass
class InventoryEffect:
    """An entry on `task_template.inventory_effects_json` or
    `asset_action.inventory_effects_json` (§08).

    `item_ref` is a SKU during authoring; resolved to an item id at
    task materialisation against the task's property. `qty` is
    strictly positive — the delta direction comes from `kind`.
    """

    item_ref: str
    kind: Literal["consume", "produce"]
    qty: float


@dataclass
class InventoryStocktake:
    """Property-wide reconciliation session — see §08 "Stocktake"."""

    id: str
    property_id: str
    started_at: datetime
    completed_at: datetime | None
    actor_kind: Literal["user", "agent"]
    actor_id: str
    note_md: str | None = None


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
    icon_name: str
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
    extraction_status: Literal[
        "pending", "extracting", "succeeded", "failed", "unsupported", "empty",
    ] = "pending"
    extracted_at: datetime | None = None


@dataclass
class FileExtraction:
    """Server-extracted body for an `asset_document` (§02 file_extraction)."""

    document_id: str
    extractor: Literal[
        "pypdf", "pdfminer", "python_docx", "openpyxl",
        "tesseract", "llm_vision", "passthrough",
    ] | None
    body_text: str
    pages: list[dict[str, int]]
    token_count: int
    has_secret_marker: bool = False
    last_error: str | None = None


@dataclass
class AgentDoc:
    """Code-shipped Markdown the chat agents read on demand (§02 agent_doc)."""

    slug: str
    title: str
    summary: str
    body_md: str
    roles: list[str]
    capabilities: list[str]
    version: int
    is_customised: bool
    default_hash: str
    updated_at: datetime


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
    # §22 cancellation policy — null falls through to workspace defaults
    # `bookings.cancellation_window_hours` / `bookings.cancellation_fee_pct`.
    cancellation_window_hours: int | None = None
    cancellation_fee_pct: int | None = None


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
class BookingBilling:
    """v1 (§22): per-(booking, client_org_id) snapshot written when a
    booking transitions to `completed` or, with `is_cancellation_fee`,
    when status flips to `cancelled_by_client` inside the cancellation
    window. Rate is snapshot at write time so later rate-card edits do
    not retroactively rewrite history.
    """

    id: str
    booking_id: str
    client_org_id: str
    user_id: str
    currency: str
    billable_minutes: int
    hourly_cents: int
    subtotal_cents: int
    rate_source: Literal["client_user_rate", "client_rate", "unpriced"]
    rate_source_id: str | None = None
    work_engagement_id: str = ""
    is_cancellation_fee: bool = False


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
    # §22 Proof-of-payment + reminders
    proof_of_payment_file_ids: list[str] = field(default_factory=list)
    reminder_last_sent_at: datetime | None = None
    reminder_next_due_at: datetime | None = None


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
    WorkRole("r-housekeeper", "ws-bernard", "maid",        "Housekeeper", icon_name="BrushCleaning"),
    WorkRole("r-cook",        "ws-bernard", "cook",        "Cook",        icon_name="Flame"),
    WorkRole("r-driver",      "ws-bernard", "driver",      "Driver",      icon_name="Car"),
    WorkRole("r-gardener",    "ws-bernard", "gardener",    "Gardener",    icon_name="Sprout"),
    WorkRole("r-handyman",    "ws-bernard", "handyman",    "Handyman",    icon_name="Wrench"),
    WorkRole("r-poolcare",    "ws-bernard", "pool_tech",   "Pool care",   icon_name="WavesLadder"),
    # VincentOps — Vincent's own workspace (just a driver for Rachid).
    WorkRole("wr-vincent-driver", "ws-vincent", "driver", "Driver", icon_name="Car"),
    # CleanCo — serves many clients, exposes a maid role.
    WorkRole("wr-cleanco-maid",   "ws-cleanco", "maid",   "Maid",   icon_name="BrushCleaning"),
]


def _caps(**overrides: bool | None) -> dict[str, bool | None]:
    base = {
        "tasks.photo_evidence": True,
        "tasks.allow_skip_with_reason": True,
        "messaging.comments": True,
        "messaging.report_issue": True,
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
        capabilities=_caps(**{"chat.assistant": True}),
        workspaces=["ws-bernard"],
        villas=["p-villa-sud", "p-apt-3b"],
        language="fr",
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
            "bookings.pay_basis": "scheduled",
            "bookings.auto_approve_overrun_minutes": 30,
        },
        user_id="u-maria", work_engagement_id="we-maria-bernard", workspace_id="ws-bernard",
    ),
    Employee(
        "e-arun", "Arun Patel", ["Driver"], ["p-villa-sud"],
        "AP", "+33 6 22 45 67 89", "arun@example.com", date(2024, 9, 14),
        capabilities=_caps(),
        workspaces=["ws-bernard"],
        villas=["p-villa-sud"],
        language="en",
        settings_override={},
        user_id="u-arun", work_engagement_id="we-arun-bernard", workspace_id="ws-bernard",
    ),
    Employee(
        "e-ben", "Ben Traoré", ["Gardener", "Pool care"], ["p-villa-sud"],
        "BT", "+33 6 33 56 78 90", "ben@example.com", date(2023, 5, 20),
        capabilities=_caps(),
        workspaces=["ws-bernard"],
        villas=["p-villa-sud"],
        language="fr",
        settings_override={
            "bookings.pay_basis": "actual",
        },
        user_id="u-ben", work_engagement_id="we-ben-bernard", workspace_id="ws-bernard",
    ),
    Employee(
        "e-ana", "Ana Rossi", ["Housekeeper", "Cook"], ["p-apt-3b", "p-chalet"],
        "AR", "+33 6 44 67 89 01", "ana@example.com", date(2024, 11, 2),
        capabilities=_caps(**{"chat.assistant": True, "voice.assistant": True}),
        workspaces=["ws-bernard"],
        villas=["p-apt-3b", "p-chalet"],
        language="fr",
        settings_override={
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
        language="fr",
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
        language="fr",
        user_id="u-rachid", work_engagement_id="we-rachid-vincent", workspace_id="ws-vincent",
    ),
    Employee(
        "e-joselyn", "Joselyn Rivera", ["Housekeeper"], ["p-villa-lac"],
        "JR", "+33 6 99 22 33 44", "joselyn@example.com", date(2025, 2, 1),
        capabilities=_caps(**{"chat.assistant": True}),
        workspaces=["ws-cleanco"],
        villas=["p-villa-lac"],
        language="es",
        weekly_availability={
            "mon": ("08:00", "16:00"),
            "tue": ("08:00", "16:00"),
            "wed": ("08:00", "16:00"),
            "thu": ("08:00", "16:00"),
            "fri": ("08:00", "16:00"),
            "sat": None,
            "sun": None,
        },
        user_id="u-joselyn", work_engagement_id="we-joselyn-cleanco", workspace_id="ws-cleanco",
    ),
    Employee(
        "e-julie", "Julie Moreau", ["Housekeeper"], [],
        "JM", "+33 6 44 33 22 11", "julie@example.com", date(2023, 9, 1),
        capabilities=_caps(**{"chat.assistant": True}),
        workspaces=["ws-cleanco"],
        villas=[],
        language="fr",
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
    # Élodie also holds an observer grant on CleanCo so the agency
    # she contracts with for cleaning Apt 3B is visible from her own
    # dashboard. Read-only — she does not dispatch CleanCo's workers,
    # she only audits the work CleanCo bills her for. This makes the
    # workspace switcher non-trivial for the manager persona too.
    RoleGrant("rg-elodie-observer-cleanco", "u-elodie", "workspace", "ws-cleanco",
              "client", binding_org_id="org-bernard-cleanco",
              started_on=date(2024, 6, 1), granted_by_user_id="u-julie"),
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
    ActionCatalogEntry("bookings.view_other",             "View another user's bookings.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§09"),
    ActionCatalogEntry("bookings.amend_other",            "Amend another user's booking time fields.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§09"),
    ActionCatalogEntry("bookings.assign_other",           "Reassign a booking to a different worker.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§09"),
    ActionCatalogEntry("bookings.cancel",                 "Cancel a booking on behalf of the workspace or a client.",
                       ["workspace", "property"],
                       ["owners", "managers"], spec="§09"),
    ActionCatalogEntry("bookings.create_pending",         "Propose an ad-hoc booking (status pending_approval until manager review).",
                       ["workspace", "property"],
                       ["owners", "managers", "all_workers"], spec="§09"),
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
    ActionCatalogEntry("inventory.stocktake",             "Open, edit, and commit an inventory stocktake session.",
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
    # Rachid drives for both Villa du Lac and Seaside Apt. No rota —
    # on-call driver; availability comes from user_weekly_availability
    # alone.
    PropertyWorkRoleAssignment("pwra-rachid-lac",     "uwr-rachid-driver", "p-villa-lac"),
    PropertyWorkRoleAssignment("pwra-rachid-seaside", "uwr-rachid-driver", "p-seaside"),
    # Joselyn is CleanCo's maid at Villa du Lac only. She follows the
    # "CleanCo weekday mornings" rota at the client property.
    PropertyWorkRoleAssignment("pwra-joselyn-lac",    "uwr-joselyn-maid",  "p-villa-lac",
                               schedule_ruleset_id="sr-cleanco-weekday-am"),
    # Maria splits her week across two Bernard properties — Villa Sud
    # in the morning, Apt 3B in the afternoon. This is the classic
    # agency rota the /scheduler page is built for.
    PropertyWorkRoleAssignment("pwra-maria-sud",      "uwr-maria-housekeeper", "p-villa-sud",
                               schedule_ruleset_id="sr-maria-villa-sud"),
    PropertyWorkRoleAssignment("pwra-maria-3b",       "uwr-maria-housekeeper", "p-apt-3b",
                               schedule_ruleset_id="sr-maria-apt-3b"),
]


SCHEDULE_RULESETS: list[ScheduleRuleset] = [
    ScheduleRuleset("sr-cleanco-weekday-am", "ws-cleanco",
                    "CleanCo weekday mornings"),
    ScheduleRuleset("sr-maria-villa-sud", "ws-bernard",
                    "Maria — Villa Sud mornings"),
    ScheduleRuleset("sr-maria-apt-3b", "ws-bernard",
                    "Maria — Apt 3B afternoons"),
]


SCHEDULE_RULESET_SLOTS: list[ScheduleRulesetSlot] = [
    # Joselyn at Villa du Lac: Mon / Wed / Fri 09:00–12:00.
    ScheduleRulesetSlot("srs-cleanco-mon", "sr-cleanco-weekday-am", 0, "09:00", "12:00"),
    ScheduleRulesetSlot("srs-cleanco-wed", "sr-cleanco-weekday-am", 2, "09:00", "12:00"),
    ScheduleRulesetSlot("srs-cleanco-fri", "sr-cleanco-weekday-am", 4, "09:00", "12:00"),
    # Maria at Villa Sud: weekday mornings 08:30–12:00 (disjoint from
    # Apt 3B in the afternoon — no overlap per §06 invariant).
    ScheduleRulesetSlot("srs-maria-sud-mon", "sr-maria-villa-sud", 0, "08:30", "12:00"),
    ScheduleRulesetSlot("srs-maria-sud-tue", "sr-maria-villa-sud", 1, "08:30", "12:00"),
    ScheduleRulesetSlot("srs-maria-sud-thu", "sr-maria-villa-sud", 3, "08:30", "12:00"),
    ScheduleRulesetSlot("srs-maria-sud-fri", "sr-maria-villa-sud", 4, "08:30", "12:00"),
    # Maria at Apt 3B: weekday afternoons 14:00–17:00.
    ScheduleRulesetSlot("srs-maria-3b-mon", "sr-maria-apt-3b", 0, "14:00", "17:00"),
    ScheduleRulesetSlot("srs-maria-3b-tue", "sr-maria-apt-3b", 1, "14:00", "17:00"),
    ScheduleRulesetSlot("srs-maria-3b-thu", "sr-maria-apt-3b", 3, "14:00", "17:00"),
    ScheduleRulesetSlot("srs-maria-3b-fri", "sr-maria-apt-3b", 4, "14:00", "17:00"),
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
    UserWorkspace("u-elodie",  "ws-cleanco", "workspace_grant"),  # client grant on CleanCo
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
    # §06 "Self-created and personal tasks" — seeded quick-add entries
    # that exercise the `is_personal` visibility rule across roles.
    Task(
        "t-p-maria-1", "Dentist appointment", "", "", "e-maria",
        _t(17, 30), 60, "normal", "pending",
        assigned_user_id="u-maria",
        created_by="u-maria",
        is_personal=True,
    ),
    Task(
        "t-p-elodie-1", "Call accountant about Q1 VAT", "", "", "",
        _t(15, 0), 30, "normal", "pending",
        assigned_user_id="u-elodie",
        created_by="u-elodie",
        is_personal=True,
    ),
]


EXPENSES: list[Expense] = [
    Expense("x-1", "e-maria", 4280, "EUR", "Carrefour",   datetime(2026, 4, 14, 17, 32), "submitted", "Cleaning supplies — bleach, sponges, 2× fresh towels", ocr_confidence=0.96, category="supplies",
            user_id="u-maria", work_engagement_id="we-maria-bernard"),
    # Same-currency approved claim: claim ccy == workspace default == destination ccy.
    # exchange_rate_to_default = 1.0, owed_* mirror the claim total.
    Expense("x-2", "e-arun",  1890, "EUR", "Total Energies", datetime(2026, 4, 13, 19, 5), "approved", "Fuel — Johnson airport run", ocr_confidence=0.99, category="fuel",
            user_id="u-arun", work_engagement_id="we-arun-bernard",
            exchange_rate_to_default=1.0,
            owed_currency="EUR", owed_amount_cents=1890,
            owed_exchange_rate=1.0, owed_rate_source="ecb"),
    Expense("x-3", "e-ben",  12500, "EUR", "Pool Pro",    datetime(2026, 4, 10, 11, 22), "submitted", "Chlorine tablets (3 month supply) + replacement skimmer basket", ocr_confidence=0.94, category="maintenance",
            user_id="u-ben", work_engagement_id="we-ben-bernard"),
    Expense("x-4", "e-ana",   2210, "EUR", "Marché Provence", datetime(2026, 4, 11, 9, 40), "approved", "Welcome-basket groceries — Apt 3B", category="food",
            user_id="u-ana", work_engagement_id="we-ana-bernard",
            exchange_rate_to_default=1.0,
            owed_currency="EUR", owed_amount_cents=2210,
            owed_exchange_rate=1.0, owed_rate_source="ecb"),
    Expense("x-5", "e-sam",   5780, "EUR", "Brico Dépôt", datetime(2026, 4, 9, 14, 58), "reimbursed", "Door handles, screws, wood filler", category="maintenance",
            user_id="u-sam", work_engagement_id="we-sam-bernard",
            exchange_rate_to_default=1.0,
            owed_currency="EUR", owed_amount_cents=5780,
            owed_exchange_rate=1.0, owed_rate_source="ecb"),
    # Multi-currency demo: Maria bought London hardware in GBP while
    # escorting a guest to Heathrow. Claim ccy = GBP, workspace default
    # = EUR, her reimbursement destination = EUR. Approved → both snaps
    # written at 2026-04-15 ECB rate (1 GBP = 1.1972 EUR).
    Expense("x-6", "e-maria", 2850, "GBP", "Screwfix Hammersmith",
            datetime(2026, 4, 15, 14, 12), "approved",
            "Replacement door handles & anchors for Apt 3B (stocked up while on the London trip).",
            ocr_confidence=0.92, category="maintenance",
            user_id="u-maria", work_engagement_id="we-maria-bernard",
            exchange_rate_to_default=1.1972,
            owed_currency="EUR", owed_amount_cents=3412,
            owed_exchange_rate=1.1972, owed_rate_source="ecb"),
    # Multi-currency pending: Arun filled the rental in USD on a US
    # courier trip. Claim ccy = USD, destination = EUR. Submitted only,
    # so owed_* is still unset — we'll snap on approval.
    Expense("x-7", "e-arun", 4200, "USD", "Shell US",
            datetime(2026, 4, 16, 11, 3), "submitted",
            "Rental-car fuel, Newark → Greenwich courier run.",
            ocr_confidence=0.97, category="fuel",
            user_id="u-arun", work_engagement_id="we-arun-bernard"),
]


# Exchange rates seed (§02 exchange_rate, §09 "Exchange rates service").
# All rows have workspace_id = "w-bernard" (the primary demo workspace)
# and base = "EUR" (its default currency). Populated by the daily
# refresh_exchange_rates job — sources: ecb for working days,
# stale_carryover for weekends, one manual override example.
EXCHANGE_RATES: list[ExchangeRate] = [
    # Friday 2026-04-17 — most recent working day.
    ExchangeRate("fx-1", "w-bernard", "EUR", "GBP", date(2026, 4, 17),
                 1.1972, "ecb",
                 datetime(2026, 4, 17, 17, 1),
                 fetched_by_job="refresh_exchange_rates"),
    ExchangeRate("fx-2", "w-bernard", "EUR", "USD", date(2026, 4, 17),
                 0.9248, "ecb",
                 datetime(2026, 4, 17, 17, 1),
                 fetched_by_job="refresh_exchange_rates"),
    ExchangeRate("fx-3", "w-bernard", "EUR", "CHF", date(2026, 4, 17),
                 1.0312, "ecb",
                 datetime(2026, 4, 17, 17, 1),
                 fetched_by_job="refresh_exchange_rates"),
    # Saturday 2026-04-18 — stale_carryover from Friday.
    ExchangeRate("fx-4", "w-bernard", "EUR", "GBP", date(2026, 4, 18),
                 1.1972, "stale_carryover",
                 datetime(2026, 4, 18, 17, 0),
                 fetched_by_job="refresh_exchange_rates"),
    ExchangeRate("fx-5", "w-bernard", "EUR", "USD", date(2026, 4, 18),
                 0.9248, "stale_carryover",
                 datetime(2026, 4, 18, 17, 0),
                 fetched_by_job="refresh_exchange_rates"),
    # 2026-04-15 ECB fix — the rate snapped on the x-6 GBP claim above.
    ExchangeRate("fx-6", "w-bernard", "EUR", "GBP", date(2026, 4, 15),
                 1.1972, "ecb",
                 datetime(2026, 4, 15, 17, 2),
                 fetched_by_job="refresh_exchange_rates"),
    # Manual override example: ECB does not publish XAF; owner entered.
    ExchangeRate("fx-7", "w-bernard", "EUR", "XAF", date(2026, 4, 17),
                 0.001524, "manual",
                 datetime(2026, 4, 17, 10, 44),
                 source_ref="u-bernard"),
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
        "Monthly pay run. Totals within 4% of last month. No pending bookings or amends.",
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
    Leave("lv-5", "e-maria", date(2026, 4, 17), date(2026, 4, 17), "personal",  "School run — parent-teacher",
          user_id="u-maria"),
]


AVAILABILITY_OVERRIDES: list[AvailabilityOverride] = [
    AvailabilityOverride(
        "ao-maria-wed", "u-maria", "ws-bernard", date(2026, 4, 15),
        available=True, starts_local="10:00", ends_local="14:00",
        reason="Covering for Ben — happy to do a half day",
        approval_required=False,
        approved_at=datetime(2026, 4, 10, 18, 20),
        approved_by="u-maria",
        created_at=datetime(2026, 4, 10, 18, 20),
    ),
    AvailabilityOverride(
        "ao-maria-thu", "u-maria", "ws-bernard", date(2026, 4, 16),
        available=True, starts_local="09:00", ends_local="14:00",
        reason="Dentist in the late afternoon",
        approval_required=True,
        approved_at=None,
        approved_by=None,
        created_at=datetime(2026, 4, 14, 9, 5),
    ),
]


CLOSURES: list[PropertyClosure] = [
    PropertyClosure("cl-1", "p-chalet",    date(2026, 4, 10), date(2026, 4, 18), "seasonal",         "Between ski and summer seasons"),
    PropertyClosure("cl-2", "p-villa-sud", date(2026, 4, 22), date(2026, 4, 23), "renovation",       "Painter in for touch-ups"),
    PropertyClosure("cl-3", "p-apt-3b",    date(2026, 4, 29), date(2026, 4, 30), "ical_unavailable", "Imported from Airbnb — blocked window"),
]


TEMPLATES: list[TaskTemplate] = [
    # §08 Inventory effects — `consume` + `produce` entries
    # declare what a task uses and outputs at completion. Clean /
    # dirty are distinct SKUs so a turnover consumes the clean
    # set and produces the dirty set; the laundry template below
    # does the inverse.
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
                 ],
                 inventory_effects=[
                     {"item_ref": "LINEN-Q-CLEAN", "kind": "consume", "qty": 1.0},
                     {"item_ref": "LINEN-Q-DIRTY", "kind": "produce", "qty": 1.0},
                     {"item_ref": "TOWEL-L-CLEAN", "kind": "consume", "qty": 4.0},
                     {"item_ref": "TOWEL-L-DIRTY", "kind": "produce", "qty": 4.0},
                     {"item_ref": "TP-12",         "kind": "consume", "qty": 0.5},
                     {"item_ref": "WINDOW-WASHER", "kind": "consume", "qty": 0.3},
                 ]),
    TaskTemplate("tpl-linen-change", "Linen change — master bedroom",
                 "Swap bedding and towels, including fitted sheet orientation.", "Housekeeper",
                 25, "any", "required", "normal",
                 checklist=[{"label": "Strip bed"}, {"label": "Fresh sheets"}, {"label": "Replace towels"}, {"label": "Photo of finished bed"}],
                 inventory_effects=[
                     {"item_ref": "LINEN-Q-CLEAN", "kind": "consume", "qty": 1.0},
                     {"item_ref": "LINEN-Q-DIRTY", "kind": "produce", "qty": 1.0},
                     {"item_ref": "TOWEL-L-CLEAN", "kind": "consume", "qty": 2.0},
                     {"item_ref": "TOWEL-L-DIRTY", "kind": "produce", "qty": 2.0},
                 ]),
    TaskTemplate("tpl-laundry", "Laundry cycle — sheets & towels",
                 "Run dirty linen through the washer + dryer. Fold and return clean stock to the linen cupboard.",
                 "Housekeeper",
                 80, "one", "optional", "normal",
                 checklist=[
                     {"label": "Load washer"},
                     {"label": "Dose detergent"},
                     {"label": "Transfer to dryer"},
                     {"label": "Fold and shelve"},
                 ],
                 inventory_effects=[
                     {"item_ref": "LINEN-Q-DIRTY", "kind": "consume", "qty": 2.0},
                     {"item_ref": "LINEN-Q-CLEAN", "kind": "produce", "qty": 2.0},
                     {"item_ref": "TOWEL-L-DIRTY", "kind": "consume", "qty": 4.0},
                     {"item_ref": "TOWEL-L-CLEAN", "kind": "produce", "qty": 4.0},
                     {"item_ref": "DETERGENT",     "kind": "consume", "qty": 0.15},
                 ]),
    TaskTemplate("tpl-pool-weekly", "Pool service — weekly",
                 "Skim, test pH and chlorine, check skimmer baskets.", "Pool care",
                 30, "one", "optional", "normal",
                 checklist=[{"label": "Skim"}, {"label": "pH"}, {"label": "Chlorine"}, {"label": "Skimmer"}],
                 inventory_effects=[
                     {"item_ref": "POOL-CL", "kind": "consume", "qty": 0.25},
                 ]),
    TaskTemplate("tpl-airport", "Airport pickup / drop-off",
                 "Standard guest transfer. Sign with family name at arrivals.", "Driver",
                 90, "any", "disabled", "high"),
    TaskTemplate("tpl-garden", "Garden upkeep", "Mow, trim, water — as needed.", "Gardener",
                 60, "one", "optional", "low"),
    # §06 Checklist template shape — one coherent maintenance template
    # whose items carry per-item RRULEs so one weekly schedule
    # generates the right work for each visit without splitting the
    # regime across three parent schedules.
    TaskTemplate("tpl-home-maint", "Home maintenance — Villa du Lac",
                 "Recurring maintenance regime. Items appear on each Monday visit "
                 "based on their individual RRULE.",
                 "Housekeeper",
                 45, "one", "optional", "normal",
                 checklist=[
                     # No RRULE → appears every visit.
                     {"key": "counters",       "label": "Wipe counters and surfaces",
                      "required": True},
                     # Monthly — 1st Monday.
                     {"key": "clean_fridge",   "label": "Clean fridge (interior)",
                      "required": True,
                      "rrule": "FREQ=MONTHLY;BYDAY=1MO",
                      "dtstart_local": "2026-01-05"},
                     # Every 2 weeks, anchored to the schedule's dtstart.
                     {"key": "clean_filter",   "label": "Clean air purifier filter",
                      "required": False,
                      "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO",
                      "dtstart_local": "2026-01-05"},
                     # Every 6 months.
                     {"key": "replace_filter", "label": "Replace air purifier filter",
                      "required": True,
                      "rrule": "FREQ=MONTHLY;INTERVAL=6;BYDAY=1MO",
                      "dtstart_local": "2026-01-05"},
                 ]),
]


SCHEDULES: list[Schedule] = [
    Schedule("sch-pool-sat", "Villa Sud pool — Saturdays 09:00", "tpl-pool-weekly",
             "p-villa-sud", "Every Saturday at 09:00", "e-ben", 30, date(2024, 4, 1)),
    Schedule("sch-linen-mon-thu", "Villa Sud linen — Mon & Thu 10:30", "tpl-linen-change",
             "p-villa-sud", "Weekly on Mon, Thu at 10:30", "e-maria", 25, date(2024, 3, 1),
             # §06 Ordered backups: if Maria is on leave, try Ana before falling
             # back to the generic candidate pool.
             backup_assignee_user_ids=["u-ana"]),
    Schedule("sch-garden-sat", "Villa Sud garden — Saturdays 08:00", "tpl-garden",
             "p-villa-sud", "Every Saturday at 08:00", "e-ben", 60, date(2024, 4, 1), paused=True),
    Schedule("sch-apt-turnover", "Apt 3B turnover (auto from stays)", "tpl-turnover",
             "p-apt-3b", "Triggered by stay check-out", "e-ana", 120, date(2025, 1, 1)),
    # §06 "Checklist template shape" — home-maintenance template whose
    # checklist items carry per-item RRULEs (fridge monthly, filter
    # bi-weekly, filter replaced every 6 months). See tpl-home-maint.
    Schedule("sch-home-maint-mon", "Villa du Lac maintenance — Mondays 10:00", "tpl-home-maint",
             "p-villa-lac", "Every Monday at 10:00", "u-joselyn", 45, date(2026, 1, 5),
             backup_assignee_user_ids=[]),
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
    # §08 — clean / dirty are distinct SKUs so a sheet change can
    # `consume` one and `produce` the other, and laundry the
    # reverse. Quantities are `float`: pool chemicals and chemicals
    # in general are fractional, window-washer drains in tenths.
    InventoryItem("inv-1",  "p-villa-sud", "Bed sheet set, queen — clean", "LINEN-Q-CLEAN", 3.0,   6.0, "sets", "Linen cupboard A"),
    InventoryItem("inv-1d", "p-villa-sud", "Bed sheet set, queen — dirty", "LINEN-Q-DIRTY", 2.0,   0.0, "sets", "Laundry hamper"),
    InventoryItem("inv-2",  "p-villa-sud", "Bath towels (L) — clean",      "TOWEL-L-CLEAN", 12.0, 16.0, "pcs",  "Linen cupboard A"),
    InventoryItem("inv-2d", "p-villa-sud", "Bath towels (L) — dirty",      "TOWEL-L-DIRTY",  4.0,  0.0, "pcs",  "Laundry hamper"),
    InventoryItem("inv-3",  "p-villa-sud", "Chlorine tablets",             "POOL-CL",        1.0,  2.0, "box",  "Pool shed"),
    InventoryItem("inv-4",  "p-villa-sud", "Toilet paper",                 "TP-12",          2.0,  4.0, "pack", "Utility"),
    InventoryItem("inv-9",  "p-villa-sud", "Window washer",                "WINDOW-WASHER",  1.7,  2.0, "L",    "Utility"),
    InventoryItem("inv-10", "p-villa-sud", "Laundry detergent",            "DETERGENT",      3.5,  5.0, "kg",   "Utility"),
    InventoryItem("inv-5",  "p-apt-3b",    "Bed sheet set, double — clean", "LINEN-D-CLEAN", 4.0,  4.0, "sets", "Hall closet"),
    InventoryItem("inv-5d", "p-apt-3b",    "Bed sheet set, double — dirty", "LINEN-D-DIRTY", 1.0,  0.0, "sets", "Laundry hamper"),
    InventoryItem("inv-6",  "p-apt-3b",    "Coffee pods",                  "COF-NESP",      24.0, 30.0, "pcs",  "Kitchen"),
    InventoryItem("inv-7",  "p-apt-3b",    "Welcome-basket wine",          "WINE-RED",       2.0,  3.0, "btl",  "Kitchen"),
    InventoryItem("inv-8",  "p-chalet",    "Firewood",                     "FW-STR",         0.5,  4.0, "stère","Ski room"),
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


# ---------------------------------------------------------------------------
# Graph seed — providers / models / provider-models.
# The /admin/llm page renders these as three columns with hover-linked edges.
# ---------------------------------------------------------------------------


LLM_PROVIDERS: list[LlmProvider] = [
    LlmProvider(
        id="prov-openrouter",
        name="OpenRouter",
        provider_type="openrouter",
        endpoint="https://openrouter.ai/api/v1",
        api_key_ref="envelope:llm:openrouter:default",
        api_key_status="present",
        default_model="google/gemma-3-27b-it",
        requests_per_minute=60,
        timeout_s=60,
        priority=0,
        is_enabled=True,
    ),
    LlmProvider(
        id="prov-openai-compat",
        name="OpenAI-compatible fallback",
        provider_type="openai_compatible",
        endpoint="",
        api_key_ref=None,
        api_key_status="missing",
        default_model=None,
        requests_per_minute=60,
        timeout_s=60,
        priority=1,
        is_enabled=False,
    ),
]


LLM_MODELS: list[LlmModel] = [
    LlmModel(
        id="mdl-gemma-3-27b-it",
        canonical_name="google/gemma-3-27b-it",
        display_name="Gemma 3 27B IT",
        vendor="google",
        capabilities=["chat", "vision", "json_mode", "function_calling", "streaming"],
        context_window=128_000,
        max_output_tokens=8192,
        price_source="openrouter",
        price_source_model_id=None,
        is_active=True,
    ),
    LlmModel(
        id="mdl-gemma-3-27b-it-free",
        canonical_name="google/gemma-3-27b-it:free",
        display_name="Gemma 3 27B IT (free tier)",
        vendor="google",
        capabilities=["chat", "vision", "json_mode", "streaming"],
        context_window=128_000,
        max_output_tokens=4096,
        price_source="openrouter",
        price_source_model_id=None,
        is_active=True,
        notes="Demo-deployment default; rate-limited by OpenRouter.",
    ),
    LlmModel(
        id="mdl-haiku-4-5",
        canonical_name="anthropic/claude-haiku-4-5",
        display_name="Claude Haiku 4.5",
        vendor="anthropic",
        capabilities=["chat", "vision", "json_mode", "function_calling", "streaming"],
        context_window=200_000,
        max_output_tokens=8192,
        price_source="openrouter",
        price_source_model_id=None,
        is_active=True,
    ),
    LlmModel(
        id="mdl-qwen3-32b",
        canonical_name="qwen/qwen3-32b-instruct",
        display_name="Qwen 3 32B Instruct",
        vendor="qwen",
        capabilities=["chat", "json_mode", "function_calling", "streaming"],
        context_window=131_072,
        max_output_tokens=8192,
        price_source="openrouter",
        price_source_model_id=None,
        is_active=True,
    ),
    LlmModel(
        id="mdl-gpt-4o-mini",
        canonical_name="openai/gpt-4o-mini",
        display_name="GPT-4o mini",
        vendor="openai",
        capabilities=["chat", "vision", "json_mode", "function_calling", "streaming"],
        context_window=128_000,
        max_output_tokens=16_384,
        price_source="openrouter",
        price_source_model_id=None,
        is_active=True,
    ),
    # Unassigned on purpose — shows the "no audio-input model assigned yet" UX.
    LlmModel(
        id="mdl-whisper-v3",
        canonical_name="openai/whisper-large-v3",
        display_name="Whisper Large v3",
        vendor="openai",
        capabilities=["audio_input"],
        context_window=None,
        max_output_tokens=None,
        price_source="",
        price_source_model_id=None,
        is_active=False,
        notes="Not yet wired up — voice.transcribe is disabled in v1.",
    ),
]


LLM_PROVIDER_MODELS: list[LlmProviderModel] = [
    LlmProviderModel(
        id="pm-or-gemma-27b",
        provider_id="prov-openrouter",
        model_id="mdl-gemma-3-27b-it",
        api_model_id="google/gemma-3-27b-it",
        input_cost_per_million=0.10,
        output_cost_per_million=0.30,
        max_tokens_override=None,
        temperature_override=None,
        supports_system_prompt=True,
        supports_temperature=True,
        reasoning_effort="",
        price_source_override="",
        price_last_synced_at="2026-04-14T03:00:12Z",
        is_enabled=True,
    ),
    LlmProviderModel(
        id="pm-or-gemma-27b-free",
        provider_id="prov-openrouter",
        model_id="mdl-gemma-3-27b-it-free",
        api_model_id="google/gemma-3-27b-it:free",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
        max_tokens_override=None,
        temperature_override=None,
        supports_system_prompt=True,
        supports_temperature=True,
        reasoning_effort="",
        price_source_override="",
        price_last_synced_at="2026-04-14T03:00:12Z",
        is_enabled=True,
    ),
    LlmProviderModel(
        id="pm-or-haiku-4-5",
        provider_id="prov-openrouter",
        model_id="mdl-haiku-4-5",
        api_model_id="anthropic/claude-haiku-4-5",
        input_cost_per_million=0.80,
        output_cost_per_million=4.00,
        max_tokens_override=None,
        temperature_override=0.3,
        supports_system_prompt=True,
        supports_temperature=True,
        reasoning_effort="",
        price_source_override="",
        price_last_synced_at="2026-04-14T03:00:12Z",
        is_enabled=True,
    ),
    LlmProviderModel(
        id="pm-or-qwen3-32b",
        provider_id="prov-openrouter",
        model_id="mdl-qwen3-32b",
        api_model_id="qwen/qwen3-32b-instruct",
        input_cost_per_million=0.12,
        output_cost_per_million=0.35,
        max_tokens_override=None,
        temperature_override=None,
        supports_system_prompt=True,
        supports_temperature=True,
        reasoning_effort="",
        price_source_override="none",     # Admin has pinned this row; skipped by the sync.
        price_last_synced_at=None,
        is_enabled=True,
    ),
    LlmProviderModel(
        id="pm-or-gpt-4o-mini",
        provider_id="prov-openrouter",
        model_id="mdl-gpt-4o-mini",
        api_model_id="openai/gpt-4o-mini",
        input_cost_per_million=0.15,
        output_cost_per_million=0.60,
        max_tokens_override=None,
        temperature_override=None,
        supports_system_prompt=True,
        supports_temperature=True,
        reasoning_effort="",
        price_source_override="",
        price_last_synced_at="2026-04-14T03:00:12Z",
        is_enabled=True,
    ),
]


# Capability catalogue — mirrors docs/specs/11 "Capability catalog".
# Drives the "required_capabilities" validation in the graph.
LLM_CAPABILITY_CATALOGUE: list[dict[str, Any]] = [
    {"key": "tasks.nl_intake",      "description": "Parse free-text into task/template/schedule drafts",         "required_capabilities": ["chat", "json_mode"]},
    {"key": "tasks.assist",         "description": "Staff chat assistant: explain an instruction, etc.",         "required_capabilities": ["chat"]},
    {"key": "digest.manager",       "description": "Morning manager digest composition",                         "required_capabilities": ["chat"]},
    {"key": "digest.employee",      "description": "Morning employee digest composition",                        "required_capabilities": ["chat"]},
    {"key": "anomaly.detect",       "description": "Compare recent completions to schedule and flag issues",     "required_capabilities": ["chat", "json_mode"]},
    {"key": "expenses.autofill",    "description": "OCR + structure a receipt image",                            "required_capabilities": ["vision", "json_mode"]},
    {"key": "instructions.draft",   "description": "Suggest an instruction from a conversation",                 "required_capabilities": ["chat"]},
    {"key": "issue.triage",         "description": "Classify severity/category of a reported issue",             "required_capabilities": ["chat", "json_mode"]},
    {"key": "stay.summarize",       "description": "Summarize a stay for a guest welcome blurb",                 "required_capabilities": ["chat"]},
    {"key": "voice.transcribe",     "description": "Turn a voice note into text",                                "required_capabilities": ["audio_input"]},
    {"key": "chat.manager",         "description": "Manager-side embedded agent (full manager tool surface)",    "required_capabilities": ["chat", "function_calling"]},
    {"key": "chat.employee",        "description": "Employee-side embedded agent (full employee tool surface)",  "required_capabilities": ["chat", "function_calling"]},
    {"key": "chat.admin",           "description": "Deployment-admin embedded agent (full admin tool surface)",  "required_capabilities": ["chat", "function_calling"]},
    {"key": "chat.compact",         "description": "Summarize resolved chat topics (hourly compaction)",         "required_capabilities": ["chat"]},
    {"key": "chat.detect_language", "description": "Detect message language for auto-translation",               "required_capabilities": ["chat", "json_mode"]},
    {"key": "chat.translate",       "description": "Translate message to workspace default language",            "required_capabilities": ["chat"]},
]


LLM_CAPABILITY_INHERITANCE: list[LlmCapabilityInheritance] = [
    LlmCapabilityInheritance(capability="chat.admin", inherits_from="chat.manager"),
]


LLM_ASSIGNMENTS_GRAPH: list[LlmAssignment] = [
    # Primary assignments + a sprinkling of fallbacks. chat.admin has no row
    # of its own — it flows through the inheritance edge to chat.manager.
    LlmAssignment("as-nl",       "tasks.nl_intake",      "Parse free-text into task/template/schedule drafts",     0, "pm-or-gemma-27b",     None, 0.2, {}, ["chat", "json_mode"],        True, "2026-04-18T09:54:11Z", 6.60, 540),
    LlmAssignment("as-assist",   "tasks.assist",         "Staff chat assistant",                                   0, "pm-or-gemma-27b",     None, 0.3, {}, ["chat"],                     True, "2026-04-18T09:58:03Z", 12.30, 960),
    LlmAssignment("as-digest-m", "digest.manager",       "Morning manager digest (primary)",                       0, "pm-or-haiku-4-5",     None, 0.3, {}, ["chat"],                     True, "2026-04-18T06:02:00Z", 2.40, 60),
    LlmAssignment("as-digest-mf","digest.manager",       "Fallback when Haiku is rate-limited",                    1, "pm-or-gemma-27b",     None, 0.3, {}, ["chat"],                     True, None,                     0.00, 0),
    LlmAssignment("as-digest-e", "digest.employee",      "Morning employee digest",                                0, "pm-or-gemma-27b",     None, 0.3, {}, ["chat"],                     True, "2026-04-18T06:04:42Z", 3.00, 150),
    LlmAssignment("as-anomaly",  "anomaly.detect",       "Anomaly candidate ranker",                               0, "pm-or-qwen3-32b",     None, 0.1, {}, ["chat", "json_mode"],        True, "2026-04-17T23:02:11Z", 0.00, 0),
    LlmAssignment("as-exp",      "expenses.autofill",    "Receipt OCR + structure",                                0, "pm-or-gemma-27b",     None, 0.2, {}, ["vision", "json_mode"],      True, "2026-04-18T08:54:01Z", 9.30, 360),
    LlmAssignment("as-instr",    "instructions.draft",   "Instruction suggestion",                                 0, "pm-or-gemma-27b",     None, 0.4, {}, ["chat"],                     True, "2026-04-17T14:20:11Z", 0.60, 30),
    LlmAssignment("as-issue",    "issue.triage",         "Severity/category classifier",                           0, "pm-or-gemma-27b",     None, 0.1, {}, ["chat", "json_mode"],        True, "2026-04-18T08:06:30Z", 0.30, 90),
    LlmAssignment("as-stay",     "stay.summarize",       "Guest welcome blurb",                                    0, "pm-or-gemma-27b",     None, 0.4, {}, ["chat"],                     True, None,                     0.00, 0),
    LlmAssignment("as-chat-m",   "chat.manager",         "Manager embedded agent",                                 0, "pm-or-gemma-27b",     None, 0.2, {}, ["chat", "function_calling"],  True, "2026-04-18T10:06:44Z", 16.50, 420),
    LlmAssignment("as-chat-mf",  "chat.manager",         "Fallback model when upstream 5xx's",                     1, "pm-or-gpt-4o-mini",   None, 0.2, {}, ["chat", "function_calling"],  True, None,                     0.00, 0),
    LlmAssignment("as-chat-e",   "chat.employee",        "Worker embedded agent",                                  0, "pm-or-gemma-27b",     None, 0.2, {}, ["chat", "function_calling"],  True, "2026-04-18T09:41:22Z", 11.40, 840),
    LlmAssignment("as-compact",  "chat.compact",         "Topic compaction",                                       0, "pm-or-gemma-27b",     None, 0.2, {}, ["chat"],                      True, "2026-04-18T01:00:00Z", 1.20, 60),
    LlmAssignment("as-detect",   "chat.detect_language", "Message language detection",                             0, "pm-or-gemma-27b",     None, 0.0, {}, ["chat", "json_mode"],         True, "2026-04-18T10:01:11Z", 0.60, 240),
    LlmAssignment("as-translate","chat.translate",       "Message translation",                                    0, "pm-or-gemma-27b",     None, 0.3, {}, ["chat"],                      True, "2026-04-18T09:30:18Z", 1.80, 180),
    # voice.transcribe has no assignment — graph shows "unassigned" pill.
]


LLM_PROMPT_TEMPLATES: list[LlmPromptTemplate] = [
    LlmPromptTemplate("pt-nl",        "tasks.nl_intake",      "Tasks NL intake",         2, True, True,  "a01f2e", "2026-04-10T11:02:00Z", 1,
                      "You convert natural-language household requests into structured task drafts. Prefer conservative assumptions and surface ambiguities..."),
    LlmPromptTemplate("pt-assist",    "tasks.assist",         "Tasks assistant",         1, True, False, "b12e3d", "2026-03-01T14:18:00Z", 0,
                      "You are the staff chat assistant. Answer concisely about the current user's bookings, tasks, and instructions..."),
    LlmPromptTemplate("pt-digest-m",  "digest.manager",       "Manager digest",          1, True, False, "c93a41", "2026-03-02T07:20:00Z", 0,
                      "Compose a short manager digest from the structured data block. Never invent numbers; if the data is empty, say so..."),
    LlmPromptTemplate("pt-digest-e",  "digest.employee",      "Employee digest",         1, True, False, "d72b55", "2026-03-02T07:20:00Z", 0,
                      "Compose the worker's morning digest. Warm tone, one short paragraph, end with the top three tasks for the day..."),
    LlmPromptTemplate("pt-anomaly",   "anomaly.detect",       "Anomaly ranker",          1, True, False, "e51c72", "2026-03-04T09:00:00Z", 0,
                      "Rank candidate anomalies by severity. Return a JSON list with {subject_id, kind, one_line_explanation}..."),
    LlmPromptTemplate("pt-exp",       "expenses.autofill",    "Receipt OCR",             3, True, True,  "f44d89", "2026-04-12T16:40:00Z", 2,
                      "Extract vendor, amount_minor, currency, date, and category from the receipt image. Return JSON matching the schema..."),
    LlmPromptTemplate("pt-instr",     "instructions.draft",   "Instruction drafter",     1, True, False, "11aabb", "2026-03-05T10:00:00Z", 0,
                      "From the manager-worker exchange, propose a crisp instruction with optional photo-evidence flag..."),
    LlmPromptTemplate("pt-issue",     "issue.triage",         "Issue triage",            1, True, False, "22bbcc", "2026-03-05T10:00:00Z", 0,
                      "Classify the reported issue. Return JSON with {category, severity ∈ {low,medium,high}, suggested_owner_role}..."),
    LlmPromptTemplate("pt-stay",      "stay.summarize",       "Stay summariser",         1, True, False, "33ccdd", "2026-03-05T10:00:00Z", 0,
                      "Draft a warm, factual welcome blurb for the upcoming stay. Use only the data provided..."),
    LlmPromptTemplate("pt-chat-m",    "chat.manager",         "Manager embedded agent",  2, True, True,  "44ddee", "2026-04-11T18:32:00Z", 1,
                      "You are the manager's embedded agent. You hold a delegated token with the caller's full authority..."),
    LlmPromptTemplate("pt-chat-e",    "chat.employee",        "Worker embedded agent",   1, True, False, "55eeff", "2026-03-07T11:00:00Z", 0,
                      "You are the worker's embedded agent. Answer in the caller's language, one short paragraph per reply..."),
    LlmPromptTemplate("pt-compact",   "chat.compact",         "Topic compactor",         1, True, False, "66ff00", "2026-03-07T11:00:00Z", 0,
                      "Summarise the resolved topic in one system-kind message. Preserve numbers, dates, and decisions verbatim..."),
    LlmPromptTemplate("pt-detect",    "chat.detect_language", "Language detector",       1, True, False, "778800", "2026-03-07T11:00:00Z", 0,
                      "Return JSON {language: ISO-639-1 code} for the provided message..."),
    LlmPromptTemplate("pt-translate", "chat.translate",       "Message translator",      1, True, False, "889900", "2026-03-07T11:00:00Z", 0,
                      "Translate the message to {{ target_lang }}. Preserve markdown, emoji, and @mentions verbatim..."),
]


# Compatibility projection: the legacy flat ModelAssignment list consumed by
# older pages. Rolls up the chain's top (priority=0) row per capability, pulls
# pricing/enabled flags from the graph, and restates the 30d spend + call
# counts so the existing manager surfaces keep working during the cut-over.
def _legacy_llm_assignments() -> list[ModelAssignment]:
    by_pm = {pm.id: pm for pm in LLM_PROVIDER_MODELS}
    by_model = {m.id: m for m in LLM_MODELS}
    rows: list[ModelAssignment] = []
    seen: set[str] = set()
    # Group by capability, pick the lowest-priority (primary) enabled row.
    for cap in LLM_CAPABILITY_CATALOGUE:
        key = cap["key"]
        chain = sorted(
            (a for a in LLM_ASSIGNMENTS_GRAPH if a.capability == key and a.is_enabled),
            key=lambda a: a.priority,
        )
        if not chain:
            rows.append(ModelAssignment(
                capability=key, description=cap["description"],
                provider="—", model_id="(unassigned)",
                enabled=False, daily_budget_usd=0.0, spent_24h_usd=0.0, calls_24h=0,
            ))
            seen.add(key)
            continue
        primary = chain[0]
        pm = by_pm[primary.provider_model_id]
        m = by_model[pm.model_id]
        # Daily approximations derived from the rolling-30d mock figures.
        rows.append(ModelAssignment(
            capability=key, description=cap["description"],
            provider=next(p.name for p in LLM_PROVIDERS if p.id == pm.provider_id).lower().replace(" ", "_"),
            model_id=m.canonical_name,
            enabled=primary.is_enabled,
            daily_budget_usd=round(primary.spend_usd_30d / 30, 2) if primary.spend_usd_30d else 0.25,
            spent_24h_usd=round(primary.spend_usd_30d / 30, 2),
            calls_24h=round(primary.calls_30d / 30),
        ))
        seen.add(key)
    return rows


LLM_ASSIGNMENTS: list[ModelAssignment] = _legacy_llm_assignments()


LLM_CALLS: list[LLMCall] = [
    LLMCall(datetime(2026, 4, 15, 10, 6, 44), "tasks.assist",      "google/gemma-3-27b-it",      1240, 310, 1, 1820, "ok",
            assignment_id="as-assist", provider_model_id="pm-or-gemma-27b", prompt_template_id="pt-assist", prompt_version=1, raw_response_available=True),
    LLMCall(datetime(2026, 4, 15, 9, 47, 2),  "anomaly.detect",    "qwen/qwen3-32b-instruct",    3100, 180, 2, 2100, "ok",
            assignment_id="as-anomaly", provider_model_id="pm-or-qwen3-32b", prompt_template_id="pt-anomaly", prompt_version=1, raw_response_available=True),
    LLMCall(datetime(2026, 4, 15, 9, 12, 18), "digest.manager",    "anthropic/claude-haiku-4-5", 4800, 720, 3, 3400, "ok",
            assignment_id="as-digest-m", provider_model_id="pm-or-haiku-4-5", prompt_template_id="pt-digest-m", prompt_version=1, fallback_attempts=0),
    LLMCall(datetime(2026, 4, 15, 9, 12, 4),  "digest.manager",    "anthropic/claude-haiku-4-5", 4800, 0,   0, 480,  "error",
            assignment_id="as-digest-m", provider_model_id="pm-or-haiku-4-5", prompt_template_id="pt-digest-m", prompt_version=1, fallback_attempts=0),
    LLMCall(datetime(2026, 4, 15, 9, 12, 4),  "digest.manager",    "google/gemma-3-27b-it",      4800, 780, 3, 2900, "ok",
            assignment_id="as-digest-mf", provider_model_id="pm-or-gemma-27b", prompt_template_id="pt-digest-m", prompt_version=1, fallback_attempts=1),
    LLMCall(datetime(2026, 4, 15, 8, 54, 1),  "expenses.autofill", "google/gemma-3-27b-it",      980,  410, 1, 1950, "ok",
            assignment_id="as-exp", provider_model_id="pm-or-gemma-27b", prompt_template_id="pt-exp", prompt_version=3, raw_response_available=True),
    LLMCall(datetime(2026, 4, 15, 8, 41, 12), "expenses.autofill", "google/gemma-3-27b-it",      1100, 390, 1, 1720, "redacted_block",
            assignment_id="as-exp", provider_model_id="pm-or-gemma-27b", prompt_template_id="pt-exp", prompt_version=3),
    LLMCall(datetime(2026, 4, 15, 8, 6, 30),  "issue.triage",      "google/gemma-3-27b-it",      620,  140, 0, 890,  "ok",
            assignment_id="as-issue", provider_model_id="pm-or-gemma-27b", prompt_template_id="pt-issue", prompt_version=1),
]


# §11 — Workspace usage budget. Manager-visible view is percent-only
# (no dollars, no tokens, no reset date). The window label is fixed
# copy for the rolling-30-day meter.
WORKSPACE_USAGE = WorkspaceUsage(
    percent=32,
    paused=False,
    window_label="Rolling 30 days",
)


AUDIT: list[AuditEntry] = [
    # v1 `actor_kind` ∈ {user, agent, system}; `actor_grant_role`
    # carries the grant under which the action was authorised.
    AuditEntry(datetime(2026, 4, 15, 10, 8, 12), "user",   "Élodie Bernard", "task.complete",          "t-1",  "web", None,
               actor_grant_role="manager", actor_was_owner_member=True, actor_action_key="tasks.complete_other", actor_id="u-elodie"),
    AuditEntry(datetime(2026, 4, 15, 9, 47, 2),  "agent",  "digest-agent",   "agent_action.requested", "a-1",  "api", "Auto-reassign pool coverage (Ben on leave)",
               agent_label="digest-agent"),
    AuditEntry(datetime(2026, 4, 15, 9, 41, 0),  "user",   "Maria Alvarez",  "booking.completed",      "bk-2", "web", None,
               actor_grant_role="worker", actor_id="u-maria"),
    AuditEntry(datetime(2026, 4, 15, 9, 12, 18), "agent",  "digest-agent",   "digest.sent",            "—",    "api", "Morning manager digest",
               agent_label="digest-agent"),
    AuditEntry(datetime(2026, 4, 15, 8, 54, 1),  "agent",  "procurement-agent", "expense.autofill",    "x-1",  "api", None,
               agent_label="procurement-agent"),
    AuditEntry(datetime(2026, 4, 15, 8, 41, 12), "system", "redaction-layer", "llm.call.blocked",      "—",    "worker", "IBAN-like string in receipt text"),
    AuditEntry(datetime(2026, 4, 15, 8, 12, 0),  "user",   "Maria Alvarez",  "booking.scheduled",      "bk-1", "web", None,
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


# §03 API tokens. A realistic mix for the mock: two scoped workspace
# tokens (one fresh, one near expiry), one delegated token backing
# the manager chat agent, one revoked scoped token still on the list
# so the UI can show a "revoked" chip, plus two PATs that only
# surface on /me for their respective subjects (Maria's task printer,
# Élodie's pay-review script). Managers see the four workspace rows;
# each user sees only their own PATs.
API_TOKENS: list[ApiToken] = [
    ApiToken(
        id="tok-1", name="nightly-scheduler", kind="scoped",
        prefix="mip_01HS8WR2ZQ",
        scopes=["tasks:read", "tasks:write", "stays:read"],
        created_by_user_id="u-elodie", created_by_display="Élodie Bernard",
        created_at=datetime(2026, 1, 14, 9, 12, 0),
        expires_at=datetime(2027, 1, 14, 0, 0, 0),
        last_used_at=datetime(2026, 4, 18, 3, 0, 12),
        last_used_ip="198.51.100.0/24",
        last_used_path="POST /api/v1/tasks",
        revoked_at=None,
        note="Hermes scheduler, runs 03:00 UTC every night",
        ip_allowlist=["198.51.100.0/24"],
    ),
    ApiToken(
        id="tok-2", name="finance-readonly", kind="scoped",
        prefix="mip_01HKQ7F4NB",
        scopes=["payroll:read", "expenses:read"],
        created_by_user_id="u-elodie", created_by_display="Élodie Bernard",
        created_at=datetime(2025, 10, 2, 15, 30, 0),
        expires_at=datetime(2026, 5, 2, 0, 0, 0),
        last_used_at=datetime(2026, 4, 17, 8, 2, 4),
        last_used_ip="203.0.113.0/24",
        last_used_path="GET /api/v1/payroll/periods",
        revoked_at=None,
        note="Accountant's read-only export script (expires in 2 weeks)",
        ip_allowlist=[],
    ),
    ApiToken(
        id="tok-3", name="desk-sidebar-agent", kind="delegated",
        prefix="mip_01HS4M0G3P",
        scopes=[],
        created_by_user_id="u-elodie", created_by_display="Élodie Bernard",
        created_at=datetime(2026, 3, 19, 11, 48, 0),
        expires_at=datetime(2026, 4, 18, 11, 48, 0),
        last_used_at=datetime(2026, 4, 18, 10, 2, 11),
        last_used_ip="100.72.198.118/24",
        last_used_path="POST /api/v1/agent/manager/message",
        revoked_at=None,
        note="Embedded chat agent (auto-rotated on next login)",
        ip_allowlist=[],
    ),
    ApiToken(
        id="tok-4", name="ops-bot-legacy", kind="scoped",
        prefix="mip_01HG4PJXKA",
        scopes=["tasks:read"],
        created_by_user_id="u-elodie", created_by_display="Élodie Bernard",
        created_at=datetime(2025, 6, 1, 12, 0, 0),
        expires_at=datetime(2026, 6, 1, 0, 0, 0),
        last_used_at=datetime(2026, 2, 14, 22, 1, 0),
        last_used_ip="192.0.2.0/24",
        last_used_path="GET /api/v1/tasks",
        revoked_at=datetime(2026, 2, 15, 9, 4, 0),
        note="Revoked after the host was decommissioned",
        ip_allowlist=[],
    ),
    # Personal access tokens. Only visible to the subject on /me;
    # the manager /tokens page must filter these out.
    ApiToken(
        id="tok-pat-maria", name="kitchen-printer", kind="personal",
        prefix="mip_01HV3Z8Q8K",
        scopes=["me.tasks:read", "me.bookings:read"],
        created_by_user_id="u-maria", created_by_display="Maria Alvarez",
        created_at=datetime(2026, 4, 2, 18, 21, 0),
        expires_at=datetime(2026, 7, 1, 0, 0, 0),
        last_used_at=datetime(2026, 4, 18, 6, 5, 48),
        last_used_ip="192.0.2.0/24",
        last_used_path="GET /api/v1/me/tasks",
        revoked_at=None,
        note="Raspberry Pi next to the coffee machine — prints today's tasks at 06:00",
        ip_allowlist=[],
    ),
    ApiToken(
        id="tok-pat-elodie", name="pay-review-laptop", kind="personal",
        prefix="mip_01HV51R2PD",
        scopes=["me.expenses:read", "me.bookings:read"],
        created_by_user_id="u-elodie", created_by_display="Élodie Bernard",
        created_at=datetime(2026, 3, 30, 9, 10, 0),
        expires_at=datetime(2026, 6, 30, 0, 0, 0),
        last_used_at=None,
        last_used_ip=None,
        last_used_path=None,
        revoked_at=None,
        note="Notebook I keep for the weekly pay check",
        ip_allowlist=[],
    ),
]


API_TOKEN_AUDIT: dict[str, list[ApiTokenAuditEntry]] = {
    "tok-1": [
        ApiTokenAuditEntry(datetime(2026, 4, 18, 3, 0, 12), "GET",
                           "/api/v1/tasks?assignee_id=u-maria", 200,
                           "198.51.100.0/24", "hermes/0.12", "req-0bf1a9e3"),
        ApiTokenAuditEntry(datetime(2026, 4, 18, 3, 0, 14), "POST",
                           "/api/v1/tasks", 201,
                           "198.51.100.0/24", "hermes/0.12", "req-0bf1a9e4"),
        ApiTokenAuditEntry(datetime(2026, 4, 17, 3, 0, 11), "GET",
                           "/api/v1/tasks?assignee_id=u-maria", 200,
                           "198.51.100.0/24", "hermes/0.12", "req-0bee71a9"),
        ApiTokenAuditEntry(datetime(2026, 4, 16, 3, 0, 18), "POST",
                           "/api/v1/tasks", 201,
                           "198.51.100.0/24", "hermes/0.12", "req-0bdc4321"),
    ],
    "tok-2": [
        ApiTokenAuditEntry(datetime(2026, 4, 17, 8, 2, 4), "GET",
                           "/api/v1/payroll/periods", 200,
                           "203.0.113.0/24", "python-requests/2.32", "req-0bea1188"),
        ApiTokenAuditEntry(datetime(2026, 4, 10, 8, 1, 2), "GET",
                           "/api/v1/expenses?status=approved", 200,
                           "203.0.113.0/24", "python-requests/2.32", "req-0b5e0f22"),
    ],
    "tok-3": [
        ApiTokenAuditEntry(datetime(2026, 4, 18, 10, 2, 11), "POST",
                           "/api/v1/agent/manager/message", 200,
                           "100.72.198.118/24", "crewday-web/1.0", "req-0bf24c10"),
    ],
    "tok-4": [
        ApiTokenAuditEntry(datetime(2026, 2, 15, 9, 4, 0), "GET",
                           "/api/v1/tasks", 401,
                           "192.0.2.0/24", "curl/8.6", "req-09a10011"),
    ],
    "tok-pat-maria": [
        ApiTokenAuditEntry(datetime(2026, 4, 18, 6, 5, 48), "GET",
                           "/api/v1/me/tasks", 200,
                           "192.0.2.0/24", "crewday-py/0.1", "req-0bf1f912"),
        ApiTokenAuditEntry(datetime(2026, 4, 17, 6, 5, 42), "GET",
                           "/api/v1/me/tasks", 200,
                           "192.0.2.0/24", "crewday-py/0.1", "req-0bee6880"),
    ],
    "tok-pat-elodie": [],
}


# ── Asset type catalog (18 pre-seeded) ──────────────────────────────

ASSET_TYPES: list[AssetType] = [
    AssetType("at-air-conditioner", "air_conditioner", "Air conditioner", "climate", "Snowflake", [
        {"key": "clean_filters", "label": "Clean filters", "interval_days": 90, "estimated_duration_minutes": 30},
        {"key": "service_unit", "label": "Annual service", "interval_days": 365, "estimated_duration_minutes": 120},
    ], default_lifespan_years=12),
    AssetType("at-oven-range", "oven_range", "Oven / range", "appliance", "CookingPot", [
        {"key": "deep_clean", "label": "Deep clean", "interval_days": 90, "estimated_duration_minutes": 45},
        {"key": "check_burners", "label": "Check burners", "interval_days": 365, "estimated_duration_minutes": 30},
    ], default_lifespan_years=15),
    AssetType("at-refrigerator", "refrigerator", "Refrigerator", "appliance", "Refrigerator", [
        {"key": "clean_coils", "label": "Clean coils", "interval_days": 180, "estimated_duration_minutes": 30},
        {"key": "replace_water_filter", "label": "Replace water filter", "interval_days": 180, "estimated_duration_minutes": 15},
        {"key": "check_seals", "label": "Check door seals", "interval_days": 365, "estimated_duration_minutes": 15},
    ], default_lifespan_years=15),
    AssetType("at-dishwasher", "dishwasher", "Dishwasher", "appliance", "Utensils", [
        {"key": "clean_filter", "label": "Clean filter", "interval_days": 30, "estimated_duration_minutes": 15},
        {"key": "descale", "label": "Descale", "interval_days": 90, "estimated_duration_minutes": 20},
    ], default_lifespan_years=10),
    AssetType("at-washing-machine", "washing_machine", "Washing machine", "appliance", "WashingMachine", [
        {"key": "clean_drum", "label": "Clean drum", "interval_days": 30, "estimated_duration_minutes": 15},
        {"key": "check_hoses", "label": "Check hoses", "interval_days": 365, "estimated_duration_minutes": 20},
    ], default_lifespan_years=10),
    AssetType("at-dryer", "dryer", "Dryer", "appliance", "Fan", [
        {"key": "clean_vent", "label": "Clean vent", "interval_days": 90, "estimated_duration_minutes": 30},
        {"key": "inspect_duct", "label": "Inspect duct", "interval_days": 365, "estimated_duration_minutes": 30},
    ], default_lifespan_years=12),
    AssetType("at-water-heater", "water_heater", "Water heater", "climate", "Flame", [
        {"key": "flush_tank", "label": "Flush tank", "interval_days": 365, "estimated_duration_minutes": 60},
        {"key": "check_anode", "label": "Check anode rod", "interval_days": 730, "estimated_duration_minutes": 45},
    ], default_lifespan_years=12),
    AssetType("at-boiler", "boiler", "Boiler / furnace", "heating", "Heater", [
        {"key": "annual_service", "label": "Annual service", "interval_days": 365, "estimated_duration_minutes": 90},
        {"key": "bleed_radiators", "label": "Bleed radiators", "interval_days": 180, "estimated_duration_minutes": 45},
    ], default_lifespan_years=15),
    AssetType("at-pool-pump", "pool_pump", "Pool pump", "pool", "Waves", [
        {"key": "clean_basket", "label": "Clean basket", "interval_days": 7, "estimated_duration_minutes": 10},
        {"key": "inspect_seals", "label": "Inspect seals", "interval_days": 180, "estimated_duration_minutes": 20},
        {"key": "service_pump", "label": "Full service", "interval_days": 365, "estimated_duration_minutes": 120},
    ], default_lifespan_years=8),
    AssetType("at-pool-heater", "pool_heater", "Pool heater", "pool", "ThermometerSun", [
        {"key": "check_thermostat", "label": "Check thermostat", "interval_days": 30, "estimated_duration_minutes": 10},
        {"key": "annual_service", "label": "Annual service", "interval_days": 365, "estimated_duration_minutes": 90},
    ], default_lifespan_years=10),
    AssetType("at-smoke-detector", "smoke_detector", "Smoke detector", "safety", "AlarmSmoke", [
        {"key": "test", "label": "Test alarm", "interval_days": 30, "estimated_duration_minutes": 5},
        {"key": "replace_battery", "label": "Replace battery", "interval_days": 365, "estimated_duration_minutes": 10},
    ], default_lifespan_years=10),
    AssetType("at-fire-extinguisher", "fire_extinguisher", "Fire extinguisher", "safety", "FireExtinguisher", [
        {"key": "check_pressure", "label": "Check pressure", "interval_days": 30, "estimated_duration_minutes": 5},
        {"key": "annual_inspection", "label": "Annual inspection", "interval_days": 365, "estimated_duration_minutes": 15},
    ], default_lifespan_years=12),
    AssetType("at-generator", "generator", "Generator", "outdoor", "Zap", [
        {"key": "test_run", "label": "Test run", "interval_days": 30, "estimated_duration_minutes": 15},
        {"key": "oil_change", "label": "Oil change", "interval_days": 180, "estimated_duration_minutes": 30},
        {"key": "annual_service", "label": "Annual service", "interval_days": 365, "estimated_duration_minutes": 120},
    ], default_lifespan_years=20),
    AssetType("at-solar-panel", "solar_panel", "Solar panels", "outdoor", "Sun", [
        {"key": "clean_panels", "label": "Clean panels", "interval_days": 90, "estimated_duration_minutes": 60},
        {"key": "check_inverter", "label": "Check inverter", "interval_days": 30, "estimated_duration_minutes": 10},
    ], default_lifespan_years=25),
    AssetType("at-septic-tank", "septic_tank", "Septic tank", "plumbing", "Biohazard", [
        {"key": "pump_tank", "label": "Pump tank", "interval_days": 1095, "estimated_duration_minutes": 120},
        {"key": "inspection", "label": "Inspection", "interval_days": 365, "estimated_duration_minutes": 30},
    ], default_lifespan_years=30),
    AssetType("at-irrigation", "irrigation", "Irrigation system", "outdoor", "Droplets", [
        {"key": "winterize", "label": "Winterize", "interval_days": 365, "estimated_duration_minutes": 60},
        {"key": "inspect_heads", "label": "Inspect heads", "interval_days": 90, "estimated_duration_minutes": 30},
    ], default_lifespan_years=15),
    AssetType("at-alarm-system", "alarm_system", "Alarm / security", "security", "ShieldCheck", [
        {"key": "test_sensors", "label": "Test sensors", "interval_days": 90, "estimated_duration_minutes": 30},
        {"key": "replace_batteries", "label": "Replace batteries", "interval_days": 365, "estimated_duration_minutes": 20},
    ], default_lifespan_years=10),
    AssetType("at-vehicle", "vehicle", "Vehicle", "vehicle", "Car", [
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
                  "msz-ap25vgk-manual.pdf", 4200, datetime(2024, 6, 15, 10, 0),
                  extraction_status="succeeded", extracted_at=datetime(2024, 6, 15, 10, 1)),
    AssetDocument("ad-2", "a-villa-ac-bed", "p-villa-sud", "warranty", "AC warranty certificate",
                  "ac-warranty-2024.pdf", 180, datetime(2024, 6, 15, 10, 5),
                  expires_on=date(2028, 6, 15),
                  extraction_status="succeeded", extracted_at=datetime(2024, 6, 15, 10, 6)),
    AssetDocument("ad-3", "a-villa-pool-pump", "p-villa-sud", "invoice", "Pool pump purchase invoice",
                  "hayward-invoice-2022.pdf", 320, datetime(2022, 3, 10, 14, 0),
                  amount_cents=85000, amount_currency="EUR",
                  extraction_status="succeeded", extracted_at=datetime(2022, 3, 10, 14, 1)),
    AssetDocument("ad-4", "a-villa-water-heater", "p-villa-sud", "manual", "Atlantic O'Pro 200L manual",
                  "atlantic-opro-manual.pdf", 3800, datetime(2019, 11, 5, 9, 0),
                  extraction_status="succeeded", extracted_at=datetime(2019, 11, 5, 9, 1)),
    AssetDocument("ad-5", "a-apt-dishwasher", "p-apt-3b", "warranty", "Bosch Serie 4 warranty",
                  "bosch-warranty-2023.pdf", 150, datetime(2023, 7, 1, 12, 0),
                  expires_on=date(2025, 7, 1),
                  extraction_status="succeeded", extracted_at=datetime(2023, 7, 1, 12, 1)),
    AssetDocument("ad-6", "a-chalet-boiler", "p-chalet", "invoice", "Boiler installation invoice",
                  "vaillant-install-invoice.pdf", 280, datetime(2020, 10, 5, 15, 0),
                  amount_cents=320000, amount_currency="EUR",
                  extraction_status="failed"),
    # Property-level documents (asset_id = None)
    AssetDocument("ad-7", None, "p-villa-sud", "insurance", "Villa Sud insurance policy 2026",
                  "villa-sud-insurance-2026.pdf", 1200, datetime(2026, 1, 5, 9, 30),
                  expires_on=date(2027, 1, 5), amount_cents=285000, amount_currency="EUR",
                  extraction_status="succeeded", extracted_at=datetime(2026, 1, 5, 9, 31)),
    AssetDocument("ad-8", None, "p-villa-sud", "permit", "Pool safety compliance certificate",
                  "pool-safety-cert-2025.pdf", 450, datetime(2025, 6, 20, 11, 0),
                  expires_on=date(2026, 6, 20),
                  extraction_status="extracting"),
]


# ── Document text extractions (server-derived bodies; §02 file_extraction) ──

FILE_EXTRACTIONS: dict[str, FileExtraction] = {
    "ad-1": FileExtraction(
        "ad-1", "pypdf",
        body_text=(
            "Mitsubishi MSZ-AP25VGK installation and user manual.\n\n"
            "## Filter cleaning (recommended monthly)\n"
            "1. Power off the unit at the wall switch.\n"
            "2. Open the front grille; the filter slides out from the top.\n"
            "3. Rinse with cool water; do not use detergent.\n"
            "4. Allow to fully air-dry before reinstalling.\n"
            "5. Replace the filter at least every 24 months.\n\n"
            "## Refrigerant\nR32. Servicing requires a certified F-gas technician.\n"
        ),
        pages=[{"page": 1, "char_start": 0, "char_end": 420}],
        token_count=132,
    ),
    "ad-2": FileExtraction(
        "ad-2", "pypdf",
        body_text=(
            "Mitsubishi Electric — Limited Warranty Certificate\n"
            "Model: MSZ-AP25VGK   Installed: 2024-06-15\n"
            "Coverage: 4 years parts, 2 years labour. "
            "Warranty void if non-OEM filters used or if unit moved without recertification."
        ),
        pages=[{"page": 1, "char_start": 0, "char_end": 220}],
        token_count=64,
    ),
    "ad-4": FileExtraction(
        "ad-4", "pypdf",
        body_text=(
            "Atlantic O'Pro 200L water heater — operating manual.\n\n"
            "## Annual maintenance\n"
            "1. Cut power at the disconnect.\n"
            "2. Drain the tank using the bottom valve.\n"
            "3. Inspect the magnesium anode; replace if eroded > 50%.\n"
            "4. Refill, restore power, observe pressure-relief valve for 30 min."
        ),
        pages=[{"page": 1, "char_start": 0, "char_end": 320}],
        token_count=98,
    ),
    "ad-5": FileExtraction(
        "ad-5", "pypdf",
        body_text=(
            "Bosch Serie 4 dishwasher warranty — 2 years parts and labour from 2023-07-01."
        ),
        pages=[{"page": 1, "char_start": 0, "char_end": 90}],
        token_count=24,
    ),
    "ad-7": FileExtraction(
        "ad-7", "pypdf",
        body_text=(
            "Villa Sud insurance policy 2026 — Allianz contract n° AZ-882441.\n"
            "Annual premium: €2 850. Coverage: building, fixtures, public liability up to €5 M.\n"
            "Excludes pool incidents involving unsupervised minors."
        ),
        pages=[{"page": 1, "char_start": 0, "char_end": 240}],
        token_count=72,
    ),
    "ad-6": FileExtraction(
        "ad-6", None,
        body_text="",
        pages=[],
        token_count=0,
        last_error="image-only PDF; tesseract returned < 16 chars per page",
    ),
}


# ── Agent docs (system-side virtual files; §02 agent_doc) ──────────────

AGENT_DOCS: list[AgentDoc] = [
    AgentDoc(
        slug="cli-cheatsheet",
        title="CLI cheat-sheet",
        summary="Crewday CLI verbs grouped by surface, with the rare flags worth remembering.",
        body_md=(
            "# CLI cheat-sheet\n\n"
            "Use this when the user asks 'how do I…?' and the answer maps to a single CLI verb.\n\n"
            "## Tasks\n- `crewday tasks list --today --assigned-to @me`\n"
            "- `crewday tasks complete <id> --note '…'`\n\n"
            "## Documents & KB\n- `crewday kb search '<query>'`\n"
            "- `crewday kb read document <id> [--page N]`\n"
            "- `crewday documents extraction status <id>`"
        ),
        roles=["manager", "employee", "admin"],
        capabilities=["chat.manager", "chat.employee", "chat.admin"],
        version=1,
        is_customised=False,
        default_hash="b27c0a1f3d49aa12",
        updated_at=datetime(2026, 4, 18, 9, 0),
    ),
    AgentDoc(
        slug="worker-tone",
        title="How to talk to workers on mobile",
        summary="Short replies, second-person plural in formal locales, no walls of text on a 4-inch screen.",
        body_md=(
            "# Worker tone\n\n"
            "Workers chat from a phone, often one-handed between tasks. Keep replies under 80 words "
            "unless they ask for a step-by-step. Never paste a whole manual; quote one line and "
            "offer 'want me to read more?'.\n\n"
            "When the worker is on a task screen, prefer the linked instructions before "
            "searching the KB. If the answer is in `search_kb` only, name the source ('From the "
            "AC manual at Villa Sud:')."
        ),
        roles=["employee"],
        capabilities=["chat.employee"],
        version=1,
        is_customised=False,
        default_hash="6f1c8842e5b0a103",
        updated_at=datetime(2026, 4, 18, 9, 0),
    ),
    AgentDoc(
        slug="approval-cards",
        title="Inline approval card phrasing",
        summary="When to skip the inline confirmation card, when to insist, what the verbs mean.",
        body_md=(
            "# Inline approval cards\n\n"
            "Every mutating action you propose may surface an `x-agent-confirm` card. Do not "
            "echo the card body in chat — the user will see the card. Instead, narrate the "
            "*reason* for the action ('Marcie's been off the schedule for two weeks; I can "
            "archive her engagement.') and let the card take it from there.\n\n"
            "If the user already typed the answer ('yes go ahead'), still show the card; the "
            "card is the audit trail."
        ),
        roles=["manager", "admin"],
        capabilities=["chat.manager", "chat.admin"],
        version=1,
        is_customised=False,
        default_hash="ab8240f04c5b6712",
        updated_at=datetime(2026, 4, 18, 9, 0),
    ),
]


BOOKINGS: list[Booking] = [
    # Maria's morning at Villa Sud — currently in progress (scheduled,
    # window includes "now"), no actual_minutes set yet.
    Booking("bk-1", "e-maria", "p-villa-sud",
            datetime(2026, 4, 15, 8, 0), datetime(2026, 4, 15, 12, 0),
            "scheduled",
            work_engagement_id="we-maria-bernard", user_id="u-maria"),
    # Ben — completed, no amend.
    Booking("bk-2", "e-ben", "p-villa-sud",
            datetime(2026, 4, 15, 8, 30), datetime(2026, 4, 15, 12, 30),
            "completed",
            work_engagement_id="we-ben-bernard", user_id="u-ben"),
    # Arun — completed yesterday, manager amended for a small overrun.
    Booking("bk-3", "e-arun", "p-villa-sud",
            datetime(2026, 4, 14, 13, 0), datetime(2026, 4, 14, 18, 0),
            "adjusted",
            actual_minutes=330, actual_minutes_paid=330,
            adjusted=True, adjustment_reason="Stayed to finish kitchen reorganisation.",
            work_engagement_id="we-arun-bernard", user_id="u-arun"),
    # Ana — completed at the Dupont property; billable.
    Booking("bk-4", "e-ana", "p-apt-3b",
            datetime(2026, 4, 14, 9, 0), datetime(2026, 4, 14, 14, 0),
            "completed",
            client_org_id="org-dupont",
            work_engagement_id="we-ana-bernard", user_id="u-ana"),
    # Sam — worker-requested overrun pending manager approval (>30 min).
    Booking("bk-5", "e-sam", "p-villa-sud",
            datetime(2026, 4, 14, 10, 0), datetime(2026, 4, 14, 12, 30),
            "completed",
            actual_minutes=150, actual_minutes_paid=150,
            pending_amend_minutes=210,
            pending_amend_reason="Pump rebuild needed extra hour.",
            work_engagement_id="we-sam-bernard", user_id="u-sam"),
    # Vincent scenario — Joselyn at Villa du Lac, billable to DupontFamily.
    Booking("bk-6", "e-joselyn", "p-villa-lac",
            datetime(2026, 4, 15, 9, 0), datetime(2026, 4, 15, 13, 0),
            "completed",
            client_org_id="org-dupont-vincent",
            work_engagement_id="we-joselyn-cleanco", user_id="u-joselyn"),
    # Rachid driving Vincent — completed.
    Booking("bk-7", "e-rachid", "p-villa-lac",
            datetime(2026, 4, 15, 14, 30), datetime(2026, 4, 15, 17, 0),
            "completed",
            work_engagement_id="we-rachid-vincent", user_id="u-rachid"),
    # Worker-proposed ad-hoc booking awaiting manager approval.
    Booking("bk-8", "e-maria", "p-apt-3b",
            datetime(2026, 4, 16, 14, 0), datetime(2026, 4, 16, 16, 0),
            "pending_approval",
            notes_md="Owner asked me to swing by for laundry pickup.",
            work_engagement_id="we-maria-bernard", user_id="u-maria"),
    # Late-cancellation by client (within 24h window) — fee row should
    # appear in BOOKING_BILLINGS.
    Booking("bk-9", "e-ana", "p-apt-3b",
            datetime(2026, 4, 13, 9, 0), datetime(2026, 4, 13, 13, 0),
            "cancelled_by_client",
            client_org_id="org-dupont",
            notes_md="Owner cancelled 6h before — within 24h policy window.",
            work_engagement_id="we-ana-bernard", user_id="u-ana"),
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
    # Bernard family is a CleanCo client too — they contract CleanCo
    # for cleaning Apt 3B. Lives in CleanCo's workspace; Élodie holds
    # a `client` grant on ws-cleanco bound to this org so she can audit
    # what CleanCo bills her family without managing CleanCo's roster.
    Organization(
        "org-bernard-cleanco", "Bernard family",
        workspace_id="ws-cleanco",
        is_client=True,
        legal_name="Bernard household",
        default_currency="EUR",
        tax_id="FR12 345 678 901",
        contacts=[
            {"label": "Primary", "name": "Élodie Bernard",
             "email": "elodie.bernard@example.com",
             "phone_e164": "+33 6 11 22 33 44",
             "role": "owner"},
        ],
        notes="Bernard family billing entity — separate from `org-dupont` which lives in ws-bernard's own scope.",
        portal_user_id="u-elodie",
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

BOOKING_BILLINGS: list[BookingBilling] = [
    BookingBilling("bb-1", "bk-4", "org-dupont", "u-ana",
                   "EUR", billable_minutes=300, hourly_cents=3600,
                   subtotal_cents=18000,
                   rate_source="client_user_rate", rate_source_id="cur-1",
                   work_engagement_id="we-ana-bernard"),
    # Vincent scenario — Joselyn's booking at Villa du Lac billable to
    # DupontFamily at the CleanCo maid rate.
    BookingBilling("bb-2", "bk-6", "org-dupont-vincent", "u-joselyn",
                   "EUR", billable_minutes=240, hourly_cents=3400,
                   subtotal_cents=13600,
                   rate_source="client_rate", rate_source_id="cr-4",
                   work_engagement_id="we-joselyn-cleanco"),
    # Cancellation fee on bk-9 — 4h scheduled × €36/h × 50% = €72.
    BookingBilling("bb-3", "bk-9", "org-dupont", "u-ana",
                   "EUR", billable_minutes=240, hourly_cents=3600,
                   subtotal_cents=7200,
                   rate_source="client_user_rate", rate_source_id="cur-1",
                   work_engagement_id="we-ana-bernard",
                   is_cancellation_fee=True),
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
    # Joselyn's maid work at Villa du Lac. Approved, reminder worker
    # already nudged the client once; next nudge queued for due_on + 1.
    VendorInvoice(
        "vi-3", currency="EUR",
        subtotal_cents=13600, tax_cents=2720, total_cents=16320,
        billed_at=date(2026, 4, 15),
        due_on=date(2026, 4, 30),
        status="approved",
        property_id="p-villa-lac",
        vendor_organization_id="org-cleanco",
        payout_destination_stub="•• FR-07",
        lines=[
            {"kind": "labor", "description": "Joselyn Rivera — Villa du Lac maid service (4h)",
             "quantity": 4, "unit": "hour", "unit_price_cents": 3400, "total_cents": 13600},
        ],
        submitted_at=datetime(2026, 4, 15, 19, 0),
        approved_at=datetime(2026, 4, 16, 9, 30),
        decided_by_user_id="u-clementine",
        reminder_last_sent_at=datetime(2026, 4, 27, 7, 0),
        reminder_next_due_at=datetime(2026, 5, 1, 7, 0),
    ),
    # Scenario 3 — single-house owner (Bernard) paying CleanCo for
    # Joselyn at Villa Sud on a one-off basis. Proof of payment
    # already uploaded by the client (Bernard's account), awaiting
    # reconciliation on the CleanCo side.
    VendorInvoice(
        "vi-4", currency="EUR",
        subtotal_cents=9600, tax_cents=1920, total_cents=11520,
        billed_at=date(2026, 4, 12),
        due_on=date(2026, 4, 26),
        status="approved",
        property_id="p-villa-sud",
        vendor_organization_id="org-cleanco",
        payout_destination_stub="•• FR-07",
        lines=[
            {"kind": "labor", "description": "One-off deep clean — Villa Sud",
             "quantity": 3, "unit": "hour", "unit_price_cents": 3200, "total_cents": 9600},
        ],
        submitted_at=datetime(2026, 4, 12, 18, 0),
        approved_at=datetime(2026, 4, 13, 8, 0),
        decided_by_user_id="u-clementine",
        proof_of_payment_file_ids=["file-proof-vi-4"],
        reminder_last_sent_at=datetime(2026, 4, 23, 7, 0),
        reminder_next_due_at=None,
    ),
]


# §22 Pending property_workspace invites. The owner workspace creates
# the invite; the target workspace's owners accept. Seeded here so the
# manager UI can render the "Sharing & client" panel with pending rows.
PROPERTY_WORKSPACE_INVITES: list[PropertyWorkspaceInvite] = [
    # Bernard (ws-bernard) is inviting CleanCo (ws-cleanco) to manage
    # Villa Sud — scenario 3. The invite carries the shareable token so
    # Bernard can copy-paste the link into a message to CleanCo.
    PropertyWorkspaceInvite(
        id="pwi-bernard-villa-sud",
        token="pwi_kzaq3m5tfwprnc7x9h2yvb8sud4gejlq",
        from_workspace_id="ws-bernard",
        property_id="p-villa-sud",
        to_workspace_id="ws-cleanco",
        proposed_membership_role="managed_workspace",
        initial_share_settings={"share_guest_identity": False},
        state="pending",
        created_by_user_id="u-elodie",
        created_at=datetime(2026, 4, 17, 10, 0),
        expires_at=datetime(2026, 5, 1, 10, 0),
    ),
]

INVENTORY_STOCKTAKES: list[InventoryStocktake] = [
    # Quarterly walk-through of Villa Sud. Surfaced deltas on three
    # items; the bath-towel line was marked `theft` after two
    # towels were missing from the linen cupboard.
    InventoryStocktake(
        id="st-1",
        property_id="p-villa-sud",
        started_at=datetime(2026, 4, 8, 9, 0),
        completed_at=datetime(2026, 4, 8, 10, 15),
        actor_kind="user",
        actor_id="u-elodie",
        note_md="Quarterly count. Towel shortfall flagged to owner.",
    ),
]

INVENTORY_MOVEMENTS: list[InventoryMovement] = [
    # `actor_kind` is the v1 collapsed enum: user | agent | system (§02).
    # Fractional deltas for chemicals / chlorine — §08 "quantities are decimal".
    InventoryMovement("im-1",  "inv-3",  -0.25, "consume", "user", "u-ben",
                      datetime(2026, 4, 12, 9, 30), "Weekly pool service",
                      source_task_id="t-1"),
    InventoryMovement("im-2",  "inv-6",  -4.0,  "consume", "user", "u-ana",
                      datetime(2026, 4, 14, 8, 0), "Turnover prep — Apt 3B"),
    InventoryMovement("im-3",  "inv-4",   6.0,  "restock", "user", "u-maria",
                      datetime(2026, 4, 13, 17, 0), "Carrefour run"),
    InventoryMovement("im-4",  "inv-8",  -1.5,  "consume", "user", "u-sam",
                      datetime(2026, 4, 10, 16, 0)),
    # Task-driven produce + consume bundle from a turnover at Villa Sud.
    InventoryMovement("im-5",  "inv-1",  -1.0,  "consume", "user", "u-maria",
                      datetime(2026, 4, 15, 11, 0), "Turnover — strip + remake master bed",
                      source_task_id="t-2"),
    InventoryMovement("im-6",  "inv-1d",  1.0,  "produce", "user", "u-maria",
                      datetime(2026, 4, 15, 11, 0), "Turnover — dirty sheets to laundry",
                      source_task_id="t-2"),
    InventoryMovement("im-7",  "inv-9",  -0.3,  "consume", "user", "u-maria",
                      datetime(2026, 4, 15, 11, 0), "Window cleaning",
                      source_task_id="t-2"),
    # Stocktake session output — three movements sharing st-1.
    InventoryMovement("im-st-1", "inv-2", -2.0, "theft",   "user", "u-elodie",
                      datetime(2026, 4, 8, 10, 10), "Two bath towels missing since last guest",
                      source_stocktake_id="st-1"),
    InventoryMovement("im-st-2", "inv-9", -0.1, "loss",    "user", "u-elodie",
                      datetime(2026, 4, 8, 10, 12), "Jug was leakier than expected",
                      source_stocktake_id="st-1"),
    InventoryMovement("im-st-3", "inv-4",  1.0, "found",   "user", "u-elodie",
                      datetime(2026, 4, 8, 10, 14), "Extra pack behind the dryer",
                      source_stocktake_id="st-1"),
]


def _backfill_demo_history() -> None:
    """Synthesise a few months of demo history so the drawer's
    infinite-scroll pager has something to walk. Runs once at import
    time, mutating ``INVENTORY_MOVEMENTS`` in place.

    Chosen items: ``inv-9`` (window washer) and ``inv-10`` (detergent)
    because they're the most obviously fractional, and ``inv-1`` /
    ``inv-2`` because they show the clean-sheet lifecycle.
    """
    backfill: list[tuple[str, float, str, str | None]] = [
        # (item_id, delta, reason, note)
        ("inv-9",  -0.2, "consume",   "Pool deck wipe-down"),
        ("inv-9",  -0.1, "consume",   "Glass doors rinse"),
        ("inv-9",   1.0, "restock",   "Aldi run — 1 L refill"),
        ("inv-9",  -0.3, "consume",   "Turnover — exterior windows"),
        ("inv-9",  -0.2, "consume",   "Mid-stay touch-up"),
        ("inv-9",  -0.1, "consume",   "Mirror cleaning"),
        ("inv-9",   1.0, "restock",   "Fresh bottle"),
        ("inv-9",  -0.3, "consume",   "Post-storm window pass"),
        ("inv-9",  -0.15, "consume",  "Spot clean after rain"),
        ("inv-9",   0.5, "found",     "Half-full bottle behind boiler"),
        ("inv-9",  -0.4, "consume",   "Spring-clean session"),
        ("inv-9",  -0.2, "consume",   "Guest feedback: water marks"),
        ("inv-9",   1.0, "restock",   None),
        ("inv-9",  -0.3, "consume",   None),
        ("inv-9",  -0.25, "waste",    "Dropped bottle on terrace"),
        ("inv-9",  -0.2, "consume",   None),
        ("inv-9",   1.0, "restock",   "Carrefour bulk"),
        ("inv-9",  -0.1, "consume",   None),
        ("inv-10", -0.3, "consume",   "Full laundry cycle"),
        ("inv-10",  2.0, "restock",   "Bulk pouch"),
        ("inv-10", -0.15, "consume",  "Laundry — towels only"),
        ("inv-10", -0.2, "consume",   "Two sheet sets"),
        ("inv-10",  1.0, "restock",   None),
        ("inv-1",  -1.0, "consume",   "Turnover"),
        ("inv-1",   1.0, "produce",   "Laundry — folded + shelved"),
        ("inv-1",  -1.0, "consume",   None),
        ("inv-1",   1.0, "produce",   None),
        ("inv-1",  -1.0, "consume",   "Mid-stay change"),
        ("inv-1",   1.0, "produce",   None),
    ]
    # Seed the backfill walking back in time from 2026-04-01 one day
    # per row (roughly a movement/day over the preceding month and a
    # half); real ordering doesn't matter much for the mock.
    anchor = datetime(2026, 4, 1, 9, 0)
    for i, (iid, delta, reason, note) in enumerate(backfill):
        at = anchor - timedelta(days=i + 1, hours=(i % 4))
        INVENTORY_MOVEMENTS.append(
            InventoryMovement(
                id=f"im-bk-{i + 1}",
                item_id=iid,
                delta=delta,
                reason=reason,  # type: ignore[arg-type]
                actor_kind="user",
                actor_id="u-maria" if i % 3 else "u-ana",
                occurred_at=at,
                note=note,
            ),
        )


_backfill_demo_history()

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
    "bookings.pay_basis": "scheduled",
    "bookings.auto_approve_overrun_minutes": 30,
    "bookings.cancellation_window_hours": 24,
    "bookings.cancellation_fee_pct": 50,
    "bookings.cancellation_pay_to_worker": True,
    "pay.frequency": "monthly",
    "pay.week_start": "monday",
    "retention.audit_days": 730,
    "retention.llm_calls_days": 90,
    "retention.task_photos_days": 365,
    "retention.template_revisions_days": 365,
    "scheduling.horizon_days": 30,
    "tasks.checklist_required": False,
    "tasks.allow_skip_with_reason": True,
    # §08 — gates consume + produce effects declared on task
    # templates / asset actions. Replaces the pre-revision
    # `inventory.consume_on_task`.
    "inventory.apply_on_task": True,
    "inventory.shrinkage_alert_pct": 10,
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
    SettingDefinition("bookings.pay_basis", "Pay basis", "enum", "scheduled",
                      enum_values=["scheduled", "actual"],
                      override_scope="W/E",
                      description="Whether payroll multiplies the booked time (scheduled) or the amended actual_minutes_paid.",
                      spec="09"),
    SettingDefinition("bookings.auto_approve_overrun_minutes", "Auto-approve overrun (min)", "int", 30,
                      override_scope="W/E",
                      description="Self-amend overruns up to this many minutes are auto-approved; beyond that, the request is queued for manager review.",
                      spec="09"),
    SettingDefinition("bookings.cancellation_window_hours", "Cancellation window (h)", "int", 24,
                      override_scope="W",
                      description="Lead time inside which a cancellation by the client incurs a fee (workspace default; per-client override on organization).",
                      spec="09, 22"),
    SettingDefinition("bookings.cancellation_fee_pct", "Cancellation fee (%)", "int", 50,
                      override_scope="W",
                      description="Percent of the booking subtotal billed to the client when cancellation falls inside the window.",
                      spec="09, 22"),
    SettingDefinition("bookings.cancellation_pay_to_worker", "Pay worker on agency cancel", "bool", True,
                      override_scope="W/E",
                      description="Pay the worker their booked amount when the workspace itself cancels with under cancellation_window_hours of notice.",
                      spec="09"),
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
    SettingDefinition("retention.template_revisions_days", "Template revision retention (days)", "int", 365,
                      override_scope="W",
                      description="Retention for every hash-self-seeded table's revision twin (prompt library, agent docs, future callers). Not task templates, not email templates, not WhatsApp templates.",
                      spec="02"),
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
    SettingDefinition("auth.self_service_recovery_enabled", "Self-service lost-device recovery", "bool", True,
                      override_scope="W",
                      description=(
                          "When on, users who lost their passkey device can re-enroll via /recover without a manager. "
                          "Managers and owners must still supply an unused break-glass code (step-up). "
                          "When off, workspace members fall back to the manager-mediated re-issue path."
                      ),
                      spec="03"),
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
        "web_admin_sidebar",
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
        "Period closed; all bookings reconciled. Totals within 4% of March.", "medium",
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
    # Raising HTTPException(404) is a minor coupling, but it matches the
    # spec's "unknown id returns 404" contract that several callers relied
    # on via the now-broken `next(...)`-raises-StopIteration pattern.
    from fastapi import HTTPException
    hit = next((p for p in PROPERTIES if p.id == pid), None)
    if hit is None:
        raise HTTPException(status_code=404, detail=f"property_not_found:{pid}")
    return hit


def employee_by_id(eid: str) -> Employee:
    from fastapi import HTTPException
    hit = next((e for e in EMPLOYEES if e.id == eid), None)
    if hit is None:
        raise HTTPException(status_code=404, detail=f"employee_not_found:{eid}")
    return hit


def tasks_for_employee(eid: str) -> list[Task]:
    return [t for t in TASKS if t.assignee_id == eid]


def tasks_for_user(uid: str) -> list[Task]:
    """Canonical v1 filter — matches §06 `assigned_user_id`. Works for
    both worker-employees (who also have an `assignee_id`) and managers
    (who have no employee row but do have a user row)."""
    return [t for t in TASKS if t.assigned_user_id == uid]


def is_owner_user(uid: str) -> bool:
    """§15 — an 'owner' for personal-task visibility is any user with an
    active `grant_role = 'owner'` role grant anywhere in the workspace."""
    return any(
        g.grant_role == "owner" and g.revoked_at is None
        for g in ROLE_GRANTS
        if g.user_id == uid
    )


def visible_to(task: Task, viewer_user_id: str) -> bool:
    """§15 Personal task visibility. A personal task is visible only to
    its creator and to workspace owners. Non-personal tasks are visible
    as usual."""
    if not task.is_personal:
        return True
    if task.created_by == viewer_user_id:
        return True
    return is_owner_user(viewer_user_id)


def task_by_id(tid: str) -> Task | None:
    return next((t for t in TASKS if t.id == tid), None)


def expenses_for_employee(eid: str) -> list[Expense]:
    return [x for x in EXPENSES if x.employee_id == eid]


def expenses_for_user(uid: str) -> list[Expense]:
    return [x for x in EXPENSES if x.user_id == uid]


def employee_by_user_id(uid: str) -> "Employee | None":
    return next((e for e in EMPLOYEES if e.user_id == uid), None)


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


def inventory_by_id(iid: str) -> InventoryItem | None:
    return next((i for i in INVENTORY if i.id == iid), None)


def inventory_by_sku(pid: str, sku: str) -> InventoryItem | None:
    return next(
        (i for i in INVENTORY if i.property_id == pid and i.sku == sku),
        None,
    )


def stocktakes_for_property(pid: str) -> list[InventoryStocktake]:
    return [s for s in INVENTORY_STOCKTAKES if s.property_id == pid]


def stocktake_by_id(sid: str) -> InventoryStocktake | None:
    return next((s for s in INVENTORY_STOCKTAKES if s.id == sid), None)


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


def document_by_id(did: str) -> AssetDocument | None:
    return next((d for d in ASSET_DOCUMENTS if d.id == did), None)


def extraction_for_document(did: str) -> FileExtraction | None:
    return FILE_EXTRACTIONS.get(did)


def agent_doc_by_slug(slug: str) -> AgentDoc | None:
    return next((d for d in AGENT_DOCS if d.slug == slug), None)


def search_kb(
    q: str,
    *,
    kind: str = "",
    property_id: str = "",
    asset_id: str = "",
    document_kind: str = "",
    limit: int = 10,
) -> list[dict]:
    """Lightweight FTS-shaped mock for §02 search_kb.

    Walks instructions (current revisions) and successfully-extracted
    asset documents; ranks by simple substring count weighted per the
    spec's vector. Snippets cap at 240 chars.
    """
    query = q.strip().lower()
    if not query:
        return []
    hits: list[dict] = []
    if kind in ("", "instruction"):
        for instr in INSTRUCTIONS:
            if property_id and instr.property_id not in (None, property_id):
                continue
            title = (instr.title or "").lower()
            tags = " ".join(instr.tags or []).lower()
            body = (instr.body_md or "").lower()
            score = (
                4 * title.count(query)
                + 3 * tags.count(query)
                + 2 * body.count(query)
            )
            if score == 0:
                continue
            snippet_src = instr.body_md or instr.title
            idx = snippet_src.lower().find(query)
            start = max(0, idx - 80)
            snippet = snippet_src[start:start + 240].strip()
            why = "Instruction"
            if instr.property_id:
                prop = property_by_id(instr.property_id)
                if prop:
                    why = f"Instruction at *{prop.name}*"
            hits.append({
                "kind": "instruction",
                "id": instr.id,
                "title": instr.title,
                "snippet": snippet,
                "score": score,
                "why": why,
            })
    if kind in ("", "document"):
        for doc in ASSET_DOCUMENTS:
            if doc.extraction_status != "succeeded":
                continue
            if property_id and doc.property_id != property_id:
                continue
            if asset_id and doc.asset_id != asset_id:
                continue
            if document_kind and doc.kind != document_kind:
                continue
            extraction = FILE_EXTRACTIONS.get(doc.id)
            if extraction is None:
                continue
            title = (doc.title or "").lower()
            body = (extraction.body_text or "").lower()
            score = (
                4 * title.count(query)
                + 3 * doc.kind.lower().count(query)
                + 2 * body.count(query)
                + 1 * (doc.filename or "").lower().count(query)
            )
            if score == 0:
                continue
            snippet_src = extraction.body_text or doc.title
            idx = snippet_src.lower().find(query)
            start = max(0, idx - 80)
            snippet = snippet_src[start:start + 240].strip()
            why_parts = [doc.kind]
            asset = asset_by_id(doc.asset_id) if doc.asset_id else None
            if asset:
                why_parts.append(f"for *{asset.name}*")
            prop = property_by_id(doc.property_id)
            if prop:
                why_parts.append(f"at *{prop.name}*")
            hits.append({
                "kind": "document",
                "id": doc.id,
                "title": doc.title,
                "snippet": snippet,
                "score": score,
                "why": " ".join(why_parts).capitalize(),
            })
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:limit]


def bookings_for_employee(eid: str) -> list[Booking]:
    return [b for b in BOOKINGS if b.employee_id == eid]


def comments_for_task(tid: str) -> list[TaskComment]:
    return [c for c in TASK_COMMENTS if c.task_id == tid]


def movements_for_item(iid: str) -> list[InventoryMovement]:
    return [m for m in INVENTORY_MOVEMENTS if m.item_id == iid]


def lifecycle_rules_for_property(pid: str) -> list[StayLifecycleRule]:
    return [r for r in LIFECYCLE_RULES if r.property_id == pid]


def slots_for_ruleset(rsid: str) -> list[ScheduleRulesetSlot]:
    return [s for s in SCHEDULE_RULESET_SLOTS if s.schedule_ruleset_id == rsid]


def ruleset_by_id(rsid: str) -> ScheduleRuleset | None:
    return next((r for r in SCHEDULE_RULESETS if r.id == rsid), None)


def assignments_for_workspace(ws_id: str) -> list[PropertyWorkRoleAssignment]:
    """All property_work_role_assignment rows whose parent user_work_role
    belongs to the given workspace."""
    uwr_ids = {r.id for r in USER_WORK_ROLES if r.workspace_id == ws_id}
    return [a for a in PROPERTY_WORK_ROLE_ASSIGNMENTS if a.user_work_role_id in uwr_ids]


def user_id_for_uwr(uwr_id: str) -> str | None:
    row = next((r for r in USER_WORK_ROLES if r.id == uwr_id), None)
    return row.user_id if row else None


def work_role_id_for_uwr(uwr_id: str) -> str | None:
    row = next((r for r in USER_WORK_ROLES if r.id == uwr_id), None)
    return row.work_role_id if row else None


# The "signed-in" user for each role.
DEFAULT_EMPLOYEE_ID = "e-maria"
DEFAULT_EMPLOYEE_USER_ID = "u-maria"
DEFAULT_MANAGER_NAME = "Élodie Bernard"
DEFAULT_MANAGER_USER_ID = "u-elodie"
DEFAULT_WORKSPACE_ID = "ws-bernard"
# §22 — Vincent Dupont is the demo client. Holds a `client` grant on
# CleanCo's workspace bound to his org `org-dupont-vincent`, and is
# also the owner_user of Villa du Lac + Seaside Apt.
DEFAULT_CLIENT_USER_ID = "u-vincent"


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


def properties_for_workspace(wsid: str) -> list[Property]:
    """All properties linked to `wsid` via the `property_workspace`
    junction (§02 multi-belonging). Includes properties the workspace
    owns, manages, or merely observes.
    """
    pids = {pw.property_id for pw in PROPERTY_WORKSPACES if pw.workspace_id == wsid}
    return [p for p in PROPERTIES if p.id in pids]


def organization_by_id(oid: str) -> Organization | None:
    return next((o for o in ORGANIZATIONS if o.id == oid), None)


def work_order_by_id(woid: str) -> WorkOrder | None:
    return next((w for w in WORK_ORDERS if w.id == woid), None)


def workspaces_for_user(uid: str) -> list[dict[str, Any]]:
    """Workspaces the user has access to, with their grant role.

    Resolution mirrors the production rule (§02): a user "belongs" to
    a workspace iff they hold an active grant on it (workspace-scope
    or via a property in `property_workspace`). For the mock we read
    `USER_WORKSPACES` (the materialised junction) and decorate each
    row with the highest-privilege `grant_role` the user holds there.
    """
    rank = {"manager": 4, "worker": 3, "client": 2, "guest": 1}
    rows: list[dict[str, Any]] = []
    grants_by_ws: dict[str, list[RoleGrant]] = {}
    for g in role_grants_for_user(uid):
        if g.scope_kind == "workspace":
            grants_by_ws.setdefault(g.scope_id, []).append(g)
    seen: set[str] = set()
    for uw in USER_WORKSPACES:
        if uw.user_id != uid or uw.workspace_id in seen:
            continue
        ws = workspace_by_id(uw.workspace_id)
        if ws is None:
            continue
        seen.add(uw.workspace_id)
        ws_grants = grants_by_ws.get(uw.workspace_id, [])
        grant_role = None
        binding_org_id = None
        if ws_grants:
            best = max(ws_grants, key=lambda g: rank.get(g.grant_role, 0))
            grant_role = best.grant_role
            binding_org_id = best.binding_org_id
        rows.append({
            "workspace": ws,
            "grant_role": grant_role,
            "binding_org_id": binding_org_id,
            "source": uw.source,
        })
    return rows


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


# ── Avatar store (§02 `file`, §12 POST /me/avatar) ─────────────────
# The mock keeps uploaded avatar bytes in memory. In production this
# would be a `file` row + blob on disk / S3 (§15). Here we just map
# `file_id -> (bytes, mime_type)` and expose the bytes via
# `GET /api/v1/files/{id}/blob`.

AVATAR_BYTES: dict[str, tuple[bytes, str]] = {}
_AVATAR_SEQ = 0


def _next_avatar_file_id() -> str:
    global _AVATAR_SEQ
    _AVATAR_SEQ += 1
    return f"f-avatar-{_AVATAR_SEQ:04d}"


def set_user_avatar(user_id: str, image_bytes: bytes, mime_type: str) -> str | None:
    """Store avatar bytes for `user_id`. Returns the previous file_id
    (to let the caller emit a before/after audit event), or None when
    no prior avatar was set.

    Keeps the compat `Employee.avatar_file_id` in sync with the
    authoritative `User.avatar_file_id` so employee-shaped serialisers
    pick up the change without extra work.
    """
    user = user_by_id(user_id)
    if user is None:
        return None
    previous = user.avatar_file_id
    new_id = _next_avatar_file_id()
    AVATAR_BYTES[new_id] = (image_bytes, mime_type)
    user.avatar_file_id = new_id
    for emp in EMPLOYEES:
        if emp.user_id == user_id:
            emp.avatar_file_id = new_id
    if previous and previous in AVATAR_BYTES:
        AVATAR_BYTES.pop(previous, None)
    return previous


def clear_user_avatar(user_id: str) -> str | None:
    """Clear `users.avatar_file_id`; returns the previous file_id."""
    user = user_by_id(user_id)
    if user is None:
        return None
    previous = user.avatar_file_id
    user.avatar_file_id = None
    for emp in EMPLOYEES:
        if emp.user_id == user_id:
            emp.avatar_file_id = None
    if previous and previous in AVATAR_BYTES:
        AVATAR_BYTES.pop(previous, None)
    return previous


def avatar_url_for_file_id(file_id: str | None) -> str | None:
    """Public URL clients drop into `<img src>` (§12)."""
    if not file_id:
        return None
    return f"/api/v1/files/{file_id}/blob"


# ── Deployment-admin seed data (§05, §11, §14, §16) ──────────────────
#
# In prod these rows live under scope_kind='deployment' with the
# reserved synthetic scope_id `00000000000000000000000000`. The mock
# keeps them as plain Python objects — the /admin API reads them
# directly, a real server would resolve them through the permission
# engine.


@dataclass
class AdminTeamMember:
    id: str
    user_id: str
    display_name: str
    email: str
    is_owner: bool
    granted_at: str
    granted_by: str


@dataclass
class AdminWorkspaceRow:
    id: str
    slug: str
    name: str
    plan: str
    verification_state: str
    properties_count: int
    members_count: int
    cap_usd_30d: float
    spent_usd_30d: float
    usage_percent: int
    paused: bool
    archived_at: str | None
    created_at: str


@dataclass
class AdminSignupSettings:
    enabled: bool
    disposable_domains_count: int
    throttle_per_ip_hour: int
    throttle_per_email_lifetime: int
    pre_verified_upload_mb_cap: int
    pre_verified_llm_percent_cap: int
    updated_at: str
    updated_by: str


@dataclass
class AdminDeploymentSetting:
    key: str
    value: Any
    kind: str  # bool | int | string
    description: str
    root_only: bool
    updated_at: str
    updated_by: str


@dataclass
class AdminChatProviderCredential:
    field: str          # e.g. "access_token", "phone_number_id"
    label: str
    display_stub: str   # e.g. "wa-tok-••••••3f2a"
    set: bool
    updated_at: str | None
    updated_by: str | None


@dataclass
class AdminChatProviderTemplate:
    name: str
    purpose: str        # human-readable intent
    status: str         # approved | pending | rejected | paused
    last_sync_at: str | None
    rejection_reason: str | None


@dataclass
class AdminChatProvider:
    channel_kind: str           # offapp_whatsapp | offapp_telegram
    label: str
    phone_display: str          # "+33 6 12 34 56 78" — stub, never the real number
    status: str                 # connected | error | not_configured
    last_webhook_at: str | None
    last_webhook_error: str | None
    webhook_url: str            # for operator to copy into Meta's console
    verify_token_stub: str
    credentials: list[AdminChatProviderCredential]
    templates: list[AdminChatProviderTemplate]
    per_workspace_soft_cap: int # soft per-workspace sub-cap on the shared number
    daily_outbound_cap: int     # Meta-tier ceiling on the whole provider
    outbound_24h: int           # observed
    delivery_error_rate_pct: float


@dataclass
class AdminChatOverrideRow:
    workspace_id: str
    workspace_name: str
    channel_kind: str
    phone_display: str
    status: str                 # connected | error | not_configured
    created_at: str
    reason: str | None


# Élodie is the bootstrap operator (also owner@bernard). Marc was added
# later as a deputy. One "owner" + one rule-driven "manager@deployment".
DEPLOYMENT_ADMINS: list[AdminTeamMember] = [
    AdminTeamMember(
        "da-1",
        user_id="u-elodie",
        display_name="Élodie Bernard",
        email="elodie@bernard.example",
        is_owner=True,
        granted_at="2025-11-02",
        granted_by="(bootstrap)",
    ),
    AdminTeamMember(
        "da-2",
        user_id="u-marc",
        display_name="Marc Faure",
        email="marc@ops.crewday.app",
        is_owner=False,
        granted_at="2026-01-14",
        granted_by="Élodie Bernard",
    ),
]


DEPLOYMENT_WORKSPACES: list[AdminWorkspaceRow] = [
    AdminWorkspaceRow(
        "ws-01", slug="bernard", name="Bernard household",
        plan="pro", verification_state="trusted",
        properties_count=4, members_count=9,
        cap_usd_30d=25.0, spent_usd_30d=8.12, usage_percent=33, paused=False,
        archived_at=None, created_at="2025-11-02",
    ),
    AdminWorkspaceRow(
        "ws-02", slug="cleanco", name="CleanCo agency",
        plan="pro", verification_state="trusted",
        properties_count=12, members_count=23,
        cap_usd_30d=40.0, spent_usd_30d=31.40, usage_percent=79, paused=False,
        archived_at=None, created_at="2025-12-17",
    ),
    AdminWorkspaceRow(
        "ws-03", slug="villa-mer", name="Villa Mer",
        plan="free", verification_state="human_verified",
        properties_count=1, members_count=3,
        cap_usd_30d=5.0, spent_usd_30d=5.00, usage_percent=100, paused=True,
        archived_at=None, created_at="2026-03-04",
    ),
    AdminWorkspaceRow(
        "ws-04", slug="jardinerie", name="Jardinerie coop",
        plan="free", verification_state="email_verified",
        properties_count=2, members_count=2,
        cap_usd_30d=0.5, spent_usd_30d=0.05, usage_percent=10, paused=False,
        archived_at=None, created_at="2026-04-10",
    ),
    AdminWorkspaceRow(
        "ws-05", slug="mas-des-oliviers", name="Mas des Oliviers",
        plan="free", verification_state="trusted",
        properties_count=2, members_count=5,
        cap_usd_30d=12.0, spent_usd_30d=1.20, usage_percent=10, paused=False,
        archived_at="2026-02-08", created_at="2025-09-01",
    ),
]


DEPLOYMENT_CHAT_PROVIDERS: list[AdminChatProvider] = [
    AdminChatProvider(
        channel_kind="offapp_whatsapp",
        label="WhatsApp (Meta Cloud API)",
        phone_display="+33 6 44 00 12 34",
        status="connected",
        last_webhook_at="2026-04-18T08:09:41Z",
        last_webhook_error=None,
        webhook_url="https://crew.day/webhooks/chat/whatsapp",
        verify_token_stub="wa-verify-••••••e71c",
        credentials=[
            AdminChatProviderCredential(
                field="access_token", label="Access token",
                display_stub="wa-tok-••••••3f2a", set=True,
                updated_at="2026-02-04T14:22:07Z", updated_by="Élodie Bernard",
            ),
            AdminChatProviderCredential(
                field="phone_number_id", label="Phone-number id",
                display_stub="1057••••", set=True,
                updated_at="2026-02-04T14:22:07Z", updated_by="Élodie Bernard",
            ),
            AdminChatProviderCredential(
                field="business_account_id", label="Business-account id",
                display_stub="5591••••", set=True,
                updated_at="2026-02-04T14:22:07Z", updated_by="Élodie Bernard",
            ),
            AdminChatProviderCredential(
                field="webhook_verify_token", label="Webhook verify-token",
                display_stub="wa-verify-••••••e71c", set=True,
                updated_at="2026-02-04T14:22:07Z", updated_by="Élodie Bernard",
            ),
        ],
        templates=[
            AdminChatProviderTemplate(
                name="chat_channel_link_code",
                purpose="Sent during the link ceremony when a user pairs their phone.",
                status="approved",
                last_sync_at="2026-04-17T11:30:00Z",
                rejection_reason=None,
            ),
            AdminChatProviderTemplate(
                name="chat_agent_nudge",
                purpose="Agent-initiated reach-out past the 24h session window.",
                status="approved",
                last_sync_at="2026-04-17T11:30:00Z",
                rejection_reason=None,
            ),
            AdminChatProviderTemplate(
                name="chat_workspace_pick",
                purpose="Asks a user which workspace an inbound message is for (shared-number disambiguation).",
                status="pending",
                last_sync_at="2026-04-18T07:02:00Z",
                rejection_reason=None,
            ),
        ],
        per_workspace_soft_cap=200,
        daily_outbound_cap=1000,
        outbound_24h=318,
        delivery_error_rate_pct=0.4,
    ),
    AdminChatProvider(
        channel_kind="offapp_telegram",
        label="Telegram (Bot API)",
        phone_display="@crewday_bot",
        status="not_configured",
        last_webhook_at=None,
        last_webhook_error=None,
        webhook_url="https://crew.day/webhooks/chat/telegram",
        verify_token_stub="—",
        credentials=[
            AdminChatProviderCredential(
                field="bot_token", label="Bot token",
                display_stub="—", set=False,
                updated_at=None, updated_by=None,
            ),
        ],
        templates=[],
        per_workspace_soft_cap=0,
        daily_outbound_cap=0,
        outbound_24h=0,
        delivery_error_rate_pct=0.0,
    ),
]


DEPLOYMENT_CHAT_OVERRIDES: list[AdminChatOverrideRow] = [
    AdminChatOverrideRow(
        workspace_id="ws-02",
        workspace_name="CleanCo agency",
        channel_kind="offapp_whatsapp",
        phone_display="+33 1 75 00 22 00",
        status="connected",
        created_at="2026-03-11",
        reason="Client-branded number — CleanCo owns the Meta business verification.",
    ),
]


DEPLOYMENT_SIGNUP_SETTINGS = AdminSignupSettings(
    enabled=True,
    disposable_domains_count=1782,
    throttle_per_ip_hour=5,
    throttle_per_email_lifetime=3,
    pre_verified_upload_mb_cap=25,
    pre_verified_llm_percent_cap=10,
    updated_at="2026-04-10T11:02:14Z",
    updated_by="Élodie Bernard",
)


DEPLOYMENT_SETTINGS: list[AdminDeploymentSetting] = [
    AdminDeploymentSetting(
        "signup_enabled", True, "bool",
        "Allow anonymous visitors to create workspaces via /signup.",
        root_only=False,
        updated_at="2026-04-10T11:02:14Z", updated_by="Élodie Bernard",
    ),
    AdminDeploymentSetting(
        "signup_disposable_domains_path", "/etc/crewday/disposable-domains.txt", "string",
        "Disposable-domain blocklist consulted at /signup/start (§15).",
        root_only=False,
        updated_at="2026-02-22T09:40:00Z", updated_by="Élodie Bernard",
    ),
    AdminDeploymentSetting(
        "llm_default_budget_cents_30d", 500, "int",
        "Default workspace rolling-30-day LLM spend cap (cents, USD).",
        root_only=False,
        updated_at="2026-01-03T14:21:00Z", updated_by="Élodie Bernard",
    ),
    AdminDeploymentSetting(
        "trusted_interfaces", "tailscale*", "string",
        "Comma-separated fnmatch globs of interface names the §15 bind guard trusts.",
        root_only=True,
        updated_at="2025-11-02T00:00:00Z", updated_by="(bootstrap)",
    ),
    AdminDeploymentSetting(
        "retention_audit_days", 365, "int",
        "Deployment default for per-workspace audit_log retention.",
        root_only=False,
        updated_at="2025-11-02T00:00:00Z", updated_by="(bootstrap)",
    ),
]


# Deployment-scoped audit rows — reuses `AuditEntry` with actor_grant_role
# set to 'admin' and actor_action_key in the deployment.* namespace.
DEPLOYMENT_AUDIT: list[AuditEntry] = [
    AuditEntry(
        datetime(2026, 4, 18, 8, 5, 0),
        "user", "Élodie Bernard", "deployment.llm.assignment_updated",
        "chat.manager", "web",
        "Switched chat.manager to google/gemma-4-31b-it (was anthropic/claude-haiku-4-5)",
        actor_grant_role="admin",
        actor_was_owner_member=True,
        actor_action_key="deployment.llm.edit",
        actor_id="u-elodie",
    ),
    AuditEntry(
        datetime(2026, 4, 17, 17, 42, 0),
        "user", "Marc Faure", "deployment.budget.updated",
        "ws-03", "api",
        "Raised cap_usd_30d from $2 to $5 for Villa Mer (human-verified)",
        actor_grant_role="admin",
        actor_was_owner_member=False,
        actor_action_key="deployment.budget.edit",
        actor_id="u-marc",
    ),
    AuditEntry(
        datetime(2026, 4, 15, 14, 3, 0),
        "user", "Élodie Bernard", "deployment.workspace.trusted",
        "ws-02", "web",
        "CleanCo promoted to verification_state='trusted' after manual review",
        actor_grant_role="admin",
        actor_was_owner_member=True,
        actor_action_key="deployment.workspaces.trust",
        actor_id="u-elodie",
    ),
    AuditEntry(
        datetime(2026, 4, 10, 11, 2, 14),
        "user", "Élodie Bernard", "deployment.signup.settings_updated",
        "signup_enabled", "web",
        "Tightened per-IP hourly throttle from 10 to 5",
        actor_grant_role="admin",
        actor_was_owner_member=True,
        actor_action_key="deployment.signup.edit",
        actor_id="u-elodie",
    ),
    AuditEntry(
        datetime(2026, 2, 8, 9, 17, 0),
        "user", "Élodie Bernard", "deployment.workspace.archived",
        "ws-05", "web",
        "Mas des Oliviers archived at the owner's request",
        actor_grant_role="admin",
        actor_was_owner_member=True,
        actor_action_key="deployment.workspaces.archive",
        actor_id="u-elodie",
    ),
    AuditEntry(
        datetime(2026, 1, 14, 16, 20, 0),
        "user", "Élodie Bernard", "deployment.admins.granted",
        "u-marc", "web",
        "Granted admin surface to Marc Faure",
        actor_grant_role="admin",
        actor_was_owner_member=True,
        actor_action_key="role_grants.create",
        actor_id="u-elodie",
    ),
]


ADMIN_AGENT_LOG: list[AgentMessage] = [
    AgentMessage(
        datetime(2026, 4, 18, 8, 2), "agent",
        "Morning. Villa Mer is paused — their $5 cap is full (5/5 spent). "
        "They're human-verified. Raise the cap to $10 or wait for the window "
        "to age out?",
    ),
    AgentMessage(
        datetime(2026, 4, 18, 8, 3), "user",
        "Raise to $10, note 'single-owner stretch'.",
    ),
    AgentMessage(
        datetime(2026, 4, 18, 8, 3), "agent",
        "Queued — that's a `deployment.budget.edit` > 2× bump, so it needs "
        "your confirmation in the actions tray →",
    ),
    AgentMessage(
        datetime(2026, 4, 18, 8, 5), "user",
        "How's chat.manager spend trending? Anthropic's Haiku felt expensive.",
    ),
    AgentMessage(
        datetime(2026, 4, 18, 8, 5), "agent",
        "Haiku ran about $0.12/1k out vs. Gemma's $0.003. Last 30 days: "
        "$12.40 on Haiku, 81% of chat.manager total. Switching chat.manager "
        "back to Gemma would save ~$9/mo at current volume.",
    ),
]


ADMIN_AGENT_ACTIONS: list[AgentAction] = [
    AgentAction(
        "aa-admin-1", "Raise Villa Mer cap to $10",
        "cap_usd_30d 5 → 10 (>2× — workspace-gated in admin policy).",
        "medium",
        card_summary="Raise Villa Mer cap from $5 to $10 (rolling 30d)?",
        card_fields=[
            ("workspace", "Villa Mer (ws-03)"),
            ("new cap", "$10.00"),
            ("current 30d spend", "$5.00"),
            ("note", "single-owner stretch"),
        ],
        gate_source="workspace_configurable",
        inline_channel="web_admin_sidebar",
    ),
]
