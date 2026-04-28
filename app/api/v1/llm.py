"""LLM context router.

Owns agents, approvals, preferences, model assignments, usage,
budgets, and outbound webhooks (spec §01 "Context map", §12
"LLM and approvals", §11).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import AgentPreference, BudgetLedger
from app.api.deps import current_workspace_context, db_session
from app.audit import write_audit
from app.authz.dep import Permission
from app.domain.agent.preferences import (
    APPROVAL_MODES,
    PreferenceContainsSecret,
    PreferenceTooLarge,
    PreferenceUpdate,
    read_preference,
    save_preference,
)
from app.tenancy import WorkspaceContext

router = APIRouter(tags=["llm"])

__all__ = ["router"]

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
ApprovalMode = Literal["bypass", "auto", "strict"]


class AgentPreferenceRead(BaseModel):
    """Read model for one preference scope."""

    model_config = ConfigDict(extra="forbid")

    scope_kind: Literal["workspace", "user"]
    scope_id: str
    body_md: str
    token_count: int
    blocked_actions: list[str] = Field(default_factory=list)
    default_approval_mode: ApprovalMode = "auto"


class AgentApprovalModeRead(BaseModel):
    """Read model for the caller's personal agent approval mode."""

    model_config = ConfigDict(extra="forbid")

    mode: ApprovalMode


class AgentApprovalModeUpdate(BaseModel):
    """Payload for updating the caller's personal agent approval mode."""

    model_config = ConfigDict(extra="forbid")

    mode: ApprovalMode


class WorkspaceUsageRead(BaseModel):
    """Manager-visible workspace agent usage tile."""

    model_config = ConfigDict(extra="forbid")

    percent: int
    paused: bool
    window_label: str


class WorkspaceAgentPreferenceUpdate(BaseModel):
    """Workspace preference update payload."""

    model_config = ConfigDict(extra="forbid")

    body_md: str = ""
    blocked_actions: list[str] = Field(default_factory=list)
    default_approval_mode: ApprovalMode = "auto"
    change_note: str | None = None


class SelfAgentPreferenceUpdate(BaseModel):
    """Self preference update payload."""

    model_config = ConfigDict(extra="forbid")

    body_md: str = ""
    change_note: str | None = None


def _empty_response(
    *, scope_kind: Literal["workspace", "user"], scope_id: str
) -> AgentPreferenceRead:
    return AgentPreferenceRead(
        scope_kind=scope_kind,
        scope_id=scope_id,
        body_md="",
        token_count=0,
        blocked_actions=[],
        default_approval_mode="auto",
    )


def _to_response(row: AgentPreference) -> AgentPreferenceRead:
    return AgentPreferenceRead(
        scope_kind=row.scope_kind,
        scope_id=row.scope_id,
        body_md=row.body_md,
        token_count=row.token_count,
        blocked_actions=list(row.blocked_actions),
        default_approval_mode=row.default_approval_mode,
    )


def _save_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PreferenceContainsSecret):
        return HTTPException(
            status_code=422,
            detail={"error": "preference_contains_secret"},
        )
    if isinstance(exc, PreferenceTooLarge):
        return HTTPException(
            status_code=422,
            detail={"error": "preference_too_large"},
        )
    raise exc


def _get_actor(session: Session, actor_id: str) -> User:
    user = session.get(User, actor_id)
    if user is None or user.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "user_not_found"},
        )
    return user


def _coerce_mode(value: str) -> ApprovalMode:
    if value == "bypass":
        return "bypass"
    if value == "strict":
        return "strict"
    return "auto"


def _current_budget_ledger(
    session: Session, ctx: WorkspaceContext
) -> BudgetLedger | None:
    return session.scalar(
        select(BudgetLedger)
        .where(BudgetLedger.workspace_id == ctx.workspace_id)
        .order_by(BudgetLedger.period_end.desc(), BudgetLedger.updated_at.desc())
        .limit(1)
    )


def _usage_response(row: BudgetLedger | None) -> WorkspaceUsageRead:
    if row is None:
        return WorkspaceUsageRead(
            percent=0,
            paused=False,
            window_label="Rolling 30 days",
        )
    if row.cap_cents <= 0:
        percent = 100
    else:
        percent = min((row.spent_cents * 100) // row.cap_cents, 100)
    return WorkspaceUsageRead(
        percent=percent,
        paused=row.cap_cents <= 0 or row.spent_cents >= row.cap_cents,
        window_label="Rolling 30 days",
    )


@router.get(
    "/agent_preferences/workspace",
    response_model=AgentPreferenceRead,
    operation_id="llm.agent_preferences.workspace.get",
    summary="Read workspace agent preferences",
    openapi_extra={
        "x-cli": {
            "group": "agent-prefs",
            "verb": "show-workspace",
            "summary": "Read workspace agent preferences",
            "mutates": False,
        },
    },
)
@router.get(
    "/workspace/agent_prefs",
    response_model=AgentPreferenceRead,
    operation_id="llm.agent_prefs.workspace.get",
    summary="Read workspace agent preferences",
    include_in_schema=False,
)
def get_workspace_agent_prefs(ctx: _Ctx, session: _Db) -> AgentPreferenceRead:
    row = read_preference(
        session,
        ctx,
        scope_kind="workspace",
        scope_id=ctx.workspace_id,
    )
    if row is None:
        return _empty_response(scope_kind="workspace", scope_id=ctx.workspace_id)
    return _to_response(row)


@router.put(
    "/agent_preferences/workspace",
    response_model=AgentPreferenceRead,
    operation_id="llm.agent_preferences.workspace.put",
    summary="Update workspace agent preferences",
    dependencies=[
        Depends(Permission("agent_prefs.edit_workspace", scope_kind="workspace"))
    ],
    openapi_extra={
        "x-agent-confirm": {
            "summary": "Update workspace agent preferences?",
            "risk": "medium",
            "fields_to_show": ["blocked_actions", "default_approval_mode"],
            "verb": "Update agent preferences",
        },
        "x-cli": {
            "group": "agent-prefs",
            "verb": "set-workspace",
            "summary": "Update workspace agent preferences",
            "mutates": True,
        },
    },
)
@router.put(
    "/workspace/agent_prefs",
    response_model=AgentPreferenceRead,
    operation_id="llm.agent_prefs.workspace.put",
    summary="Update workspace agent preferences",
    dependencies=[
        Depends(Permission("agent_prefs.edit_workspace", scope_kind="workspace"))
    ],
    openapi_extra={
        "x-agent-confirm": {
            "summary": "Update workspace agent preferences?",
            "risk": "medium",
            "fields_to_show": ["blocked_actions", "default_approval_mode"],
            "verb": "Update agent preferences",
        },
        "x-cli": {
            "group": "agent-prefs",
            "verb": "set-workspace",
            "summary": "Update workspace agent preferences",
            "mutates": True,
        },
    },
    include_in_schema=False,
)
def put_workspace_agent_prefs(
    payload: WorkspaceAgentPreferenceUpdate,
    ctx: _Ctx,
    session: _Db,
) -> AgentPreferenceRead:
    if payload.default_approval_mode not in APPROVAL_MODES:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_approval_mode"},
        )
    try:
        row = save_preference(
            session,
            ctx,
            scope_kind="workspace",
            scope_id=ctx.workspace_id,
            update=PreferenceUpdate(
                body_md=payload.body_md,
                blocked_actions=tuple(payload.blocked_actions),
                default_approval_mode=payload.default_approval_mode,
                change_note=payload.change_note,
            ),
            actor_user_id=ctx.actor_id,
        )
    except (PreferenceContainsSecret, PreferenceTooLarge) as exc:
        raise _save_error(exc) from exc
    return _to_response(row)


@router.get(
    "/agent_preferences/me",
    response_model=AgentPreferenceRead,
    operation_id="llm.agent_preferences.me.get",
    summary="Read my agent preferences",
    openapi_extra={
        "x-cli": {
            "group": "agent-prefs",
            "verb": "show-me",
            "summary": "Read my agent preferences",
            "mutates": False,
        },
    },
)
@router.get(
    "/users/me/agent_prefs",
    response_model=AgentPreferenceRead,
    operation_id="llm.agent_prefs.me.get",
    summary="Read my agent preferences",
    include_in_schema=False,
)
def get_my_agent_prefs(ctx: _Ctx, session: _Db) -> AgentPreferenceRead:
    row = read_preference(
        session,
        ctx,
        scope_kind="user",
        scope_id=ctx.actor_id,
    )
    if row is None:
        return _empty_response(scope_kind="user", scope_id=ctx.actor_id)
    return _to_response(row)


@router.put(
    "/agent_preferences/me",
    response_model=AgentPreferenceRead,
    operation_id="llm.agent_preferences.me.put",
    summary="Update my agent preferences",
    openapi_extra={
        "x-agent-confirm": {
            "summary": "Update your agent preferences?",
            "risk": "low",
            "fields_to_show": [],
            "verb": "Update my agent preferences",
        },
        "x-cli": {
            "group": "agent-prefs",
            "verb": "set-me",
            "summary": "Update my agent preferences",
            "mutates": True,
        },
    },
)
@router.put(
    "/users/me/agent_prefs",
    response_model=AgentPreferenceRead,
    operation_id="llm.agent_prefs.me.put",
    summary="Update my agent preferences",
    openapi_extra={
        "x-agent-confirm": {
            "summary": "Update your agent preferences?",
            "risk": "low",
            "fields_to_show": [],
            "verb": "Update my agent preferences",
        },
        "x-cli": {
            "group": "agent-prefs",
            "verb": "set-me",
            "summary": "Update my agent preferences",
            "mutates": True,
        },
    },
    include_in_schema=False,
)
def put_my_agent_prefs(
    payload: SelfAgentPreferenceUpdate,
    ctx: _Ctx,
    session: _Db,
) -> AgentPreferenceRead:
    try:
        row = save_preference(
            session,
            ctx,
            scope_kind="user",
            scope_id=ctx.actor_id,
            update=PreferenceUpdate(
                body_md=payload.body_md,
                change_note=payload.change_note,
            ),
            actor_user_id=ctx.actor_id,
        )
    except (PreferenceContainsSecret, PreferenceTooLarge) as exc:
        raise _save_error(exc) from exc
    return _to_response(row)


@router.get(
    "/me/agent_approval_mode",
    response_model=AgentApprovalModeRead,
    operation_id="llm.agent_approval_mode.me.get",
    summary="Read my agent approval mode",
    openapi_extra={
        "x-cli": {
            "group": "agent",
            "verb": "approval-mode",
            "summary": "Read my agent approval mode",
            "mutates": False,
        },
    },
)
def get_my_agent_approval_mode(ctx: _Ctx, session: _Db) -> AgentApprovalModeRead:
    user = _get_actor(session, ctx.actor_id)
    return AgentApprovalModeRead(mode=_coerce_mode(user.agent_approval_mode))


@router.put(
    "/me/agent_approval_mode",
    response_model=AgentApprovalModeRead,
    operation_id="llm.agent_approval_mode.me.put",
    summary="Update my agent approval mode",
    openapi_extra={
        "x-agent-confirm": {
            "summary": "Update your agent approval mode?",
            "risk": "medium",
            "fields_to_show": ["mode"],
            "verb": "Update agent approval mode",
        },
        "x-cli": {
            "group": "agent",
            "verb": "set-approval-mode",
            "summary": "Update my agent approval mode",
            "mutates": True,
        },
    },
)
def put_my_agent_approval_mode(
    payload: AgentApprovalModeUpdate,
    ctx: _Ctx,
    session: _Db,
) -> AgentApprovalModeRead:
    user = _get_actor(session, ctx.actor_id)
    before = _coerce_mode(user.agent_approval_mode)
    user.agent_approval_mode = payload.mode
    write_audit(
        session,
        ctx,
        entity_kind="user",
        entity_id=user.id,
        action="auth.agent_mode_changed",
        diff={"before": {"mode": before}, "after": {"mode": payload.mode}},
    )
    session.flush()
    return AgentApprovalModeRead(mode=payload.mode)


@router.get(
    "/workspace/usage",
    response_model=WorkspaceUsageRead,
    operation_id="llm.workspace.usage.get",
    summary="Read workspace agent usage",
    dependencies=[Depends(Permission("scope.edit_settings", scope_kind="workspace"))],
    openapi_extra={
        "x-cli": {
            "group": "agent",
            "verb": "usage",
            "summary": "Read workspace agent usage",
            "mutates": False,
        },
    },
)
def get_workspace_usage(ctx: _Ctx, session: _Db) -> WorkspaceUsageRead:
    return _usage_response(_current_budget_ledger(session, ctx))
