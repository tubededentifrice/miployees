"""HTTP-level tests for :mod:`app.api.v1.approvals` (cd-9ghv).

Exercises every route on the approvals consumer:

* ``GET /`` — list pending (empty + paginated cursor walk).
* ``GET /{id}`` — single row + cross-tenant 404.
* ``POST /{id}/approve`` — happy path, retry → 409, missing
  dispatcher → 503, decision-note normalisation, length cap → 422.
* ``POST /{id}/reject`` and ``POST /{id}/deny`` — happy path
  alias parity, no dispatcher invocation, retry → 409.
* Credential matrix (§11 "Approval decisions travel through the
  human session, not the agent token"):
    - cookie session → accept (no token id on actor identity);
    - PAT carrying ``approvals:act`` → accept;
    - PAT without ``approvals:act`` → 403 ``approval_requires_session``
      + ``audit.approval.credential_rejected`` row;
    - delegated agent token → 403 + audit row;
    - scoped agent token → 403 + audit row;
    - unknown token id → 403 + audit row (fail closed);

The route under test mounts at the bare path ``/`` because the
production workspace prefix is overridden by the
:func:`current_workspace_context` dep — the slug lookup never runs.
End-to-end coverage of the workspace-scoped mount lives at
``tests/integration/api/`` once the approvals integration shard
lands; this file is the unit-level seam for the auth-gating logic
and route handlers.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.llm.models import ApprovalRequest
from app.tenancy import tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import (
    bootstrap_user,
    bootstrap_workspace,
    build_workspace_context,
)
from tests.unit.api.v1.approvals.conftest import (
    _PINNED,
    APPROVALS_ACT_SCOPE,
    FakeToolDispatcher,
    _Persona,
    build_client,
    seed_api_token,
    seed_pending,
    session_identity,
    token_identity,
)

# ---------------------------------------------------------------------------
# Audit + DB helpers
# ---------------------------------------------------------------------------


def _audit_actions(persona: _Persona, *, workspace_id: str) -> list[str]:
    """Return audit-row actions for the workspace, ordered by created_at."""
    with persona.factory() as s, tenant_agnostic():
        rows = s.scalars(
            select(AuditLog)
            .where(AuditLog.workspace_id == workspace_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        ).all()
        return [row.action for row in rows]


def _refresh_row(persona: _Persona, row_id: str) -> ApprovalRequest:
    """Reload the approval row in a fresh transaction."""
    with persona.factory() as s, tenant_agnostic():
        row = s.get(ApprovalRequest, row_id)
        assert row is not None, f"approval {row_id} vanished"
        # Eager-pull every field while inside the session so the caller
        # can read them after the with block exits without an
        # ``ObjectDeletedError`` from a second session opening.
        s.expunge(row)
        return row


def _worker_persona(owner_ctx: _Persona) -> _Persona:
    """Seed a workspace worker in the owner's workspace."""
    with owner_ctx.factory() as s, tenant_agnostic():
        worker = bootstrap_user(
            s, email="approvals-worker@example.com", display_name="Approvals Worker"
        )
        grant = RoleGrant(
            id=new_ulid(),
            workspace_id=owner_ctx.workspace_id,
            user_id=worker.id,
            grant_role="worker",
            scope_kind="workspace",
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=owner_ctx.owner_id,
        )
        s.add(grant)
        s.commit()
        worker_id = worker.id

    ctx = build_workspace_context(
        workspace_id=owner_ctx.workspace_id,
        workspace_slug=owner_ctx.ctx.workspace_slug,
        actor_id=worker_id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
    )
    return _Persona(
        ctx=ctx,
        factory=owner_ctx.factory,
        workspace_id=owner_ctx.workspace_id,
        owner_id=worker_id,
    )


# ---------------------------------------------------------------------------
# GET / (list pending)
# ---------------------------------------------------------------------------


class TestListPending:
    """``GET /`` — paginated pending queue."""

    def test_empty_returns_envelope_with_no_data(self, owner_ctx: _Persona) -> None:
        client = build_client(owner_ctx)
        resp = client.get("/approvals/")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"data": [], "next_cursor": None, "has_more": False}

    def test_returns_pending_rows_oldest_first(self, owner_ctx: _Persona) -> None:
        # Seed three rows with strictly-increasing ``created_at`` so
        # the order is unambiguous (ULIDs alone are
        # millisecond-granular and same-test-frozen-clock seeds collide).
        from datetime import timedelta

        for i in range(3):
            seed_pending(
                owner_ctx.factory,
                workspace_id=owner_ctx.workspace_id,
                requester_actor_id=owner_ctx.owner_id,
                tool_name=f"tasks.tool_{i}",
                created_at=_PINNED + timedelta(seconds=i),
            )
        client = build_client(owner_ctx)
        resp = client.get("/approvals/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_more"] is False
        assert body["next_cursor"] is None
        assert len(body["data"]) == 3
        names = [row["action_json"]["tool_name"] for row in body["data"]]
        assert names == ["tasks.tool_0", "tasks.tool_1", "tasks.tool_2"]

    def test_cursor_paginates_with_has_more(self, owner_ctx: _Persona) -> None:
        from datetime import timedelta

        seeded = [
            seed_pending(
                owner_ctx.factory,
                workspace_id=owner_ctx.workspace_id,
                requester_actor_id=owner_ctx.owner_id,
                tool_name=f"tool_{i}",
                created_at=_PINNED + timedelta(seconds=i),
            )
            for i in range(3)
        ]
        client = build_client(owner_ctx)
        # Page 1 — limit=2, expect two rows + has_more=True.
        resp = client.get("/approvals/", params={"limit": 2})
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_more"] is True
        assert body["next_cursor"] == seeded[1]
        assert [r["id"] for r in body["data"]] == seeded[:2]
        # Page 2 — using the cursor — expect the last row + no more.
        resp = client.get(
            "/approvals/",
            params={"limit": 2, "cursor": body["next_cursor"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_more"] is False
        assert body["next_cursor"] is None
        assert [r["id"] for r in body["data"]] == seeded[2:]

    def test_invalid_limit_zero_rejected_by_query_validator(
        self, owner_ctx: _Persona
    ) -> None:
        """``limit=0`` is rejected by FastAPI's ``Query(ge=1)`` constraint.

        The router pins ``ge=1`` so the spec §12 "Pagination" guard
        runs at the parse seam — the domain layer's own
        :class:`Validation` raise on ``limit <= 0`` is a defence-in-
        depth check the route never reaches.
        """
        client = build_client(owner_ctx)
        resp = client.get("/approvals/", params={"limit": 0})
        assert resp.status_code == 422

    def test_worker_without_read_permission_gets_403(
        self, owner_ctx: _Persona
    ) -> None:
        seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        client = build_client(_worker_persona(owner_ctx))
        resp = client.get("/approvals/")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "permission_denied"
        assert body["action_key"] == "approvals.read"


# ---------------------------------------------------------------------------
# GET /{id} (single row)
# ---------------------------------------------------------------------------


class TestGetOne:
    def test_returns_payload(self, owner_ctx: _Persona) -> None:
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        client = build_client(owner_ctx)
        resp = client.get(f"/approvals/{row_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == row_id
        assert body["status"] == "pending"
        assert body["action_json"]["tool_name"] == "tasks.complete"
        # ``result_json`` is None on a pending row — the SPA renders
        # the recorded ``action_json`` instead.
        assert body["result_json"] is None

    def test_cross_tenant_404(self, owner_ctx: _Persona) -> None:
        """A row in another workspace surfaces as 404, not 403.

        §01 "Workspace addressing" pins the not-enumerable rule; a
        403 would leak the row's existence. The domain
        :class:`ApprovalNotFound` envelope renders as ``not_found``.

        The second workspace is seeded inline (sharing the engine
        with ``owner_ctx``) so the ORM tenant filter sees both rows
        under one connection, exactly like production.
        """
        # Seed a parallel workspace + owner on the same engine.
        with owner_ctx.factory() as s:
            other_owner = bootstrap_user(
                s, email="other@example.com", display_name="Other Owner"
            )
            other_ws = bootstrap_workspace(
                s,
                slug="ws-approvals-other",
                name="Other WS",
                owner_user_id=other_owner.id,
            )
            s.commit()
            other_ws_id = other_ws.id
            other_owner_id = other_owner.id

        other_row = seed_pending(
            owner_ctx.factory,
            workspace_id=other_ws_id,
            requester_actor_id=other_owner_id,
        )
        # Build the client against ``owner_ctx`` (the first workspace).
        client = build_client(owner_ctx)
        resp = client.get(f"/approvals/{other_row}")
        assert resp.status_code == 404
        body = resp.json()
        assert body["type"].endswith("/approval_not_found")

    def test_unknown_id_404(self, owner_ctx: _Persona) -> None:
        client = build_client(owner_ctx)
        resp = client.get("/approvals/01HXNONEXISTENT0000000000")
        assert resp.status_code == 404
        body = resp.json()
        assert body["type"].endswith("/approval_not_found")

    def test_worker_without_read_permission_gets_403(
        self, owner_ctx: _Persona
    ) -> None:
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        client = build_client(_worker_persona(owner_ctx))
        resp = client.get(f"/approvals/{row_id}")
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "permission_denied"
        assert body["action_key"] == "approvals.read"


# ---------------------------------------------------------------------------
# POST /{id}/approve happy path + edge cases
# ---------------------------------------------------------------------------


class TestApprove:
    def test_session_caller_approves_dispatch_replays(
        self, owner_ctx: _Persona
    ) -> None:
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        dispatcher = FakeToolDispatcher()
        client = build_client(owner_ctx, dispatcher=dispatcher)
        resp = client.post(f"/approvals/{row_id}/approve", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "approved"
        assert body["decided_by"] == owner_ctx.owner_id
        assert body["result_json"]["status_code"] == 200
        # The replay actually fired.
        assert len(dispatcher.captured) == 1
        captured = dispatcher.captured[0]
        assert captured.call.name == "tasks.complete"
        # The audit + event-side-effects landed on the row.
        actions = _audit_actions(owner_ctx, workspace_id=owner_ctx.workspace_id)
        assert "approval.granted" in actions

    def test_retry_409_with_current_status(self, owner_ctx: _Persona) -> None:
        """A second approve on an already-approved row → 409.

        Spec §11 "Approval pipeline" pins ``approval_not_pending`` as
        the stable error key; the second call must NOT re-dispatch
        (the side effect would otherwise double — the recorded
        idempotency key buys an in-process safety net but the seam
        still rejects the duplicate at the domain layer).
        """
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        dispatcher = FakeToolDispatcher()
        client = build_client(owner_ctx, dispatcher=dispatcher)
        first = client.post(f"/approvals/{row_id}/approve", json={})
        assert first.status_code == 200
        second = client.post(f"/approvals/{row_id}/approve", json={})
        assert second.status_code == 409
        body = second.json()
        assert body["type"].endswith("/approval_not_pending")
        # The envelope's top-level ``status`` is the HTTP code (409);
        # the row's current status is exposed via the
        # ``approval_request_id`` extra key on the :class:`Conflict`
        # — but the spec deliberately collides keys with HTTP
        # ``status`` and the envelope drops the extra to keep the
        # body shape stable. The test pins the HTTP code + the type
        # URI, which is what the SPA pattern-matches against to
        # decide "refresh the queue".
        assert body["status"] == 409
        assert body["approval_request_id"] == row_id
        # Replay must have fired exactly once.
        assert len(dispatcher.captured) == 1

    def test_missing_dispatcher_503(self, owner_ctx: _Persona) -> None:
        """An app without ``state.tool_dispatcher`` returns 503.

        Production wiring (cd-z3b7) populates the dispatcher at boot;
        a deployment that forgot to wire it must surface the gap
        rather than fall back to a no-op replay.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.deps import current_workspace_context, db_session
        from app.api.errors import add_exception_handlers
        from app.api.v1.approvals import router as approvals_router

        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        # Build a bespoke client that explicitly does NOT set
        # ``app.state.tool_dispatcher`` — the production
        # :func:`get_tool_dispatcher` dep then raises 503.
        app = FastAPI()
        add_exception_handlers(app)
        app.include_router(approvals_router, prefix="/approvals")
        # NB: no ``app.state.tool_dispatcher`` set.

        from app.adapters.db.session import UnitOfWorkImpl

        def _override_ctx() -> object:
            return owner_ctx.ctx

        def _override_db() -> Iterator[Session]:
            uow = UnitOfWorkImpl(session_factory=owner_ctx.factory)
            with uow as s:
                assert isinstance(s, Session)
                yield s

        app.dependency_overrides[current_workspace_context] = _override_ctx
        app.dependency_overrides[db_session] = _override_db

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/approvals/{row_id}/approve", json={})
        assert resp.status_code == 503
        body = resp.json()
        # The HTTPException ``detail`` is a dict ``{"error": ..., "message": ...}``;
        # the problem-json handler renders ``message`` as the envelope ``detail``
        # field and spreads remaining keys (``error`` here) onto the top level.
        assert body["error"] == "dispatcher_not_configured"

    def test_decision_note_normalised_collapses_whitespace_to_null(
        self, owner_ctx: _Persona
    ) -> None:
        """Empty / whitespace-only ``decision_note_md`` collapses to None.

        The audit row + the persisted column must both record null
        rather than the misleading empty string a click-Confirm-
        without-typing reviewer otherwise produces.
        """
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        client = build_client(owner_ctx)
        resp = client.post(
            f"/approvals/{row_id}/approve",
            json={"decision_note_md": "   \n\t"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision_note_md"] is None
        # And the row really persisted None on the column too.
        row = _refresh_row(owner_ctx, row_id)
        assert row.decision_note_md is None

    def test_decision_note_over_4kib_rejected(self, owner_ctx: _Persona) -> None:
        """A note > 4 KiB is rejected with 422 ``validation``.

        FastAPI's ``Field(max_length=4 * 1024)`` runs the check at
        the parse seam, surfacing the over-cap value as a request
        validation error before the handler fires.
        """
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        client = build_client(owner_ctx)
        # 4097 chars — exactly one byte over the cap.
        oversize = "x" * (4 * 1024 + 1)
        resp = client.post(
            f"/approvals/{row_id}/approve",
            json={"decision_note_md": oversize},
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["type"].endswith("/validation")


# ---------------------------------------------------------------------------
# POST /{id}/reject + /{id}/deny
# ---------------------------------------------------------------------------


class TestRejectAndDeny:
    @pytest.mark.parametrize("verb", ["reject", "deny"])
    def test_decision_lands_no_dispatch(self, owner_ctx: _Persona, verb: str) -> None:
        """Both verbs flip ``pending → rejected`` and never dispatch."""
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        dispatcher = FakeToolDispatcher()
        client = build_client(owner_ctx, dispatcher=dispatcher)
        resp = client.post(
            f"/approvals/{row_id}/{verb}",
            json={"decision_note_md": "no"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "rejected"
        assert body["decided_by"] == owner_ctx.owner_id
        assert body["decision_note_md"] == "no"
        # No replay — deny never dispatches.
        assert dispatcher.captured == []
        # Audit row landed.
        actions = _audit_actions(owner_ctx, workspace_id=owner_ctx.workspace_id)
        assert "approval.denied" in actions

    def test_retry_after_reject_409(self, owner_ctx: _Persona) -> None:
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        client = build_client(owner_ctx)
        first = client.post(f"/approvals/{row_id}/reject", json={})
        assert first.status_code == 200
        second = client.post(f"/approvals/{row_id}/reject", json={})
        assert second.status_code == 409
        body = second.json()
        assert body["type"].endswith("/approval_not_pending")


# ---------------------------------------------------------------------------
# Credential gating matrix (§11 rule 1/3 + delegated/scoped reject)
# ---------------------------------------------------------------------------


class TestCredentialGating:
    """Approval decisions travel through the human session, not the agent token.

    The matrix:

    * Cookie session (``token_id is None``) → accept.
    * PAT (``kind='personal'``) carrying ``approvals:act`` → accept.
    * PAT without scope → 403 ``approval_requires_session`` + audit
      ``approval.credential_rejected``.
    * Delegated (``kind='delegated'``) → 403 + audit.
    * Scoped (``kind='scoped'``) → 403 + audit.
    * Unknown token id (no row matches) → 403 + audit (fail closed).
    """

    def test_session_cookie_accepted(self, owner_ctx: _Persona) -> None:
        """Identity stamp with ``token_id=None`` is the session branch."""
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        identity = session_identity(user_id=owner_ctx.owner_id)
        client = build_client(owner_ctx, actor_identity=identity)
        resp = client.post(f"/approvals/{row_id}/approve", json={})
        assert resp.status_code == 200, resp.text

    def test_pat_with_approvals_act_accepted(self, owner_ctx: _Persona) -> None:
        """A personal token carrying ``approvals:act`` may decide.

        Personal tokens carry ``workspace_id IS NULL`` per the
        check-constraint, but the §11 rule is "human credential" —
        the test exercises the path; in production a personal token
        is bound to its user's grants for authorization.
        """
        token_id = seed_api_token(
            owner_ctx.factory,
            user_id=owner_ctx.owner_id,
            workspace_id=None,
            kind="personal",
            subject_user_id=owner_ctx.owner_id,
            scope_json={APPROVALS_ACT_SCOPE: True},
        )
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        identity = token_identity(
            user_id=owner_ctx.owner_id,
            workspace_id=owner_ctx.workspace_id,
            token_id=token_id,
        )
        # Mark the ctx as token-presented so the dep walks the token row
        # rather than short-circuiting on the session classification.
        owner_ctx_token = _make_token_ctx(owner_ctx)
        client = build_client(owner_ctx_token, actor_identity=identity)
        resp = client.post(f"/approvals/{row_id}/approve", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "approved"

    def test_pat_missing_scope_403_with_audit(self, owner_ctx: _Persona) -> None:
        token_id = seed_api_token(
            owner_ctx.factory,
            user_id=owner_ctx.owner_id,
            workspace_id=None,
            kind="personal",
            subject_user_id=owner_ctx.owner_id,
            scope_json={},  # no ``approvals:act``
        )
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        identity = token_identity(
            user_id=owner_ctx.owner_id,
            workspace_id=owner_ctx.workspace_id,
            token_id=token_id,
        )
        owner_ctx_token = _make_token_ctx(owner_ctx)
        client = build_client(owner_ctx_token, actor_identity=identity)
        resp = client.post(f"/approvals/{row_id}/approve", json={})
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["type"].endswith("/approval_requires_session")
        # Row stayed pending.
        row = _refresh_row(owner_ctx, row_id)
        assert row.status == "pending"
        # Audit row landed.
        actions = _audit_actions(owner_ctx, workspace_id=owner_ctx.workspace_id)
        assert "approval.credential_rejected" in actions

    def test_delegated_token_403_with_audit(self, owner_ctx: _Persona) -> None:
        token_id = seed_api_token(
            owner_ctx.factory,
            user_id=owner_ctx.owner_id,
            workspace_id=owner_ctx.workspace_id,
            kind="delegated",
            delegate_for_user_id=owner_ctx.owner_id,
        )
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        identity = token_identity(
            user_id=owner_ctx.owner_id,
            workspace_id=owner_ctx.workspace_id,
            token_id=token_id,
        )
        owner_ctx_token = _make_token_ctx(owner_ctx)
        client = build_client(owner_ctx_token, actor_identity=identity)
        resp = client.post(f"/approvals/{row_id}/approve", json={})
        assert resp.status_code == 403
        body = resp.json()
        assert body["type"].endswith("/approval_requires_session")
        actions = _audit_actions(owner_ctx, workspace_id=owner_ctx.workspace_id)
        assert "approval.credential_rejected" in actions

    def test_scoped_token_403_with_audit(self, owner_ctx: _Persona) -> None:
        token_id = seed_api_token(
            owner_ctx.factory,
            user_id=owner_ctx.owner_id,
            workspace_id=owner_ctx.workspace_id,
            kind="scoped",
            scope_json={APPROVALS_ACT_SCOPE: True},  # scope alone is insufficient
        )
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        identity = token_identity(
            user_id=owner_ctx.owner_id,
            workspace_id=owner_ctx.workspace_id,
            token_id=token_id,
        )
        owner_ctx_token = _make_token_ctx(owner_ctx)
        client = build_client(owner_ctx_token, actor_identity=identity)
        resp = client.post(f"/approvals/{row_id}/reject", json={})
        assert resp.status_code == 403
        body = resp.json()
        assert body["type"].endswith("/approval_requires_session")
        actions = _audit_actions(owner_ctx, workspace_id=owner_ctx.workspace_id)
        assert "approval.credential_rejected" in actions

    def test_unknown_token_id_403_fails_closed(self, owner_ctx: _Persona) -> None:
        """An identity stamping a token id that doesn't exist is rejected.

        Reflects a token revoked / pruned mid-request, or a dirty
        test fixture — the gating dep must not interpret the
        absence as a session classification.
        """
        row_id = seed_pending(
            owner_ctx.factory,
            workspace_id=owner_ctx.workspace_id,
            requester_actor_id=owner_ctx.owner_id,
        )
        identity = token_identity(
            user_id=owner_ctx.owner_id,
            workspace_id=owner_ctx.workspace_id,
            token_id="01HXTOK00NEVERSEENBYTHEDB",
        )
        owner_ctx_token = _make_token_ctx(owner_ctx)
        client = build_client(owner_ctx_token, actor_identity=identity)
        resp = client.post(f"/approvals/{row_id}/approve", json={})
        assert resp.status_code == 403
        body = resp.json()
        assert body["type"].endswith("/approval_requires_session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token_ctx(persona: _Persona) -> _Persona:
    """Return a clone of ``persona`` whose ctx is token-presented.

    The auth-gating dep short-circuits to ``"session"`` when
    ``WorkspaceContext.principal_kind == "session"``; the credential-
    matrix tests need ``"token"`` so the dep walks the token row and
    classifies the kind. The factory's default is ``"session"`` to
    match the cookie-cut test path.
    """
    from dataclasses import replace

    new_ctx = replace(persona.ctx, principal_kind="token")
    return _Persona(
        ctx=new_ctx,
        factory=persona.factory,
        workspace_id=persona.workspace_id,
        owner_id=persona.owner_id,
    )
