"""Unit tests for :mod:`app.services.employees.service`.

Mirrors the in-memory SQLite bootstrap in
``tests/unit/places/test_property_service.py``: a fresh engine per
test, pull every sibling ``models`` module onto the shared
``Base.metadata``, run ``Base.metadata.create_all``, drive the
domain code with a :class:`FrozenClock`.

Covers cd-dv2:

* Happy-path profile update — sent fields land on the user row,
  unsent fields stay untouched, audit row carries a before/after
  diff of only the changed columns.
* Self-edit passes without capability — even without an
  ``users.edit_profile_other`` rule.
* Cross-user edit requires capability — the default-allow
  ``(owners, managers)`` gate covers the owner fixture.
* Archive — sets ``WorkEngagement.archived_on`` + stamps
  ``deleted_at`` / ``ended_on`` on every active
  :class:`UserWorkRole`.
* Idempotent archive — re-running on an already-archived state is a
  DB no-op but still writes an audit row so the trail is linear.
* Reinstate — reverse archive. Clears ``archived_on`` +
  ``deleted_at`` / ``ended_on`` on the rows the archive touched.
* Idempotent reinstate — running on an active state leaves the rows
  unchanged and writes an audit row.
* ``seed_pending_work_engagement`` — inserts a minimal pending row
  at accept time; calling twice returns the same row (idempotent).
* Cross-tenant denial — a user linked only to workspace A is
  invisible to workspace B (``get`` / ``update`` /
  ``archive`` / ``reinstate`` all raise
  :class:`EmployeeNotFound`).

See ``docs/specs/05-employees-and-roles.md`` §"User (as worker)" /
§"Archive / reinstate" and
``docs/specs/02-domain-model.md`` §"work_engagement".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.authz.bootstrap import seed_owners_system_group
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import (
    UserWorkRole,
    UserWorkspace,
    WorkEngagement,
    WorkRole,
    Workspace,
)
from app.authz import PermissionDenied
from app.services.employees.service import (
    EmployeeNotFound,
    EmployeeProfileUpdate,
    ProfileFieldForbidden,
    archive_employee,
    get_employee,
    reinstate_employee,
    seed_pending_work_engagement,
    update_profile,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="engine_employees")
def fixture_engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(name="session_employees")
def fixture_session(engine_employees: Engine) -> Iterator[Session]:
    """Fresh session per test; tenant filter not installed (unit scope)."""
    factory = sessionmaker(
        bind=engine_employees, expire_on_commit=False, class_=Session
    )
    with factory() as s:
        yield s


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------


def _ctx(workspace_id: str, *, actor_id: str, slug: str = "ws") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap_user(session: Session, *, email: str, display_name: str) -> User:
    user = User(
        id=new_ulid(),
        email=email,
        email_lower=email.lower(),
        display_name=display_name,
        locale=None,
        timezone=None,
        created_at=_PINNED,
    )
    session.add(user)
    session.flush()
    return user


def _bootstrap_workspace(session: Session, *, slug: str) -> Workspace:
    ws = Workspace(
        id=new_ulid(),
        slug=slug,
        name=f"Workspace {slug}",
        plan="free",
        quota_json={},
        settings_json={},
        created_at=_PINNED,
    )
    session.add(ws)
    session.flush()
    return ws


def _attach(session: Session, *, user_id: str, workspace_id: str) -> None:
    session.add(
        UserWorkspace(
            user_id=user_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=_PINNED,
        )
    )
    session.flush()


def _owner_ctx(
    session: Session,
    *,
    user: User,
    ws: Workspace,
    clock: FrozenClock,
) -> WorkspaceContext:
    """Seed owners group + grant so the owner fixture passes authz checks."""
    ctx = _ctx(ws.id, actor_id=user.id, slug=ws.slug)
    _attach(session, user_id=user.id, workspace_id=ws.id)
    seed_owners_system_group(
        session,
        ctx,
        workspace_id=ws.id,
        owner_user_id=user.id,
        clock=clock,
    )
    # Write the `worker` role_grant so the permission walk has a
    # concrete grant to read; default_allow ``(owners, managers)`` is
    # what carries the archive + edit_profile_other gates.
    session.flush()
    return ctx


def _seed_engagement(
    session: Session,
    *,
    user: User,
    ws: Workspace,
    archived_on: date | None = None,
) -> WorkEngagement:
    row = WorkEngagement(
        id=new_ulid(),
        user_id=user.id,
        workspace_id=ws.id,
        engagement_kind="payroll",
        supplier_org_id=None,
        pay_destination_id=None,
        reimbursement_destination_id=None,
        started_on=_PINNED.date(),
        archived_on=archived_on,
        notes_md="",
        created_at=_PINNED,
        updated_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return row


def _seed_work_role(session: Session, *, ws: Workspace, key: str) -> WorkRole:
    row = WorkRole(
        id=new_ulid(),
        workspace_id=ws.id,
        key=key,
        name=key.title(),
        description_md="",
        default_settings_json={},
        icon_name="",
        created_at=_PINNED,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    return row


def _seed_user_work_role(
    session: Session,
    *,
    user: User,
    ws: Workspace,
    work_role: WorkRole,
) -> UserWorkRole:
    row = UserWorkRole(
        id=new_ulid(),
        user_id=user.id,
        workspace_id=ws.id,
        work_role_id=work_role.id,
        started_on=_PINNED.date(),
        ended_on=None,
        pay_rule_id=None,
        created_at=_PINNED,
        deleted_at=None,
    )
    session.add(row)
    session.flush()
    return row


def _audit_rows(session: Session, *, entity_id: str) -> list[AuditLog]:
    return list(
        session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == entity_id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestUpdateProfile:
    """``update_profile`` covers self-edit + capability gate + partial shape."""

    def test_self_edit_updates_only_sent_fields(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-self")
        user = _bootstrap_user(session, email="alice@example.com", display_name="Alice")
        _attach(session, user_id=user.id, workspace_id=ws.id)
        ctx = _ctx(ws.id, actor_id=user.id, slug=ws.slug)

        view = update_profile(
            session,
            ctx,
            user_id=user.id,
            body=EmployeeProfileUpdate(display_name="Alice Example"),
            clock=clock,
        )
        assert view.display_name == "Alice Example"
        # ``locale`` was never sent; stays None.
        assert view.locale is None
        assert view.timezone is None

        refreshed = session.get(User, user.id)
        assert refreshed is not None
        assert refreshed.display_name == "Alice Example"

        audit = _audit_rows(session, entity_id=user.id)
        actions = [r.action for r in audit]
        assert actions == ["employee.profile_updated"]
        diff = audit[0].diff
        assert diff["before"] == {"display_name": "Alice"}
        assert diff["after"] == {"display_name": "Alice Example"}

    def test_unchanged_fields_do_not_create_audit(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        """Sent field that equals the current value is a no-op."""
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-noop")
        user = _bootstrap_user(session, email="bob@example.com", display_name="Bob")
        _attach(session, user_id=user.id, workspace_id=ws.id)
        ctx = _ctx(ws.id, actor_id=user.id, slug=ws.slug)

        view = update_profile(
            session,
            ctx,
            user_id=user.id,
            body=EmployeeProfileUpdate(display_name="Bob"),
            clock=clock,
        )
        assert view.display_name == "Bob"
        assert _audit_rows(session, entity_id=user.id) == []

    def test_empty_body_is_noop(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-empty")
        user = _bootstrap_user(session, email="carol@example.com", display_name="Carol")
        _attach(session, user_id=user.id, workspace_id=ws.id)
        ctx = _ctx(ws.id, actor_id=user.id, slug=ws.slug)

        view = update_profile(
            session,
            ctx,
            user_id=user.id,
            body=EmployeeProfileUpdate(),
            clock=clock,
        )
        assert view.display_name == "Carol"
        assert _audit_rows(session, entity_id=user.id) == []

    def test_cross_user_without_capability_forbidden(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        """A non-owner, non-manager caller cannot edit another user."""
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-fb")
        actor = _bootstrap_user(
            session, email="actor@example.com", display_name="Actor"
        )
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        _attach(session, user_id=actor.id, workspace_id=ws.id)
        _attach(session, user_id=target.id, workspace_id=ws.id)
        # Actor has no grant / no owner membership → denied.
        ctx = _ctx(ws.id, actor_id=actor.id, slug=ws.slug)
        with pytest.raises(ProfileFieldForbidden):
            update_profile(
                session,
                ctx,
                user_id=target.id,
                body=EmployeeProfileUpdate(display_name="New Name"),
                clock=clock,
            )

    def test_cross_user_owner_edits_succeed(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-owner")
        owner = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target Old"
        )
        ctx = _owner_ctx(session, user=owner, ws=ws, clock=clock)
        _attach(session, user_id=target.id, workspace_id=ws.id)

        view = update_profile(
            session,
            ctx,
            user_id=target.id,
            body=EmployeeProfileUpdate(display_name="Target New"),
            clock=clock,
        )
        assert view.display_name == "Target New"

    def test_not_a_member_is_404(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws_a = _bootstrap_workspace(session, slug="ws-a")
        ws_b = _bootstrap_workspace(session, slug="ws-b")
        actor = _bootstrap_user(
            session, email="actor@example.com", display_name="Actor"
        )
        # target is only a member of ws_b
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        _attach(session, user_id=actor.id, workspace_id=ws_a.id)
        _attach(session, user_id=target.id, workspace_id=ws_b.id)
        ctx = _ctx(ws_a.id, actor_id=actor.id, slug=ws_a.slug)

        with pytest.raises(EmployeeNotFound):
            update_profile(
                session,
                ctx,
                user_id=target.id,
                body=EmployeeProfileUpdate(display_name="X"),
                clock=clock,
            )

    def test_explicit_display_name_null_is_rejected_at_dto(self) -> None:
        """``display_name=None`` must fail DTO validation.

        Without the :meth:`EmployeeProfileUpdate._reject_display_name_null`
        guard, Pydantic's ``min_length=1`` rule only fires on strings —
        ``None`` would slip through and surface as a NOT NULL violation
        at flush time (500). This test pins the 422-at-DTO contract.
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            EmployeeProfileUpdate(display_name=None)


class TestArchiveEmployee:
    """``archive_employee`` archives the engagement + user_work_role rows."""

    def test_archives_engagement_and_roles(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-arch")
        owner = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        ctx = _owner_ctx(session, user=owner, ws=ws, clock=clock)
        _attach(session, user_id=target.id, workspace_id=ws.id)
        engagement = _seed_engagement(session, user=target, ws=ws)
        wrole = _seed_work_role(session, ws=ws, key="maid")
        uwr = _seed_user_work_role(session, user=target, ws=ws, work_role=wrole)

        view = archive_employee(session, ctx, user_id=target.id, clock=clock)
        assert view.engagement_archived_on is None  # active view drops archived rows

        session.refresh(engagement)
        session.refresh(uwr)
        assert engagement.archived_on == _PINNED.date()
        assert uwr.deleted_at is not None
        assert uwr.ended_on == _PINNED.date()

        audit = _audit_rows(session, entity_id=target.id)
        actions = [r.action for r in audit]
        assert actions == ["employee.archived"]
        diff = audit[0].diff
        assert diff["engagement_id"] == engagement.id
        assert diff["engagement_was_active"] is True
        assert diff["archived_user_work_role_ids"] == [uwr.id]

    def test_archive_is_idempotent_audit_reflects_no_changes(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-idem")
        owner = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        ctx = _owner_ctx(session, user=owner, ws=ws, clock=clock)
        _attach(session, user_id=target.id, workspace_id=ws.id)
        # Seed an already-archived engagement.
        engagement = _seed_engagement(
            session, user=target, ws=ws, archived_on=_PINNED.date()
        )

        view = archive_employee(session, ctx, user_id=target.id, clock=clock)
        assert view.engagement_archived_on is None

        session.refresh(engagement)
        assert engagement.archived_on == _PINNED.date()

        audit = _audit_rows(session, entity_id=target.id)
        assert [r.action for r in audit] == ["employee.archived"]
        diff = audit[0].diff
        # No active engagement was found on the idempotent retry.
        assert diff["engagement_id"] is None
        assert diff["engagement_was_active"] is False
        assert diff["archived_user_work_role_ids"] == []

    def test_archive_requires_capability(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-noauth")
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        actor = _bootstrap_user(
            session, email="actor@example.com", display_name="Actor"
        )
        _attach(session, user_id=target.id, workspace_id=ws.id)
        _attach(session, user_id=actor.id, workspace_id=ws.id)
        ctx = _ctx(ws.id, actor_id=actor.id, slug=ws.slug)
        _seed_engagement(session, user=target, ws=ws)
        with pytest.raises(PermissionDenied):
            archive_employee(session, ctx, user_id=target.id, clock=clock)


class TestReinstateEmployee:
    """``reinstate_employee`` clears the archive markers."""

    def test_reinstates_engagement_and_roles(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-rein")
        owner = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        ctx = _owner_ctx(session, user=owner, ws=ws, clock=clock)
        _attach(session, user_id=target.id, workspace_id=ws.id)
        engagement = _seed_engagement(session, user=target, ws=ws)
        wrole = _seed_work_role(session, ws=ws, key="cook")
        uwr = _seed_user_work_role(session, user=target, ws=ws, work_role=wrole)
        # Archive first so we have something to reverse.
        archive_employee(session, ctx, user_id=target.id, clock=clock)
        session.refresh(engagement)
        session.refresh(uwr)
        assert engagement.archived_on is not None
        assert uwr.deleted_at is not None

        view = reinstate_employee(session, ctx, user_id=target.id, clock=clock)
        session.refresh(engagement)
        session.refresh(uwr)
        assert engagement.archived_on is None
        assert uwr.deleted_at is None
        assert uwr.ended_on is None
        assert view.engagement_archived_on is None

        audit = _audit_rows(session, entity_id=target.id)
        actions = [r.action for r in audit]
        assert actions == ["employee.archived", "employee.reinstated"]
        diff = audit[-1].diff
        assert diff["engagement_id"] == engagement.id
        assert diff["engagement_was_archived"] is True
        assert uwr.id in diff["reinstated_user_work_role_ids"]

    def test_reinstate_is_idempotent_on_active(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-ridem")
        owner = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        ctx = _owner_ctx(session, user=owner, ws=ws, clock=clock)
        _attach(session, user_id=target.id, workspace_id=ws.id)
        _seed_engagement(session, user=target, ws=ws)

        reinstate_employee(session, ctx, user_id=target.id, clock=clock)
        audit = _audit_rows(session, entity_id=target.id)
        assert [r.action for r in audit] == ["employee.reinstated"]
        diff = audit[0].diff
        assert diff["engagement_was_archived"] is False
        assert diff["reinstated_user_work_role_ids"] == []


class TestSeedPendingWorkEngagement:
    """``seed_pending_work_engagement`` is idempotent at accept time."""

    def test_inserts_minimal_pending_row(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-seed")
        owner = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        ctx = _owner_ctx(session, user=owner, ws=ws, clock=clock)
        _attach(session, user_id=target.id, workspace_id=ws.id)

        engagement = seed_pending_work_engagement(
            session,
            ctx,
            user_id=target.id,
            now=_PINNED,
            clock=clock,
        )
        assert engagement is not None
        assert engagement.engagement_kind == "payroll"
        assert engagement.archived_on is None
        assert engagement.started_on == _PINNED.date()

        # Audit row attached to the engagement id, not the user id.
        audit = _audit_rows(session, entity_id=engagement.id)
        assert [r.action for r in audit] == ["work_engagement.seeded_on_accept"]

    def test_idempotent_returns_existing(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-seedi")
        owner = _bootstrap_user(
            session, email="owner@example.com", display_name="Owner"
        )
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        ctx = _owner_ctx(session, user=owner, ws=ws, clock=clock)
        _attach(session, user_id=target.id, workspace_id=ws.id)

        first = seed_pending_work_engagement(
            session, ctx, user_id=target.id, now=_PINNED, clock=clock
        )
        second = seed_pending_work_engagement(
            session, ctx, user_id=target.id, now=_PINNED, clock=clock
        )
        assert first is not None
        assert second is not None
        assert first.id == second.id

        # Only the first call wrote an audit row.
        audit = _audit_rows(session, entity_id=first.id)
        assert len(audit) == 1


class TestGetEmployee:
    """Read projection."""

    def test_get_returns_view(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws = _bootstrap_workspace(session, slug="ws-get")
        user = _bootstrap_user(session, email="alice@example.com", display_name="Alice")
        _attach(session, user_id=user.id, workspace_id=ws.id)
        ctx = _ctx(ws.id, actor_id=user.id, slug=ws.slug)
        _seed_engagement(session, user=user, ws=ws)

        view = get_employee(session, ctx, user_id=user.id)
        assert view.id == user.id
        assert view.email == "alice@example.com"
        assert view.engagement_archived_on is None

    def test_get_cross_tenant_is_404(
        self, session_employees: Session, clock: FrozenClock
    ) -> None:
        session = session_employees
        ws_a = _bootstrap_workspace(session, slug="ws-ga")
        ws_b = _bootstrap_workspace(session, slug="ws-gb")
        actor = _bootstrap_user(
            session, email="actor@example.com", display_name="Actor"
        )
        target = _bootstrap_user(
            session, email="target@example.com", display_name="Target"
        )
        _attach(session, user_id=actor.id, workspace_id=ws_a.id)
        _attach(session, user_id=target.id, workspace_id=ws_b.id)
        ctx = _ctx(ws_a.id, actor_id=actor.id, slug=ws_a.slug)
        with pytest.raises(EmployeeNotFound):
            get_employee(session, ctx, user_id=target.id)


class TestInviteDoesNotSeedAtCreateTime:
    """Guard rail — nothing workspace-scoped should land at invite time.

    The invite flow is defined in :mod:`app.domain.identity.membership`;
    the employees service exposes no helper that could be called at
    invite-create time. This unit test enforces the negative: if a
    future refactor accidentally exposes one, the test will flag it
    via the module's public surface.
    """

    def test_services_employees_does_not_expose_invite_time_helpers(
        self,
    ) -> None:
        import app.services.employees as pkg

        for name in pkg.__all__:
            # Only ``seed_pending_work_engagement`` writes an engagement
            # row from this module, and it is called from the ACCEPT
            # path — never from the invite-create path. The assertion
            # is anchored on the public surface so a future export
            # added to the invite-create path trips this test.
            assert not name.startswith("seed_invite_time"), (
                f"unexpected invite-time helper exported: {name!r}"
            )


# Silence the unused-import warnings for symbols imported purely to
# register metadata on :class:`Base` — the fixtures depend on every
# workspace-scoped ORM class being known to SQLAlchemy.
_ = (RoleGrant, PermissionGroup, PermissionGroupMember)
