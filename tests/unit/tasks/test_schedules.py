"""Unit tests for :mod:`app.domain.tasks.schedules`.

Exercises the CRUD + pause / resume + preview surface against an
in-memory SQLite engine built via ``Base.metadata.create_all()``.
Mirrors ``tests/unit/test_tasks_templates.py`` — same bootstrap
helpers, same ``_load_all_models`` trick to pull sibling ORM
modules onto the shared ``Base.metadata``, no alembic, no tenant
filter.

Covers:

* :class:`ScheduleCreate` / :class:`ScheduleUpdate` validation:
  every shape rule, active-range ordering, backup-list dedup.
* Error-class hierarchy: 404 → ``LookupError``, 422 →
  ``ValueError`` / ``InvalidRRule`` /
  ``InvalidBackupWorkRole``.
* CRUD round-trip: create → read → list (with each filter) →
  update → soft-delete; every mutation writes one ``audit_log``
  row with the expected action + diff shape.
* RRULE round-trip through the store: the value we wrote is the
  value we load, byte-for-byte.
* Pause / resume: ``paused_at`` toggles correctly and is
  idempotent; pausing does **not** cancel materialised tasks.
* Pause-vs-active-range precedence (§06): a paused schedule is
  paused even when inside the active range.
* ``apply_to_existing`` patches only ``state IN
  ('scheduled', 'pending')`` rows.
* :func:`delete` cascades ``state='scheduled'`` → ``'cancelled'``
  with ``cancellation_reason = 'schedule deleted'`` and leaves
  every other linked task alone.
* :func:`preview_occurrences` honours RDATE / EXDATE.
* Backup-list validation: the injectable hook fires on the create
  + update path, the default is a no-op, a stubbed hook returning
  bad ids raises :class:`InvalidBackupWorkRole` with the spec
  error code.
* ``WorkspaceContext`` tenancy: rows in workspace A are invisible
  from workspace B on every read / write path.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Schedule",
§"UI expose-levels", §"Pause / resume", §"Deleting and editing",
§"Pause vs active range", §"Assignment algorithm".
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Occurrence, Schedule, TaskTemplate
from app.adapters.db.workspace.models import Workspace
from app.domain.tasks.schedules import (
    InvalidBackupWorkRole,
    InvalidRRule,
    ScheduleCreate,
    ScheduleNotFound,
    ScheduleUpdate,
    ScheduleView,
    create,
    delete,
    list_schedules,
    pause,
    preview_occurrences,
    read,
    resume,
    update,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve.

    Matches the fixture helper in ``tests/unit/test_tasks_templates.py``.
    Without this, ``Base.metadata.create_all`` raises
    :class:`~sqlalchemy.exc.NoReferencedTableError` on the tasks FKs
    into ``user`` / ``property`` / ``workspace``.
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
    """Fresh session per test; no tenant filter installed here."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def _bootstrap_workspace(session: Session, *, slug: str) -> str:
    workspace_id = new_ulid()
    row = Workspace(
        id=workspace_id,
        slug=slug,
        name=f"Workspace {slug}",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    session.add(row)
    session.flush()
    return workspace_id


def _ctx(workspace_id: str, *, slug: str = "ws") -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id="01HWA00000000000000000USR1",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap_template(
    session: Session,
    *,
    workspace_id: str,
    role_id: str | None = "role-housekeeper",
    duration_minutes: int = 60,
) -> TaskTemplate:
    """Insert a minimum live :class:`TaskTemplate` row.

    We hit the ORM directly rather than go through the template
    CRUD service so the test boundary stays narrow — schedule
    tests should not depend on template-service behaviour.
    """
    tpl = TaskTemplate(
        id=new_ulid(),
        workspace_id=workspace_id,
        title="Villa Sud pool",
        name="Villa Sud pool",
        description_md="",
        default_duration_min=duration_minutes,
        duration_minutes=duration_minutes,
        required_evidence="none",
        photo_required=False,
        default_assignee_role=None,
        role_id=role_id,
        property_scope="any",
        listed_property_ids=[],
        area_scope="any",
        listed_area_ids=[],
        checklist_template_json=[],
        photo_evidence="disabled",
        linked_instruction_ids=[],
        priority="normal",
        inventory_consumption_json={},
        llm_hints_md=None,
        created_at=_PINNED,
    )
    session.add(tpl)
    session.flush()
    return tpl


def _minimal_payload(template_id: str, **overrides: object) -> dict[str, object]:
    """Return a plain dict of sensible-default schedule fields."""
    base: dict[str, object] = {
        "name": "Villa Sud pool — Saturdays 09:00",
        "template_id": template_id,
        "property_id": None,
        "area_id": None,
        "default_assignee": None,
        "backup_assignee_user_ids": [],
        "rrule": "FREQ=WEEKLY;BYDAY=SA",
        "dtstart_local": "2026-04-18T09:00",
        "duration_minutes": 60,
        "rdate_local": "",
        "exdate_local": "",
        "active_from": "2026-04-01",
        "active_until": None,
    }
    base.update(overrides)
    return base


def _minimal_body(template_id: str, **overrides: object) -> ScheduleCreate:
    return ScheduleCreate.model_validate(_minimal_payload(template_id, **overrides))


def _minimal_update(template_id: str, **overrides: object) -> ScheduleUpdate:
    return ScheduleUpdate.model_validate(_minimal_payload(template_id, **overrides))


def _insert_occurrence(
    session: Session,
    *,
    workspace_id: str,
    schedule_id: str,
    template_id: str,
    property_id: str,
    state: str,
    starts_at: datetime = _PINNED,
) -> Occurrence:
    """Insert a minimum :class:`Occurrence` row for cascade tests.

    ``property_id`` must point at a real row; the caller is
    responsible for bootstrapping one via :func:`_bootstrap_property`.
    """
    occ = Occurrence(
        id=new_ulid(),
        workspace_id=workspace_id,
        schedule_id=schedule_id,
        template_id=template_id,
        property_id=property_id,
        assignee_user_id=None,
        starts_at=starts_at,
        ends_at=starts_at.replace(hour=(starts_at.hour + 1) % 24),
        state=state,
        cancellation_reason=None,
        created_at=_PINNED,
    )
    session.add(occ)
    session.flush()
    return occ


def _bootstrap_property(session: Session) -> str:
    """Insert a minimum :class:`~app.adapters.db.places.models.Property` row."""
    from app.adapters.db.places.models import Property

    prop_id = new_ulid()
    session.add(
        Property(
            id=prop_id,
            address="1 Villa Sud Way",
            timezone="Europe/Paris",
            tags_json=[],
            created_at=_PINNED,
        )
    )
    session.flush()
    return prop_id


def _bootstrap_user(session: Session, *, email: str) -> str:
    """Insert a minimum :class:`~app.adapters.db.identity.models.User` row.

    The tasks ``assignee_user_id`` FK (``ON DELETE SET NULL``) forces
    every user id we store on a schedule to exist in ``user``; the
    unit-test engine has FKs on, so we can't fake a pointer. The
    helper is kept narrow — just enough to satisfy the FK.
    """
    from app.adapters.db.identity.models import User

    uid = new_ulid()
    session.add(
        User(
            id=uid,
            email=email,
            email_lower=email.lower(),
            display_name=email.split("@")[0],
            locale=None,
            timezone=None,
            avatar_blob_hash=None,
            created_at=_PINNED,
            last_login_at=None,
        )
    )
    session.flush()
    return uid


# ---------------------------------------------------------------------------
# Error-class hierarchy
# ---------------------------------------------------------------------------


class TestErrorTypes:
    """Each error subclasses the right stdlib parent for router mapping."""

    def test_not_found_is_lookup_error(self) -> None:
        assert issubclass(ScheduleNotFound, LookupError)

    def test_invalid_rrule_is_value_error(self) -> None:
        assert issubclass(InvalidRRule, ValueError)

    def test_invalid_backup_is_value_error(self) -> None:
        assert issubclass(InvalidBackupWorkRole, ValueError)

    def test_errors_are_distinct(self) -> None:
        classes = {ScheduleNotFound, InvalidRRule, InvalidBackupWorkRole}
        assert len(classes) == 3

    def test_backup_error_carries_spec_code(self) -> None:
        """Per §06 the 422 body carries ``error = 'backup_invalid_work_role'``."""
        err = InvalidBackupWorkRole(
            schedule_id="sched-1",
            invalid_user_ids=["user-1"],
            role_id="role-1",
        )
        assert err.error == "backup_invalid_work_role"
        assert err.invalid_user_ids == ("user-1",)
        assert err.schedule_id == "sched-1"
        assert err.role_id == "role-1"


# ---------------------------------------------------------------------------
# DTO validators
# ---------------------------------------------------------------------------


class TestBodyValidation:
    """Shape-level rules surface as pydantic ``ValidationError`` (422)."""

    def test_name_min_length(self) -> None:
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(_minimal_payload("tmpl-1", name=""))

    def test_name_max_length(self) -> None:
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(_minimal_payload("tmpl-1", name="x" * 201))

    def test_active_until_before_active_from_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ScheduleCreate.model_validate(
                _minimal_payload(
                    "tmpl-1",
                    active_from="2026-05-01",
                    active_until="2026-04-01",
                )
            )
        assert "active_until" in str(exc.value)

    def test_active_until_equal_to_active_from_accepted(self) -> None:
        """A single-day active range is legal (same-day start + end)."""
        body = ScheduleCreate.model_validate(
            _minimal_payload(
                "tmpl-1",
                active_from="2026-05-01",
                active_until="2026-05-01",
            )
        )
        assert body.active_until == date(2026, 5, 1)

    def test_duplicate_backup_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            ScheduleCreate.model_validate(
                _minimal_payload(
                    "tmpl-1",
                    backup_assignee_user_ids=["u1", "u1"],
                )
            )
        assert "duplicate" in str(exc.value).lower()

    def test_empty_backup_entry_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(
                _minimal_payload(
                    "tmpl-1",
                    backup_assignee_user_ids=["", "u1"],
                )
            )

    def test_default_assignee_cannot_be_in_backup(self) -> None:
        """Primary + backups are walked as ``[primary, *backups]`` in order."""
        with pytest.raises(ValidationError) as exc:
            ScheduleCreate.model_validate(
                _minimal_payload(
                    "tmpl-1",
                    default_assignee="u1",
                    backup_assignee_user_ids=["u1", "u2"],
                )
            )
        assert "default_assignee" in str(exc.value)

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(_minimal_payload("tmpl-1", extra_field="x"))

    def test_duration_range_enforced(self) -> None:
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(
                _minimal_payload("tmpl-1", duration_minutes=0)
            )
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(
                _minimal_payload("tmpl-1", duration_minutes=24 * 60 + 1)
            )

    def test_dtstart_local_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(_minimal_payload("tmpl-1", dtstart_local=""))

    def test_rrule_cannot_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            ScheduleCreate.model_validate(_minimal_payload("tmpl-1", rrule=""))


# ---------------------------------------------------------------------------
# ScheduleView invariants
# ---------------------------------------------------------------------------


class TestScheduleView:
    """``ScheduleView`` is a frozen, slotted value type."""

    def _view(self) -> ScheduleView:
        return ScheduleView(
            id="sched-1",
            workspace_id="ws-1",
            name="Sample",
            template_id="tmpl-1",
            property_id=None,
            area_id=None,
            default_assignee=None,
            backup_assignee_user_ids=(),
            rrule="FREQ=WEEKLY",
            dtstart_local="2026-04-18T09:00",
            duration_minutes=60,
            rdate_local="",
            exdate_local="",
            active_from=date(2026, 4, 1),
            active_until=None,
            paused_at=None,
            created_at=_PINNED,
            deleted_at=None,
        )

    def test_view_is_slotted(self) -> None:
        view = self._view()
        with pytest.raises((AttributeError, TypeError)):
            view.extra = "nope"  # type: ignore[attr-defined]

    def test_view_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        view = self._view()
        with pytest.raises(FrozenInstanceError):
            view.name = "other"  # type: ignore[misc]

    def test_equality_by_value(self) -> None:
        assert self._view() == self._view()


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


class TestCreate:
    """:func:`create` inserts a row, returns a view, and writes one audit entry."""

    def test_happy_path(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="create-happy")
        ctx = _ctx(ws, slug="create-happy")
        tpl = _bootstrap_template(session, workspace_id=ws)

        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        assert view.id
        assert view.workspace_id == ws
        assert view.template_id == tpl.id
        assert view.rrule == "FREQ=WEEKLY;BYDAY=SA"
        assert view.dtstart_local == "2026-04-18T09:00"
        assert view.duration_minutes == 60
        assert view.paused_at is None
        assert view.deleted_at is None

    def test_writes_audit_row(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="create-audit")
        ctx = _ctx(ws, slug="create-audit")
        tpl = _bootstrap_template(session, workspace_id=ws)

        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        audit = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).one()
        assert audit.entity_kind == "schedule"
        assert audit.action == "create"
        assert audit.workspace_id == ws
        assert isinstance(audit.diff, dict)
        assert audit.diff["after"]["rrule"] == view.rrule

    def test_unknown_template_rejected(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="create-bad-tpl")
        ctx = _ctx(ws, slug="create-bad-tpl")

        with pytest.raises(ValueError) as exc:
            create(
                session,
                ctx,
                body=_minimal_body("tmpl-does-not-exist"),
                clock=FrozenClock(_PINNED),
            )
        assert "template_id" in str(exc.value)

    def test_soft_deleted_template_rejected(self, session: Session) -> None:
        """A schedule cannot attach to a retired template."""
        ws = _bootstrap_workspace(session, slug="create-del-tpl")
        ctx = _ctx(ws, slug="create-del-tpl")
        tpl = _bootstrap_template(session, workspace_id=ws)
        tpl.deleted_at = _PINNED
        session.flush()

        with pytest.raises(ValueError):
            create(
                session,
                ctx,
                body=_minimal_body(tpl.id),
                clock=FrozenClock(_PINNED),
            )

    def test_invalid_rrule_rejected(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="create-bad-rrule")
        ctx = _ctx(ws, slug="create-bad-rrule")
        tpl = _bootstrap_template(session, workspace_id=ws)

        with pytest.raises(InvalidRRule):
            create(
                session,
                ctx,
                body=_minimal_body(tpl.id, rrule="TOTALLY BROKEN"),
                clock=FrozenClock(_PINNED),
            )

    def test_zero_occurrence_rrule_rejected(self, session: Session) -> None:
        """A rule that yields nothing in its bounded window is an authoring error."""
        ws = _bootstrap_workspace(session, slug="create-zero")
        ctx = _ctx(ws, slug="create-zero")
        tpl = _bootstrap_template(session, workspace_id=ws)

        with pytest.raises(InvalidRRule):
            create(
                session,
                ctx,
                body=_minimal_body(tpl.id, rrule="FREQ=WEEKLY;COUNT=0"),
                clock=FrozenClock(_PINNED),
            )

    def test_invalid_rdate_rejected(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="create-bad-rdate")
        ctx = _ctx(ws, slug="create-bad-rdate")
        tpl = _bootstrap_template(session, workspace_id=ws)

        with pytest.raises(InvalidRRule) as exc:
            create(
                session,
                ctx,
                body=_minimal_body(tpl.id, rdate_local="not-a-date"),
                clock=FrozenClock(_PINNED),
            )
        assert "rdate_local" in str(exc.value)

    def test_backup_validator_fires(self, session: Session) -> None:
        """A validator hook that returns bad ids raises the 422."""
        ws = _bootstrap_workspace(session, slug="create-backup-bad")
        ctx = _ctx(ws, slug="create-backup-bad")
        tpl = _bootstrap_template(session, workspace_id=ws, role_id="role-x")

        def validator(
            sess: Session, context: WorkspaceContext, role_id: str, ids: Sequence[str]
        ) -> list[str]:
            return list(ids)  # every id is invalid

        with pytest.raises(InvalidBackupWorkRole) as exc:
            create(
                session,
                ctx,
                body=_minimal_body(tpl.id, backup_assignee_user_ids=["u1", "u2"]),
                clock=FrozenClock(_PINNED),
                backup_validator=validator,
            )
        assert exc.value.error == "backup_invalid_work_role"
        assert exc.value.invalid_user_ids == ("u1", "u2")
        assert exc.value.role_id == "role-x"

    def test_backup_validator_default_is_noop(self, session: Session) -> None:
        """Default hook lets any backup id through — validation lands with cd-65kn."""
        ws = _bootstrap_workspace(session, slug="create-backup-noop")
        ctx = _ctx(ws, slug="create-backup-noop")
        tpl = _bootstrap_template(session, workspace_id=ws)

        view = create(
            session,
            ctx,
            body=_minimal_body(tpl.id, backup_assignee_user_ids=["u1", "u2"]),
            clock=FrozenClock(_PINNED),
        )
        assert view.backup_assignee_user_ids == ("u1", "u2")

    def test_backup_validator_skipped_when_no_role_id(self, session: Session) -> None:
        """The spec's role check needs a role to check against.

        Template without ``role_id`` → validator not called; the
        backup list passes through as authored.
        """
        ws = _bootstrap_workspace(session, slug="create-no-role")
        ctx = _ctx(ws, slug="create-no-role")
        tpl = _bootstrap_template(session, workspace_id=ws, role_id=None)

        calls: list[object] = []

        def validator(
            sess: Session, context: WorkspaceContext, role_id: str, ids: Sequence[str]
        ) -> list[str]:
            calls.append(role_id)
            return list(ids)

        view = create(
            session,
            ctx,
            body=_minimal_body(tpl.id, backup_assignee_user_ids=["u1"]),
            clock=FrozenClock(_PINNED),
            backup_validator=validator,
        )
        assert view.backup_assignee_user_ids == ("u1",)
        assert calls == []


class TestRead:
    """:func:`read` returns the live schedule or raises 404."""

    def test_happy_path(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="read-happy")
        ctx = _ctx(ws, slug="read-happy")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        loaded = read(session, ctx, schedule_id=view.id)
        assert loaded.id == view.id
        assert loaded.name == view.name
        assert loaded.rrule == view.rrule

    def test_unknown_id_raises(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="read-404")
        ctx = _ctx(ws, slug="read-404")
        with pytest.raises(ScheduleNotFound):
            read(session, ctx, schedule_id="01HWA00000000000000NONE01")

    def test_cross_workspace_hidden(self, session: Session) -> None:
        ws_a = _bootstrap_workspace(session, slug="read-iso-a")
        ws_b = _bootstrap_workspace(session, slug="read-iso-b")
        ctx_a = _ctx(ws_a, slug="read-iso-a")
        ctx_b = _ctx(ws_b, slug="read-iso-b")
        tpl = _bootstrap_template(session, workspace_id=ws_a)
        view = create(
            session, ctx_a, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        with pytest.raises(ScheduleNotFound):
            read(session, ctx_b, schedule_id=view.id)

    def test_soft_deleted_hidden_by_default(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="read-deleted")
        ctx = _ctx(ws, slug="read-deleted")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        with pytest.raises(ScheduleNotFound):
            read(session, ctx, schedule_id=view.id)

    def test_soft_deleted_surfaced_with_flag(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="read-deleted-flag")
        ctx = _ctx(ws, slug="read-deleted-flag")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        loaded = read(session, ctx, schedule_id=view.id, include_deleted=True)
        assert loaded.id == view.id
        assert loaded.deleted_at is not None


class TestList:
    """:func:`list_schedules` honours every advertised filter."""

    def test_empty_workspace_returns_empty_list(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-empty")
        ctx = _ctx(ws, slug="list-empty")
        assert list_schedules(session, ctx) == []

    def test_template_id_filter(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-tpl")
        ctx = _ctx(ws, slug="list-tpl")
        tpl_a = _bootstrap_template(session, workspace_id=ws)
        tpl_b = _bootstrap_template(session, workspace_id=ws)
        view_a = create(
            session, ctx, body=_minimal_body(tpl_a.id), clock=FrozenClock(_PINNED)
        )
        create(session, ctx, body=_minimal_body(tpl_b.id), clock=FrozenClock(_PINNED))

        rows = list_schedules(session, ctx, template_id=tpl_a.id)
        assert [r.id for r in rows] == [view_a.id]

    def test_paused_filter(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-paused")
        ctx = _ctx(ws, slug="list-paused")
        tpl = _bootstrap_template(session, workspace_id=ws)
        live = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        gone = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        pause(session, ctx, schedule_id=gone.id, clock=FrozenClock(_PINNED))

        paused = list_schedules(session, ctx, paused=True)
        assert [r.id for r in paused] == [gone.id]
        unpaused = list_schedules(session, ctx, paused=False)
        assert [r.id for r in unpaused] == [live.id]

    def test_deleted_filter_excludes_by_default(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-del-def")
        ctx = _ctx(ws, slug="list-del-def")
        tpl = _bootstrap_template(session, workspace_id=ws)
        live = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        gone = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        delete(session, ctx, schedule_id=gone.id, clock=FrozenClock(_PINNED))

        rows = list_schedules(session, ctx)
        assert [r.id for r in rows] == [live.id]

    def test_deleted_filter_returns_only_deleted(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-del-flag")
        ctx = _ctx(ws, slug="list-del-flag")
        tpl = _bootstrap_template(session, workspace_id=ws)
        create(session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED))
        gone = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        delete(session, ctx, schedule_id=gone.id, clock=FrozenClock(_PINNED))

        rows = list_schedules(session, ctx, deleted=True)
        assert [r.id for r in rows] == [gone.id]

    def test_cross_workspace_isolation(self, session: Session) -> None:
        ws_a = _bootstrap_workspace(session, slug="list-iso-a")
        ws_b = _bootstrap_workspace(session, slug="list-iso-b")
        ctx_a = _ctx(ws_a, slug="list-iso-a")
        ctx_b = _ctx(ws_b, slug="list-iso-b")
        tpl_a = _bootstrap_template(session, workspace_id=ws_a)
        tpl_b = _bootstrap_template(session, workspace_id=ws_b)
        create(session, ctx_a, body=_minimal_body(tpl_a.id), clock=FrozenClock(_PINNED))
        create(session, ctx_b, body=_minimal_body(tpl_b.id), clock=FrozenClock(_PINNED))

        rows_a = list_schedules(session, ctx_a)
        assert len(rows_a) == 1
        assert rows_a[0].workspace_id == ws_a


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


class TestUpdate:
    """:func:`update` replaces the mutable body, writes an audit with diff."""

    def test_happy_path(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="upd-happy")
        ctx = _ctx(ws, slug="upd-happy")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        updated = update(
            session,
            ctx,
            schedule_id=view.id,
            body=_minimal_update(
                tpl.id, name="Renamed", rrule="FREQ=DAILY", duration_minutes=90
            ),
            clock=FrozenClock(_PINNED),
        )
        assert updated.name == "Renamed"
        assert updated.rrule == "FREQ=DAILY"
        assert updated.duration_minutes == 90

    def test_audit_carries_before_and_after(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="upd-audit")
        ctx = _ctx(ws, slug="upd-audit")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        update(
            session,
            ctx,
            schedule_id=view.id,
            body=_minimal_update(tpl.id, name="Renamed"),
            clock=FrozenClock(_PINNED),
        )

        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == view.id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
        update_row = next(a for a in audits if a.action == "update")
        assert update_row.diff["before"]["name"] != update_row.diff["after"]["name"]
        assert update_row.diff["after"]["name"] == "Renamed"

    def test_unknown_id_raises(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="upd-404")
        ctx = _ctx(ws, slug="upd-404")
        tpl = _bootstrap_template(session, workspace_id=ws)
        with pytest.raises(ScheduleNotFound):
            update(
                session,
                ctx,
                schedule_id="01HWA00000000000000NONE02",
                body=_minimal_update(tpl.id),
                clock=FrozenClock(_PINNED),
            )

    def test_soft_deleted_not_updateable(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="upd-deleted")
        ctx = _ctx(ws, slug="upd-deleted")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        with pytest.raises(ScheduleNotFound):
            update(
                session,
                ctx,
                schedule_id=view.id,
                body=_minimal_update(tpl.id, name="after delete"),
                clock=FrozenClock(_PINNED),
            )

    def test_cross_workspace_rejected(self, session: Session) -> None:
        ws_a = _bootstrap_workspace(session, slug="upd-iso-a")
        ws_b = _bootstrap_workspace(session, slug="upd-iso-b")
        ctx_a = _ctx(ws_a, slug="upd-iso-a")
        ctx_b = _ctx(ws_b, slug="upd-iso-b")
        tpl_a = _bootstrap_template(session, workspace_id=ws_a)
        tpl_b = _bootstrap_template(session, workspace_id=ws_b)
        view = create(
            session, ctx_a, body=_minimal_body(tpl_a.id), clock=FrozenClock(_PINNED)
        )

        with pytest.raises(ScheduleNotFound):
            update(
                session,
                ctx_b,
                schedule_id=view.id,
                body=_minimal_update(tpl_b.id, name="x"),
                clock=FrozenClock(_PINNED),
            )

    def test_backup_validator_fires_on_update(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="upd-backup-bad")
        ctx = _ctx(ws, slug="upd-backup-bad")
        tpl = _bootstrap_template(session, workspace_id=ws, role_id="role-x")
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        def validator(
            sess: Session, context: WorkspaceContext, role_id: str, ids: Sequence[str]
        ) -> list[str]:
            return list(ids)

        with pytest.raises(InvalidBackupWorkRole):
            update(
                session,
                ctx,
                schedule_id=view.id,
                body=_minimal_update(tpl.id, backup_assignee_user_ids=["bad-user"]),
                clock=FrozenClock(_PINNED),
                backup_validator=validator,
            )


class TestApplyToExisting:
    """``apply_to_existing=True`` patches only scheduled / pending tasks."""

    def test_only_scheduled_and_pending_are_patched(self, session: Session) -> None:
        """Tasks in other states must survive the update untouched (§06)."""
        ws = _bootstrap_workspace(session, slug="ate-states")
        ctx = _ctx(ws, slug="ate-states")
        tpl = _bootstrap_template(session, workspace_id=ws)
        prop = _bootstrap_property(session)
        new_user = _bootstrap_user(session, email="new-assignee@example.com")

        view = create(
            session,
            ctx,
            body=_minimal_body(tpl.id, property_id=prop),
            clock=FrozenClock(_PINNED),
        )

        sched_occ = _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="scheduled",
        )
        pending_occ = _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="pending",
        )
        in_prog_occ = _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="in_progress",
        )
        done_occ = _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="done",
        )

        update(
            session,
            ctx,
            schedule_id=view.id,
            body=_minimal_update(
                tpl.id,
                property_id=prop,
                default_assignee=new_user,
            ),
            apply_to_existing=True,
            clock=FrozenClock(_PINNED),
        )

        session.refresh(sched_occ)
        session.refresh(pending_occ)
        session.refresh(in_prog_occ)
        session.refresh(done_occ)
        assert sched_occ.assignee_user_id == new_user
        assert pending_occ.assignee_user_id == new_user
        assert in_prog_occ.assignee_user_id is None  # untouched
        assert done_occ.assignee_user_id is None  # untouched

    def test_flag_default_is_false(self, session: Session) -> None:
        """Without ``apply_to_existing`` the scheduled task is untouched."""
        ws = _bootstrap_workspace(session, slug="ate-default")
        ctx = _ctx(ws, slug="ate-default")
        tpl = _bootstrap_template(session, workspace_id=ws)
        prop = _bootstrap_property(session)
        new_user = _bootstrap_user(session, email="ate-default@example.com")
        view = create(
            session,
            ctx,
            body=_minimal_body(tpl.id, property_id=prop),
            clock=FrozenClock(_PINNED),
        )

        occ = _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="scheduled",
        )

        update(
            session,
            ctx,
            schedule_id=view.id,
            body=_minimal_update(tpl.id, property_id=prop, default_assignee=new_user),
            clock=FrozenClock(_PINNED),
        )
        session.refresh(occ)
        assert occ.assignee_user_id is None

    def test_apply_writes_dedicated_audit(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="ate-audit")
        ctx = _ctx(ws, slug="ate-audit")
        tpl = _bootstrap_template(session, workspace_id=ws)
        prop = _bootstrap_property(session)
        new_user = _bootstrap_user(session, email="ate-audit@example.com")
        view = create(
            session,
            ctx,
            body=_minimal_body(tpl.id, property_id=prop),
            clock=FrozenClock(_PINNED),
        )
        _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="scheduled",
        )

        update(
            session,
            ctx,
            schedule_id=view.id,
            body=_minimal_update(tpl.id, property_id=prop, default_assignee=new_user),
            apply_to_existing=True,
            clock=FrozenClock(_PINNED),
        )

        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == view.id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
        actions = [a.action for a in audits]
        assert "apply_to_existing" in actions


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


class TestPauseResume:
    """Pause and resume toggle ``paused_at`` and never cancel materialised tasks."""

    def test_pause_sets_paused_at(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="pause-set")
        ctx = _ctx(ws, slug="pause-set")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        paused = pause(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))
        assert paused.paused_at is not None

    def test_resume_clears_paused_at(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="resume-clear")
        ctx = _ctx(ws, slug="resume-clear")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        pause(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        resumed = resume(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))
        assert resumed.paused_at is None

    def test_pause_writes_audit(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="pause-audit")
        ctx = _ctx(ws, slug="pause-audit")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        pause(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == view.id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
        assert any(a.action == "pause" for a in audits)

    def test_resume_writes_audit(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="resume-audit")
        ctx = _ctx(ws, slug="resume-audit")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        pause(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))
        resume(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == view.id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
        assert any(a.action == "resume" for a in audits)

    def test_pause_is_idempotent_at_anchor(self, session: Session) -> None:
        """A second pause does not move ``paused_at`` (the anchor is the first pause).

        SQLite strips ``tzinfo`` on round-trip (our integration shard
        on Postgres proves the aware-UTC contract), so we compare the
        naive forms — the instant is identical on both reads.
        """
        ws = _bootstrap_workspace(session, slug="pause-idem")
        ctx = _ctx(ws, slug="pause-idem")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        first_clock = FrozenClock(_PINNED)
        first = pause(session, ctx, schedule_id=view.id, clock=first_clock)
        assert first.paused_at is not None
        first_anchor = first.paused_at

        later = _PINNED.replace(hour=13)
        second_clock = FrozenClock(later)
        second = pause(session, ctx, schedule_id=view.id, clock=second_clock)
        assert second.paused_at is not None
        # Re-load from the DB so both values round-trip through the
        # same SQLite tzinfo-stripping lens.
        row = session.scalars(select(Schedule).where(Schedule.id == view.id)).one()
        assert row.paused_at == first_anchor.replace(tzinfo=None) or (
            row.paused_at is not None
            and row.paused_at.replace(tzinfo=None) == first_anchor.replace(tzinfo=None)
        )

    def test_pause_does_not_cancel_materialised(self, session: Session) -> None:
        """Per §06 pause does not touch already-materialised tasks."""
        ws = _bootstrap_workspace(session, slug="pause-no-cancel")
        ctx = _ctx(ws, slug="pause-no-cancel")
        tpl = _bootstrap_template(session, workspace_id=ws)
        prop = _bootstrap_property(session)
        view = create(
            session,
            ctx,
            body=_minimal_body(tpl.id, property_id=prop),
            clock=FrozenClock(_PINNED),
        )
        occ = _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="scheduled",
        )

        pause(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))
        session.refresh(occ)
        assert occ.state == "scheduled"
        assert occ.cancellation_reason is None


class TestPauseVsActiveRange:
    """Pause always wins — §06 "Pause vs active range"."""

    def test_paused_schedule_inside_active_range_is_still_paused(
        self, session: Session
    ) -> None:
        """The view reflects both fields; callers see pause overriding the range."""
        ws = _bootstrap_workspace(session, slug="pausevsrange")
        ctx = _ctx(ws, slug="pausevsrange")
        tpl = _bootstrap_template(session, workspace_id=ws)
        # Active range covering today's date.
        view = create(
            session,
            ctx,
            body=_minimal_body(
                tpl.id,
                active_from="2026-01-01",
                active_until="2030-12-31",
            ),
            clock=FrozenClock(_PINNED),
        )
        paused = pause(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        # Both fields are visible in the view; the generator reads
        # ``paused_at`` first and returns a no-op — we assert the
        # data shape the generator will see.
        assert paused.paused_at is not None
        assert paused.active_from == date(2026, 1, 1)
        assert paused.active_until == date(2030, 12, 31)


# ---------------------------------------------------------------------------
# Delete + cascade
# ---------------------------------------------------------------------------


class TestDelete:
    """:func:`delete` soft-deletes and cancels scheduled linked tasks."""

    def test_soft_delete_sets_timestamp(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="del-soft")
        ctx = _ctx(ws, slug="del-soft")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        result = delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))
        assert result.deleted_at is not None
        row = session.scalars(select(Schedule).where(Schedule.id == view.id)).one()
        assert row.deleted_at is not None

    def test_cascade_cancels_scheduled_tasks_only(self, session: Session) -> None:
        """Only ``state='scheduled'`` rows flip; others stay untouched (§06)."""
        ws = _bootstrap_workspace(session, slug="del-cascade")
        ctx = _ctx(ws, slug="del-cascade")
        tpl = _bootstrap_template(session, workspace_id=ws)
        prop = _bootstrap_property(session)
        view = create(
            session,
            ctx,
            body=_minimal_body(tpl.id, property_id=prop),
            clock=FrozenClock(_PINNED),
        )
        sched_occ = _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="scheduled",
        )
        pending_occ = _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="pending",
        )
        done_occ = _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="done",
        )

        delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        session.refresh(sched_occ)
        session.refresh(pending_occ)
        session.refresh(done_occ)
        assert sched_occ.state == "cancelled"
        assert sched_occ.cancellation_reason == "schedule deleted"
        assert pending_occ.state == "pending"
        assert pending_occ.cancellation_reason is None
        assert done_occ.state == "done"
        assert done_occ.cancellation_reason is None

    def test_cascade_writes_audit(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="del-cascade-audit")
        ctx = _ctx(ws, slug="del-cascade-audit")
        tpl = _bootstrap_template(session, workspace_id=ws)
        prop = _bootstrap_property(session)
        view = create(
            session,
            ctx,
            body=_minimal_body(tpl.id, property_id=prop),
            clock=FrozenClock(_PINNED),
        )
        _insert_occurrence(
            session,
            workspace_id=ws,
            schedule_id=view.id,
            template_id=tpl.id,
            property_id=prop,
            state="scheduled",
        )

        delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == view.id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
        actions = [a.action for a in audits]
        assert "delete" in actions
        assert "delete_cascade" in actions
        cascade = next(a for a in audits if a.action == "delete_cascade")
        assert cascade.diff["cancellation_reason"] == "schedule deleted"

    def test_no_cascade_audit_when_no_scheduled_rows(self, session: Session) -> None:
        """No cascade row when there's nothing to cascade."""
        ws = _bootstrap_workspace(session, slug="del-no-cascade")
        ctx = _ctx(ws, slug="del-no-cascade")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == view.id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
        actions = [a.action for a in audits]
        assert "delete" in actions
        assert "delete_cascade" not in actions

    def test_soft_delete_is_idempotent(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="del-idem")
        ctx = _ctx(ws, slug="del-idem")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        with pytest.raises(ScheduleNotFound):
            delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

    def test_cross_workspace_rejected(self, session: Session) -> None:
        ws_a = _bootstrap_workspace(session, slug="del-iso-a")
        ws_b = _bootstrap_workspace(session, slug="del-iso-b")
        ctx_a = _ctx(ws_a, slug="del-iso-a")
        ctx_b = _ctx(ws_b, slug="del-iso-b")
        tpl = _bootstrap_template(session, workspace_id=ws_a)
        view = create(
            session, ctx_a, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        with pytest.raises(ScheduleNotFound):
            delete(
                session,
                ctx_b,
                schedule_id=view.id,
                clock=FrozenClock(_PINNED),
            )


# ---------------------------------------------------------------------------
# RRULE round-trip
# ---------------------------------------------------------------------------


class TestRRuleRoundTrip:
    """RRULE bodies survive the DB round-trip byte-for-byte."""

    def test_weekly_rrule_survives(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="rr-weekly")
        ctx = _ctx(ws, slug="rr-weekly")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session,
            ctx,
            body=_minimal_body(
                tpl.id,
                rrule="FREQ=WEEKLY;BYDAY=MO,TH;INTERVAL=2",
                dtstart_local="2026-04-20T07:30",
            ),
            clock=FrozenClock(_PINNED),
        )

        loaded = read(session, ctx, schedule_id=view.id)
        assert loaded.rrule == "FREQ=WEEKLY;BYDAY=MO,TH;INTERVAL=2"
        assert loaded.dtstart_local == "2026-04-20T07:30"

    def test_monthly_rrule_survives(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="rr-monthly")
        ctx = _ctx(ws, slug="rr-monthly")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session,
            ctx,
            body=_minimal_body(
                tpl.id,
                rrule="FREQ=MONTHLY;BYMONTHDAY=1",
                dtstart_local="2026-04-01T09:00",
            ),
            clock=FrozenClock(_PINNED),
        )
        loaded = read(session, ctx, schedule_id=view.id)
        assert loaded.rrule == "FREQ=MONTHLY;BYMONTHDAY=1"


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


class TestPreviewOccurrences:
    """The pure preview helper honours RDATE / EXDATE."""

    def test_weekly_preview(self) -> None:
        out = preview_occurrences(
            rrule="FREQ=WEEKLY;BYDAY=MO",
            dtstart_local="2026-04-20T09:00",  # Mon
            n=3,
        )
        assert len(out) == 3
        # 20 Apr, 27 Apr, 4 May 2026 are all Mondays.
        assert out[0].isoformat() == "2026-04-20T09:00:00"
        assert out[1].isoformat() == "2026-04-27T09:00:00"
        assert out[2].isoformat() == "2026-05-04T09:00:00"

    def test_exdate_skips_listed_date(self) -> None:
        """An EXDATE entry matching an RRULE occurrence is subtracted."""
        out = preview_occurrences(
            rrule="FREQ=WEEKLY;BYDAY=MO",
            dtstart_local="2026-04-20T09:00",
            n=3,
            exdate_local="2026-04-27T09:00",
        )
        # Mondays: 20 Apr, skip 27 Apr, 4 May, 11 May.
        starts = [o.isoformat() for o in out]
        assert "2026-04-27T09:00:00" not in starts
        assert starts[0] == "2026-04-20T09:00:00"
        assert starts[1] == "2026-05-04T09:00:00"
        assert starts[2] == "2026-05-11T09:00:00"

    def test_rdate_adds_extra_date(self) -> None:
        """RDATE dates are additive — they appear in the output sorted."""
        out = preview_occurrences(
            rrule="FREQ=WEEKLY;BYDAY=MO",
            dtstart_local="2026-04-20T09:00",
            n=4,
            rdate_local="2026-04-22T09:00",  # a Wednesday
        )
        starts = [o.isoformat() for o in out]
        assert starts == [
            "2026-04-20T09:00:00",
            "2026-04-22T09:00:00",
            "2026-04-27T09:00:00",
            "2026-05-04T09:00:00",
        ]

    def test_rdate_and_exdate_combine(self) -> None:
        out = preview_occurrences(
            rrule="FREQ=WEEKLY;BYDAY=MO",
            dtstart_local="2026-04-20T09:00",
            n=3,
            rdate_local="2026-04-22T09:00",
            exdate_local="2026-04-27T09:00",
        )
        starts = [o.isoformat() for o in out]
        assert starts == [
            "2026-04-20T09:00:00",
            "2026-04-22T09:00:00",
            "2026-05-04T09:00:00",
        ]

    def test_invalid_n_rejected(self) -> None:
        with pytest.raises(ValueError):
            preview_occurrences(
                rrule="FREQ=WEEKLY", dtstart_local="2026-04-20T09:00", n=0
            )
        with pytest.raises(ValueError):
            preview_occurrences(
                rrule="FREQ=WEEKLY", dtstart_local="2026-04-20T09:00", n=10_000
            )

    def test_invalid_rrule_rejected(self) -> None:
        with pytest.raises(InvalidRRule):
            preview_occurrences(rrule="nope", dtstart_local="2026-04-20T09:00", n=3)

    def test_invalid_rdate_rejected(self) -> None:
        with pytest.raises(InvalidRRule) as exc:
            preview_occurrences(
                rrule="FREQ=WEEKLY",
                dtstart_local="2026-04-20T09:00",
                n=3,
                rdate_local="not-a-date",
            )
        assert "rdate_local" in str(exc.value)

    def test_multi_line_rdate_payload(self) -> None:
        """Newline-separated RDATE lines are each parsed."""
        out = preview_occurrences(
            rrule="FREQ=WEEKLY;BYDAY=MO",
            dtstart_local="2026-04-20T09:00",
            n=4,
            rdate_local="2026-04-22T09:00\n2026-04-23T09:00",
        )
        starts = {o.isoformat() for o in out}
        assert "2026-04-22T09:00:00" in starts
        assert "2026-04-23T09:00:00" in starts

    def test_semicolon_separated_rdate(self) -> None:
        """A legacy semicolon-separated payload is accepted too."""
        out = preview_occurrences(
            rrule="FREQ=WEEKLY;BYDAY=MO",
            dtstart_local="2026-04-20T09:00",
            n=3,
            rdate_local="2026-04-22T09:00;2026-04-23T09:00",
        )
        starts = {o.isoformat() for o in out}
        assert "2026-04-22T09:00:00" in starts
        assert "2026-04-23T09:00:00" in starts

    def test_tz_aware_dtstart_rejected(self) -> None:
        """A tz-suffixed ``dtstart_local`` is rejected, not silently stripped."""
        with pytest.raises(InvalidRRule) as exc:
            preview_occurrences(
                rrule="FREQ=WEEKLY",
                dtstart_local="2026-04-20T09:00+02:00",
                n=3,
            )
        assert "timezone-naive" in str(exc.value)

    def test_zulu_dtstart_rejected(self) -> None:
        """A Zulu-suffixed ``dtstart_local`` is rejected for the same reason."""
        with pytest.raises(InvalidRRule):
            preview_occurrences(
                rrule="FREQ=WEEKLY",
                dtstart_local="2026-04-20T09:00:00Z",
                n=3,
            )


# ---------------------------------------------------------------------------
# Scheduler-hygiene side effects
# ---------------------------------------------------------------------------


class TestSchedulerHygiene:
    """``update()`` + ``delete()`` keep the worker's hot-path key honest.

    The generator (cd-22e) reads ``next_generation_at`` to decide when to
    walk a schedule. After a body replacement or a soft-delete, the
    pre-computed value is stale or actively dangerous; these tests pin
    the invariant so a future worker can trust the column.
    """

    def test_update_resets_next_generation_at(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="hyg-update")
        ctx = _ctx(ws, slug="hyg-update")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        row = session.scalars(select(Schedule).where(Schedule.id == view.id)).one()
        row.next_generation_at = _PINNED
        session.flush()

        update(
            session,
            ctx,
            schedule_id=view.id,
            body=_minimal_update(tpl.id, name="renamed"),
            clock=FrozenClock(_PINNED),
        )

        session.refresh(row)
        assert row.next_generation_at is None

    def test_delete_nulls_next_generation_at_and_disables(
        self, session: Session
    ) -> None:
        ws = _bootstrap_workspace(session, slug="hyg-delete")
        ctx = _ctx(ws, slug="hyg-delete")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )
        row = session.scalars(select(Schedule).where(Schedule.id == view.id)).one()
        row.next_generation_at = _PINNED
        row.enabled = True
        session.flush()

        delete(session, ctx, schedule_id=view.id, clock=FrozenClock(_PINNED))

        session.refresh(row)
        assert row.next_generation_at is None
        assert row.enabled is False
        assert row.deleted_at is not None


# ---------------------------------------------------------------------------
# View shape regressions
# ---------------------------------------------------------------------------


class TestViewShape:
    """Covers regression-prone corners of the row → view projection."""

    def test_active_from_none_for_pre_migration_rows(self, session: Session) -> None:
        """A row with NULL ``active_from`` surfaces ``None``, not ``date.min``.

        New writes through :func:`create` always populate the column,
        so this only matters for pre-migration survivors. The view
        must reflect the DB nullability faithfully rather than inventing
        a year-1 placeholder.
        """
        ws = _bootstrap_workspace(session, slug="view-pre-mig")
        ctx = _ctx(ws, slug="view-pre-mig")
        tpl = _bootstrap_template(session, workspace_id=ws)
        view = create(
            session, ctx, body=_minimal_body(tpl.id), clock=FrozenClock(_PINNED)
        )

        # Simulate a pre-migration row by clearing the new column.
        row = session.scalars(select(Schedule).where(Schedule.id == view.id)).one()
        row.active_from = None
        session.flush()

        loaded = read(session, ctx, schedule_id=view.id)
        assert loaded.active_from is None
