"""End-to-end integration test for :mod:`app.domain.agent.runtime` (cd-nyvm).

One scenario, exercised against the migrated schema and the real
:mod:`app.domain.llm.router` resolver: a manager agent turn that
calls a write tool. We assert the audit_log row carries:

* ``actor_id = manager user`` (the delegating user, not the agent
  token row).
* ``token_id = <delegated token id>`` from the real
  :func:`app.auth.tokens.mint`.
* ``agent_label = "manager-chat-agent"`` denormalised onto the diff.

The dispatcher in this test is a thin in-process fake (cd-z3b7
lands the production OpenAPI walker); we mirror what the production
dispatcher will do — invoke a domain function, return the
:class:`ToolResult` shape — without coupling to the FastAPI surface
that doesn't yet exist.

See ``docs/specs/11-llm-and-agents.md`` §"Agent audit trail",
``docs/specs/03-auth-and-tokens.md`` §"Delegated tokens".
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import User
from app.adapters.db.llm.models import (
    BudgetLedger,
    LlmModel,
    LlmProvider,
    LlmProviderModel,
    ModelAssignment,
)
from app.adapters.db.messaging.models import ChatChannel
from app.adapters.db.workspace.models import Workspace
from app.adapters.llm.ports import LLMResponse, LLMUsage
from app.auth import tokens as tokens_module
from app.domain.agent.runtime import (
    DelegatedToken,
    GateDecision,
    ToolCall,
    ToolResult,
    run_turn,
)
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext, registry, tenant_agnostic
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import install_tenant_filter
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _ensure_registered() -> None:
    """Belt-and-braces — same pattern as the LLM integration tests."""
    for table in (
        "model_assignment",
        "llm_capability_inheritance",
        "llm_usage",
        "budget_ledger",
        "audit_log",
        "approval_request",
        "agent_token",
        "chat_channel",
        "chat_message",
    ):
        registry.register(table)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    with engine.connect() as raw_connection:
        outer = raw_connection.begin()
        factory = sessionmaker(
            bind=raw_connection,
            expire_on_commit=False,
            class_=Session,
            join_transaction_mode="create_savepoint",
        )
        install_tenant_filter(factory)
        session = factory()
        try:
            yield session
        finally:
            session.close()
            if outer.is_active:
                outer.rollback()


# ---------------------------------------------------------------------------
# Real-token factory — mints a §03 delegated token via app.auth.tokens
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RealDelegatedTokenFactory:
    """Mints a real §03 ``delegated`` token for the integration test.

    Wraps :func:`app.auth.tokens.mint` so the audit row carries a
    real ``token_id`` from the live ``api_token`` table; the
    integration scenario doesn't need to reach into the password
    hasher's argon2id parameters because the test only inspects the
    persisted id.
    """

    session: Session

    def mint_for(
        self,
        ctx: WorkspaceContext,
        *,
        agent_label: str,
        expires_at: datetime,
    ) -> DelegatedToken:
        minted = tokens_module.mint(
            self.session,
            ctx,
            user_id=ctx.actor_id,
            label=agent_label,
            scopes={},
            expires_at=expires_at,
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
        )
        return DelegatedToken(plaintext=minted.token, token_id=minted.key_id)


# ---------------------------------------------------------------------------
# Minimal scripted LLM client + dispatcher
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ScriptedLLM:
    replies: list[LLMResponse]
    chat_calls: int = 0

    def complete(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def chat(self, **kwargs):  # type: ignore[no-untyped-def]
        self.chat_calls += 1
        return self.replies.pop(0)

    def ocr(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def stream_chat(self, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError


@dataclass(slots=True)
class _CountingDispatcher:
    """Simulates the production dispatcher: the call lands, the row
    that the dispatcher would write to the API surface lands here as
    a successful 201."""

    captured: list[ToolCall] = field(default_factory=list)

    def is_gated(self, call: ToolCall) -> GateDecision:
        return GateDecision(gated=False)

    def dispatch(
        self,
        call: ToolCall,
        *,
        token: DelegatedToken,
        headers: Mapping[str, str],
    ) -> ToolResult:
        self.captured.append(call)
        # The headers MUST carry an Authorization-equivalent
        # (the runtime hands the token separately, the production
        # dispatcher then folds it onto a Bearer header). The
        # integration test asserts the runtime supplies it via the
        # ``token`` argument, not via headers — that's the contract
        # the production dispatcher keeps.
        assert token.plaintext.startswith("mip_")
        return ToolResult(
            call_id=call.id,
            status_code=201,
            body={"task_id": "task_001"},
            mutated=True,
        )


# ---------------------------------------------------------------------------
# The end-to-end scenario
# ---------------------------------------------------------------------------


def _seed_workspace_and_user(session: Session) -> tuple[Workspace, User]:
    """Insert a workspace + delegating user."""
    workspace = Workspace(
        id=new_ulid(),
        slug=f"int-{new_ulid().lower()[:10]}",
        name="Integration WS",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    user = User(
        id=new_ulid(),
        email=f"manager-{new_ulid().lower()[:8]}@example.com",
        display_name="Manager",
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add_all([workspace, user])
        session.flush()
    return workspace, user


def _seed_llm_assignment(session: Session, *, workspace_id: str) -> None:
    """Seed a ``chat.manager`` assignment + the registry trio."""
    pm_id = new_ulid()
    provider = LlmProvider(
        id=new_ulid(),
        name="fake-provider",
        provider_type="fake",
        timeout_s=60,
        requests_per_minute=60,
        priority=0,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    model = LlmModel(
        id=new_ulid(),
        canonical_name="fake/integration-model",
        display_name="fake/integration-model",
        vendor="other",
        capabilities=["chat"],
        is_active=True,
        price_source="",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add_all([provider, model])
    session.flush()
    provider_model = LlmProviderModel(
        id=pm_id,
        provider_id=provider.id,
        model_id=model.id,
        api_model_id="fake/integration-model",
        supports_system_prompt=True,
        supports_temperature=True,
        is_enabled=True,
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(provider_model)
    session.flush()
    assignment = ModelAssignment(
        id=new_ulid(),
        workspace_id=workspace_id,
        capability="chat.manager",
        model_id=pm_id,
        provider="fake",
        priority=0,
        enabled=True,
        max_tokens=None,
        temperature=None,
        extra_api_params={},
        required_capabilities=[],
        created_at=_PINNED,
    )
    session.add(assignment)
    session.flush()


def _seed_budget_ledger(session: Session, *, workspace_id: str) -> None:
    row = BudgetLedger(
        id=new_ulid(),
        workspace_id=workspace_id,
        period_start=_PINNED - timedelta(days=30),
        period_end=_PINNED,
        spent_cents=0,
        cap_cents=10_000,
        updated_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(row)
        session.flush()


def _seed_channel(session: Session, *, workspace_id: str) -> str:
    row_id = new_ulid()
    channel = ChatChannel(
        id=row_id,
        workspace_id=workspace_id,
        kind="manager",
        source="app",
        external_ref=None,
        title="Manager chat",
        created_at=_PINNED,
    )
    with tenant_agnostic():
        session.add(channel)
        session.flush()
    return row_id


def test_manager_turn_writes_audit_with_real_delegated_token(
    db_session: Session,
) -> None:
    workspace, user = _seed_workspace_and_user(db_session)
    ctx = WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )
    set_current(ctx)
    _seed_llm_assignment(db_session, workspace_id=workspace.id)
    _seed_budget_ledger(db_session, workspace_id=workspace.id)
    channel_id = _seed_channel(db_session, workspace_id=workspace.id)

    clock = FrozenClock(_PINNED)
    bus = EventBus()
    dispatcher = _CountingDispatcher()
    factory = RealDelegatedTokenFactory(session=db_session)

    # First reply: a tool call to ``tasks.create``. Second: a plain
    # text reply that closes the turn.
    llm = _ScriptedLLM(
        replies=[
            LLMResponse(
                text=(
                    '<tool_call name="tasks.create" '
                    'input=\'{"title":"Restock kitchen","property_id":"p1"}\'/>'
                ),
                usage=LLMUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
                model_id="fake/integration-model",
                finish_reason="tool_calls",
            ),
            LLMResponse(
                text="Created the task.",
                usage=LLMUsage(prompt_tokens=22, completion_tokens=5, total_tokens=27),
                model_id="fake/integration-model",
                finish_reason="stop",
            ),
        ]
    )

    outcome = run_turn(
        ctx,
        session=db_session,
        scope="manager",
        thread_id=channel_id,
        user_message="Please create a task to restock the kitchen.",
        trigger="event",
        llm_client=llm,
        tool_dispatcher=dispatcher,
        token_factory=factory,
        agent_label="manager-chat-agent",
        capability="chat.manager",
        event_bus=bus,
        clock=clock,
    )

    assert outcome.outcome == "replied"
    assert outcome.tool_calls_made == 1
    assert outcome.llm_calls_made == 2
    assert dispatcher.captured

    # AC: audit row carries the delegating user's id, the real
    # delegated token id, the agent label.
    audit_rows = list(db_session.scalars(select(AuditLog)).all())
    # Filter to the agent-tool audit row — token mint also writes
    # one (api_token.minted) which we don't need to assert against
    # here, but we do want to make sure the agent row is there too.
    tool_rows = [r for r in audit_rows if r.action == "agent.tool.tasks.create"]
    assert len(tool_rows) == 1
    row = tool_rows[0]
    assert row.actor_id == user.id, "audit must attribute to the delegating user"
    assert row.actor_kind == "user"
    diff = row.diff
    assert isinstance(diff, dict)
    assert diff["agent_label"] == "manager-chat-agent"
    # The delegated token was minted by the real ``tokens.mint``
    # path; the id should resolve to a live ``api_token`` row.
    token_id = diff["token_id"]
    assert isinstance(token_id, str) and token_id

    from app.adapters.db.identity.models import ApiToken

    with tenant_agnostic():
        token_row = db_session.get(ApiToken, token_id)
    assert token_row is not None, "token_id must point at a real api_token row"
    assert token_row.kind == "delegated"
    assert token_row.delegate_for_user_id == user.id
    assert token_row.label == "manager-chat-agent"
