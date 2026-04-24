"""Unit tests for :mod:`app.domain.identity.work_engagements` (cd-dcfw)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.domain.identity.work_engagements import (
    WorkEngagementInvariantViolated,
    WorkEngagementNotFound,
    WorkEngagementUpdate,
    archive_work_engagement,
    get_work_engagement,
    list_work_engagements,
    reinstate_work_engagement,
    update_work_engagement,
)
from app.util.clock import FrozenClock
from tests.unit.domain.identity.conftest import (
    attach_user,
    ctx_for,
    make_engagement,
    make_user,
    make_workspace,
)


def _audit_actions(session: Session, *, entity_id: str) -> list[str]:
    rows = list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    )
    return [r.action for r in rows]


class TestRead:
    def test_get_happy_path(self, session: Session) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(session, user_id=user.id, workspace_id=ws.id)

        view = get_work_engagement(session, ctx, engagement_id=eng.id)
        assert view.user_id == user.id
        assert view.engagement_kind == "payroll"

    def test_get_unknown_id_raises(self, session: Session) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)

        with pytest.raises(WorkEngagementNotFound):
            get_work_engagement(session, ctx, engagement_id="nope")

    def test_get_cross_workspace_collapses_to_404(self, session: Session) -> None:
        ws_a = make_workspace(session, slug="ws-a")
        ws_b = make_workspace(session, slug="ws-b")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws_a.id)
        attach_user(session, user_id=user.id, workspace_id=ws_b.id)
        eng_b = make_engagement(session, user_id=user.id, workspace_id=ws_b.id)

        with pytest.raises(WorkEngagementNotFound):
            get_work_engagement(
                session,
                ctx_for(workspace=ws_a, actor_id=user.id),
                engagement_id=eng_b.id,
            )


class TestList:
    def test_user_id_filter_narrows(self, session: Session) -> None:
        ws = make_workspace(session, slug="ws")
        alice = make_user(session, email="a@e.com", display_name="Alice")
        bob = make_user(session, email="b@e.com", display_name="Bob")
        attach_user(session, user_id=alice.id, workspace_id=ws.id)
        attach_user(session, user_id=bob.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=alice.id)
        make_engagement(session, user_id=alice.id, workspace_id=ws.id)
        make_engagement(session, user_id=bob.id, workspace_id=ws.id)

        alice_only = list(
            list_work_engagements(session, ctx, limit=50, user_id=alice.id)
        )
        assert len(alice_only) == 1
        assert alice_only[0].user_id == alice.id

    def test_include_archived_toggle(self, session: Session) -> None:
        from datetime import date

        ws = make_workspace(session, slug="ws")
        alice = make_user(session, email="a@e.com", display_name="Alice")
        bob = make_user(session, email="b@e.com", display_name="Bob")
        attach_user(session, user_id=alice.id, workspace_id=ws.id)
        attach_user(session, user_id=bob.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=alice.id)
        # Alice active; Bob archived.
        make_engagement(session, user_id=alice.id, workspace_id=ws.id)
        make_engagement(
            session,
            user_id=bob.id,
            workspace_id=ws.id,
            archived_on=date(2026, 3, 1),
        )

        with_archived = list(list_work_engagements(session, ctx, limit=50))
        without_archived = list(
            list_work_engagements(session, ctx, limit=50, include_archived=False)
        )
        assert len(with_archived) == 2
        assert len(without_archived) == 1
        assert without_archived[0].user_id == alice.id


class TestUpdate:
    def test_patch_notes_and_destination(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(session, user_id=user.id, workspace_id=ws.id)

        view = update_work_engagement(
            session,
            ctx,
            engagement_id=eng.id,
            body=WorkEngagementUpdate(
                notes_md="New note", pay_destination_id="dest_123"
            ),
            clock=clock,
        )
        assert view.notes_md == "New note"
        assert view.pay_destination_id == "dest_123"
        assert "work_engagement.updated" in _audit_actions(session, entity_id=eng.id)

    def test_switching_to_agency_requires_supplier(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """§02 biconditional: kind=agency_supplied without supplier_org_id raises."""
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(session, user_id=user.id, workspace_id=ws.id)

        with pytest.raises(WorkEngagementInvariantViolated):
            update_work_engagement(
                session,
                ctx,
                engagement_id=eng.id,
                body=WorkEngagementUpdate(engagement_kind="agency_supplied"),
                clock=clock,
            )

    def test_switch_kind_with_supplier_in_same_patch_ok(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """A single PATCH carrying both fields merges and validates together."""
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(session, user_id=user.id, workspace_id=ws.id)

        view = update_work_engagement(
            session,
            ctx,
            engagement_id=eng.id,
            body=WorkEngagementUpdate(
                engagement_kind="agency_supplied", supplier_org_id="org_abc"
            ),
            clock=clock,
        )
        assert view.engagement_kind == "agency_supplied"
        assert view.supplier_org_id == "org_abc"

    def test_non_agency_with_supplier_rejected(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """Reverse direction: engagement_kind != agency_supplied must clear supplier."""
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            kind="agency_supplied",
            supplier_org_id="org_abc",
        )

        with pytest.raises(WorkEngagementInvariantViolated):
            update_work_engagement(
                session,
                ctx,
                engagement_id=eng.id,
                body=WorkEngagementUpdate(engagement_kind="payroll"),
                clock=clock,
            )

    def test_dto_rejects_notes_md_null(self) -> None:
        with pytest.raises(ValueError):
            WorkEngagementUpdate.model_validate({"notes_md": None})

    def test_empty_patch_is_noop(self, session: Session, clock: FrozenClock) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(session, user_id=user.id, workspace_id=ws.id)

        view = update_work_engagement(
            session, ctx, engagement_id=eng.id, body=WorkEngagementUpdate(), clock=clock
        )
        assert view.notes_md == ""
        assert _audit_actions(session, entity_id=eng.id) == []


class TestArchive:
    def test_archive_happy_path(self, session: Session, clock: FrozenClock) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(session, user_id=user.id, workspace_id=ws.id)

        view = archive_work_engagement(session, ctx, engagement_id=eng.id, clock=clock)
        assert view.archived_on is not None
        assert "work_engagement.archived" in _audit_actions(session, entity_id=eng.id)

    def test_archive_is_idempotent(self, session: Session, clock: FrozenClock) -> None:
        """Repeat call on an already-archived row is a DB no-op but writes audit."""
        from datetime import date

        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            archived_on=date(2026, 3, 1),
        )
        archive_work_engagement(session, ctx, engagement_id=eng.id, clock=clock)
        # DB state preserves the original archived_on (not stamped again).
        assert eng.archived_on == date(2026, 3, 1)
        # Audit row still recorded.
        assert "work_engagement.archived" in _audit_actions(session, entity_id=eng.id)

    def test_archive_unknown_id_raises(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)

        with pytest.raises(WorkEngagementNotFound):
            archive_work_engagement(session, ctx, engagement_id="unknown", clock=clock)


class TestReinstate:
    def test_reinstate_happy_path(self, session: Session, clock: FrozenClock) -> None:
        from datetime import date

        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            archived_on=date(2026, 3, 1),
        )

        view = reinstate_work_engagement(
            session, ctx, engagement_id=eng.id, clock=clock
        )
        assert view.archived_on is None
        assert "work_engagement.reinstated" in _audit_actions(session, entity_id=eng.id)

    def test_reinstate_on_active_engagement_is_noop(
        self, session: Session, clock: FrozenClock
    ) -> None:
        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        eng = make_engagement(session, user_id=user.id, workspace_id=ws.id)

        view = reinstate_work_engagement(
            session, ctx, engagement_id=eng.id, clock=clock
        )
        assert view.archived_on is None
        # Audit row is still written so the trail is linear even on no-op.
        assert "work_engagement.reinstated" in _audit_actions(session, entity_id=eng.id)

    def test_reinstate_blocked_by_existing_active_engagement(
        self, session: Session, clock: FrozenClock
    ) -> None:
        """Reinstating when a second active engagement exists must 422, not 500.

        The partial UNIQUE index ``(user_id, workspace_id) WHERE
        archived_on IS NULL`` would reject the INSERT at flush time;
        surfacing that as the generic ``IntegrityError`` 500 hides the
        actual remedy. The service checks pre-flush and raises a
        typed :class:`WorkEngagementInvariantViolated` so the HTTP
        layer can map it to a 422 with a clear message.
        """
        from datetime import date

        ws = make_workspace(session, slug="ws")
        user = make_user(session, email="u@e.com", display_name="U")
        attach_user(session, user_id=user.id, workspace_id=ws.id)
        ctx = ctx_for(workspace=ws, actor_id=user.id)
        archived = make_engagement(
            session,
            user_id=user.id,
            workspace_id=ws.id,
            archived_on=date(2026, 3, 1),
        )
        # A second, currently-active engagement for the same user —
        # the partial UNIQUE would trip if we reinstated ``archived``.
        make_engagement(session, user_id=user.id, workspace_id=ws.id)

        with pytest.raises(WorkEngagementInvariantViolated):
            reinstate_work_engagement(
                session, ctx, engagement_id=archived.id, clock=clock
            )

        # DB row is untouched — no half-applied state.
        session.refresh(archived)
        assert archived.archived_on == date(2026, 3, 1)
