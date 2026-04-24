"""Unit tests for :mod:`app.domain.tasks.comments` (cd-cfe4).

Mirrors the in-memory SQLite bootstrap in
``tests/unit/tasks/test_completion.py`` — fresh engine per test, load
every sibling ``models`` module onto the shared metadata, run
``create_all``, drive the service with :class:`FrozenClock` and a
private :class:`EventBus` so subscriptions don't leak between tests.

Covers:

* :func:`post_comment` happy path for ``kind='user'`` — persists body,
  mentions, attachments; fires :class:`TaskCommentAdded`; writes
  ``task_comment.create`` audit row.
* :func:`post_comment` ``kind='agent'`` allowed with
  ``ctx.actor_kind='agent'``; refused otherwise.
* :func:`post_comment` ``kind='system'`` allowed with
  ``internal_caller=True``; refused otherwise.
* Mention of non-member → :class:`CommentMentionInvalid` (422).
* Attachments: valid evidence ids persist denormalised; unknown or
  cross-workspace ids → :class:`CommentAttachmentInvalid` (422).
* :func:`edit_comment`: author within window → ok; outside window →
  :class:`CommentEditWindowExpired`; other-user → 403;
  agent / system / deleted rows never editable.
* :func:`delete_comment`: author → ok; non-author non-owner → 403;
  owner moderator → ok; already-deleted → 409; soft-deleted rows
  hidden from non-owner list views.
* :func:`list_comments`: oldest-first ordering, ``after`` cursor,
  owner sees soft-deleted, non-owner does not.
* Personal-task gate: non-creator non-owner gets
  :class:`CommentNotFound` on every entry point.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Task notes are the
agent inbox".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.places.models import Property
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import (
    Comment,
    Evidence,
    Occurrence,
)
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.domain.tasks.comments import (
    EDIT_WINDOW,
    CommentAttachmentInvalid,
    CommentCreate,
    CommentEditWindowExpired,
    CommentKindForbidden,
    CommentMentionAmbiguous,
    CommentMentionInvalid,
    CommentNotEditable,
    CommentNotFound,
    delete_comment,
    edit_comment,
    get_comment,
    list_comments,
    post_comment,
)
from app.events.bus import EventBus
from app.events.types import TaskCommentAdded
from app.tenancy.context import ActorGrantRole, ActorKind, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures + bootstrap
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


def _ctx(
    workspace_id: str,
    *,
    slug: str = "ws",
    role: ActorGrantRole = "manager",
    owner: bool = True,
    actor_id: str | None = None,
    actor_kind: ActorKind = "user",
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id if actor_id is not None else new_ulid(),
        actor_kind=actor_kind,
        actor_grant_role=role,
        actor_was_owner_member=owner,
        audit_correlation_id=new_ulid(),
    )


def _bootstrap_workspace(session: Session, *, slug: str = "ws") -> str:
    workspace_id = new_ulid()
    session.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    session.flush()
    return workspace_id


def _bootstrap_property(session: Session) -> str:
    pid = new_ulid()
    session.add(
        Property(
            id=pid,
            address="1 Villa Sud Way",
            timezone="Europe/Paris",
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return pid


def _bootstrap_user(
    session: Session,
    *,
    workspace_id: str | None = None,
    display_name: str | None = None,
) -> str:
    """Insert a user row + optional ``user_workspace`` membership."""
    from app.adapters.db.identity.models import User

    uid = new_ulid()
    name = display_name or uid
    session.add(
        User(
            id=uid,
            email=f"{uid}@example.com",
            email_lower=f"{uid}@example.com".lower(),
            display_name=name,
            locale=None,
            timezone=None,
            avatar_blob_hash=None,
            created_at=_PINNED,
            last_login_at=None,
        )
    )
    if workspace_id is not None:
        session.add(
            UserWorkspace(
                user_id=uid,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=_PINNED,
            )
        )
    session.flush()
    return uid


def _bootstrap_occurrence(
    session: Session,
    *,
    workspace_id: str,
    property_id: str | None,
    assignee_user_id: str | None = None,
    state: str = "pending",
    is_personal: bool = False,
    created_by_user_id: str | None = None,
) -> str:
    oid = new_ulid()
    session.add(
        Occurrence(
            id=oid,
            workspace_id=workspace_id,
            schedule_id=None,
            template_id=None,
            property_id=property_id,
            assignee_user_id=assignee_user_id,
            starts_at=_PINNED,
            ends_at=_PINNED + timedelta(minutes=30),
            scheduled_for_local="2026-04-19T14:00",
            originally_scheduled_for="2026-04-19T14:00",
            state=state,
            cancellation_reason=None,
            title="Pool clean",
            description_md="",
            priority="normal",
            photo_evidence="disabled",
            duration_minutes=30,
            area_id=None,
            unit_id=None,
            expected_role_id=None,
            linked_instruction_ids=[],
            inventory_consumption_json={},
            is_personal=is_personal,
            created_by_user_id=created_by_user_id,
            created_at=_PINNED,
        )
    )
    session.flush()
    return oid


def _bootstrap_evidence(
    session: Session,
    *,
    workspace_id: str,
    occurrence_id: str,
    kind: str = "photo",
) -> str:
    eid = new_ulid()
    session.add(
        Evidence(
            id=eid,
            workspace_id=workspace_id,
            occurrence_id=occurrence_id,
            kind=kind,
            blob_hash=f"blob-{eid}",
            note_md=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    session.flush()
    return eid


def _record(bus: EventBus) -> list[TaskCommentAdded]:
    captured: list[TaskCommentAdded] = []
    bus.subscribe(TaskCommentAdded)(captured.append)
    return captured


# ---------------------------------------------------------------------------
# post_comment — happy paths
# ---------------------------------------------------------------------------


class TestPostCommentUser:
    """``post_comment`` with ``kind='user'``."""

    def test_happy_path_persists_body_and_audit(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws, display_name="Maya")
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        captured = _record(bus)

        view = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="All set."),
            clock=clock,
            event_bus=bus,
        )

        assert view.kind == "user"
        assert view.author_user_id == author
        assert view.body_md == "All set."
        assert view.mentioned_user_ids == ()
        assert view.attachments == ()
        row = session.get(Comment, view.id)
        assert row is not None
        assert row.body_md == "All set."

        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).all()
        assert [a.action for a in audits] == ["task_comment.create"]
        assert audits[0].diff["after"]["body_md"] == "All set."

        assert len(captured) == 1
        assert captured[0].task_id == occ
        assert captured[0].comment_id == view.id
        assert captured[0].kind == "user"
        assert captured[0].author_user_id == author
        assert captured[0].mentioned_user_ids == []

    def test_mention_resolves_to_user_id(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws, display_name="Author")
        maya = _bootstrap_user(session, workspace_id=ws, display_name="Maya")
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        view = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="Hey @maya, filter replaced."),
            clock=clock,
            event_bus=bus,
        )

        assert view.mentioned_user_ids == (maya,)
        assert "@maya" in view.body_md  # textual form preserved

    def test_mention_of_non_member_rejected_422(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws, display_name="Author")
        # Insert a user in a DIFFERENT workspace so the slug exists
        # but the membership doesn't.
        other_ws = _bootstrap_workspace(session, slug="other")
        _bootstrap_user(session, workspace_id=other_ws, display_name="Stranger")
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        with pytest.raises(CommentMentionInvalid) as excinfo:
            post_comment(
                session,
                ctx,
                occ,
                CommentCreate(body_md="cc @stranger"),
                clock=clock,
                event_bus=bus,
            )
        assert "stranger" in excinfo.value.unknown_slugs

    def test_mention_dedupe_and_order_preserved(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws, display_name="Author")
        maya = _bootstrap_user(session, workspace_id=ws, display_name="Maya")
        alex = _bootstrap_user(session, workspace_id=ws, display_name="Alex")
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        view = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="cc @maya and @alex — also again @maya"),
            clock=clock,
            event_bus=bus,
        )

        assert list(view.mentioned_user_ids) == [maya, alex]

    def test_email_shaped_mention_does_not_match(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """``email@foo.com`` must NOT hit the ``@foo`` mention regex."""
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws, display_name="Author")
        _bootstrap_user(session, workspace_id=ws, display_name="foo")
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        view = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="email me at author@foo.com"),
            clock=clock,
            event_bus=bus,
        )
        assert view.mentioned_user_ids == ()

    def test_ambiguous_mention_rejected_422(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """Two workspace members whose display names normalise to the
        same slug must surface :class:`CommentMentionAmbiguous` — the
        §10 offline-mention fanout cannot be allowed to silently pick
        one of them.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws, display_name="Author")
        # Two display names that both normalise to ``maya`` via
        # ``_normalise_slug`` (lowercase + alphanumeric + ``-``/``_``).
        _bootstrap_user(session, workspace_id=ws, display_name="Maya")
        _bootstrap_user(session, workspace_id=ws, display_name="MAYA")
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        with pytest.raises(CommentMentionAmbiguous) as excinfo:
            post_comment(
                session,
                ctx,
                occ,
                CommentCreate(body_md="cc @maya"),
                clock=clock,
                event_bus=bus,
            )
        assert "maya" in excinfo.value.ambiguous_slugs


# ---------------------------------------------------------------------------
# post_comment — kind gates
# ---------------------------------------------------------------------------


class TestPostCommentAgentKind:
    """``kind='agent'`` requires ``ctx.actor_kind='agent'``."""

    def test_agent_allowed_with_agent_actor_kind(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        agent = _bootstrap_user(session, workspace_id=ws, display_name="agent")
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)
        ctx = _ctx(ws, role="worker", owner=False, actor_id=agent, actor_kind="agent")

        view = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="I summarised the thread."),
            kind="agent",
            llm_call_id="llm-call-1",
            clock=clock,
            event_bus=bus,
        )

        assert view.kind == "agent"
        assert view.author_user_id == agent
        assert view.llm_call_id == "llm-call-1"
        # Agent rows skip mention resolution — the body may reference
        # users but nothing lands in mentioned_user_ids.
        assert view.mentioned_user_ids == ()

    def test_agent_refused_for_non_agent_caller(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        user = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)
        # actor_kind defaults to 'user'
        ctx = _ctx(ws, role="manager", owner=True, actor_id=user)

        with pytest.raises(CommentKindForbidden):
            post_comment(
                session,
                ctx,
                occ,
                CommentCreate(body_md="Pretending to be the agent"),
                kind="agent",
                clock=clock,
                event_bus=bus,
            )


class TestPostCommentSystemKind:
    """``kind='system'`` is internal-only."""

    def test_system_allowed_via_internal_caller(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        user = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)
        ctx = _ctx(ws, role="manager", owner=True, actor_id=user)

        view = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="Task marked done by Maya at 14:02"),
            kind="system",
            internal_caller=True,
            clock=clock,
            event_bus=bus,
        )

        assert view.kind == "system"
        # System rows carry NULL author — the audit log is the
        # canonical "who did this" record.
        assert view.author_user_id is None

    def test_system_refused_externally(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        user = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)
        ctx = _ctx(ws, role="manager", owner=True, actor_id=user)

        with pytest.raises(CommentKindForbidden):
            post_comment(
                session,
                ctx,
                occ,
                CommentCreate(body_md="Forged system marker"),
                kind="system",
                # internal_caller omitted
                clock=clock,
                event_bus=bus,
            )


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


class TestPostCommentAttachments:
    """``payload.attachments`` round-trips through evidence ids."""

    def test_valid_attachment_persists_denormalised(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        eid = _bootstrap_evidence(session, workspace_id=ws, occurrence_id=occ)
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        view = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="See photo", attachments=[eid]),
            clock=clock,
            event_bus=bus,
        )

        assert len(view.attachments) == 1
        attachment = view.attachments[0]
        assert attachment["evidence_id"] == eid
        assert attachment["kind"] == "photo"
        assert attachment["blob_hash"] == f"blob-{eid}"

    def test_unknown_attachment_rejected_422(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        with pytest.raises(CommentAttachmentInvalid) as excinfo:
            post_comment(
                session,
                ctx,
                occ,
                CommentCreate(body_md="body", attachments=["ev-missing"]),
                clock=clock,
                event_bus=bus,
            )
        assert "ev-missing" in excinfo.value.unknown_ids

    def test_cross_workspace_attachment_rejected_422(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="a")
        ws_b = _bootstrap_workspace(session, slug="b")
        prop_a = _bootstrap_property(session)
        prop_b = _bootstrap_property(session)
        author_a = _bootstrap_user(session, workspace_id=ws_a)
        occ_a = _bootstrap_occurrence(
            session, workspace_id=ws_a, property_id=prop_a, assignee_user_id=author_a
        )
        occ_b = _bootstrap_occurrence(session, workspace_id=ws_b, property_id=prop_b)
        foreign = _bootstrap_evidence(session, workspace_id=ws_b, occurrence_id=occ_b)
        ctx_a = _ctx(ws_a, role="worker", owner=False, actor_id=author_a)

        with pytest.raises(CommentAttachmentInvalid):
            post_comment(
                session,
                ctx_a,
                occ_a,
                CommentCreate(body_md="cross-ws", attachments=[foreign]),
                clock=clock,
                event_bus=bus,
            )

    def test_cross_occurrence_attachment_rejected_422(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """An evidence id anchored to a DIFFERENT occurrence in the
        same workspace is still rejected — the gate is per-occurrence,
        not per-workspace.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ_a = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        occ_b = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ev_b = _bootstrap_evidence(session, workspace_id=ws, occurrence_id=occ_b)
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        with pytest.raises(CommentAttachmentInvalid):
            post_comment(
                session,
                ctx,
                occ_a,
                CommentCreate(body_md="other task", attachments=[ev_b]),
                clock=clock,
                event_bus=bus,
            )


# ---------------------------------------------------------------------------
# edit_comment
# ---------------------------------------------------------------------------


class TestEditComment:
    """Author-only edit within the 5-minute grace window."""

    def _seed(
        self,
        session: Session,
        clock: FrozenClock,
        bus: EventBus,
    ) -> tuple[str, str, WorkspaceContext, Any]:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        view = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="original"),
            clock=clock,
            event_bus=bus,
        )
        return ws, author, ctx, view

    def test_author_within_window_succeeds(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        _, _, ctx, view = self._seed(session, clock, bus)
        # Advance inside the window.
        clock.advance(timedelta(minutes=2))
        updated = edit_comment(session, ctx, view.id, "edited body", clock=clock)
        assert updated.body_md == "edited body"
        assert updated.edited_at is not None
        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).all()
        actions = [a.action for a in audits]
        assert actions == ["task_comment.create", "task_comment.edit"]

    def test_outside_window_rejected_409(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        _, _, ctx, view = self._seed(session, clock, bus)
        clock.advance(EDIT_WINDOW + timedelta(seconds=1))
        with pytest.raises(CommentEditWindowExpired):
            edit_comment(session, ctx, view.id, "too late", clock=clock)

    def test_other_user_rejected_403(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws, _author, _author_ctx, view = self._seed(session, clock, bus)
        outsider = _bootstrap_user(session, workspace_id=ws)
        outsider_ctx = _ctx(ws, role="worker", owner=False, actor_id=outsider)
        with pytest.raises(CommentKindForbidden):
            edit_comment(session, outsider_ctx, view.id, "hijack", clock=clock)

    def test_agent_row_never_editable(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        agent = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)
        agent_ctx = _ctx(
            ws, role="worker", owner=False, actor_id=agent, actor_kind="agent"
        )
        view = post_comment(
            session,
            agent_ctx,
            occ,
            CommentCreate(body_md="agent reply"),
            kind="agent",
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(CommentNotEditable):
            edit_comment(session, agent_ctx, view.id, "amended", clock=clock)

    def test_system_row_never_editable(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        user = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(session, workspace_id=ws, property_id=prop)
        ctx = _ctx(ws, role="manager", owner=True, actor_id=user)
        view = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="marker"),
            kind="system",
            internal_caller=True,
            clock=clock,
            event_bus=bus,
        )
        with pytest.raises(CommentNotEditable):
            edit_comment(session, ctx, view.id, "amended", clock=clock)

    def test_deleted_row_not_editable(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        _, _, ctx, view = self._seed(session, clock, bus)
        delete_comment(session, ctx, view.id, clock=clock)
        with pytest.raises(CommentNotEditable):
            edit_comment(session, ctx, view.id, "amended", clock=clock)


# ---------------------------------------------------------------------------
# delete_comment
# ---------------------------------------------------------------------------


class TestDeleteComment:
    """Author or owner soft-deletes the row."""

    def test_author_can_delete_own(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        view = post_comment(
            session, ctx, occ, CommentCreate(body_md="oops"), clock=clock, event_bus=bus
        )
        deleted = delete_comment(session, ctx, view.id, clock=clock)
        assert deleted.deleted_at is not None
        audits = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).all()
        actions = [a.action for a in audits]
        assert actions == ["task_comment.create", "task_comment.delete"]

    def test_owner_can_delete_others(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        author_ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        view = post_comment(
            session,
            author_ctx,
            occ,
            CommentCreate(body_md="rude"),
            clock=clock,
            event_bus=bus,
        )
        owner_ctx = _ctx(ws, role="manager", owner=True)
        deleted = delete_comment(session, owner_ctx, view.id, clock=clock)
        assert deleted.deleted_at is not None

    def test_non_author_non_owner_rejected_403(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        stranger = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        author_ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        view = post_comment(
            session,
            author_ctx,
            occ,
            CommentCreate(body_md="note"),
            clock=clock,
            event_bus=bus,
        )
        stranger_ctx = _ctx(ws, role="worker", owner=False, actor_id=stranger)
        with pytest.raises(CommentKindForbidden):
            delete_comment(session, stranger_ctx, view.id, clock=clock)

    def test_already_deleted_409(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        view = post_comment(
            session, ctx, occ, CommentCreate(body_md="x"), clock=clock, event_bus=bus
        )
        delete_comment(session, ctx, view.id, clock=clock)
        with pytest.raises(CommentNotEditable):
            delete_comment(session, ctx, view.id, clock=clock)

    def test_manager_with_role_grant_can_moderate(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """A non-author, non-owner caller with a ``grant_role='manager'``
        row flows through :func:`app.authz.require` on
        ``tasks.comment_moderate`` and is allowed by the catalog's
        default_allow (``owners, managers``). Exercises the
        defence-in-depth branch added by selfreview against cd-cfe4.
        """
        from app.adapters.db.authz.models import RoleGrant

        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        manager = _bootstrap_user(session, workspace_id=ws)
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=ws,
                user_id=manager,
                grant_role="manager",
                scope_property_id=None,
                created_at=_PINNED,
                created_by_user_id=None,
            )
        )
        session.flush()
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        author_ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        view = post_comment(
            session,
            author_ctx,
            occ,
            CommentCreate(body_md="chatter"),
            clock=clock,
            event_bus=bus,
        )
        # Manager is NOT a workspace owner (owner=False on the ctx)
        # and NOT the author — the only path through the service is
        # the ``require()`` call on ``tasks.comment_moderate``.
        manager_ctx = _ctx(ws, role="manager", owner=False, actor_id=manager)
        deleted = delete_comment(session, manager_ctx, view.id, clock=clock)
        assert deleted.deleted_at is not None


# ---------------------------------------------------------------------------
# list_comments
# ---------------------------------------------------------------------------


class TestListComments:
    """Pagination, ordering, soft-delete visibility."""

    def test_ordered_oldest_first(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        v1 = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="first"),
            clock=clock,
            event_bus=bus,
        )
        clock.advance(timedelta(seconds=30))
        v2 = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="second"),
            clock=clock,
            event_bus=bus,
        )
        clock.advance(timedelta(seconds=30))
        v3 = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="third"),
            clock=clock,
            event_bus=bus,
        )

        listing = list_comments(session, ctx, occ)
        assert [v.id for v in listing] == [v1.id, v2.id, v3.id]

    def test_after_cursor_narrows_result(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        v1 = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="first"),
            clock=clock,
            event_bus=bus,
        )
        clock.advance(timedelta(seconds=30))
        v2 = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="second"),
            clock=clock,
            event_bus=bus,
        )

        listing = list_comments(session, ctx, occ, after=v1.created_at)
        assert [v.id for v in listing] == [v2.id]

    def test_soft_deleted_hidden_for_non_owner_visible_to_owner(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        author_ctx = _ctx(ws, role="worker", owner=False, actor_id=author)

        live = post_comment(
            session,
            author_ctx,
            occ,
            CommentCreate(body_md="live"),
            clock=clock,
            event_bus=bus,
        )
        clock.advance(timedelta(seconds=10))
        deleted = post_comment(
            session,
            author_ctx,
            occ,
            CommentCreate(body_md="to be removed"),
            clock=clock,
            event_bus=bus,
        )
        delete_comment(session, author_ctx, deleted.id, clock=clock)

        non_owner_listing = list_comments(session, author_ctx, occ)
        assert [v.id for v in non_owner_listing] == [live.id]

        owner_ctx = _ctx(ws, role="manager", owner=True)
        owner_listing = list_comments(session, owner_ctx, occ)
        assert sorted(v.id for v in owner_listing) == sorted([live.id, deleted.id])

    def test_zero_limit_rejected(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        with pytest.raises(ValueError):
            list_comments(session, ctx, occ, limit=0)

    def test_after_id_without_after_rejected(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """``after_id`` alone is a malformed cursor — pairs only."""
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        with pytest.raises(ValueError):
            list_comments(session, ctx, occ, after_id="01HWA00000000000000000CMX")

    def test_cursor_tie_breaks_on_id_when_clock_ticks_collide(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        """Two comments sharing the same ``created_at`` must paginate
        in ULID order when ``(after, after_id)`` is passed — without
        the tuple cursor, the second comment is silently skipped.
        """
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        author = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws, property_id=prop, assignee_user_id=author
        )
        ctx = _ctx(ws, role="worker", owner=False, actor_id=author)
        # Same clock tick for both posts — simulates an agent batch
        # writing several messages inside one APScheduler second.
        v1 = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="tick-a"),
            clock=clock,
            event_bus=bus,
        )
        v2 = post_comment(
            session,
            ctx,
            occ,
            CommentCreate(body_md="tick-b"),
            clock=clock,
            event_bus=bus,
        )
        # ULIDs are lexicographically time-ordered; within a single
        # millisecond the random tail determines order. Normalise
        # the expected pair by the id ordering the DB will return.
        first, second = sorted([v1, v2], key=lambda v: v.id)
        # Full listing carries both in (created_at, id) order.
        listing = list_comments(session, ctx, occ)
        assert [v.id for v in listing] == [first.id, second.id]
        # Cursor after the first row must still surface the second
        # — the plain ``created_at > after`` predicate would drop it.
        next_page = list_comments(
            session, ctx, occ, after=first.created_at, after_id=first.id
        )
        assert [v.id for v in next_page] == [second.id]


# ---------------------------------------------------------------------------
# Personal-task visibility
# ---------------------------------------------------------------------------


class TestPersonalTaskGate:
    """§06 "Self-created and personal tasks" visibility."""

    def test_non_creator_non_owner_gets_404(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        creator = _bootstrap_user(session, workspace_id=ws)
        stranger = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            is_personal=True,
            created_by_user_id=creator,
        )
        creator_ctx = _ctx(ws, role="worker", owner=False, actor_id=creator)
        # Creator can post:
        view = post_comment(
            session,
            creator_ctx,
            occ,
            CommentCreate(body_md="private note"),
            clock=clock,
            event_bus=bus,
        )

        # Stranger (workspace member but not creator, not owner) gets 404
        # on every read surface.
        stranger_ctx = _ctx(ws, role="worker", owner=False, actor_id=stranger)
        with pytest.raises(CommentNotFound):
            list_comments(session, stranger_ctx, occ)
        with pytest.raises(CommentNotFound):
            get_comment(session, stranger_ctx, view.id)
        with pytest.raises(CommentNotFound):
            post_comment(
                session,
                stranger_ctx,
                occ,
                CommentCreate(body_md="sneaky"),
                clock=clock,
                event_bus=bus,
            )

    def test_owner_sees_personal_task(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws = _bootstrap_workspace(session)
        prop = _bootstrap_property(session)
        creator = _bootstrap_user(session, workspace_id=ws)
        occ = _bootstrap_occurrence(
            session,
            workspace_id=ws,
            property_id=prop,
            is_personal=True,
            created_by_user_id=creator,
        )
        creator_ctx = _ctx(ws, role="worker", owner=False, actor_id=creator)
        view = post_comment(
            session,
            creator_ctx,
            occ,
            CommentCreate(body_md="private"),
            clock=clock,
            event_bus=bus,
        )
        owner_ctx = _ctx(ws, role="manager", owner=True)
        fetched = get_comment(session, owner_ctx, view.id)
        assert fetched.id == view.id


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    """Cross-workspace loads collapse to :class:`CommentNotFound`."""

    def test_other_workspace_comment_is_404(
        self, session: Session, clock: FrozenClock, bus: EventBus
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="a")
        ws_b = _bootstrap_workspace(session, slug="b")
        prop = _bootstrap_property(session)
        author_a = _bootstrap_user(session, workspace_id=ws_a)
        occ = _bootstrap_occurrence(
            session, workspace_id=ws_a, property_id=prop, assignee_user_id=author_a
        )
        ctx_a = _ctx(ws_a, role="worker", owner=False, actor_id=author_a)
        view = post_comment(
            session,
            ctx_a,
            occ,
            CommentCreate(body_md="tenant a"),
            clock=clock,
            event_bus=bus,
        )
        ctx_b = _ctx(ws_b, role="manager", owner=True)
        with pytest.raises(CommentNotFound):
            get_comment(session, ctx_b, view.id)
