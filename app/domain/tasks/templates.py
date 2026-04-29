"""``task_template`` CRUD service.

The :class:`TaskTemplate` row is the reusable blueprint every
schedule / stay-lifecycle rule materialises occurrences from. This
module is the only place that inserts, updates, soft-deletes, or
reads template rows at the domain layer (§01 "Handlers are thin").

Public surface:

* **DTOs** — Pydantic v2 models for the request / response shape
  (:class:`TaskTemplateCreate`, :class:`TaskTemplateUpdate`,
  :class:`TaskTemplateView`). Scope-consistency is enforced inside
  the DTO via ``model_validator`` so the same rule fires whether
  the caller posts a JSON body or builds the model in Python.
* **Service functions** — ``create`` / ``read`` / ``list`` /
  ``update`` / ``delete``. Every function takes a
  :class:`~app.tenancy.WorkspaceContext` as its first argument; the
  ``workspace_id`` is resolved from the context, never from the
  caller's payload (v1 invariant §01).
* **Errors** — :class:`TaskTemplateNotFound`,
  :class:`ScopeInconsistent`, :class:`TemplateInUseError`. Each
  subclasses the stdlib parent the router's global handler maps to
  (``LookupError`` → 404, ``ValueError`` → 422, ``ValueError`` →
  409 with a dedicated ``detail`` payload for the in-use case).

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation
writes one :mod:`app.audit` row in the same transaction.

**Scope-consistency rules.**

* ``property_scope='any'`` — ``listed_property_ids`` MUST be empty.
* ``property_scope='one'`` — exactly one id in
  ``listed_property_ids``.
* ``property_scope='listed'`` — non-empty list; duplicates rejected.
* ``area_scope`` follows the same three rules;
  ``area_scope='derived'`` additionally requires an empty list (the
  ids come from the stay context at generation time, not from the
  template).

**Soft-delete.** :func:`delete` sets ``deleted_at`` rather than
issuing a ``DELETE``. Refuse if the template is referenced by any
live ``schedule.template_id`` — those references are *active* (the
``schedule.deleted_at`` column lands with cd-k4l; until then every
row is considered active). Refusal raises
:class:`TemplateInUseError` carrying the offending ids. The
``stay_lifecycle_rule`` table does not exist yet (§06 is ahead of
the schema); the service leaves a hook for that check so cd-4qr
can add it without another service-wide refactor.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Task template",
§"Checklist template shape", §"Evidence policy inheritance";
``docs/specs/02-domain-model.md`` §"task_template".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from dateutil.rrule import rrulestr
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.adapters.db.tasks.models import Schedule, TaskTemplate
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ChecklistRRULEError",
    "ChecklistTemplateItem",
    "ChecklistTemplateItemPayload",
    "ScopeInconsistent",
    "TaskTemplateCreate",
    "TaskTemplateNotFound",
    "TaskTemplateUpdate",
    "TaskTemplateView",
    "TemplateInUseError",
    "create",
    "delete",
    "expand_checklist_for_task",
    "list_templates",
    "read",
    "read_many",
    "reorder_checklist",
    "update",
    "validate_checklist_template",
]


# ---------------------------------------------------------------------------
# Enums (string literals — keep parity with the DB CHECK constraints)
# ---------------------------------------------------------------------------


PropertyScope = Literal["any", "one", "listed"]
AreaScope = Literal["any", "one", "listed", "derived"]
PhotoEvidence = Literal["disabled", "optional", "required"]
Priority = Literal["low", "normal", "high", "urgent"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TaskTemplateNotFound(LookupError):
    """The requested template does not exist in the caller's workspace.

    404-equivalent. Raised by :func:`read`, :func:`update`, and
    :func:`delete` when the id is unknown or already soft-deleted
    (soft-deleted rows are hidden from every non-admin read path;
    an explicit ``deleted=True`` filter on :func:`list_templates`
    surfaces them).
    """


class ScopeInconsistent(ValueError):
    """Scope shape contradicts the ``property_scope`` / ``area_scope`` enum.

    422-equivalent. Fired by the DTO's ``model_validator`` before
    any DB write — the CHECK constraint at the DB layer is a
    safety net, not the primary gate. The message names the
    offending field so the UI can surface it next to the right
    input; callers that need machine-readable detail should read
    the pydantic ``ValidationError`` that wraps this exception.
    """


class TemplateInUseError(ValueError):
    """Refuse to soft-delete a template referenced by live callers.

    409-equivalent. Carries the offending ids so the UI can render
    "In use by 2 schedules: 01HWA…, 01HWA…". The payload is split
    across :attr:`schedule_ids` and :attr:`stay_lifecycle_rule_ids`
    so the caller doesn't have to guess which table a given id
    belongs to.

    ``stay_lifecycle_rule_ids`` is always an empty tuple in v1 —
    the ``stay_lifecycle_rule`` table lands with cd-4qr. The field
    exists so the shape is stable across that migration.
    """

    def __init__(
        self,
        *,
        template_id: str,
        schedule_ids: Sequence[str] = (),
        stay_lifecycle_rule_ids: Sequence[str] = (),
    ) -> None:
        self.template_id = template_id
        self.schedule_ids: tuple[str, ...] = tuple(schedule_ids)
        self.stay_lifecycle_rule_ids: tuple[str, ...] = tuple(stay_lifecycle_rule_ids)
        parts: list[str] = []
        if self.schedule_ids:
            parts.append(f"{len(self.schedule_ids)} schedule(s)")
        if self.stay_lifecycle_rule_ids:
            parts.append(f"{len(self.stay_lifecycle_rule_ids)} stay-lifecycle rule(s)")
        detail = " and ".join(parts) if parts else "unknown consumers"
        super().__init__(
            f"task_template {template_id!r} is in use by {detail}; "
            "soft-delete is refused"
        )


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


# Caps chosen to keep the DB + audit payload bounded without being
# restrictive in practice. The spec ceiling on template name length
# is not explicit; 200 is a friendly upper bound that matches the
# UI truncation target and comfortably covers UTF-8 emoji-inclusive
# names.
_MAX_NAME_LEN = 200
_MAX_DESC_LEN = 20_000
_MAX_HINTS_LEN = 20_000
# Per-template caps: a template referencing 200 properties is an
# authoring mistake, not a legitimate workflow; we reject early to
# protect the `IN (?, ?, …)` query at generation time.
_MAX_SCOPE_IDS = 200
_MAX_INSTRUCTION_LINKS = 200
_MAX_CHECKLIST_ITEMS = 200


class ChecklistRRULEError(ValueError):
    """A checklist item's RRULE is not parseable as RFC 5545."""

    def __init__(self, rrule: str) -> None:
        self.rrule = rrule
        super().__init__(f"invalid checklist RRULE {rrule!r}")


class ChecklistTemplateItem(BaseModel):
    """One entry in the ``checklist_template_json`` list.

    Mirrors §06 "Checklist template shape":

    * ``key`` — stable per-template slug, unique within the template.
    * ``text`` — rendered label.
    * ``required`` — must be checked to complete the occurrence.
    * ``guest_visible`` — shown to the guest-facing view.
    * ``rrule`` (optional, RFC 5545) — visibility filter evaluated
      in the property timezone at generation time.
    * ``dtstart_local`` (optional, ISO-8601 date) — RRULE anchor.

    RRULE strings are validated at write time and normalised to the
    canonical ``RRULE:`` body without the prefix. Expansion still
    resolves the effective ``dtstart_local`` because schedule / stay
    generation may supply that anchor later.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    text: str = Field(..., min_length=1, max_length=500)
    required: bool = False
    guest_visible: bool = False
    rrule: str | None = Field(default=None, max_length=500)
    dtstart_local: date | None = None

    @field_validator("rrule")
    @classmethod
    def _validate_rrule(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalise_rrule(value)


ChecklistTemplateItemPayload = ChecklistTemplateItem


class _TaskTemplateBody(BaseModel):
    """Shared body of the create + update DTOs.

    Held as a private base so the ``model_validator`` that enforces
    scope consistency runs on both. Pydantic v2's ``model_validator``
    decorates the parent class once and every subclass inherits the
    rule — :class:`TaskTemplateCreate` and :class:`TaskTemplateUpdate`
    therefore share the exact same validation surface without
    duplication.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    description_md: str = Field(default="", max_length=_MAX_DESC_LEN)
    role_id: str | None = Field(default=None, max_length=32)
    duration_minutes: int = Field(default=30, ge=1, le=24 * 60)
    property_scope: PropertyScope = "any"
    listed_property_ids: list[str] = Field(
        default_factory=list, max_length=_MAX_SCOPE_IDS
    )
    area_scope: AreaScope = "any"
    listed_area_ids: list[str] = Field(default_factory=list, max_length=_MAX_SCOPE_IDS)
    checklist_template_json: list[ChecklistTemplateItem] = Field(
        default_factory=list, max_length=_MAX_CHECKLIST_ITEMS
    )
    photo_evidence: PhotoEvidence = "disabled"
    linked_instruction_ids: list[str] = Field(
        default_factory=list, max_length=_MAX_INSTRUCTION_LINKS
    )
    priority: Priority = "normal"
    # SKU → quantity map. Value must be a positive integer (count of
    # units consumed per occurrence). We validate the outer shape
    # here; the per-SKU reference is resolved at consume-on-task
    # time by the inventory worker (§08).
    inventory_consumption_json: dict[str, int] = Field(default_factory=dict)
    llm_hints_md: str | None = Field(default=None, max_length=_MAX_HINTS_LEN)

    @model_validator(mode="after")
    def _validate_scopes_and_checklist(self) -> _TaskTemplateBody:
        """Enforce scope shape + checklist-key uniqueness + inventory shape.

        Raises :class:`ScopeInconsistent` (wrapped by pydantic into a
        ``ValidationError``) when the shape rules don't hold. Doing
        this at model-validator time means the router returns a 422
        with the full pydantic error payload without the service
        having to re-validate on its side.
        """
        _assert_property_scope(self.property_scope, self.listed_property_ids)
        _assert_area_scope(self.area_scope, self.listed_area_ids)
        validate_checklist_template(self.checklist_template_json)
        _assert_inventory_positive(self.inventory_consumption_json)
        return self


class TaskTemplateCreate(_TaskTemplateBody):
    """Request body for ``POST /api/v1/task_templates``.

    Same shape as :class:`_TaskTemplateBody`; the distinct class
    lets the router wire up the OpenAPI schema name and gives the
    service a type-safe create surface separate from update.
    """


class TaskTemplateUpdate(_TaskTemplateBody):
    """Request body for ``PATCH /api/v1/task_templates/{id}``.

    v1 treats update as a full replacement of the mutable body —
    the spec does not (yet) call for per-field PATCH. Callers send
    the full desired state; the service diffs against the current
    row, writes through, and records the before/after diff in the
    audit log. A future task (cd-ttc-patch) can introduce per-
    field partial update once the UI needs it.
    """


@dataclass(frozen=True, slots=True)
class TaskTemplateView:
    """Immutable read projection of a ``task_template`` row.

    Returned by every service read + write. A frozen / slotted
    dataclass (not a Pydantic model) because reads carry audit-
    sensitive fields (``deleted_at``, ``created_at``) that are
    managed by the service, not the caller's payload. Keeping the
    read shape separate from the write shape removes the accidental
    "echo the DB timestamp back to the client, accept it on
    round-trip" class of bug.
    """

    id: str
    workspace_id: str
    name: str
    description_md: str
    role_id: str | None
    duration_minutes: int
    property_scope: PropertyScope
    listed_property_ids: tuple[str, ...]
    area_scope: AreaScope
    listed_area_ids: tuple[str, ...]
    checklist_template_json: tuple[ChecklistTemplateItem, ...]
    photo_evidence: PhotoEvidence
    linked_instruction_ids: tuple[str, ...]
    priority: Priority
    inventory_consumption_json: dict[str, int]
    llm_hints_md: str | None
    created_at: datetime
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# Validators (shared between DTO and service)
# ---------------------------------------------------------------------------


def _assert_property_scope(scope: str, ids: Sequence[str]) -> None:
    """Enforce the ``property_scope`` ↔ ``listed_property_ids`` shape."""
    if scope == "any" and ids:
        raise ScopeInconsistent(
            "property_scope='any' requires listed_property_ids to be empty"
        )
    if scope == "one" and len(ids) != 1:
        raise ScopeInconsistent(
            "property_scope='one' requires exactly one id in listed_property_ids"
        )
    if scope == "listed" and not ids:
        raise ScopeInconsistent(
            "property_scope='listed' requires a non-empty listed_property_ids"
        )
    if len(set(ids)) != len(ids):
        raise ScopeInconsistent("listed_property_ids must not contain duplicates")


def _assert_area_scope(scope: str, ids: Sequence[str]) -> None:
    """Enforce the ``area_scope`` ↔ ``listed_area_ids`` shape."""
    if scope == "any" and ids:
        raise ScopeInconsistent("area_scope='any' requires listed_area_ids to be empty")
    if scope == "one" and len(ids) != 1:
        raise ScopeInconsistent(
            "area_scope='one' requires exactly one id in listed_area_ids"
        )
    if scope == "listed" and not ids:
        raise ScopeInconsistent(
            "area_scope='listed' requires a non-empty listed_area_ids"
        )
    if scope == "derived" and ids:
        # ``derived`` pulls the area id from the stay at generation
        # time — any template-level list is dead weight at best, a
        # contradiction at worst. Reject.
        raise ScopeInconsistent(
            "area_scope='derived' requires listed_area_ids to be empty"
        )
    if len(set(ids)) != len(ids):
        raise ScopeInconsistent("listed_area_ids must not contain duplicates")


def _assert_checklist_keys_unique(items: Sequence[ChecklistTemplateItem]) -> None:
    """Reject duplicate ``key`` entries within a single template.

    The spec pins ``key`` as a stable per-template slug used by
    downstream reports and asset-action linking; allowing two items
    to share a key would silently corrupt that pairing.
    """
    seen: set[str] = set()
    for item in items:
        if item.key in seen:
            raise ScopeInconsistent(
                f"duplicate checklist key {item.key!r} in checklist_template_json"
            )
        seen.add(item.key)


def validate_checklist_template(
    items: Sequence[ChecklistTemplateItem | dict[str, Any]],
) -> tuple[ChecklistTemplateItem, ...]:
    """Validate and return a typed checklist-template payload.

    This is the shared editor/generator contract: callers may pass
    already-parsed items from the DTO or raw JSON dictionaries loaded
    from storage. The function enforces per-item schema, RRULE
    parseability, and per-template key uniqueness.
    """
    normalised_items: list[ChecklistTemplateItem | dict[str, Any]] = []
    for item in items:
        if isinstance(item, ChecklistTemplateItem):
            normalised_items.append(item)
            continue
        rrule = item.get("rrule")
        if isinstance(rrule, str):
            item = {**item, "rrule": _normalise_rrule(rrule)}
        normalised_items.append(item)
    parsed = tuple(
        item
        if isinstance(item, ChecklistTemplateItem)
        else ChecklistTemplateItem.model_validate(item)
        for item in normalised_items
    )
    _assert_checklist_keys_unique(parsed)
    return parsed


def expand_checklist_for_task(
    items: Sequence[ChecklistTemplateItem | dict[str, Any]],
    *,
    scheduled_for_local: date | datetime,
    is_ad_hoc: bool,
    dtstart_local: date | datetime | None = None,
) -> tuple[ChecklistTemplateItem, ...]:
    """Return the checklist items that should seed a generated task.

    Ad-hoc tasks include every item because they have no stable
    calendar anchor. Scheduled and stay-generated tasks evaluate each
    optional RRULE against ``scheduled_for_local.date()`` per §06. When
    an item omits its own ``dtstart_local``, callers can pass the
    resolved schedule / stay / template anchor via ``dtstart_local``.
    """
    parsed = validate_checklist_template(items)
    if is_ad_hoc:
        return parsed
    scheduled_date = (
        scheduled_for_local.date()
        if isinstance(scheduled_for_local, datetime)
        else scheduled_for_local
    )
    fallback_anchor = (
        dtstart_local.date() if isinstance(dtstart_local, datetime) else dtstart_local
    )
    return tuple(
        item
        for item in parsed
        if _checklist_item_applies(
            item,
            scheduled_date,
            dtstart_local=fallback_anchor,
        )
    )


def reorder_checklist(
    items: Sequence[ChecklistTemplateItem | dict[str, Any]],
    ordered_keys: Sequence[str],
) -> tuple[ChecklistTemplateItem, ...]:
    """Return ``items`` in ``ordered_keys`` order, preserving each item by key."""
    parsed = validate_checklist_template(items)
    by_key = {item.key: item for item in parsed}
    expected = set(by_key)
    requested = set(ordered_keys)
    if len(requested) != len(ordered_keys):
        raise ScopeInconsistent("ordered_keys must not contain duplicates")
    if requested != expected:
        missing = ", ".join(sorted(expected - requested))
        unknown = ", ".join(sorted(requested - expected))
        detail = "; ".join(
            part
            for part in (
                f"missing keys: {missing}" if missing else "",
                f"unknown keys: {unknown}" if unknown else "",
            )
            if part
        )
        raise ScopeInconsistent(f"ordered_keys must match checklist keys ({detail})")
    return tuple(by_key[key] for key in ordered_keys)


def _checklist_item_applies(
    item: ChecklistTemplateItem,
    scheduled_date: date,
    *,
    dtstart_local: date | None,
) -> bool:
    if item.rrule is None:
        return True
    anchor = item.dtstart_local or dtstart_local or scheduled_date
    rule = rrulestr(
        item.rrule,
        dtstart=datetime.combine(anchor, time.min),
    )
    window_start = datetime.combine(scheduled_date, time.min)
    window_end = window_start + timedelta(days=1)
    return bool(rule.between(window_start, window_end, inc=True))


def _normalise_rrule(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ChecklistRRULEError(value)
    try:
        parsed = rrulestr(raw, dtstart=datetime(2000, 1, 1))
    except (TypeError, ValueError) as exc:
        raise ChecklistRRULEError(value) from exc
    for line in str(parsed).splitlines():
        if line.startswith("RRULE:"):
            return line.removeprefix("RRULE:")
    raise ChecklistRRULEError(value)


def _assert_inventory_positive(payload: dict[str, int]) -> None:
    """Each inventory consumption entry must be a positive integer."""
    for sku, qty in payload.items():
        if qty <= 0:
            raise ScopeInconsistent(
                f"inventory_consumption_json[{sku!r}]={qty} must be a positive integer"
            )


# ---------------------------------------------------------------------------
# Row ↔ view projection
# ---------------------------------------------------------------------------


def _row_to_view(row: TaskTemplate) -> TaskTemplateView:
    """Project a loaded :class:`TaskTemplate` row into a read view.

    Falls back to the legacy cd-chd columns (``title``,
    ``default_duration_min``) when the new columns are ``NULL`` on
    a pre-cd-0tg row. New writes through the service always fill
    both names, so this fallback only matters for rows created
    before the migration.
    """
    # Pydantic parses the list-of-dicts payload back into the
    # typed model so downstream readers can still rely on the same
    # schema they would post.
    raw_checklist = row.checklist_template_json or []
    parsed_checklist = tuple(
        ChecklistTemplateItem.model_validate(item) for item in raw_checklist
    )
    # ``property_scope`` / ``area_scope`` / ``photo_evidence`` /
    # ``priority`` columns are NOT NULL at the DB layer (CHECK-gated
    # enum strings). Narrow to the matching ``Literal`` via the
    # per-value ``_narrow_*`` helpers below so mypy sees a proper
    # narrow without a ``cast`` or ``# type: ignore``.
    return TaskTemplateView(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name if row.name is not None else row.title,
        description_md=row.description_md,
        role_id=row.role_id,
        duration_minutes=(
            row.duration_minutes
            if row.duration_minutes is not None
            else row.default_duration_min
        ),
        property_scope=_narrow_property_scope(row.property_scope),
        listed_property_ids=tuple(row.listed_property_ids or []),
        area_scope=_narrow_area_scope(row.area_scope),
        listed_area_ids=tuple(row.listed_area_ids or []),
        checklist_template_json=parsed_checklist,
        photo_evidence=_narrow_photo_evidence(row.photo_evidence),
        linked_instruction_ids=tuple(row.linked_instruction_ids or []),
        priority=_narrow_priority(row.priority),
        inventory_consumption_json=_consumption_from_effects(row.inventory_effects_json),
        llm_hints_md=row.llm_hints_md,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


def _narrow_property_scope(value: str) -> PropertyScope:
    """Narrow a loaded DB string to the :data:`PropertyScope` literal.

    The DB CHECK gate already rejects anything else; this helper
    exists purely to satisfy mypy's strict-Literal reading without
    a ``cast``. Silent downgrade to ``any`` on an unexpected value
    would hide a schema drift, so we raise instead. The explicit
    per-value returns are what let mypy narrow ``str`` to the
    :class:`Literal` without a ``# type: ignore``.
    """
    if value == "any":
        return "any"
    if value == "one":
        return "one"
    if value == "listed":
        return "listed"
    raise ValueError(f"unknown property_scope {value!r} on loaded row")


def _narrow_area_scope(value: str) -> AreaScope:
    """Narrow a loaded DB string to the :data:`AreaScope` literal."""
    if value == "any":
        return "any"
    if value == "one":
        return "one"
    if value == "listed":
        return "listed"
    if value == "derived":
        return "derived"
    raise ValueError(f"unknown area_scope {value!r} on loaded row")


def _narrow_photo_evidence(value: str) -> PhotoEvidence:
    """Narrow a loaded DB string to the :data:`PhotoEvidence` literal."""
    if value == "disabled":
        return "disabled"
    if value == "optional":
        return "optional"
    if value == "required":
        return "required"
    raise ValueError(f"unknown photo_evidence {value!r} on loaded row")


def _narrow_priority(value: str) -> Priority:
    """Narrow a loaded DB string to the :data:`Priority` literal."""
    if value == "low":
        return "low"
    if value == "normal":
        return "normal"
    if value == "high":
        return "high"
    if value == "urgent":
        return "urgent"
    raise ValueError(f"unknown priority {value!r} on loaded row")


def _view_to_diff_dict(view: TaskTemplateView) -> dict[str, Any]:
    """Flatten a :class:`TaskTemplateView` into a JSON-safe dict.

    Audit rows expect JSON-compatible payloads; the view carries
    a ``datetime`` on ``created_at`` / ``deleted_at`` that we
    stringify here. The checklist payload is re-serialised via
    pydantic's ``model_dump`` so the audit log mirrors the shape
    the client originally posted.
    """
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "name": view.name,
        "description_md": view.description_md,
        "role_id": view.role_id,
        "duration_minutes": view.duration_minutes,
        "property_scope": view.property_scope,
        "listed_property_ids": list(view.listed_property_ids),
        "area_scope": view.area_scope,
        "listed_area_ids": list(view.listed_area_ids),
        "checklist_template_json": [
            item.model_dump(mode="json") for item in view.checklist_template_json
        ],
        "photo_evidence": view.photo_evidence,
        "linked_instruction_ids": list(view.linked_instruction_ids),
        "priority": view.priority,
        "inventory_consumption_json": dict(view.inventory_consumption_json),
        "llm_hints_md": view.llm_hints_md,
        "created_at": view.created_at.isoformat(),
        "deleted_at": (
            view.deleted_at.isoformat() if view.deleted_at is not None else None
        ),
    }


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    template_id: str,
    include_deleted: bool = False,
) -> TaskTemplate:
    """Load ``template_id`` scoped to the caller's workspace.

    The ORM tenant filter already constrains SELECTs to
    ``ctx.workspace_id``; the explicit predicate below is defence-
    in-depth against a misconfigured context (matches the
    convention on :mod:`app.domain.identity.role_grants`).

    Soft-deleted rows are excluded unless ``include_deleted`` is
    set — the normal caller-facing surface never sees them. The
    flag exists for the admin / audit path (not wired yet; cd-sn26).
    """
    stmt = select(TaskTemplate).where(
        TaskTemplate.id == template_id,
        TaskTemplate.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(TaskTemplate.deleted_at.is_(None))
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise TaskTemplateNotFound(template_id)
    return row


def _apply_body(row: TaskTemplate, body: _TaskTemplateBody) -> None:
    """Copy every mutable DTO field onto ``row``.

    Writes through to both the new (``name``, ``duration_minutes``)
    and legacy (``title``, ``default_duration_min``) columns so the
    row stays readable by the cd-chd slice's tests / adapters until
    a follow-up migration drops the legacy pair. The CHECK-gated
    legacy columns (``required_evidence``, ``photo_required``) also
    mirror ``photo_evidence`` so their NOT NULL constraints hold
    on INSERT.
    """
    row.name = body.name
    row.title = body.name
    row.description_md = body.description_md
    row.role_id = body.role_id
    row.duration_minutes = body.duration_minutes
    row.default_duration_min = body.duration_minutes
    row.property_scope = body.property_scope
    row.listed_property_ids = list(body.listed_property_ids)
    row.area_scope = body.area_scope
    row.listed_area_ids = list(body.listed_area_ids)
    row.checklist_template_json = [
        item.model_dump(mode="json") for item in body.checklist_template_json
    ]
    row.photo_evidence = body.photo_evidence
    row.linked_instruction_ids = list(body.linked_instruction_ids)
    row.priority = body.priority
    row.inventory_effects_json = _effects_from_consumption(
        body.inventory_consumption_json
    )
    row.llm_hints_md = body.llm_hints_md
    # Mirror the photo-evidence policy onto the legacy columns so the
    # cd-chd CHECK constraints stay green on INSERT. ``required``
    # lands as ``photo`` (the only non-``none`` enum value the
    # legacy column models); ``optional`` / ``disabled`` map to
    # ``none`` since the legacy column is boolean-ish and can't
    # represent a three-value policy on its own.
    row.required_evidence = "photo" if body.photo_evidence == "required" else "none"
    row.photo_required = body.photo_evidence == "required"
    # ``default_assignee_role`` is a four-value legacy enum and has
    # no clean mapping from ``role_id``; leave it ``NULL`` on inserts
    # — the column is nullable at the DB layer and cd-chd's
    # integration tests already cover the NULL path.
    row.default_assignee_role = None


def _effects_from_consumption(payload: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {"item_ref": item_ref, "kind": "consume", "qty": qty}
        for item_ref, qty in payload.items()
    ]


def _consumption_from_effects(payload: Sequence[Any]) -> dict[str, int]:
    consumption: dict[str, int] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != "consume":
            continue
        item_ref = entry.get("item_ref")
        qty = entry.get("qty")
        if isinstance(item_ref, str) and isinstance(qty, int):
            consumption[item_ref] = qty
    return consumption


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read(
    session: Session,
    ctx: WorkspaceContext,
    *,
    template_id: str,
    include_deleted: bool = False,
) -> TaskTemplateView:
    """Return the live template identified by ``template_id``.

    Raises :class:`TaskTemplateNotFound` if the id is unknown in
    the caller's workspace (or is soft-deleted and
    ``include_deleted`` is false — the default).
    """
    row = _load_row(
        session, ctx, template_id=template_id, include_deleted=include_deleted
    )
    return _row_to_view(row)


def read_many(
    session: Session,
    ctx: WorkspaceContext,
    *,
    template_ids: Sequence[str],
    include_deleted: bool = False,
) -> list[TaskTemplateView]:
    """Return live templates for every id in ``template_ids`` in one query.

    Used by collection endpoints that piggy-back referenced templates
    onto the response (e.g. ``GET /schedules`` returns the schedules
    plus a ``templates_by_id`` sidecar — see ``docs/specs/12-rest-api.md``
    §"Tasks / templates / schedules"). Unknown ids and soft-deleted rows
    are silently skipped (the sidecar is best-effort: a stale schedule
    row referencing a now-deleted template should still render, with
    the UI falling back to ``"—"``). Cross-tenant ids are filtered out
    by the workspace predicate. Returns an empty list when
    ``template_ids`` is empty.
    """
    unique_ids = list(set(template_ids))
    if not unique_ids:
        return []
    stmt = select(TaskTemplate).where(
        TaskTemplate.id.in_(unique_ids),
        TaskTemplate.workspace_id == ctx.workspace_id,
    )
    if not include_deleted:
        stmt = stmt.where(TaskTemplate.deleted_at.is_(None))
    rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


def list_templates(
    session: Session,
    ctx: WorkspaceContext,
    *,
    q: str | None = None,
    role_id: str | None = None,
    deleted: bool = False,
) -> Sequence[TaskTemplateView]:
    """Return every template in the caller's workspace, optionally filtered.

    Ordered by ``created_at`` ascending with ``id`` as a stable
    tiebreaker inside the same millisecond. Filter semantics:

    * ``q`` — case-insensitive substring match against ``name`` and
      ``description_md``. Matches either; callers who need AND
      semantics can pass a narrower string.
    * ``role_id`` — strict equality on the ``role_id`` column.
      ``None`` returns every row regardless of role.
    * ``deleted`` — ``False`` (the default) returns only live rows;
      ``True`` returns only soft-deleted rows. There is
      intentionally no "both" mode — mixing active + retired rows
      in one list screen is an anti-pattern (the manager view
      should pick one).
    """
    stmt = select(TaskTemplate).where(TaskTemplate.workspace_id == ctx.workspace_id)
    if deleted:
        stmt = stmt.where(TaskTemplate.deleted_at.is_not(None))
    else:
        stmt = stmt.where(TaskTemplate.deleted_at.is_(None))
    if role_id is not None:
        stmt = stmt.where(TaskTemplate.role_id == role_id)
    if q is not None and q.strip():
        # Case-insensitivity relies on SQL's ``LOWER`` rather than a
        # dialect-specific ``ILIKE`` so the query stays portable
        # between SQLite and PG. Match against the new ``name``
        # column AND the description; the two ``lower()`` calls
        # stay inline because extracting the helper would force an
        # ``Any``-typed wrapper at the domain layer (SQLAlchemy's
        # ``func.lower`` column signature is untyped at that seam).
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(TaskTemplate.name).like(needle),
                func.lower(TaskTemplate.description_md).like(needle),
            )
        )
    stmt = stmt.order_by(TaskTemplate.created_at.asc(), TaskTemplate.id.asc())
    rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: TaskTemplateCreate,
    clock: Clock | None = None,
) -> TaskTemplateView:
    """Insert a fresh template row and record one audit entry.

    The DTO's ``model_validator`` has already enforced every
    scope-consistency rule; this function trusts the shape and
    maps it onto the ORM row. Returns the full :class:`TaskTemplateView`
    so the router can echo it back to the client without a second
    SELECT.
    """
    now = (clock if clock is not None else SystemClock()).now()
    row = TaskTemplate(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        created_at=now,
        # ``_apply_body`` fills the rest; we seed only the immutable
        # per-row fields here.
    )
    _apply_body(row, body)
    session.add(row)
    session.flush()

    view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="task_template",
        entity_id=row.id,
        action="create",
        diff={"after": _view_to_diff_dict(view)},
        clock=clock,
    )
    return view


def update(
    session: Session,
    ctx: WorkspaceContext,
    *,
    template_id: str,
    body: TaskTemplateUpdate,
    clock: Clock | None = None,
) -> TaskTemplateView:
    """Replace the mutable body of ``template_id``.

    Raises :class:`TaskTemplateNotFound` when the id is unknown or
    already soft-deleted. Records one audit row with the full
    before/after diff so operators can reconstruct the change.
    """
    row = _load_row(session, ctx, template_id=template_id)
    before = _row_to_view(row)
    _apply_body(row, body)
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="task_template",
        entity_id=row.id,
        action="update",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=clock,
    )
    return after


def delete(
    session: Session,
    ctx: WorkspaceContext,
    *,
    template_id: str,
    clock: Clock | None = None,
) -> TaskTemplateView:
    """Soft-delete ``template_id`` and record one audit entry.

    Raises:

    * :class:`TaskTemplateNotFound` — the id is unknown in the
      caller's workspace.
    * :class:`TemplateInUseError` — the template is referenced by
      at least one live ``schedule.template_id`` (or, once cd-4qr
      lands, a live ``stay_lifecycle_rule.template_id``). The
      exception carries the offending ids.
    """
    row = _load_row(session, ctx, template_id=template_id)

    schedule_ids = _active_schedule_ids(session, ctx, template_id=row.id)
    stay_lifecycle_ids = _active_stay_lifecycle_rule_ids(
        session, ctx, template_id=row.id
    )
    if schedule_ids or stay_lifecycle_ids:
        raise TemplateInUseError(
            template_id=row.id,
            schedule_ids=schedule_ids,
            stay_lifecycle_rule_ids=stay_lifecycle_ids,
        )

    now = (clock if clock is not None else SystemClock()).now()
    before = _row_to_view(row)
    row.deleted_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="task_template",
        entity_id=row.id,
        action="delete",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=clock,
    )
    return after


def _active_schedule_ids(
    session: Session, ctx: WorkspaceContext, *, template_id: str
) -> tuple[str, ...]:
    """Return every live ``schedule.id`` referencing ``template_id``.

    Soft-deleted schedules (``deleted_at IS NOT NULL``, added by
    cd-k4l) no longer block a template's soft-delete — a retired
    schedule is not a live consumer. The ordering keeps the error
    payload deterministic for the UI rendering
    "In use by 2 schedules: 01HWA…, 01HWA…".
    """
    stmt = (
        select(Schedule.id)
        .where(
            Schedule.workspace_id == ctx.workspace_id,
            Schedule.template_id == template_id,
            Schedule.deleted_at.is_(None),
        )
        .order_by(Schedule.id.asc())
    )
    return tuple(session.scalars(stmt).all())


def _active_stay_lifecycle_rule_ids(
    session: Session,
    ctx: WorkspaceContext,
    *,
    template_id: str,
) -> tuple[str, ...]:
    """Return every active ``stay_lifecycle_rule.id`` referencing ``template_id``.

    The ``stay_lifecycle_rule`` table lands with cd-4qr; until then
    the function returns an empty tuple. The hook exists so the
    :class:`TemplateInUseError` shape stays stable across that
    migration — when cd-4qr adds the table, only this function
    needs an implementation change.
    """
    # Unused arguments kept in the signature so the contract stays
    # stable once cd-4qr lands — flagging them to the linter.
    _ = session, ctx, template_id
    return ()
