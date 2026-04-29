"""Natural-language task intake capability.

The public entry points are :func:`draft` and :func:`commit`.
``draft`` asks the configured ``tasks.nl_intake`` model for structured
JSON, runs a deterministic resolver over property, assignee, and
evidence mentions, then persists the preview for 24 hours. ``commit``
loads that preview, applies any caller edits, and creates a task
template plus schedule through the existing task-domain services.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.tasks.models import NlTaskPreview
from app.adapters.db.workspace.models import UserWorkspace
from app.adapters.llm.ports import LLMClient, LLMResponse
from app.audit import write_audit
from app.domain.llm.budget import (
    PricingTable,
    check_budget,
    default_pricing_table,
    estimate_cost_cents,
)
from app.domain.llm.router import CapabilityUnassignedError, ModelPick, resolve_model
from app.domain.llm.usage_recorder import AgentAttribution, record
from app.domain.tasks.schedules import ScheduleCreate, ScheduleView
from app.domain.tasks.schedules import create as create_schedule
from app.domain.tasks.templates import TaskTemplateCreate, TaskTemplateView
from app.domain.tasks.templates import create as create_template
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "TASKS_INTAKE_CAPABILITY",
    "Ambiguity",
    "Candidate",
    "NlCommitEdits",
    "NlIntakeContext",
    "NlPreview",
    "NlPreviewExpired",
    "NlPreviewNotFound",
    "NlPreviewUnresolved",
    "ResolvedSchedule",
    "ResolvedTask",
    "ResolvedTemplate",
    "ScheduledTask",
    "TaskIntakeParseError",
    "commit",
    "draft",
]


TASKS_INTAKE_CAPABILITY: Final[str] = "tasks.nl_intake"
_PROJECTED_PROMPT_TOKENS: Final[int] = 2048
_PROJECTED_COMPLETION_TOKENS: Final[int] = 768
_PREVIEW_TTL: Final[timedelta] = timedelta(hours=24)

_PROMPT: Final[str] = (
    "Return only JSON for this task instruction. Shape: "
    '{"property_name": string|null, "assignee_name": string|null, '
    '"template": {"name": string, "description_md": string|null, '
    '"duration_minutes": integer|null, "photo_evidence": '
    '"disabled|optional|required|null", "priority": '
    '"low|normal|high|urgent|null"}, '
    '"schedule": {"rrule": string, "dtstart_local": '
    '"YYYY-MM-DDTHH:MM", "active_from": "YYYY-MM-DD"|null, '
    '"active_until": "YYYY-MM-DD"|null}, '
    '"evidence": [string], "assumptions": [string]}. '
    "Do not invent property ids or user ids."
)

PhotoEvidence = Literal["disabled", "optional", "required"]
Priority = Literal["low", "normal", "high", "urgent"]


class TaskIntakeParseError(ValueError):
    """The LLM output was not a usable task-intake draft."""


class NlPreviewNotFound(LookupError):
    """The preview id does not exist in the caller's workspace."""


class NlPreviewExpired(LookupError):
    """The preview exists but its 24-hour confirmation window elapsed."""


class NlPreviewUnresolved(ValueError):
    """Commit was attempted while required fields were still ambiguous."""

    def __init__(self, ambiguities: list[Ambiguity]) -> None:
        self.ambiguities = tuple(ambiguities)
        super().__init__("nl task preview has unresolved ambiguities")


class Candidate(BaseModel):
    """Concrete row the deterministic resolver can offer to the caller."""

    model_config = ConfigDict(frozen=True)

    id: str
    label: str


class Ambiguity(BaseModel):
    """A named entity could not be resolved to exactly one row."""

    model_config = ConfigDict(frozen=True)

    field: Literal["property", "assignee", "evidence"]
    text: str | None = None
    candidates: list[Candidate] = Field(default_factory=list)


class ResolvedTemplate(BaseModel):
    """Template fields resolved from the free-text instruction."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description_md: str = Field(default="", max_length=20_000)
    duration_minutes: int = Field(default=30, ge=1, le=24 * 60)
    photo_evidence: PhotoEvidence = "disabled"
    priority: Priority = "normal"

    @field_validator("description_md", mode="before")
    @classmethod
    def _description_none_to_blank(cls, value: object) -> object:
        return "" if value is None else value


class ResolvedSchedule(BaseModel):
    """Schedule fields resolved from the free-text instruction."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    rrule: str = Field(min_length=1, max_length=8_000)
    dtstart_local: str = Field(min_length=1, max_length=32)
    active_from: date | None = None
    active_until: date | None = None


class ResolvedTask(BaseModel):
    """Resolved preview body stored in ``nl_task_preview``."""

    model_config = ConfigDict(extra="forbid")

    property_id: str | None = None
    assigned_user_id: str | None = None
    template: ResolvedTemplate
    schedule: ResolvedSchedule


class NlPreview(BaseModel):
    """Dry-run response for a natural-language task instruction."""

    model_config = ConfigDict(frozen=True)

    preview_id: str
    resolved: ResolvedTask
    assumptions: list[str]
    ambiguities: list[Ambiguity]
    expires_at: datetime


class NlCommitEdits(BaseModel):
    """Optional caller edits applied at preview confirmation time."""

    model_config = ConfigDict(extra="forbid")

    resolved: ResolvedTask | None = None
    assumptions: list[str] | None = None
    ambiguities: list[Ambiguity] | None = None


@dataclass(frozen=True, slots=True)
class ScheduledTask:
    """Rows created by committing an NL preview."""

    template: TaskTemplateView
    schedule: ScheduleView


@dataclass(frozen=True, slots=True)
class NlIntakeContext:
    """Dependencies for one NL task-intake call."""

    session: Session
    workspace_ctx: WorkspaceContext
    llm: LLMClient | None
    pricing: PricingTable | None = None
    attribution: AgentAttribution | None = None
    clock: Clock | None = None


class _ModelDraft(BaseModel):
    model_config = ConfigDict(extra="allow")

    property_name: str | None = None
    assignee_name: str | None = None
    template: ResolvedTemplate
    schedule: ResolvedSchedule
    evidence: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)

    @field_validator("property_name", "assignee_name")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


def draft(ctx: NlIntakeContext, text: str) -> NlPreview:
    """Create and persist a 24-hour NL task preview.

    Budget and capability-assignment errors deliberately propagate before any
    preview row is written, matching the receipt OCR capability's refusal
    semantics.
    """
    if not text.strip():
        raise TaskIntakeParseError("text is empty; cannot draft a task")

    clock = ctx.clock if ctx.clock is not None else SystemClock()
    pricing = ctx.pricing if ctx.pricing is not None else default_pricing_table()
    attribution = ctx.attribution or AgentAttribution(
        actor_user_id=ctx.workspace_ctx.actor_id,
        token_id=None,
        agent_label="tasks-nl-intake",
    )

    model_chain = resolve_model(
        ctx.session,
        ctx.workspace_ctx,
        TASKS_INTAKE_CAPABILITY,
        clock=clock,
    )
    if not model_chain:
        raise CapabilityUnassignedError(
            TASKS_INTAKE_CAPABILITY, ctx.workspace_ctx.workspace_id
        )
    if ctx.llm is None:
        raise CapabilityUnassignedError(
            TASKS_INTAKE_CAPABILITY, ctx.workspace_ctx.workspace_id
        )

    correlation_id = new_ulid(clock=clock)
    last_parse_error: TaskIntakeParseError | None = None
    for attempt, model_pick in enumerate(model_chain):
        _check_budget(ctx, model_pick=model_pick, pricing=pricing, clock=clock)

        started = clock.now()
        response = ctx.llm.chat(
            model_id=model_pick.api_model_id,
            messages=[{"role": "user", "content": f"{_PROMPT}\n\n{text}"}],
            max_tokens=model_pick.max_tokens or _PROJECTED_COMPLETION_TOKENS,
            temperature=(
                model_pick.temperature if model_pick.temperature is not None else 0.0
            ),
        )
        latency_ms = max(0, int((clock.now() - started).total_seconds() * 1000))
        _record_usage(
            ctx,
            model_pick=model_pick,
            response=response,
            correlation_id=correlation_id,
            latency_ms=latency_ms,
            pricing=pricing,
            clock=clock,
            attribution=attribution,
            fallback_attempts=attempt,
            attempt=attempt,
        )

        try:
            parsed = _parse_model_response(response.text)
        except TaskIntakeParseError as exc:
            last_parse_error = exc
            continue

        preview = _build_preview(ctx, text=text, parsed=parsed, clock=clock)
        _persist_preview(ctx, preview=preview, original_text=text, clock=clock)
        return preview

    if last_parse_error is not None:
        raise last_parse_error
    raise TaskIntakeParseError("LLM output was not a usable task draft")


def commit(
    ctx: NlIntakeContext,
    preview_id: str,
    edits: NlCommitEdits | None = None,
) -> ScheduledTask:
    """Commit a stored preview into a template + schedule."""
    clock = ctx.clock if ctx.clock is not None else SystemClock()
    row = _load_preview_row(ctx, preview_id=preview_id, clock=clock)
    stored = _preview_from_row(row)
    body = (
        edits.resolved if edits is not None and edits.resolved is not None else stored
    )
    if edits is not None and edits.ambiguities is not None:
        ambiguities = edits.ambiguities
    elif edits is not None and edits.resolved is not None:
        ambiguities = []
    else:
        ambiguities = _ambiguities_from_row(row)
    blocking = _commit_blockers(body, ambiguities)
    if blocking:
        raise NlPreviewUnresolved(blocking)

    template = create_template(
        ctx.session,
        ctx.workspace_ctx,
        body=TaskTemplateCreate(
            name=body.template.name,
            description_md=body.template.description_md,
            duration_minutes=body.template.duration_minutes,
            property_scope="one" if body.property_id is not None else "any",
            listed_property_ids=(
                [body.property_id] if body.property_id is not None else []
            ),
            area_scope="any",
            listed_area_ids=[],
            checklist_template_json=[],
            photo_evidence=body.template.photo_evidence,
            linked_instruction_ids=[],
            priority=body.template.priority,
            inventory_consumption_json={},
            llm_hints_md=None,
        ),
        clock=clock,
    )
    active_from = body.schedule.active_from or _date_from_local(
        body.schedule.dtstart_local
    )
    schedule = create_schedule(
        ctx.session,
        ctx.workspace_ctx,
        body=ScheduleCreate(
            name=body.schedule.name or body.template.name,
            template_id=template.id,
            property_id=body.property_id,
            area_id=None,
            default_assignee=body.assigned_user_id,
            backup_assignee_user_ids=[],
            rrule=body.schedule.rrule,
            dtstart_local=body.schedule.dtstart_local,
            duration_minutes=body.template.duration_minutes,
            rdate_local="",
            exdate_local="",
            active_from=active_from,
            active_until=body.schedule.active_until,
        ),
        clock=clock,
    )
    row.committed_at = clock.now()
    _write_commit_audit(
        ctx,
        preview_id=preview_id,
        template=template,
        schedule=schedule,
        edited=edits is not None and edits.resolved is not None,
        clock=clock,
    )
    ctx.session.flush()
    return ScheduledTask(template=template, schedule=schedule)


def _check_budget(
    ctx: NlIntakeContext,
    *,
    model_pick: ModelPick,
    pricing: PricingTable,
    clock: Clock,
) -> None:
    projected_cost = estimate_cost_cents(
        prompt_tokens=_PROJECTED_PROMPT_TOKENS,
        max_output_tokens=_PROJECTED_COMPLETION_TOKENS,
        api_model_id=model_pick.api_model_id,
        pricing=pricing,
        workspace_id=ctx.workspace_ctx.workspace_id,
    )
    check_budget(
        ctx.session,
        ctx.workspace_ctx,
        capability=TASKS_INTAKE_CAPABILITY,
        projected_cost_cents=projected_cost,
        clock=clock,
    )


def _record_usage(
    ctx: NlIntakeContext,
    *,
    model_pick: ModelPick,
    response: LLMResponse,
    correlation_id: str,
    latency_ms: int,
    pricing: PricingTable,
    clock: Clock,
    attribution: AgentAttribution,
    fallback_attempts: int,
    attempt: int,
) -> None:
    cost_cents = estimate_cost_cents(
        prompt_tokens=response.usage.prompt_tokens,
        max_output_tokens=response.usage.completion_tokens,
        api_model_id=model_pick.api_model_id,
        pricing=pricing,
        workspace_id=ctx.workspace_ctx.workspace_id,
    )
    record(
        ctx.session,
        ctx.workspace_ctx,
        capability=TASKS_INTAKE_CAPABILITY,
        model_pick=model_pick,
        fallback_attempts=fallback_attempts,
        correlation_id=correlation_id,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        cost_cents=cost_cents,
        latency_ms=latency_ms,
        status="ok",
        finish_reason=response.finish_reason,
        attribution=attribution,
        attempt=attempt,
        clock=clock,
    )


def _parse_model_response(text: str) -> _ModelDraft:
    raw = _load_json_object(text)
    try:
        return _ModelDraft.model_validate(raw)
    except ValidationError as exc:
        raise TaskIntakeParseError(
            f"LLM output failed schema validation: {exc}"
        ) from exc


def _load_json_object(text: str) -> dict[str, object]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskIntakeParseError("LLM output was not valid JSON") from exc
    if not isinstance(raw, dict):
        raise TaskIntakeParseError("LLM output must be a JSON object")
    return raw


def _build_preview(
    ctx: NlIntakeContext,
    *,
    text: str,
    parsed: _ModelDraft,
    clock: Clock,
) -> NlPreview:
    assumptions = [item for item in parsed.assumptions if item.strip()]
    ambiguities: list[Ambiguity] = []

    property_id, property_notes, property_ambiguity = _resolve_property(
        ctx, parsed.property_name
    )
    assumptions.extend(property_notes)
    if property_ambiguity is not None:
        ambiguities.append(property_ambiguity)

    assignee_id, assignee_notes, assignee_ambiguity = _resolve_assignee(
        ctx, parsed.assignee_name
    )
    assumptions.extend(assignee_notes)
    if assignee_ambiguity is not None:
        ambiguities.append(assignee_ambiguity)

    template, evidence_notes = _template_with_evidence(
        parsed.template, parsed.evidence, ambiguities
    )
    assumptions.extend(evidence_notes)
    preview_id = f"nlp_{new_ulid(clock=clock)}"
    return NlPreview(
        preview_id=preview_id,
        resolved=ResolvedTask(
            property_id=property_id,
            assigned_user_id=assignee_id,
            template=template,
            schedule=parsed.schedule,
        ),
        assumptions=_dedupe_preserving_order(assumptions),
        ambiguities=ambiguities,
        expires_at=clock.now() + _PREVIEW_TTL,
    )


def _persist_preview(
    ctx: NlIntakeContext,
    *,
    preview: NlPreview,
    original_text: str,
    clock: Clock,
) -> None:
    ctx.session.add(
        NlTaskPreview(
            id=preview.preview_id,
            workspace_id=ctx.workspace_ctx.workspace_id,
            requested_by_user_id=ctx.workspace_ctx.actor_id,
            original_text=original_text,
            resolved_json=preview.resolved.model_dump(mode="json"),
            assumptions_json=list(preview.assumptions),
            ambiguities_json=[
                ambiguity.model_dump(mode="json") for ambiguity in preview.ambiguities
            ],
            created_at=clock.now(),
            expires_at=preview.expires_at,
        )
    )
    ctx.session.flush()


def _resolve_property(
    ctx: NlIntakeContext, name: str | None
) -> tuple[str | None, list[str], Ambiguity | None]:
    rows = _property_candidates(ctx, name)
    if name is None:
        if len(rows) == 1:
            return rows[0].id, [f"Assumed {rows[0].label} (only property)."], None
        if len(rows) == 0:
            return None, [], None
        return None, [], Ambiguity(field="property", text=None, candidates=rows)
    if len(rows) == 1:
        return rows[0].id, [f"Assumed {rows[0].label} (only match)."], None
    return None, [], Ambiguity(field="property", text=name, candidates=rows)


def _property_candidates(ctx: NlIntakeContext, name: str | None) -> list[Candidate]:
    stmt = (
        select(Property.id, Property.name, PropertyWorkspace.label)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            PropertyWorkspace.workspace_id == ctx.workspace_ctx.workspace_id,
            PropertyWorkspace.status == "active",
            Property.deleted_at.is_(None),
        )
        .order_by(PropertyWorkspace.label.asc(), Property.id.asc())
    )
    if name is not None:
        needle = f"%{name.lower()}%"
        stmt = stmt.where(
            (func.lower(Property.name).like(needle))
            | (func.lower(PropertyWorkspace.label).like(needle))
            | (func.lower(Property.address).like(needle))
        )
    candidates = [
        Candidate(id=row_id, label=label or prop_name or row_id)
        for row_id, prop_name, label in ctx.session.execute(stmt).all()
    ]
    if name is None:
        return candidates
    exact = [
        candidate
        for candidate in candidates
        if candidate.label.strip().lower() == name.strip().lower()
    ]
    return exact if exact else candidates


def _resolve_assignee(
    ctx: NlIntakeContext, name: str | None
) -> tuple[str | None, list[str], Ambiguity | None]:
    rows = _assignee_candidates(ctx, name)
    if name is None:
        if len(rows) == 1:
            return (
                rows[0].id,
                [f"Assumed assignee {rows[0].label} (only member)."],
                None,
            )
        return None, [], None
    if len(rows) == 1:
        return rows[0].id, [f"Resolved {name} to {rows[0].id}."], None
    return None, [], Ambiguity(field="assignee", text=name, candidates=rows)


def _assignee_candidates(ctx: NlIntakeContext, name: str | None) -> list[Candidate]:
    stmt = (
        select(User.id, User.display_name, User.email)
        .join(UserWorkspace, UserWorkspace.user_id == User.id)
        .where(
            UserWorkspace.workspace_id == ctx.workspace_ctx.workspace_id,
            User.archived_at.is_(None),
        )
        .order_by(User.display_name.asc(), User.id.asc())
    )
    if name is not None:
        needle = f"%{name.lower()}%"
        stmt = stmt.where(
            (func.lower(User.display_name).like(needle))
            | (func.lower(User.email).like(needle))
        )
    candidates = [
        Candidate(id=row_id, label=display_name or email or row_id)
        for row_id, display_name, email in ctx.session.execute(stmt).all()
    ]
    if name is None:
        return candidates
    exact = [
        candidate
        for candidate in candidates
        if candidate.label.strip().lower() == name.strip().lower()
    ]
    return exact if exact else candidates


def _template_with_evidence(
    template: ResolvedTemplate,
    evidence: list[str],
    ambiguities: list[Ambiguity],
) -> tuple[ResolvedTemplate, list[str]]:
    resolved = template
    assumptions: list[str] = []
    for raw_kind in evidence:
        kind = raw_kind.strip().lower()
        if not kind:
            continue
        if kind in {"photo", "photo_evidence", "image"}:
            if resolved.photo_evidence != "required":
                resolved = resolved.model_copy(update={"photo_evidence": "required"})
            assumptions.append(
                "Photo evidence flagged because photo evidence was mentioned."
            )
            continue
        ambiguities.append(Ambiguity(field="evidence", text=raw_kind, candidates=[]))
    return resolved, assumptions


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out


def _load_preview_row(
    ctx: NlIntakeContext, *, preview_id: str, clock: Clock
) -> NlTaskPreview:
    row = ctx.session.scalars(
        select(NlTaskPreview).where(
            NlTaskPreview.id == preview_id,
            NlTaskPreview.workspace_id == ctx.workspace_ctx.workspace_id,
        )
    ).one_or_none()
    if row is None:
        raise NlPreviewNotFound(preview_id)
    if _as_aware_utc(row.expires_at) <= clock.now():
        raise NlPreviewExpired(preview_id)
    return row


def _preview_from_row(row: NlTaskPreview) -> ResolvedTask:
    try:
        return ResolvedTask.model_validate(row.resolved_json)
    except ValidationError as exc:
        raise TaskIntakeParseError("stored preview resolved_json is invalid") from exc


def _ambiguities_from_row(row: NlTaskPreview) -> list[Ambiguity]:
    try:
        return [Ambiguity.model_validate(item) for item in row.ambiguities_json]
    except ValidationError as exc:
        raise TaskIntakeParseError(
            "stored preview ambiguities_json is invalid"
        ) from exc


def _commit_blockers(
    resolved: ResolvedTask, ambiguities: list[Ambiguity]
) -> list[Ambiguity]:
    blockers = list(ambiguities)
    if resolved.property_id is None:
        blockers.append(Ambiguity(field="property", text=None, candidates=[]))
    if resolved.assigned_user_id is None:
        blockers.append(Ambiguity(field="assignee", text=None, candidates=[]))
    return blockers


def _date_from_local(value: str) -> date:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as exc:
        raise TaskIntakeParseError(
            f"dtstart_local must be an ISO-8601 local timestamp; got {value!r}"
        ) from exc


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _write_commit_audit(
    ctx: NlIntakeContext,
    *,
    preview_id: str,
    template: TaskTemplateView,
    schedule: ScheduleView,
    edited: bool,
    clock: Clock,
) -> None:
    diff: dict[str, object] = {
        "preview_id": preview_id,
        "template_id": template.id,
        "schedule_id": schedule.id,
        "edited": edited,
    }
    if ctx.attribution is not None:
        diff.update(_attribution_diff(ctx.attribution))
    write_audit(
        ctx.session,
        ctx.workspace_ctx,
        entity_kind="nl_task_preview",
        entity_id=preview_id,
        action="commit",
        diff=diff,
        via="api",
        clock=clock,
    )


def _attribution_diff(attribution: AgentAttribution) -> dict[str, str]:
    diff: dict[str, str] = {}
    if attribution.token_id is not None:
        diff["token_id"] = attribution.token_id
    if attribution.agent_label is not None:
        diff["agent_label"] = attribution.agent_label
    if attribution.agent_conversation_ref is not None:
        diff["agent_conversation_ref"] = attribution.agent_conversation_ref
    return diff
