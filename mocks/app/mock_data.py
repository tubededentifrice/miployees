"""Static mock data for the miployees UI preview.

Shapes and vocabulary follow the specs in docs/specs/. The point is to
make the eventual product feel real — not to simulate it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Literal


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


@dataclass
class Role:
    id: str
    name: str


@dataclass
class Employee:
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


@dataclass
class Stay:
    id: str
    property_id: str
    guest: str
    source: Literal["Airbnb", "VRBO", "Booking.com", "Direct"]
    check_in: date
    check_out: date
    guests: int
    status: Literal["booked", "in_house", "checked_out", "cancelled"] = "booked"


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
    status: Literal["pending", "in_progress", "completed", "skipped"]
    checklist: list[dict] = field(default_factory=list)
    photo_evidence: Literal["disabled", "optional", "required"] = "disabled"
    instructions_ids: list[str] = field(default_factory=list)
    template_id: str | None = None
    schedule_id: str | None = None
    turnover_bundle_id: str | None = None


@dataclass
class Expense:
    id: str
    employee_id: str
    amount_cents: int
    currency: str
    merchant: str
    submitted_at: datetime
    status: Literal["pending", "approved", "rejected", "reimbursed"]
    note: str
    ocr_confidence: float | None = None


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


@dataclass
class Leave:
    id: str
    employee_id: str
    starts_on: date
    ends_on: date
    category: Literal["vacation", "sick", "personal", "bereavement", "other"]
    note: str
    approved_at: datetime | None = None


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
    severity: Literal["low", "medium", "high"]
    category: Literal["damage", "broken", "supplies", "safety", "other"]
    title: str
    body: str
    reported_at: datetime
    status: Literal["open", "in_progress", "resolved"]


@dataclass
class PaySlip:
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
    at: datetime
    actor_kind: Literal["human", "agent", "system"]
    actor: str
    action: str
    target: str
    via: Literal["web", "api", "cli", "system"]
    reason: str | None = None


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


# ── Canonical starter data ───────────────────────────────────────────

PROPERTIES: list[Property] = [
    Property("p-villa-sud", "Villa Sud", "Antibes", "Europe/Paris", "moss", "str",
             areas=["Master bedroom", "Kitchen", "Pool", "Garden", "Entryway", "Living room"]),
    Property("p-apt-3b", "Apt 3B", "Paris", "Europe/Paris", "sky", "str",
             areas=["Full unit", "Kitchen", "Bathroom 1", "Bathroom 2"]),
    Property("p-chalet", "Chalet Cœur", "Megève", "Europe/Paris", "rust", "vacation",
             areas=["Kitchen", "Fireplace room", "Master bedroom", "Ski room"]),
]

ROLES: list[Role] = [
    Role("r-housekeeper", "Housekeeper"),
    Role("r-cook", "Cook"),
    Role("r-driver", "Driver"),
    Role("r-gardener", "Gardener"),
    Role("r-handyman", "Handyman"),
    Role("r-poolcare", "Pool care"),
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
    ),
    Employee(
        "e-arun", "Arun Patel", ["Driver"], ["p-villa-sud"],
        "AP", "+33 6 22 45 67 89", "arun@example.com", date(2024, 9, 14),
        capabilities=_caps(**{"time.geofence_required": True}),
    ),
    Employee(
        "e-ben", "Ben Traoré", ["Gardener", "Pool care"], ["p-villa-sud"],
        "BT", "+33 6 33 56 78 90", "ben@example.com", date(2023, 5, 20),
        capabilities=_caps(),
    ),
    Employee(
        "e-ana", "Ana Rossi", ["Housekeeper", "Cook"], ["p-apt-3b", "p-chalet"],
        "AR", "+33 6 44 67 89 01", "ana@example.com", date(2024, 11, 2),
        capabilities=_caps(**{"chat.assistant": True, "voice.assistant": True}),
    ),
    Employee(
        "e-sam", "Sam Leclerc", ["Handyman"], ["p-villa-sud", "p-chalet"],
        "SL", "+33 6 55 78 90 12", "sam@example.com", date(2025, 1, 9),
        capabilities=_caps(),
    ),
]

STAYS: list[Stay] = [
    Stay("s-1", "p-villa-sud", "Johnson family",   "Airbnb",       date(2026, 4, 13), date(2026, 4, 16), 4, "in_house"),
    Stay("s-2", "p-villa-sud", "Park couple",      "VRBO",         date(2026, 4, 17), date(2026, 4, 22), 2),
    Stay("s-3", "p-apt-3b",    "Nakamura",         "Airbnb",       date(2026, 4, 15), date(2026, 4, 18), 2, "in_house"),
    Stay("s-4", "p-chalet",    "Müller family",    "Direct",       date(2026, 4, 19), date(2026, 4, 26), 6),
    Stay("s-5", "p-apt-3b",    "Svensson",         "Booking.com",  date(2026, 4, 24), date(2026, 4, 28), 3),
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
]


EXPENSES: list[Expense] = [
    Expense("x-1", "e-maria", 4280, "EUR", "Carrefour",   datetime(2026, 4, 14, 17, 32), "pending", "Cleaning supplies — bleach, sponges, 2× fresh towels", ocr_confidence=0.96),
    Expense("x-2", "e-arun",  1890, "EUR", "Total Energies", datetime(2026, 4, 13, 19, 5), "approved", "Fuel — Johnson airport run", ocr_confidence=0.99),
    Expense("x-3", "e-ben",  12500, "EUR", "Pool Pro",    datetime(2026, 4, 10, 11, 22), "pending", "Chlorine tablets (3 month supply) + replacement skimmer basket", ocr_confidence=0.94),
    Expense("x-4", "e-ana",   2210, "EUR", "Marché Provence", datetime(2026, 4, 11, 9, 40), "approved", "Welcome-basket groceries — Apt 3B"),
    Expense("x-5", "e-sam",   5780, "EUR", "Brico Dépôt", datetime(2026, 4, 9, 14, 58), "reimbursed", "Door handles, screws, wood filler"),
]


APPROVALS: list[ApprovalRequest] = [
    ApprovalRequest(
        "a-1", "digest-agent", "tasks.reassign",
        "Pool check (Villa Sud) → Sam Leclerc",
        "Ben Traoré is on approved leave 18–21 Apr; Sam is the configured backup for pool care.",
        datetime(2026, 4, 15, 9, 47), "low",
        diff=["assignee: e-ben → e-sam", "note appended: 'auto-reassigned: covering leave'"],
    ),
    ApprovalRequest(
        "a-2", "payroll-agent", "payroll.issue",
        "April payslips — 5 employees",
        "Monthly pay run. Totals within 4% of last month. No open shifts.",
        datetime(2026, 4, 15, 8, 2), "medium",
        diff=["5× payslip draft → issued", "period 2026-04: locked → paid (on last payslip pay)"],
    ),
    ApprovalRequest(
        "a-3", "procurement-agent", "expenses.agent_purchase",
        "Dyson V11 vacuum · €449 · delivered to Villa Sud",
        "Current vacuum flagged by Maria with photo; motor burned. Budget remaining €820.",
        datetime(2026, 4, 14, 16, 18), "medium",
        diff=["create expense: €449 EUR to Brico Dépôt", "attach issue #iss-3 as justification"],
    ),
]


LEAVES: list[Leave] = [
    Leave("lv-1", "e-ben",   date(2026, 4, 18), date(2026, 4, 21), "personal",  "Family visit — Bordeaux",
          approved_at=datetime(2026, 4, 3, 11, 0)),
    Leave("lv-2", "e-ana",   date(2026, 5, 1),  date(2026, 5, 3),  "vacation",  "Long weekend"),
    Leave("lv-3", "e-sam",   date(2026, 4, 22), date(2026, 4, 22), "sick",      "Migraine — will try to make Thursday"),
    Leave("lv-4", "e-arun",  date(2026, 6, 15), date(2026, 6, 29), "vacation",  "Annual trip home — India",
          approved_at=datetime(2026, 2, 20, 10, 15)),
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
          "medium", "broken", "Bedside lamp flickers",
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
    PaySlip("ps-1", "e-maria", date(2026, 3, 1), date(2026, 3, 31), 240000, 6420, 246420, "paid", 168.5, 4.0),
    PaySlip("ps-2", "e-arun",  date(2026, 3, 1), date(2026, 3, 31), 120000, 1890, 121890, "paid", 84.0, 0),
    PaySlip("ps-3", "e-ben",   date(2026, 3, 1), date(2026, 3, 31), 180000, 0,    180000, "paid", 104.0, 2.0),
    PaySlip("ps-4", "e-ana",   date(2026, 3, 1), date(2026, 3, 31), 210000, 2210, 212210, "paid", 142.0, 0),
    PaySlip("ps-5", "e-sam",   date(2026, 3, 1), date(2026, 3, 31), 160000, 5780, 165780, "paid", 96.0, 0),
    PaySlip("ps-6", "e-maria", date(2026, 4, 1), date(2026, 4, 30), 248000, 4280, 252280, "draft", 170.0, 6.0),
    PaySlip("ps-7", "e-arun",  date(2026, 4, 1), date(2026, 4, 30), 118000, 0,    118000, "draft", 82.0, 0),
    PaySlip("ps-8", "e-ben",   date(2026, 4, 1), date(2026, 4, 30), 170000, 12500, 182500, "draft", 98.0, 0),
    PaySlip("ps-9", "e-ana",   date(2026, 4, 1), date(2026, 4, 30), 212000, 2210, 214210, "draft", 144.0, 1.0),
    PaySlip("ps-10","e-sam",   date(2026, 4, 1), date(2026, 4, 30), 162000, 0,    162000, "draft", 97.0, 0),
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
    AuditEntry(datetime(2026, 4, 15, 10, 8, 12), "human", "Élodie Bernard", "task.complete",          "t-1",  "web", None),
    AuditEntry(datetime(2026, 4, 15, 9, 47, 2),  "agent", "digest-agent",   "agent_action.requested", "a-1",  "api", "Auto-reassign pool coverage (Ben on leave)"),
    AuditEntry(datetime(2026, 4, 15, 9, 41, 0),  "human", "Maria Alvarez",  "shift.clock_in",         "sh-…", "web", None),
    AuditEntry(datetime(2026, 4, 15, 9, 12, 18), "agent", "digest-agent",   "digest.sent",            "—",    "api", "Morning manager digest"),
    AuditEntry(datetime(2026, 4, 15, 8, 54, 1),  "agent", "procurement-agent", "expense.autofill",    "x-1",  "api", None),
    AuditEntry(datetime(2026, 4, 15, 8, 41, 12), "system", "redaction-layer", "llm.call.blocked",    "—",    "system", "IBAN-like string in receipt text"),
    AuditEntry(datetime(2026, 4, 15, 8, 12, 0),  "human", "Maria Alvarez",  "shift.clock_in",         "sh-…", "web", None),
    AuditEntry(datetime(2026, 4, 14, 17, 32, 0), "human", "Maria Alvarez",  "expense.submit",         "x-1",  "web", None),
    AuditEntry(datetime(2026, 4, 14, 16, 18, 0), "agent", "procurement-agent", "agent_action.requested", "a-3","api", "Vacuum replacement"),
    AuditEntry(datetime(2026, 4, 14, 11, 32, 0), "human", "Maria Alvarez",  "issue.open",             "iss-1","web", None),
]


WEBHOOKS: list[Webhook] = [
    Webhook("wh-1", "https://hooks.example.com/miployees/digest", ["digest.manager", "digest.employee"], True, 200, datetime(2026, 4, 15, 9, 12, 22)),
    Webhook("wh-2", "https://n8n.local/webhook/miployees-payroll", ["payroll.period_locked", "payroll.period_paid"], True, 200, datetime(2026, 3, 31, 22, 1, 0)),
    Webhook("wh-3", "https://slack.internal/hooks/T042/.../B08/...", ["approval.requested", "approval.resolved"], True, 200, datetime(2026, 4, 15, 9, 47, 3)),
    Webhook("wh-4", "https://legacy.host/webhooks/tasks", ["task.completed"], False, 502, datetime(2026, 3, 20, 14, 55, 0)),
]


HOUSEHOLD_SETTINGS: dict[str, Any] = {
    "name": "Bernard household",
    "timezone": "Europe/Paris",
    "currency": "EUR",
    "week_start": "Monday",
    "pay_frequency": "monthly",
    "default_photo_evidence": "optional",
    "geofence_radius_m": 150,
    "retention_days": {"llm_calls": 90, "audit": 730, "task_photos": 365},
    "approvals": {
        "always_gated": ["payout_destination.*", "employee.set_default_pay_destination"],
        "configurable": ["tasks.bulk_reassign>50", "broadcast.email_many"],
    },
    "danger_zone": ["Rotate envelope key (host CLI only)", "Purge employee (host CLI only)", "Export household backup"],
}


GUEST_STAY_ID = "s-3"  # the preview guest page renders this stay


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


# The "signed-in" user for each role.
DEFAULT_EMPLOYEE_ID = "e-maria"
DEFAULT_MANAGER_NAME = "Élodie Bernard"
