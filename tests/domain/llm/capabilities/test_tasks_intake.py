"""Focused tests for ``tasks.nl_intake`` natural-language task intake."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.llm.models import BudgetLedger
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.tasks.models import NlTaskPreview, Schedule, TaskTemplate
from app.adapters.db.workspace.models import UserWorkspace
from app.adapters.llm.ports import ChatMessage, LLMResponse, LLMUsage
from app.domain.llm.budget import WINDOW_DAYS, BudgetExceeded
from app.domain.llm.capabilities.tasks_intake import (
    TASKS_INTAKE_CAPABILITY,
    NlCommitEdits,
    NlIntakeContext,
    NlPreviewExpired,
    ResolvedSchedule,
    ResolvedTask,
    ResolvedTemplate,
    commit,
    draft,
)
from app.domain.llm.usage_recorder import AgentAttribution
from app.tenancy import WorkspaceContext, registry, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.domain.llm.conftest import (
    seed_assignment,
    seed_workspace,
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


class StubLLM:
    def __init__(self, payload: dict[str, Any] | str) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def complete(
        self,
        *,
        model_id: str,
        prompt: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        raise NotImplementedError

    def ocr(self, *, model_id: str, image_bytes: bytes) -> str:
        raise NotImplementedError

    def chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append(("chat", model_id))
        text = (
            self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        )
        return LLMResponse(
            text=text,
            usage=LLMUsage(prompt_tokens=90, completion_tokens=45, total_tokens=135),
            model_id=model_id,
            finish_reason="stop",
        )

    def stream_chat(
        self,
        *,
        model_id: str,
        messages: Sequence[ChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> Iterator[str]:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _ensure_scoped_tables_registered() -> None:
    for table in (
        "property_workspace",
        "user_workspace",
        "task_template",
        "schedule",
        "nl_task_preview",
        "llm_usage",
        "budget_ledger",
        "audit_log",
    ):
        registry.register(table)


def _seed_ledger(
    session: Session,
    *,
    workspace_id: str,
    cap_cents: int = 500,
    spent_cents: int = 0,
) -> BudgetLedger:
    row = BudgetLedger(
        id=new_ulid(),
        workspace_id=workspace_id,
        period_start=_PINNED - timedelta(days=WINDOW_DAYS),
        period_end=_PINNED,
        spent_cents=spent_cents,
        cap_cents=cap_cents,
        updated_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def _seed_capability(session: Session, *, workspace_id: str) -> None:
    seed_assignment(
        session,
        workspace_id=workspace_id,
        capability=TASKS_INTAKE_CAPABILITY,
        model_id="01HWA00000000000000000TASK",
        api_model_id="fake/task-intake-model",
        required_capabilities=["json_mode"],
        max_tokens=768,
    )


def _seed_property(
    session: Session,
    *,
    workspace_id: str,
    name: str,
    label: str | None = None,
) -> Property:
    prop = Property(
        id=new_ulid(),
        name=name,
        kind="vacation",
        address=name,
        address_json={},
        country="FR",
        timezone="Europe/Paris",
        tags_json=[],
        welcome_defaults_json={},
        property_notes_md="",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    link = PropertyWorkspace(
        property_id=prop.id,
        workspace_id=workspace_id,
        label=label or name,
        membership_role="owner_workspace",
        share_guest_identity=False,
        status="active",
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add_all([prop, link])
        session.flush()
    return prop


def _seed_user(
    session: Session,
    *,
    workspace_id: str,
    display_name: str,
) -> User:
    email = f"{display_name.lower().replace(' ', '.')}@example.com"
    user = User(
        id=new_ulid(),
        email=email,
        email_lower=canonicalise_email(email),
        display_name=display_name,
        created_at=_PINNED,
    )
    membership = UserWorkspace(
        user_id=user.id,
        workspace_id=workspace_id,
        source="workspace_grant",
        added_at=_PINNED,
    )
    with tenant_agnostic():
        session.add_all([user, membership])
        session.flush()
    return user


def _ctx_for(ws_id: str, actor_id: str | None = None) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=ws_id,
        workspace_slug="tasks-nl",
        actor_id=actor_id or new_ulid(),
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


def _draft_payload(
    *,
    property_name: str | None = "Villa Sud",
    assignee_name: str | None = "Maria",
) -> dict[str, Any]:
    return {
        "property_name": property_name,
        "assignee_name": assignee_name,
        "template": {
            "name": "Deep-clean the guest bath",
            "description_md": "",
            "duration_minutes": 60,
            "photo_evidence": "disabled",
            "priority": "normal",
        },
        "schedule": {
            "rrule": "FREQ=WEEKLY;BYDAY=TU",
            "dtstart_local": "2026-04-21T09:00",
            "active_from": "2026-04-21",
            "active_until": None,
        },
        "evidence": ["photo"],
        "assumptions": [],
    }


def _make_context(
    session: Session,
    *,
    ctx: WorkspaceContext,
    llm: StubLLM,
    clock: FrozenClock,
) -> NlIntakeContext:
    return NlIntakeContext(
        session=session,
        workspace_ctx=ctx,
        llm=llm,
        pricing={"fake/task-intake-model": (0, 0)},
        clock=clock,
    )


def test_spec_sample_resolves_shape_and_assumptions(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    maria = _seed_user(db_session, workspace_id=ws.id, display_name="Maria")
    prop = _seed_property(db_session, workspace_id=ws.id, name="Villa Sud")
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = _ctx_for(ws.id, actor_id=maria.id)
    llm = StubLLM(payload=_draft_payload())

    token = set_current(ctx)
    try:
        preview = draft(
            _make_context(db_session, ctx=ctx, llm=llm, clock=clock),
            (
                "Have Maria deep-clean the guest bath every Tuesday 9am "
                "at Villa Sud, 1 hour, needs photo evidence"
            ),
        )
    finally:
        reset_current(token)

    assert preview.resolved.property_id == prop.id
    assert preview.resolved.assigned_user_id == maria.id
    assert preview.resolved.template.name == "Deep-clean the guest bath"
    assert preview.resolved.template.duration_minutes == 60
    assert preview.resolved.template.photo_evidence == "required"
    assert preview.resolved.schedule.rrule == "FREQ=WEEKLY;BYDAY=TU"
    assert preview.resolved.schedule.dtstart_local == "2026-04-21T09:00"
    assert preview.ambiguities == []
    assert preview.assumptions == [
        "Assumed Villa Sud (only match).",
        f"Resolved Maria to {maria.id}.",
        "Photo evidence flagged because photo evidence was mentioned.",
    ]
    assert llm.calls == [("chat", "fake/task-intake-model")]


def test_two_properties_matching_name_surface_ambiguity(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    maria = _seed_user(db_session, workspace_id=ws.id, display_name="Maria")
    _seed_property(db_session, workspace_id=ws.id, name="Villa Sud")
    _seed_property(db_session, workspace_id=ws.id, name="Villa Sud Annex")
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = _ctx_for(ws.id, actor_id=maria.id)
    llm = StubLLM(payload=_draft_payload(property_name="Villa"))

    token = set_current(ctx)
    try:
        preview = draft(_make_context(db_session, ctx=ctx, llm=llm, clock=clock), "x")
    finally:
        reset_current(token)

    assert preview.resolved.property_id is None
    assert len(preview.ambiguities) == 1
    assert preview.ambiguities[0].field == "property"
    assert [candidate.label for candidate in preview.ambiguities[0].candidates] == [
        "Villa Sud",
        "Villa Sud Annex",
    ]


def test_commit_unmodified_preview_creates_template_and_schedule(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    maria = _seed_user(db_session, workspace_id=ws.id, display_name="Maria")
    prop = _seed_property(db_session, workspace_id=ws.id, name="Villa Sud")
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = _ctx_for(ws.id, actor_id=maria.id)
    llm = StubLLM(payload=_draft_payload())

    token = set_current(ctx)
    try:
        preview = draft(_make_context(db_session, ctx=ctx, llm=llm, clock=clock), "x")
        scheduled = commit(
            NlIntakeContext(
                session=db_session,
                workspace_ctx=ctx,
                llm=None,
                pricing={"fake/task-intake-model": (0, 0)},
                clock=clock,
            ),
            preview.preview_id,
            None,
        )
        template_count = db_session.scalar(
            select(func.count()).select_from(TaskTemplate)
        )
        schedule_count = db_session.scalar(select(func.count()).select_from(Schedule))
    finally:
        reset_current(token)

    assert template_count == 1
    assert schedule_count == 1
    assert scheduled.template.name == "Deep-clean the guest bath"
    assert scheduled.template.listed_property_ids == (prop.id,)
    assert scheduled.schedule.template_id == scheduled.template.id
    assert scheduled.schedule.property_id == prop.id
    assert scheduled.schedule.default_assignee == maria.id
    assert scheduled.schedule.rrule == "FREQ=WEEKLY;BYDAY=TU"


def test_commit_after_edits_applies_resolved_patch(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    maria = _seed_user(db_session, workspace_id=ws.id, display_name="Maria")
    prop = _seed_property(db_session, workspace_id=ws.id, name="Villa Sud")
    edited_prop = _seed_property(db_session, workspace_id=ws.id, name="Villa Nord")
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = _ctx_for(ws.id, actor_id=maria.id)
    llm = StubLLM(payload=_draft_payload(property_name="Villa"))

    token = set_current(ctx)
    try:
        preview = draft(_make_context(db_session, ctx=ctx, llm=llm, clock=clock), "x")
        edited = ResolvedTask(
            property_id=edited_prop.id,
            assigned_user_id=maria.id,
            template=ResolvedTemplate(
                name="Guest bath deep clean",
                duration_minutes=45,
                photo_evidence="required",
            ),
            schedule=ResolvedSchedule(
                name="Edited weekly clean",
                rrule="FREQ=WEEKLY;BYDAY=WE",
                dtstart_local="2026-04-22T10:30",
                active_from=datetime(2026, 4, 22).date(),
            ),
        )
        scheduled = commit(
            _make_context(db_session, ctx=ctx, llm=llm, clock=clock),
            preview.preview_id,
            NlCommitEdits(resolved=edited, ambiguities=[]),
        )
    finally:
        reset_current(token)

    assert prop.id != edited_prop.id
    assert scheduled.template.name == "Guest bath deep clean"
    assert scheduled.template.duration_minutes == 45
    assert scheduled.template.listed_property_ids == (edited_prop.id,)
    assert scheduled.schedule.name == "Edited weekly clean"
    assert scheduled.schedule.property_id == edited_prop.id
    assert scheduled.schedule.rrule == "FREQ=WEEKLY;BYDAY=WE"
    assert scheduled.schedule.dtstart_local == "2026-04-22T10:30"


def test_commit_resolved_edit_clears_stored_ambiguities_by_default(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    maria = _seed_user(db_session, workspace_id=ws.id, display_name="Maria")
    edited_prop = _seed_property(db_session, workspace_id=ws.id, name="Villa Nord")
    _seed_property(db_session, workspace_id=ws.id, name="Villa Sud")
    _seed_property(db_session, workspace_id=ws.id, name="Villa Sud Annex")
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = _ctx_for(ws.id, actor_id=maria.id)
    llm = StubLLM(payload=_draft_payload(property_name="Villa"))

    token = set_current(ctx)
    try:
        preview = draft(_make_context(db_session, ctx=ctx, llm=llm, clock=clock), "x")
        edited = ResolvedTask(
            property_id=edited_prop.id,
            assigned_user_id=maria.id,
            template=preview.resolved.template,
            schedule=preview.resolved.schedule,
        )
        scheduled = commit(
            _make_context(db_session, ctx=ctx, llm=llm, clock=clock),
            preview.preview_id,
            NlCommitEdits(resolved=edited),
        )
    finally:
        reset_current(token)

    assert preview.ambiguities[0].field == "property"
    assert scheduled.template.listed_property_ids == (edited_prop.id,)
    assert scheduled.schedule.property_id == edited_prop.id


def test_commit_audit_includes_delegated_attribution(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    maria = _seed_user(db_session, workspace_id=ws.id, display_name="Maria")
    _seed_property(db_session, workspace_id=ws.id, name="Villa Sud")
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = _ctx_for(ws.id, actor_id=maria.id)
    llm = StubLLM(payload=_draft_payload())
    attribution = AgentAttribution(
        actor_user_id=maria.id,
        token_id="tok_delegated",
        agent_label="manager-chat-agent",
        agent_conversation_ref="thread-123",
    )

    token = set_current(ctx)
    try:
        intake_ctx = NlIntakeContext(
            session=db_session,
            workspace_ctx=ctx,
            llm=llm,
            pricing={"fake/task-intake-model": (0, 0)},
            attribution=attribution,
            clock=clock,
        )
        preview = draft(intake_ctx, "x")
        scheduled = commit(intake_ctx, preview.preview_id, None)
        row = db_session.scalars(
            select(AuditLog).where(
                AuditLog.entity_kind == "nl_task_preview",
                AuditLog.entity_id == preview.preview_id,
                AuditLog.action == "commit",
            )
        ).one()
    finally:
        reset_current(token)

    assert row.actor_id == maria.id
    assert row.actor_kind == "user"
    assert row.correlation_id == ctx.audit_correlation_id
    assert row.diff["template_id"] == scheduled.template.id
    assert row.diff["schedule_id"] == scheduled.schedule.id
    assert row.diff["token_id"] == "tok_delegated"
    assert row.diff["agent_label"] == "manager-chat-agent"
    assert row.diff["agent_conversation_ref"] == "thread-123"


def test_expired_preview_raises_preview_expired(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    maria = _seed_user(db_session, workspace_id=ws.id, display_name="Maria")
    _seed_property(db_session, workspace_id=ws.id, name="Villa Sud")
    _seed_ledger(db_session, workspace_id=ws.id)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = _ctx_for(ws.id, actor_id=maria.id)
    llm = StubLLM(payload=_draft_payload())

    token = set_current(ctx)
    try:
        preview = draft(_make_context(db_session, ctx=ctx, llm=llm, clock=clock), "x")
        clock.advance(timedelta(hours=24, seconds=1))
        with pytest.raises(NlPreviewExpired):
            commit(
                _make_context(db_session, ctx=ctx, llm=llm, clock=clock),
                preview.preview_id,
                None,
            )
    finally:
        reset_current(token)


def test_budget_refusal_writes_no_preview_or_usage(
    db_session: Session, clock: FrozenClock
) -> None:
    ws = seed_workspace(db_session)
    maria = _seed_user(db_session, workspace_id=ws.id, display_name="Maria")
    _seed_property(db_session, workspace_id=ws.id, name="Villa Sud")
    _seed_ledger(db_session, workspace_id=ws.id, cap_cents=1, spent_cents=1)
    _seed_capability(db_session, workspace_id=ws.id)
    ctx = _ctx_for(ws.id, actor_id=maria.id)
    llm = StubLLM(payload=_draft_payload())

    token = set_current(ctx)
    try:
        with pytest.raises(BudgetExceeded):
            draft(
                NlIntakeContext(
                    session=db_session,
                    workspace_ctx=ctx,
                    llm=llm,
                    pricing={"fake/task-intake-model": (1_000_000, 1_000_000)},
                    clock=clock,
                ),
                "x",
            )
        preview_count = db_session.scalar(
            select(func.count()).select_from(NlTaskPreview)
        )
        usage_count = db_session.scalar(select(func.count()).select_from(LlmUsageRow))
    finally:
        reset_current(token)

    assert preview_count == 0
    assert usage_count == 0
    assert llm.calls == []
