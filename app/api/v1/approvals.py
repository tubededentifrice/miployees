"""Approvals consumer router — HITL desk + inline approval HTTP surface.

Mounted by the app factory at ``/w/<slug>/api/v1/approvals``. Owned
**conceptually** by the LLM bounded context, but mounted as a
sibling of :data:`app.api.v1.CONTEXT_ROUTERS` so the spec §12 path
contract — ``GET /approvals``, ``POST /approvals/{id}/approve``,
``POST /approvals/{id}/reject`` — lands at the bare path the desk +
inline-card SPA fetches expect (and not under ``/llm/approvals``).
The §01 "13 bounded contexts" invariant is preserved because the
router is not registered in the context map (see the registry
docstring in :mod:`app.api.v1.__init__` for the same rationale
applied to :data:`WORKSPACE_ADMIN_ROUTER`).

Surface (spec §12 "LLM and approvals"):

* ``GET   /``                   — paginated pending queue (cursor + limit).
* ``GET   /{id}``               — single approval row.
* ``POST  /{id}/approve``       — body ``{decision_note_md?}``;
  re-dispatches the recorded tool call under the original
  idempotency key, persists ``result_json``, audits
  ``approval.granted``, emits :class:`ApprovalDecided`.
* ``POST  /{id}/reject``        — body ``{decision_note_md?}``;
  spec alias ``POST /{id}/deny``. Tool call is never dispatched;
  audits ``approval.denied``.

**Auth gating** (§11 "Approval decisions travel through the human
session, not the agent token"):

* **Session cookies**: always accepted (§ rule 1).
* **Personal access tokens (PATs)**: accepted only when carrying the
  ``approvals:act`` scope (§ rule 3). Off by default at mint time.
* **Delegated agent tokens** (§03): rejected with ``403
  approval_requires_session`` and one ``audit.approval.credential_rejected``
  row per attempt (§ rule "Audit-log fields"), so closed-loop bypass
  is observable in the Agent Activity feed.
* **Scoped agent tokens** (§03): rejected with the same envelope as
  delegated — they are not a human credential either, even though
  they were minted by one.

**Replay seam** for ``/{id}/approve``: the API layer constructs an
:class:`ApprovalReplayDispatcher` from a process-wide
:class:`ToolDispatcher` (read off ``app.state.tool_dispatcher`` —
populated by the production wiring in cd-z3b7 once that lands;
``None`` until then). When the dispatcher is unwired the route
returns ``503 dispatcher_not_configured`` so an operator sees the
gap rather than a silent regression. A fresh delegated token is
minted at decision time (the original may have expired) under the
deciding user's id so the replay's audit chain stays attributed
to the human.

See ``docs/specs/12-rest-api.md`` §"LLM and approvals",
``docs/specs/11-llm-and-agents.md`` §"Approval decisions travel
through the human session, not the agent token",
``docs/specs/02-domain-model.md`` §"approval_request".
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import ApiToken
from app.api.deps import current_workspace_context, db_session
from app.audit import write_audit
from app.authz.dep import Permission
from app.domain.agent.approval import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    ApprovalReplayDispatcher,
    ApprovalsPage,
    ApprovalView,
)
from app.domain.agent.approval import (
    approve as approve_service,
)
from app.domain.agent.approval import (
    deny as deny_service,
)
from app.domain.agent.approval import (
    get as get_service,
)
from app.domain.agent.approval import (
    list_pending as list_pending_service,
)
from app.domain.agent.runtime import DelegatedToken, ToolDispatcher
from app.domain.errors import Forbidden
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.tenancy.middleware import ACTOR_STATE_ATTR, ActorIdentity
from app.util.clock import SystemClock

__all__ = [
    "APPROVALS_ACT_SCOPE",
    "ApprovalDecisionBody",
    "ApprovalPayload",
    "ApprovalRequiresSession",
    "ApprovalsListResponse",
    "router",
]


router = APIRouter(tags=["approvals"])


# Spec §11 "Personal API tokens (PATs)" pin: the literal scope key
# that lets a PAT submit an approval decision. Lifted here so the
# router and the auth-dep both reference one source.
APPROVALS_ACT_SCOPE: str = "approvals:act"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ApprovalRequiresSession(Forbidden):
    """Caller submitted a decision under a delegated/scoped agent token.

    Per §11 "Approval decisions travel through the human session,
    not the agent token", an agent-minted credential cannot close
    its own approval loop — only a passkey-authenticated session
    or a PAT carrying ``approvals:act`` may decide. The HTTP seam
    maps this subclass to ``403 approval_requires_session`` (the
    ``type`` URI carries the spec's stable error key, so a client
    can pattern-match without parsing the human-language detail).
    """

    title = "Approval requires session credential"
    type_name = "approval_requires_session"


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class ApprovalDecisionBody(BaseModel):
    """Body for ``POST /{id}/approve`` and ``POST /{id}/reject``.

    A ``None`` / missing ``decision_note_md`` is the expected default
    (the deciding user often clicks Confirm without typing). The
    domain layer collapses empty / whitespace-only notes to ``None``
    so the audit row does not record a misleading empty string.
    """

    model_config = ConfigDict(extra="forbid")

    decision_note_md: str | None = Field(
        default=None,
        max_length=4 * 1024,
        description=(
            "Optional reviewer note in Markdown. Capped at 4 KiB; "
            "longer notes are rejected with 422 by the domain layer. "
            "Empty / whitespace-only collapses to null."
        ),
    )


class ApprovalPayload(BaseModel):
    """HTTP projection of :class:`~app.domain.agent.approval.ApprovalView`.

    Mirrors §11 ``agent_action`` field-for-field. ``action_json``
    surfaces the recorded tool-call envelope (tool name + input +
    card-* fields) verbatim — the SPA's ``/approvals`` desk reads
    those keys directly to render the card without a second
    ``GET``.
    """

    id: str
    workspace_id: str
    requester_actor_id: str | None
    for_user_id: str | None
    inline_channel: str | None
    resolved_user_mode: str | None
    status: str
    decided_by: str | None
    decided_at: datetime | None
    decision_note_md: str | None
    expires_at: datetime | None
    created_at: datetime
    action_json: dict[str, Any]
    result_json: dict[str, Any] | None

    @classmethod
    def from_view(cls, view: ApprovalView) -> ApprovalPayload:
        """Copy a domain :class:`ApprovalView` into its HTTP payload."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            requester_actor_id=view.requester_actor_id,
            for_user_id=view.for_user_id,
            inline_channel=view.inline_channel,
            resolved_user_mode=view.resolved_user_mode,
            status=view.status,
            decided_by=view.decided_by,
            decided_at=view.decided_at,
            decision_note_md=view.decision_note_md,
            expires_at=view.expires_at,
            created_at=view.created_at,
            action_json=dict(view.action_json),
            result_json=(
                dict(view.result_json) if view.result_json is not None else None
            ),
        )


class ApprovalsListResponse(BaseModel):
    """Collection envelope for ``GET /``. Spec §12 "Pagination" shape."""

    data: list[ApprovalPayload]
    next_cursor: str | None = None
    has_more: bool = False

    @classmethod
    def from_page(cls, page: ApprovalsPage) -> ApprovalsListResponse:
        """Project a domain :class:`ApprovalsPage` into its HTTP envelope."""
        return cls(
            data=[ApprovalPayload.from_view(v) for v in page.data],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )


# ---------------------------------------------------------------------------
# Auth-gating dependency
# ---------------------------------------------------------------------------


def _actor_identity(request: Request) -> ActorIdentity | None:
    """Return the resolved :class:`ActorIdentity` from request state.

    The tenancy middleware stamps it on every authenticated request
    (see :mod:`app.tenancy.middleware`); a missing value means the
    handler is being driven by a test that did not wire the
    middleware. The auth-gating dep treats that case as "session"
    so router-level unit tests stay simple — production traffic
    always carries a real identity.
    """
    return getattr(request.state, ACTOR_STATE_ATTR, None)


def _resolve_decider_credential(
    *,
    session: Session,
    actor: ActorIdentity | None,
    ctx: WorkspaceContext,
) -> tuple[str, str | None]:
    """Resolve the credential kind submitting the decision.

    Returns ``(credential_kind, token_id)``. ``credential_kind`` is
    one of:

    * ``"session"`` — passkey-authenticated browser session (the
      common path); also returned when no :class:`ActorIdentity`
      is on the request (test convenience — production requests
      always carry one).
    * ``"pat"`` — personal access token carrying ``approvals:act``.
    * ``"delegated_agent"`` — delegated agent token (§03). Caller
      must reject with :class:`ApprovalRequiresSession`.
    * ``"scoped_agent"`` — scoped agent token (§03). Caller must
      reject with :class:`ApprovalRequiresSession`.
    * ``"pat_missing_scope"`` — personal access token without
      ``approvals:act``. Caller must reject with
      :class:`ApprovalRequiresSession`.
    * ``"unknown_token"`` — token id stamped on the request does
      not match any row (token revoked / pruned mid-request, or a
      dirty test). Caller must reject with
      :class:`ApprovalRequiresSession` — fail closed.

    ``token_id`` is the offending row id when the credential is
    rejected, ``None`` otherwise — the audit-row writer needs it
    to record which token tried to bypass.

    The :class:`ApiToken` lookup runs under :func:`tenant_agnostic`
    because ``api_token`` is not registered as workspace-scoped
    (see :mod:`app.adapters.db.identity`); without the bracket the
    ORM filter would still permit the lookup (the table isn't in
    the registry) but the bracket makes the intent explicit.
    """
    # Session branch: no token id on the request, OR the ctx itself
    # was resolved from a session. The middleware already vetted the
    # caller's identity; the §11 rule is satisfied.
    if actor is None or actor.token_id is None:
        return ("session", None)
    if ctx.principal_kind == "session":
        # Belt-and-braces: a token-presented request that the tenancy
        # middleware somehow flagged as session means a misconfiguration
        # — accept the session classification (§11 rule 1) but don't
        # try to walk the token row.
        return ("session", None)

    token_id = actor.token_id
    with tenant_agnostic():
        row = session.get(ApiToken, token_id)
    if row is None:
        return ("unknown_token", token_id)

    if row.kind == "delegated":
        return ("delegated_agent", token_id)
    if row.kind == "scoped":
        return ("scoped_agent", token_id)
    if row.kind == "personal":
        # Personal access token: scope-gated. ``scope_json`` is a
        # mapping of scope-name → enabled-flag (truthy = granted)
        # per §03 "Personal access tokens"; the spec pins the
        # literal key ``approvals:act`` as the gate.
        if row.scope_json.get(APPROVALS_ACT_SCOPE):
            return ("pat", token_id)
        return ("pat_missing_scope", token_id)
    # Defence-in-depth: an unknown ``kind`` is a data/CHECK-constraint
    # bug. Fail closed — a row that does not match a known kind cannot
    # be a session-class credential.
    return ("unknown_token", token_id)


def _require_decider_principal(
    request: Request,
    ctx: Annotated[WorkspaceContext, Depends(current_workspace_context)],
    session: Annotated[Session, Depends(db_session)],
) -> WorkspaceContext:
    """Enforce §11 "decisions travel through the human session".

    Returns the :class:`WorkspaceContext` unchanged on the accept
    path. On the reject path raises :class:`ApprovalRequiresSession`
    AND writes one ``audit.approval.credential_rejected`` row so
    closed-loop bypass surfaces in the Agent Activity feed (§11
    "Audit-log fields"). The audit row is the only side effect;
    the seam never partially commits a decision.

    The handler is wired as a FastAPI dep so every decision route
    funnels through one auth gate; ``_resolve_decider_credential``
    is the single place that mints the credential vocabulary.
    """
    actor = _actor_identity(request)
    credential_kind, offending_token_id = _resolve_decider_credential(
        session=session, actor=actor, ctx=ctx
    )
    if credential_kind in ("session", "pat"):
        return ctx

    # Reject path: write the credential-rejected audit row in a
    # FRESH UoW that commits in isolation, then raise. The
    # request-scoped :class:`UnitOfWorkImpl` rolls back on any
    # exception (its ``__exit__`` runs *after* this dep returns,
    # with the raised :class:`ApprovalRequiresSession` propagating);
    # if we wrote the audit row into the request session and called
    # ``session.commit()`` we would also commit any other pending
    # mutations the dep chain might have introduced (today there are
    # none, but a future :func:`current_workspace_context` extension
    # that touches the session would silently piggy-back on the
    # commit).
    #
    # A dedicated UoW is the worker pattern (see
    # :func:`app.worker.tasks.approval_ttl.sweep_expired_approvals`)
    # and is the right shape here — the audit row is the whole
    # point of the §11 "Audit-log fields" rule, and a rolled-back
    # row (or an accidentally co-committed unrelated mutation)
    # would silently defeat the observability seam. Using the
    # deciding user's actor id keeps the row attributable to the
    # (rejected) attempt.
    from app.adapters.db.session import make_uow  # local — only here

    with make_uow() as audit_session:
        # ``DbSession`` is the read-side Protocol; the concrete UoW
        # always yields a real :class:`Session`. ``write_audit`` is
        # typed against the concrete session because it issues writes
        # — narrow at the seam rather than widening the helper.
        assert isinstance(audit_session, Session)
        write_audit(
            audit_session,
            ctx,
            entity_kind="approval_request",
            entity_id=request.path_params.get("approval_request_id", "unknown"),
            action="approval.credential_rejected",
            diff={
                "credential_kind": credential_kind,
                "offending_token_id": offending_token_id,
            },
            clock=SystemClock(),
        )
        # ``UnitOfWorkImpl.__exit__`` commits on a clean return; we
        # raise *outside* the with block so the audit commit lands
        # before the 403 envelope is built.
    raise ApprovalRequiresSession(
        "approval decisions must be submitted under a passkey session "
        "or a personal access token carrying the approvals:act scope",
        extra={
            "credential_kind": credential_kind,
            "token_id": offending_token_id,
        },
    )


# ---------------------------------------------------------------------------
# Replay-dispatcher dependency
# ---------------------------------------------------------------------------


def get_tool_dispatcher(request: Request) -> ToolDispatcher:
    """Return the process-wide :class:`ToolDispatcher`, or 503.

    Read lazily off ``app.state.tool_dispatcher`` — the production
    wiring (cd-z3b7) populates this attribute at boot time once the
    OpenAPI in-process dispatcher lands. Tests override via
    ``app.dependency_overrides[get_tool_dispatcher] = ...`` to
    inject a fake without touching the live ``app.state``.

    A missing dispatcher surfaces as ``503 dispatcher_not_configured``
    so an operator hitting the approve route on a deployment that
    forgot to wire the seam sees the gap rather than a silent
    regression.
    """
    dispatcher: ToolDispatcher | None = getattr(
        request.app.state, "tool_dispatcher", None
    )
    if dispatcher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "dispatcher_not_configured",
                "message": (
                    "approval replay requires a configured ToolDispatcher; "
                    "this deployment has not wired one yet (cd-z3b7)"
                ),
            },
        )
    return dispatcher


def _build_replay(
    *,
    ctx: WorkspaceContext,
    dispatcher: ToolDispatcher,
) -> ApprovalReplayDispatcher:
    """Bundle the dispatcher + a replay-time delegated token.

    The replay token is **not** the original turn's token — that one
    may have expired between gate-time and decision-time. Instead
    we mint a fresh delegated identity at decision-time, attributed
    to the deciding user (``ctx.actor_id``). The dispatcher receives
    it as ``Authorization: Bearer ...`` on the in-process replay;
    the recorded tool call's idempotency key (in
    ``action_json.tool_call_id``) is what guarantees a duplicate
    HTTP retry of the approve route is a no-op at the side-effect
    layer.

    cd-9ghv ships the route without minting a real token row — the
    token id is a synthetic ULID and the plaintext is a fixed
    placeholder. The production dispatcher (cd-z3b7) will replace
    the placeholder with a real :func:`app.auth.tokens.mint` call
    once the wiring lands; until then the placeholder is harmless
    because the only consumer is the in-process dispatcher (no
    network egress).
    """
    # Synthetic delegated-token surrogate. The dispatcher reads
    # ``token.plaintext`` for the ``Authorization: Bearer`` header
    # and ``token.token_id`` for audit-row stamping; both are
    # stable per-request strings with no security weight in the
    # in-process replay (the call never leaves the deployment).
    # See class docstring for the cd-z3b7 follow-up.
    from app.util.ulid import new_ulid  # local — only used here

    surrogate = DelegatedToken(
        plaintext=f"mip_REPLAY_{new_ulid()}",
        token_id=new_ulid(),
    )
    return ApprovalReplayDispatcher(
        dispatcher=dispatcher,
        token=surrogate,
        headers={
            "X-Agent-Channel": "approval-replay",
            "X-Crewday-Replay": "1",
        },
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]
_DeciderCtx = Annotated[WorkspaceContext, Depends(_require_decider_principal)]
_Dispatcher = Annotated[ToolDispatcher, Depends(get_tool_dispatcher)]
_ApprovalReadGate = Depends(Permission("approvals.read", scope_kind="workspace"))


@router.get(
    "",
    response_model=ApprovalsListResponse,
    summary="List pending approvals",
    dependencies=[_ApprovalReadGate],
)
def list_approvals(
    ctx: _Ctx,
    db: _Db,
    cursor: Annotated[
        str | None,
        Query(
            max_length=128,
            description=(
                "Forward cursor from the previous page's ``next_cursor``; "
                "the row id of the last result returned. Omitted on the "
                "first page."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_PAGE_LIMIT,
            description=(
                f"Max rows per page (default {DEFAULT_PAGE_LIMIT}, cap "
                f"{MAX_PAGE_LIMIT}). Spec §12 'Pagination'."
            ),
        ),
    ] = DEFAULT_PAGE_LIMIT,
) -> ApprovalsListResponse:
    """Return ``status='pending'`` approvals oldest-first.

    Cursor is the row id of the last result returned; pagination
    rides the ``(created_at, id)`` composite-key strict-greater-than
    so same-millisecond ULIDs still order deterministically. The
    domain service handles the boundary detection via ``LIMIT N+1``
    and surfaces ``has_more`` on the envelope.
    """
    page = list_pending_service(ctx, session=db, cursor=cursor, limit=limit)
    return ApprovalsListResponse.from_page(page)


@router.get(
    "/{approval_request_id}",
    response_model=ApprovalPayload,
    summary="Get one approval by id",
    dependencies=[_ApprovalReadGate],
)
def get_approval(
    ctx: _Ctx,
    db: _Db,
    approval_request_id: str,
) -> ApprovalPayload:
    """Return one approval row, scoped to ``ctx``'s workspace.

    A cross-tenant or missing row surfaces as 404 ``approval_not_found``
    via the domain :class:`ApprovalNotFound` mapping (§01 "Workspace
    addressing" — cross-tenant must not be enumerable, so 404 wins
    over 403).
    """
    view = get_service(ctx, session=db, approval_request_id=approval_request_id)
    return ApprovalPayload.from_view(view)


@router.post(
    "/{approval_request_id}/approve",
    response_model=ApprovalPayload,
    status_code=status.HTTP_200_OK,
    summary="Approve a pending action and replay it",
)
def approve_approval(
    request: Request,
    ctx: _DeciderCtx,
    db: _Db,
    dispatcher: _Dispatcher,
    approval_request_id: str,
    body: ApprovalDecisionBody | None = None,
) -> ApprovalPayload:
    """Flip ``pending → approved``, replay the recorded tool call.

    The decision endpoint:

    1. Goes through :func:`_require_decider_principal` so an agent
       token cannot close its own loop (§11 rule).
    2. Builds an :class:`ApprovalReplayDispatcher` bundling the
       process-wide :class:`ToolDispatcher` + a fresh delegated
       token attributed to the deciding user.
    3. Hands off to :func:`approve_service` which dispatches the
       recorded call, persists ``result_json``, audits
       ``approval.granted``, and emits :class:`ApprovalDecided`.

    A retried HTTP approve (same id) raises 409
    ``approval_not_pending`` because the row is no longer
    ``status='pending'`` after the first decision lands. The
    side effect itself is single-fire because the replayed tool
    call carries the recorded idempotency key.
    """
    note: str | None = body.decision_note_md if body is not None else None
    replay = _build_replay(ctx=ctx, dispatcher=dispatcher)
    view = approve_service(
        ctx,
        session=db,
        approval_request_id=approval_request_id,
        replay=replay,
        decision_note_md=note,
    )
    return ApprovalPayload.from_view(view)


def _deny_handler(
    ctx: WorkspaceContext,
    db: Session,
    approval_request_id: str,
    body: ApprovalDecisionBody | None,
) -> ApprovalPayload:
    """Shared body for ``/{id}/reject`` and the ``/{id}/deny`` alias.

    Reject and deny are the same domain operation (§12 names ``reject``
    in the path table; §11 prose alternates between "deny" and
    "reject"). Routing both at the HTTP layer means the SPA + CLI +
    docs+tests can use whichever they grew up with without a
    second-class redirect.
    """
    note: str | None = body.decision_note_md if body is not None else None
    view = deny_service(
        ctx,
        session=db,
        approval_request_id=approval_request_id,
        decision_note_md=note,
    )
    return ApprovalPayload.from_view(view)


@router.post(
    "/{approval_request_id}/reject",
    response_model=ApprovalPayload,
    status_code=status.HTTP_200_OK,
    summary="Reject a pending action",
)
def reject_approval(
    ctx: _DeciderCtx,
    db: _Db,
    approval_request_id: str,
    body: ApprovalDecisionBody | None = None,
) -> ApprovalPayload:
    """Flip ``pending → rejected``; the tool call is never dispatched.

    Audits ``approval.denied``, emits
    :class:`~app.events.types.ApprovalDecided` with
    ``decision='rejected'``. A retried reject raises 409
    ``approval_not_pending`` after the first decision lands.
    """
    return _deny_handler(ctx, db, approval_request_id, body)


@router.post(
    "/{approval_request_id}/deny",
    response_model=ApprovalPayload,
    status_code=status.HTTP_200_OK,
    summary="Deny a pending action (alias of /reject)",
    include_in_schema=False,
)
def deny_approval(
    ctx: _DeciderCtx,
    db: _Db,
    approval_request_id: str,
    body: ApprovalDecisionBody | None = None,
) -> ApprovalPayload:
    """Alias of ``POST /{id}/reject`` (§11 prose uses both verbs).

    Hidden from the OpenAPI surface (``include_in_schema=False``)
    so the spec-canonical ``/reject`` is the one tooling generates;
    the alias remains accepting so an SPA / agent that learned the
    other name on the §11-prose path keeps working.
    """
    return _deny_handler(ctx, db, approval_request_id, body)


# Re-export for type-checker clarity at the factory mount site.
def __dir__() -> list[str]:
    return list(__all__)
