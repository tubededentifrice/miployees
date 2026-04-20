"""Unit tests for :mod:`app.domain.time.shifts`.

Exercises the service surface against an in-memory SQLite engine
built via ``Base.metadata.create_all()`` — no alembic, no tenant
filter, just the ORM round-trip + the pure-Python DTO validators and
authz seam.

Covers:

* Error-class hierarchy: 404 → ``LookupError``, 409 →
  ``ShiftAlreadyOpen`` (a ``ValueError``), 422 →
  ``ShiftBoundaryInvalid`` (a ``ValueError``), 403 →
  ``ShiftEditForbidden`` (a ``PermissionError``).
* DTO shape: ``extra="forbid"`` rejects unknown fields; the
  enum narrows ``source`` to the DB CHECK set.
* :func:`open_shift`: happy path, defaults ``starts_at`` to the
  clock, rejects a second open while one is live, worker opening
  for another user without ``time.edit_others`` → 403, owner /
  manager can open a retroactive shift for someone else.
* :func:`close_shift`: happy path, defaults ``ends_at`` to clock;
  worker can close their own shift; worker cannot close someone
  else's (403); manager can; negative window rejected with 422;
  idempotent re-close on an already-closed shift.
* :func:`edit_shift`: manager-only; PATCH semantics (omitted
  fields untouched); rejects zero-length window.
* :func:`list_open_shifts` + :func:`list_shifts`: tenant filter
  isolates workspaces; optional filters narrow correctly.
* Audit row is written on every mutation with the right action
  label and diff shape.
* SSE: :class:`ShiftChanged` event fires on every mutation; a
  captured handler observes the right ``action`` + ``shift_id``.

The integration shard (``tests/integration/test_shifts_api.py``)
covers the HTTP boundary + tenant-filter under the real ORM.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_engine
from app.adapters.db.time.models import Shift
from app.adapters.db.workspace.models import Workspace
from app.domain.time.shifts import (
    ShiftAlreadyOpen,
    ShiftBoundaryInvalid,
    ShiftClose,
    ShiftEdit,
    ShiftEditForbidden,
    ShiftNotFound,
    ShiftOpen,
    ShiftView,
    close_shift,
    edit_shift,
    get_shift,
    list_open_shifts,
    list_shifts,
    open_shift,
)
from app.events import ShiftChanged, bus, get_event_type
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve.

    Mirrors the discovery loop in ``tests/unit/test_tasks_templates.py`` —
    without every context's models on the shared ``Base.metadata``,
    ``Base.metadata.create_all()`` raises
    :class:`~sqlalchemy.exc.NoReferencedTableError`.
    """
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
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Fresh session per test; no tenant filter installed."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture(autouse=True)
def reset_bus() -> Iterator[None]:
    """Drop every subscription between tests so captures don't bleed."""
    yield
    bus._reset_for_tests()


def _bootstrap_workspace(s: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(s: Session, *, email: str, display_name: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=display_name,
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _grant(s: Session, *, workspace_id: str, user_id: str, grant_role: str) -> None:
    """Insert a workspace-scope ``role_grant`` row for ``user_id``."""
    s.add(
        RoleGrant(
            id=new_ulid(),
            workspace_id=workspace_id,
            user_id=user_id,
            grant_role=grant_role,
            scope_property_id=None,
            created_at=_PINNED,
            created_by_user_id=None,
        )
    )
    s.flush()


def _ctx(
    *,
    workspace_id: str,
    actor_id: str,
    slug: str = "ws",
    grant_role: ActorGrantRole = "worker",
    was_owner: bool = False,
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role=grant_role,
        actor_was_owner_member=was_owner,
        audit_correlation_id=new_ulid(),
    )


@pytest.fixture
def worker_env(
    session: Session,
) -> tuple[WorkspaceContext, str, FrozenClock]:
    """Worker with ``all_workers`` membership via a ``worker`` grant."""
    ws_id = _bootstrap_workspace(session, slug="worker-env")
    user_id = _bootstrap_user(session, email="w@example.com", display_name="W")
    _grant(session, workspace_id=ws_id, user_id=user_id, grant_role="worker")
    session.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="worker")
    return ctx, user_id, FrozenClock(_PINNED)


@pytest.fixture
def manager_env(
    session: Session,
) -> tuple[WorkspaceContext, str, FrozenClock]:
    """Manager with ``managers`` membership via a ``manager`` grant."""
    ws_id = _bootstrap_workspace(session, slug="manager-env")
    user_id = _bootstrap_user(session, email="m@example.com", display_name="M")
    _grant(session, workspace_id=ws_id, user_id=user_id, grant_role="manager")
    session.commit()
    ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="manager")
    return ctx, user_id, FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrorTypes:
    def test_not_found_is_lookup_error(self) -> None:
        assert issubclass(ShiftNotFound, LookupError)

    def test_already_open_is_value_error(self) -> None:
        assert issubclass(ShiftAlreadyOpen, ValueError)

    def test_boundary_invalid_is_value_error(self) -> None:
        assert issubclass(ShiftBoundaryInvalid, ValueError)

    def test_forbidden_is_permission_error(self) -> None:
        assert issubclass(ShiftEditForbidden, PermissionError)

    def test_errors_are_distinct(self) -> None:
        classes = {
            ShiftNotFound,
            ShiftAlreadyOpen,
            ShiftBoundaryInvalid,
            ShiftEditForbidden,
        }
        assert len(classes) == 4

    def test_already_open_carries_payload(self) -> None:
        err = ShiftAlreadyOpen(user_id="u1", existing_shift_id="s1")
        assert err.user_id == "u1"
        assert err.existing_shift_id == "s1"
        assert "s1" in str(err)


# ---------------------------------------------------------------------------
# ShiftView invariants
# ---------------------------------------------------------------------------


class TestShiftView:
    def _view(self) -> ShiftView:
        return ShiftView(
            id="s",
            workspace_id="w",
            user_id="u",
            starts_at=_PINNED,
            ends_at=None,
            property_id=None,
            source="manual",
            notes_md=None,
            approved_by=None,
            approved_at=None,
        )

    def test_view_is_slotted(self) -> None:
        view = self._view()
        with pytest.raises((AttributeError, TypeError)):
            view.extra = "nope"  # type: ignore[attr-defined]

    def test_view_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        view = self._view()
        with pytest.raises(FrozenInstanceError):
            view.starts_at = _PINNED + timedelta(hours=1)  # type: ignore[misc]

    def test_equality_by_value(self) -> None:
        assert self._view() == self._view()


# ---------------------------------------------------------------------------
# DTO validation
# ---------------------------------------------------------------------------


class TestShiftDtos:
    def test_shift_open_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ShiftOpen(bogus="yes")  # type: ignore[call-arg]

    def test_shift_open_defaults(self) -> None:
        dto = ShiftOpen()
        assert dto.user_id is None
        assert dto.property_id is None
        assert dto.source == "manual"
        assert dto.notes_md is None

    def test_shift_open_bad_source_literal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ShiftOpen(source="nope")  # type: ignore[arg-type]

    def test_shift_close_defaults(self) -> None:
        assert ShiftClose().ends_at is None

    def test_shift_close_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ShiftClose(starts_at=_PINNED)  # type: ignore[call-arg]

    def test_shift_edit_all_optional(self) -> None:
        dto = ShiftEdit()
        assert dto.starts_at is None
        assert dto.ends_at is None
        assert dto.property_id is None
        assert dto.notes_md is None


# ---------------------------------------------------------------------------
# Event registry — the ShiftChanged class is registered under the right name
# ---------------------------------------------------------------------------


class TestShiftChangedRegistration:
    def test_registered_under_expected_name(self) -> None:
        """``time.shift.changed`` resolves to :class:`ShiftChanged`."""
        assert get_event_type("time.shift.changed") is ShiftChanged

    def test_event_is_frozen(self) -> None:
        evt = ShiftChanged(
            workspace_id="w",
            actor_id="u",
            correlation_id="c",
            occurred_at=_PINNED,
            shift_id="s",
            user_id="u",
            action="opened",
        )
        with pytest.raises(ValidationError):
            evt.shift_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# open_shift
# ---------------------------------------------------------------------------


class TestOpenShift:
    def test_worker_opens_own_shift(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, user_id, clock = worker_env
        view = open_shift(session, ctx, clock=clock)
        assert view.user_id == user_id
        assert view.starts_at == _PINNED
        assert view.ends_at is None
        assert view.source == "manual"

    def test_open_shift_defaults_starts_at_to_clock(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        other_when = _PINNED + timedelta(hours=3)
        clock.set(other_when)
        view = open_shift(session, ctx, clock=clock)
        assert view.starts_at == other_when

    def test_second_open_rejected_while_first_is_live(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, user_id, clock = worker_env
        first = open_shift(session, ctx, clock=clock)
        with pytest.raises(ShiftAlreadyOpen) as exc:
            open_shift(session, ctx, clock=clock)
        assert exc.value.user_id == user_id
        assert exc.value.existing_shift_id == first.id

    def test_worker_cannot_open_for_another_user(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        # Seed a second user in the same workspace (not the caller).
        other_id = _bootstrap_user(session, email="x@example.com", display_name="X")
        _grant(
            session,
            workspace_id=ctx.workspace_id,
            user_id=other_id,
            grant_role="worker",
        )
        session.commit()
        with pytest.raises(ShiftEditForbidden):
            open_shift(session, ctx, user_id=other_id, clock=clock)

    def test_manager_can_open_for_another_user(
        self,
        session: Session,
        manager_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _mid, clock = manager_env
        other_id = _bootstrap_user(session, email="y@example.com", display_name="Y")
        _grant(
            session,
            workspace_id=ctx.workspace_id,
            user_id=other_id,
            grant_role="worker",
        )
        session.commit()
        view = open_shift(session, ctx, user_id=other_id, clock=clock)
        assert view.user_id == other_id

    def test_stranger_cannot_clock_self(
        self,
        session: Session,
    ) -> None:
        """A user with no grants at all hits ``time.clock_self`` 403."""
        ws_id = _bootstrap_workspace(session, slug="stranger")
        user_id = _bootstrap_user(
            session, email="stranger@example.com", display_name="S"
        )
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id, grant_role="guest")
        with pytest.raises(ShiftEditForbidden):
            open_shift(session, ctx, clock=FrozenClock(_PINNED))


# ---------------------------------------------------------------------------
# close_shift
# ---------------------------------------------------------------------------


class TestCloseShift:
    def test_worker_closes_own_shift(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        clock.advance(timedelta(hours=4))
        closed = close_shift(session, ctx, shift_id=opened.id, clock=clock)
        assert closed.ends_at == _PINNED + timedelta(hours=4)

    def test_worker_cannot_close_other_users_shift(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="cc")
        a_id = _bootstrap_user(session, email="a@example.com", display_name="A")
        b_id = _bootstrap_user(session, email="b@example.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        session.commit()

        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        clock = FrozenClock(_PINNED)
        opened = open_shift(session, ctx_a, clock=clock)

        with pytest.raises(ShiftEditForbidden):
            close_shift(session, ctx_b, shift_id=opened.id, clock=clock)

    def test_manager_can_close_other_users_shift(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="mc")
        worker_id = _bootstrap_user(session, email="w@example.com", display_name="W")
        mgr_id = _bootstrap_user(session, email="m@example.com", display_name="M")
        _grant(session, workspace_id=ws_id, user_id=worker_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=mgr_id, grant_role="manager")
        session.commit()

        ctx_worker = _ctx(workspace_id=ws_id, actor_id=worker_id, grant_role="worker")
        ctx_mgr = _ctx(workspace_id=ws_id, actor_id=mgr_id, grant_role="manager")
        clock = FrozenClock(_PINNED)
        opened = open_shift(session, ctx_worker, clock=clock)
        clock.advance(timedelta(hours=2))
        closed = close_shift(session, ctx_mgr, shift_id=opened.id, clock=clock)
        assert closed.ends_at is not None

    def test_close_defaults_ends_at_to_clock(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        clock.advance(timedelta(hours=5))
        closed = close_shift(session, ctx, shift_id=opened.id, clock=clock)
        assert closed.ends_at == _PINNED + timedelta(hours=5)

    def test_close_with_negative_window_rejected(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        earlier = _PINNED - timedelta(hours=1)
        with pytest.raises(ShiftBoundaryInvalid):
            close_shift(session, ctx, shift_id=opened.id, ends_at=earlier, clock=clock)

    def test_close_zero_length_shift_accepted(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """A zero-minute window is a legitimate mis-click path."""
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        closed = close_shift(
            session, ctx, shift_id=opened.id, ends_at=opened.starts_at, clock=clock
        )
        assert closed.ends_at == opened.starts_at

    def test_close_missing_shift_404(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        with pytest.raises(ShiftNotFound):
            close_shift(session, ctx, shift_id="no-such-id", clock=clock)

    def test_idempotent_reclose(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Re-closing a closed shift is a no-op.

        The second call must:

        * return the same ``ends_at`` as the first close;
        * NOT write a second ``close`` audit row (one mutation, one
          audit row — the whole point of the short-circuit);
        * NOT re-publish a ``ShiftChanged`` event (downstream
          subscribers would otherwise double-invalidate on every
          double-tap).
        """
        ctx, _uid, clock = worker_env

        captured: list[ShiftChanged] = []

        @bus.subscribe(ShiftChanged)
        def _cap(event: ShiftChanged) -> None:
            captured.append(event)

        opened = open_shift(session, ctx, clock=clock)
        clock.advance(timedelta(hours=1))
        closed_first = close_shift(session, ctx, shift_id=opened.id, clock=clock)
        clock.advance(timedelta(hours=1))
        closed_again = close_shift(session, ctx, shift_id=opened.id, clock=clock)
        assert closed_again.ends_at == closed_first.ends_at

        # Only one close-shaped audit row — the second call short-
        # circuited before ``write_audit``.
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        shift_actions = [r.action for r in rows if r.entity_kind == "shift"]
        assert shift_actions == ["open", "close"]

        # Events: "opened" + "closed" only — no second "closed".
        assert [e.action for e in captured] == ["opened", "closed"]

    def test_idempotent_reclose_with_junky_ends_at(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Re-closing a closed shift with a bad ``ends_at`` is still a no-op.

        The idempotency short-circuit must fire BEFORE the boundary
        validation. Otherwise a stale client with a bad client-clock
        would hit a 422 on a shift that is already closed — breaking
        the "double-click is safe" guarantee.
        """
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        clock.advance(timedelta(hours=1))
        close_shift(session, ctx, shift_id=opened.id, clock=clock)

        # Pass an ``ends_at`` firmly before ``starts_at`` — the check
        # would normally raise ShiftBoundaryInvalid, but the shift is
        # already closed so the call returns a no-op view.
        result = close_shift(
            session,
            ctx,
            shift_id=opened.id,
            ends_at=opened.starts_at - timedelta(hours=10),
            clock=clock,
        )
        assert result.id == opened.id
        assert result.ends_at is not None


# ---------------------------------------------------------------------------
# edit_shift
# ---------------------------------------------------------------------------


class TestEditShift:
    def test_worker_cannot_edit(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        with pytest.raises(ShiftEditForbidden):
            edit_shift(session, ctx, shift_id=opened.id, notes_md="bump", clock=clock)

    def test_manager_can_edit(
        self,
        session: Session,
        manager_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = manager_env
        opened = open_shift(session, ctx, clock=clock)
        edited = edit_shift(
            session,
            ctx,
            shift_id=opened.id,
            notes_md="manager edit",
            clock=clock,
        )
        assert edited.notes_md == "manager edit"

    def test_edit_rejects_zero_length_window(
        self,
        session: Session,
        manager_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = manager_env
        opened = open_shift(session, ctx, clock=clock)
        with pytest.raises(ShiftBoundaryInvalid):
            edit_shift(
                session,
                ctx,
                shift_id=opened.id,
                ends_at=opened.starts_at,  # zero-length → strict reject
                clock=clock,
            )

    def test_edit_rejects_negative_window(
        self,
        session: Session,
        manager_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = manager_env
        opened = open_shift(session, ctx, clock=clock)
        with pytest.raises(ShiftBoundaryInvalid):
            edit_shift(
                session,
                ctx,
                shift_id=opened.id,
                ends_at=opened.starts_at - timedelta(hours=1),
                clock=clock,
            )

    def test_edit_patch_semantics(
        self,
        session: Session,
        manager_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        """Fields not passed remain untouched."""
        ctx, _uid, clock = manager_env
        opened = open_shift(
            session, ctx, property_id="prop-1", notes_md="first", clock=clock
        )
        edited = edit_shift(
            session, ctx, shift_id=opened.id, notes_md="second", clock=clock
        )
        assert edited.notes_md == "second"
        assert edited.property_id == "prop-1"
        assert edited.starts_at == opened.starts_at

    def test_edit_missing_shift_404(
        self,
        session: Session,
        manager_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = manager_env
        with pytest.raises(ShiftNotFound):
            edit_shift(session, ctx, shift_id="nope", notes_md="x", clock=clock)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class TestListShifts:
    def test_list_open_shifts_returns_only_open_rows(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        clock.advance(timedelta(hours=1))
        close_shift(session, ctx, shift_id=opened.id, clock=clock)
        # Open a fresh one now — should be the only entry in the list.
        clock.advance(timedelta(hours=1))
        second = open_shift(session, ctx, clock=clock)

        views = list_open_shifts(session, ctx)
        assert [v.id for v in views] == [second.id]

    def test_list_open_shifts_filter_by_user_id(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="list-uid")
        a_id = _bootstrap_user(session, email="a@x.com", display_name="A")
        b_id = _bootstrap_user(session, email="b@x.com", display_name="B")
        _grant(session, workspace_id=ws_id, user_id=a_id, grant_role="worker")
        _grant(session, workspace_id=ws_id, user_id=b_id, grant_role="worker")
        session.commit()

        ctx_a = _ctx(workspace_id=ws_id, actor_id=a_id, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_id, actor_id=b_id, grant_role="worker")
        clock = FrozenClock(_PINNED)
        open_shift(session, ctx_a, clock=clock)
        open_shift(session, ctx_b, clock=clock)

        # Either ctx sees both open shifts (workspace scope), but the
        # ``user_id`` filter narrows.
        only_a = list_open_shifts(session, ctx_a, user_id=a_id)
        assert {v.user_id for v in only_a} == {a_id}
        both = list_open_shifts(session, ctx_a)
        assert len(both) == 2

    def test_list_shifts_range_filter(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env

        # Shift #1 at T+0.
        opened1 = open_shift(session, ctx, clock=clock)
        clock.advance(timedelta(hours=1))
        close_shift(session, ctx, shift_id=opened1.id, clock=clock)
        # Shift #2 at T+24.
        clock.advance(timedelta(hours=23))
        opened2 = open_shift(session, ctx, clock=clock)
        clock.advance(timedelta(hours=1))
        close_shift(session, ctx, shift_id=opened2.id, clock=clock)

        all_views = list_shifts(session, ctx)
        assert {v.id for v in all_views} == {opened1.id, opened2.id}

        window = list_shifts(
            session,
            ctx,
            starts_from=_PINNED + timedelta(hours=12),
            starts_until=_PINNED + timedelta(hours=36),
        )
        assert [v.id for v in window] == [opened2.id]

    def test_list_is_workspace_scoped(
        self,
        session: Session,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="wa")
        ws_b = _bootstrap_workspace(session, slug="wb")
        user_a = _bootstrap_user(session, email="a@a.com", display_name="A")
        user_b = _bootstrap_user(session, email="b@b.com", display_name="B")
        _grant(session, workspace_id=ws_a, user_id=user_a, grant_role="worker")
        _grant(session, workspace_id=ws_b, user_id=user_b, grant_role="worker")
        session.commit()

        ctx_a = _ctx(workspace_id=ws_a, actor_id=user_a, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_b, actor_id=user_b, grant_role="worker")
        clock = FrozenClock(_PINNED)
        open_shift(session, ctx_a, clock=clock)
        open_shift(session, ctx_b, clock=clock)

        views_a = list_shifts(session, ctx_a)
        views_b = list_shifts(session, ctx_b)
        assert len(views_a) == 1 and views_a[0].user_id == user_a
        assert len(views_b) == 1 and views_b[0].user_id == user_b


# ---------------------------------------------------------------------------
# Audit rows
# ---------------------------------------------------------------------------


def _audit_rows(session: Session, *, workspace_id: str) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.workspace_id == workspace_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )
    return list(session.scalars(stmt).all())


class TestAudit:
    def test_open_writes_audit_row(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        view = open_shift(session, ctx, clock=clock)
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        actions = [r.action for r in rows if r.entity_kind == "shift"]
        assert actions == ["open"]
        row = next(r for r in rows if r.entity_kind == "shift")
        assert row.entity_id == view.id
        assert "after" in row.diff
        assert "before" not in row.diff  # create-only action

    def test_close_writes_before_after_diff(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        clock.advance(timedelta(hours=2))
        close_shift(session, ctx, shift_id=opened.id, clock=clock)
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        shift_rows = [r for r in rows if r.entity_kind == "shift"]
        assert [r.action for r in shift_rows] == ["open", "close"]
        close_row = shift_rows[1]
        assert "before" in close_row.diff and "after" in close_row.diff
        assert close_row.diff["before"]["ends_at"] is None

    def test_edit_writes_before_after_diff(
        self,
        session: Session,
        manager_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = manager_env
        opened = open_shift(session, ctx, clock=clock)
        edit_shift(session, ctx, shift_id=opened.id, notes_md="patched", clock=clock)
        rows = _audit_rows(session, workspace_id=ctx.workspace_id)
        shift_rows = [r for r in rows if r.entity_kind == "shift"]
        assert [r.action for r in shift_rows] == ["open", "edit"]


# ---------------------------------------------------------------------------
# Event fan-out
# ---------------------------------------------------------------------------


class TestShiftChangedEvents:
    def _capture_events(self) -> list[ShiftChanged]:
        captured: list[ShiftChanged] = []

        @bus.subscribe(ShiftChanged)
        def _cap(event: ShiftChanged) -> None:
            captured.append(event)

        return captured

    def test_open_publishes_opened_action(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, user_id, clock = worker_env
        captured = self._capture_events()
        view = open_shift(session, ctx, clock=clock)
        assert len(captured) == 1
        evt = captured[0]
        assert evt.shift_id == view.id
        assert evt.user_id == user_id
        assert evt.action == "opened"
        assert evt.workspace_id == ctx.workspace_id

    def test_close_publishes_closed_action(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        captured = self._capture_events()
        opened = open_shift(session, ctx, clock=clock)
        clock.advance(timedelta(hours=1))
        close_shift(session, ctx, shift_id=opened.id, clock=clock)
        actions = [e.action for e in captured]
        assert actions == ["opened", "closed"]

    def test_edit_publishes_edited_action(
        self,
        session: Session,
        manager_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = manager_env
        captured = self._capture_events()
        opened = open_shift(session, ctx, clock=clock)
        edit_shift(session, ctx, shift_id=opened.id, notes_md="a", clock=clock)
        actions = [e.action for e in captured]
        assert actions == ["opened", "edited"]


# ---------------------------------------------------------------------------
# Get single shift
# ---------------------------------------------------------------------------


class TestGetShift:
    def test_get_missing_raises(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, *_ = worker_env
        with pytest.raises(ShiftNotFound):
            get_shift(session, ctx, shift_id="no-such")

    def test_get_returns_view(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        view = get_shift(session, ctx, shift_id=opened.id)
        assert view.id == opened.id

    def test_get_is_tenant_scoped(
        self,
        session: Session,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="ga")
        ws_b = _bootstrap_workspace(session, slug="gb")
        user_a = _bootstrap_user(session, email="ga@x.com", display_name="A")
        user_b = _bootstrap_user(session, email="gb@x.com", display_name="B")
        _grant(session, workspace_id=ws_a, user_id=user_a, grant_role="worker")
        _grant(session, workspace_id=ws_b, user_id=user_b, grant_role="worker")
        session.commit()
        ctx_a = _ctx(workspace_id=ws_a, actor_id=user_a, grant_role="worker")
        ctx_b = _ctx(workspace_id=ws_b, actor_id=user_b, grant_role="worker")
        clock = FrozenClock(_PINNED)
        opened = open_shift(session, ctx_a, clock=clock)

        # ws_b can't see ws_a's shift.
        with pytest.raises(ShiftNotFound):
            get_shift(session, ctx_b, shift_id=opened.id)


# ---------------------------------------------------------------------------
# DB row shape sanity
# ---------------------------------------------------------------------------


class TestDbRowShape:
    def test_open_row_has_expected_columns(
        self,
        session: Session,
        worker_env: tuple[WorkspaceContext, str, FrozenClock],
    ) -> None:
        ctx, _uid, clock = worker_env
        opened = open_shift(session, ctx, clock=clock)
        row = session.get(Shift, opened.id)
        assert row is not None
        assert row.ends_at is None
        assert row.source == "manual"
        assert row.workspace_id == ctx.workspace_id
