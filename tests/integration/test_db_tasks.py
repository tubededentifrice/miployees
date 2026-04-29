"""Integration tests for :mod:`app.adapters.db.tasks` against a real DB.

Covers the post-migration schema shape (tables, unique composites,
FKs, CHECK constraints), the referential-integrity contract on all
seven tables (CASCADE on workspace / template / occurrence, SET
NULL on user pointers + schedule / property, RESTRICT on
``occurrence.template_id``), happy-path round-trip of the full
template → schedule → occurrence → {checklist_item / evidence /
comment} chain, CHECK violations, and tenant-filter behaviour (all
seven tables scoped; SELECT without a
:class:`WorkspaceContext` raises :class:`TenantFilterMissing`).

The sibling ``tests/unit/test_db_tasks.py`` covers pure-Python
model construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"task_template",
§"schedule", §"occurrence", §"checklist_item", §"evidence",
§"comment", and ``docs/specs/06-tasks-and-scheduling.md``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import User
from app.adapters.db.places.models import Property
from app.adapters.db.tasks.models import (
    ChecklistItem,
    ChecklistTemplateItem,
    Comment,
    Evidence,
    Occurrence,
    Schedule,
    TaskTemplate,
)
from app.adapters.db.workspace.models import Workspace
from app.tenancy import registry, tenant_agnostic
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.tenancy.orm_filter import TenantFilterMissing, install_tenant_filter
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_PINNED_END = _PINNED + timedelta(hours=1)


_TASKS_TABLES: tuple[str, ...] = (
    "task_template",
    "checklist_template_item",
    "schedule",
    "occurrence",
    "checklist_item",
    "evidence",
    "comment",
)


@pytest.fixture(scope="module")
def filtered_factory(engine: Engine) -> sessionmaker[Session]:
    """Session factory with the tenant filter installed.

    Module-scoped so SQLAlchemy's per-sessionmaker event dispatch
    doesn't churn across tests. The top-level ``db_session`` fixture
    binds directly to a raw connection for SAVEPOINT isolation and
    therefore bypasses the filter; tests that need to observe
    :class:`TenantFilterMissing` use this factory explicitly.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    install_tenant_filter(factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`."""
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


@pytest.fixture(autouse=True)
def _ensure_tasks_registered() -> None:
    """Re-register the seven tasks tables as workspace-scoped before each test.

    ``app.adapters.db.tasks.__init__`` registers them at import time,
    but a sibling unit test
    (``tests/unit/test_tenancy_orm_filter.py``) calls
    :func:`registry._reset_for_tests` in an autouse fixture, which
    wipes the process-wide registry. Without this fixture the
    import-time registration loses the race and our tenant-filter
    assertions pass in isolation yet silently drop the filter under
    the full suite.
    """
    for table in _TASKS_TABLES:
        registry.register(table)


def _ctx_for(workspace: Workspace, actor_id: str) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace``."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRLT",
    )


def _seed_property(session: Session, *, property_id: str) -> Property:
    """Insert a :class:`Property` row (tenant-agnostic table)."""
    prop = Property(
        id=property_id,
        address="12 Chemin des Oliviers, Antibes",
        timezone="Europe/Paris",
        tags_json=[],
        created_at=_PINNED,
    )
    session.add(prop)
    session.flush()
    return prop


def _seed_template(
    session: Session,
    *,
    template_id: str,
    workspace_id: str,
    required_evidence: str = "none",
    default_assignee_role: str | None = None,
) -> TaskTemplate:
    """Insert a :class:`TaskTemplate` under the current ctx."""
    template = TaskTemplate(
        id=template_id,
        workspace_id=workspace_id,
        title="Clean kitchen",
        description_md="Wipe every surface.",
        default_duration_min=30,
        required_evidence=required_evidence,
        photo_required=False,
        default_assignee_role=default_assignee_role,
        checklist_template_json=[],
        created_at=_PINNED,
    )
    session.add(template)
    session.flush()
    return template


def _seed_schedule(
    session: Session,
    *,
    schedule_id: str,
    workspace_id: str,
    template_id: str,
    property_id: str | None,
) -> Schedule:
    sched = Schedule(
        id=schedule_id,
        workspace_id=workspace_id,
        template_id=template_id,
        property_id=property_id,
        rrule_text="FREQ=WEEKLY;BYDAY=MO",
        dtstart=_PINNED,
        enabled=True,
        created_at=_PINNED,
    )
    session.add(sched)
    session.flush()
    return sched


def _seed_occurrence(
    session: Session,
    *,
    occurrence_id: str,
    workspace_id: str,
    template_id: str,
    property_id: str,
    schedule_id: str | None = None,
    assignee_user_id: str | None = None,
    state: str = "pending",
) -> Occurrence:
    occ = Occurrence(
        id=occurrence_id,
        workspace_id=workspace_id,
        schedule_id=schedule_id,
        template_id=template_id,
        property_id=property_id,
        assignee_user_id=assignee_user_id,
        starts_at=_PINNED,
        ends_at=_PINNED_END,
        state=state,
        created_at=_PINNED,
    )
    session.add(occ)
    session.flush()
    return occ


class TestMigrationShape:
    """The migration lands all seven tables with correct keys + indexes."""

    def test_all_tables_exist(self, engine: Engine) -> None:
        tables = set(inspect(engine).get_table_names())
        for table in _TASKS_TABLES:
            assert table in tables, f"{table} missing from schema"

    def test_task_template_columns(self, engine: Engine) -> None:
        """Post-cd-0tg column set.

        The cd-chd slice landed the ten core columns (legacy pair
        preserved at the bottom for backward compat); cd-0tg's
        additive migration ``a9b3c7d5e2f1`` adds the richer spec
        columns needed by the CRUD service (``name``, ``role_id``,
        ``duration_minutes``, scope shape, photo-evidence enum,
        priority, linked instructions, inventory hints, LLM hints,
        soft-delete). Any further expansion or rename should update
        this list in the same commit.
        """
        cols = {c["name"]: c for c in inspect(engine).get_columns("task_template")}
        expected = {
            # cd-chd v1 slice — legacy columns, still present.
            "id",
            "workspace_id",
            "title",
            "description_md",
            "default_duration_min",
            "required_evidence",
            "photo_required",
            "default_assignee_role",
            "checklist_template_json",
            "created_at",
            # cd-0tg additive columns.
            "name",
            "role_id",
            "duration_minutes",
            "property_scope",
            "listed_property_ids",
            "area_scope",
            "listed_area_ids",
            "photo_evidence",
            "linked_instruction_ids",
            "priority",
            "inventory_effects_json",
            "llm_hints_md",
            "deleted_at",
        }
        assert set(cols) == expected
        assert cols["default_assignee_role"]["nullable"] is True
        assert cols["deleted_at"]["nullable"] is True

    def test_occurrence_per_acceptance_indexes(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("occurrence")}
        assert "ix_occurrence_workspace_assignee_starts" in indexes
        assert indexes["ix_occurrence_workspace_assignee_starts"]["column_names"] == [
            "workspace_id",
            "assignee_user_id",
            "starts_at",
        ]
        assert "ix_occurrence_workspace_state_starts" in indexes
        assert indexes["ix_occurrence_workspace_state_starts"]["column_names"] == [
            "workspace_id",
            "state",
            "starts_at",
        ]

    def test_schedule_workspace_next_gen_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("schedule")}
        assert "ix_schedule_workspace_next_gen" in indexes
        assert indexes["ix_schedule_workspace_next_gen"]["column_names"] == [
            "workspace_id",
            "next_generation_at",
        ]

    def test_comment_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("comment")}
        assert "ix_comment_workspace_occurrence_created" in indexes
        assert indexes["ix_comment_workspace_occurrence_created"]["column_names"] == [
            "workspace_id",
            "occurrence_id",
            "created_at",
        ]

    def test_evidence_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("evidence")}
        assert "ix_evidence_workspace_occurrence" in indexes
        assert indexes["ix_evidence_workspace_occurrence"]["column_names"] == [
            "workspace_id",
            "occurrence_id",
        ]

    def test_checklist_template_item_unique(self, engine: Engine) -> None:
        uniques = {
            u["name"]: u
            for u in inspect(engine).get_unique_constraints("checklist_template_item")
        }
        assert "uq_checklist_template_item_template_position" in uniques
        assert uniques["uq_checklist_template_item_template_position"][
            "column_names"
        ] == ["template_id", "position"]

    def test_checklist_item_unique(self, engine: Engine) -> None:
        uniques = {
            u["name"]: u
            for u in inspect(engine).get_unique_constraints("checklist_item")
        }
        assert "uq_checklist_item_occurrence_position" in uniques
        assert uniques["uq_checklist_item_occurrence_position"]["column_names"] == [
            "occurrence_id",
            "position",
        ]

    def test_occurrence_fks(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("occurrence")
        }
        # Template history: RESTRICT on delete (spec-critical).
        assert fks[("template_id",)]["referred_table"] == "task_template"
        assert fks[("template_id",)]["options"].get("ondelete") == "RESTRICT"
        # Schedule history: SET NULL (one-off & deleted-schedule both legal).
        assert fks[("schedule_id",)]["referred_table"] == "schedule"
        assert fks[("schedule_id",)]["options"].get("ondelete") == "SET NULL"
        # Property cascade.
        assert fks[("property_id",)]["referred_table"] == "property"
        assert fks[("property_id",)]["options"].get("ondelete") == "CASCADE"
        # Actor pointers all SET NULL.
        for col in ("assignee_user_id", "completed_by_user_id", "reviewer_user_id"):
            assert fks[(col,)]["referred_table"] == "user"
            assert fks[(col,)]["options"].get("ondelete") == "SET NULL"
        # Workspace cascade.
        assert fks[("workspace_id",)]["referred_table"] == "workspace"
        assert fks[("workspace_id",)]["options"].get("ondelete") == "CASCADE"

    def test_schedule_property_set_null(self, engine: Engine) -> None:
        fks = {
            tuple(fk["constrained_columns"]): fk
            for fk in inspect(engine).get_foreign_keys("schedule")
        }
        assert fks[("property_id",)]["referred_table"] == "property"
        assert fks[("property_id",)]["options"].get("ondelete") == "SET NULL"


class TestFullChainRoundTrip:
    """Insert the full template → schedule → occurrence → children chain."""

    def test_round_trip(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="chain-round@example.com",
            display_name="ChainRound",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="chain-round-ws",
            name="ChainRoundWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPR")

        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLR",
                workspace_id=ws.id,
                required_evidence="photo",
                default_assignee_role="worker",
            )
            db_session.add(
                ChecklistTemplateItem(
                    id="01HWA00000000000000000CTIR",
                    workspace_id=ws.id,
                    template_id=template.id,
                    label="Sweep floors",
                    position=0,
                    requires_photo=True,
                    created_at=_PINNED,
                )
            )
            sched = _seed_schedule(
                db_session,
                schedule_id="01HWA00000000000000000SCHR",
                workspace_id=ws.id,
                template_id=template.id,
                property_id=prop.id,
            )
            occ = _seed_occurrence(
                db_session,
                occurrence_id="01HWA00000000000000000OCCR",
                workspace_id=ws.id,
                template_id=template.id,
                property_id=prop.id,
                schedule_id=sched.id,
                assignee_user_id=user.id,
            )
            db_session.add(
                ChecklistItem(
                    id="01HWA00000000000000000CIR0",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    label="Sweep floors",
                    position=0,
                    requires_photo=True,
                    checked=False,
                )
            )
            db_session.add(
                Evidence(
                    id="01HWA00000000000000000EVR0",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    kind="photo",
                    blob_hash="sha256:deadbeef",
                    created_at=_PINNED,
                    created_by_user_id=user.id,
                )
            )
            db_session.add(
                Comment(
                    id="01HWA00000000000000000CMR0",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    author_user_id=user.id,
                    body_md="All good.",
                    created_at=_PINNED,
                    attachments_json=[],
                )
            )
            db_session.flush()

            # Read every row back under the same ctx.
            loaded_template = db_session.get(TaskTemplate, template.id)
            assert loaded_template is not None
            assert loaded_template.title == "Clean kitchen"
            assert loaded_template.required_evidence == "photo"
            assert loaded_template.default_assignee_role == "worker"

            ctis = db_session.scalars(
                select(ChecklistTemplateItem).where(
                    ChecklistTemplateItem.template_id == template.id
                )
            ).all()
            assert len(ctis) == 1
            assert ctis[0].label == "Sweep floors"

            loaded_sched = db_session.get(Schedule, sched.id)
            assert loaded_sched is not None
            assert loaded_sched.property_id == prop.id

            loaded_occ = db_session.get(Occurrence, occ.id)
            assert loaded_occ is not None
            assert loaded_occ.schedule_id == sched.id
            assert loaded_occ.state == "pending"

            assert (
                len(
                    db_session.scalars(
                        select(ChecklistItem).where(
                            ChecklistItem.occurrence_id == occ.id
                        )
                    ).all()
                )
                == 1
            )
            assert (
                len(
                    db_session.scalars(
                        select(Evidence).where(Evidence.occurrence_id == occ.id)
                    ).all()
                )
                == 1
            )
            assert (
                len(
                    db_session.scalars(
                        select(Comment).where(Comment.occurrence_id == occ.id)
                    ).all()
                )
                == 1
            )
        finally:
            reset_current(token)


class TestCheckConstraints:
    """CHECK constraints reject values outside the v1 enums."""

    def test_bogus_state_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-state@example.com",
            display_name="BogusState",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-state-ws",
            name="BogusStateWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPS")
        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLS",
                workspace_id=ws.id,
            )
            db_session.add(
                Occurrence(
                    id="01HWA00000000000000000OCCS",
                    workspace_id=ws.id,
                    template_id=template.id,
                    property_id=prop.id,
                    starts_at=_PINNED,
                    ends_at=_PINNED_END,
                    state="bogus",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_evidence_kind_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-evi@example.com",
            display_name="BogusEvi",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-evi-ws",
            name="BogusEviWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPE")
        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLE",
                workspace_id=ws.id,
            )
            occ = _seed_occurrence(
                db_session,
                occurrence_id="01HWA00000000000000000OCCE",
                workspace_id=ws.id,
                template_id=template.id,
                property_id=prop.id,
            )
            db_session.add(
                Evidence(
                    id="01HWA00000000000000000EVE0",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    kind="bogus",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_default_assignee_role_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-role@example.com",
            display_name="BogusRole",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-role-ws",
            name="BogusRoleWS",
            owner_user_id=user.id,
            clock=clock,
        )
        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                TaskTemplate(
                    id="01HWA00000000000000000TPLR",
                    workspace_id=ws.id,
                    title="bogus",
                    description_md="",
                    default_duration_min=15,
                    required_evidence="none",
                    photo_required=False,
                    default_assignee_role="overlord",
                    checklist_template_json=[],
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_bogus_required_evidence_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="bogus-req@example.com",
            display_name="BogusReq",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="bogus-req-ws",
            name="BogusReqWS",
            owner_user_id=user.id,
            clock=clock,
        )
        token = set_current(_ctx_for(ws, user.id))
        try:
            db_session.add(
                TaskTemplate(
                    id="01HWA00000000000000000TPLQ",
                    workspace_id=ws.id,
                    title="bogus",
                    description_md="",
                    default_duration_min=15,
                    required_evidence="bogus",
                    photo_required=False,
                    checklist_template_json=[],
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_ends_before_starts_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="rev-end@example.com",
            display_name="RevEnd",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="rev-end-ws",
            name="RevEndWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPZ")
        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLZ",
                workspace_id=ws.id,
            )
            db_session.add(
                Occurrence(
                    id="01HWA00000000000000000OCCZ",
                    workspace_id=ws.id,
                    template_id=template.id,
                    property_id=prop.id,
                    starts_at=_PINNED_END,
                    ends_at=_PINNED,
                    state="pending",
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_until_before_dtstart_rejected(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="rev-until@example.com",
            display_name="RevUntil",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="rev-until-ws",
            name="RevUntilWS",
            owner_user_id=user.id,
            clock=clock,
        )
        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLU",
                workspace_id=ws.id,
            )
            db_session.add(
                Schedule(
                    id="01HWA00000000000000000SCHU",
                    workspace_id=ws.id,
                    template_id=template.id,
                    rrule_text="FREQ=DAILY",
                    dtstart=_PINNED_END,
                    until=_PINNED,
                    enabled=True,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_null_until_accepted(self, db_session: Session) -> None:
        """A schedule with ``until IS NULL`` must persist.

        The CHECK is ``until IS NULL OR until > dtstart``; the null
        escape is what makes an open-ended recurrence legal.
        """
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="null-until@example.com",
            display_name="NullUntil",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="null-until-ws",
            name="NullUntilWS",
            owner_user_id=user.id,
            clock=clock,
        )
        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLN",
                workspace_id=ws.id,
            )
            _seed_schedule(
                db_session,
                schedule_id="01HWA00000000000000000SCHN",
                workspace_id=ws.id,
                template_id=template.id,
                property_id=None,
            )
        finally:
            reset_current(token)


class TestUniqueConstraints:
    """Unique composites reject duplicate positions."""

    def test_duplicate_checklist_template_item_position_rejected(
        self, db_session: Session
    ) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="dup-cti@example.com",
            display_name="DupCTI",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="dup-cti-ws",
            name="DupCTIWS",
            owner_user_id=user.id,
            clock=clock,
        )
        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLD",
                workspace_id=ws.id,
            )
            db_session.add(
                ChecklistTemplateItem(
                    id="01HWA00000000000000000CT10",
                    workspace_id=ws.id,
                    template_id=template.id,
                    label="First",
                    position=0,
                    requires_photo=False,
                    created_at=_PINNED,
                )
            )
            db_session.add(
                ChecklistTemplateItem(
                    id="01HWA00000000000000000CT11",
                    workspace_id=ws.id,
                    template_id=template.id,
                    label="Duplicate position",
                    position=0,
                    requires_photo=False,
                    created_at=_PINNED,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)

    def test_duplicate_checklist_item_position_rejected(
        self, db_session: Session
    ) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="dup-ci@example.com",
            display_name="DupCI",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="dup-ci-ws",
            name="DupCIWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPD")
        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLDI",
                workspace_id=ws.id,
            )
            occ = _seed_occurrence(
                db_session,
                occurrence_id="01HWA00000000000000000OCCD",
                workspace_id=ws.id,
                template_id=template.id,
                property_id=prop.id,
            )
            db_session.add(
                ChecklistItem(
                    id="01HWA00000000000000000CI00",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    label="First",
                    position=0,
                    requires_photo=False,
                    checked=False,
                )
            )
            db_session.add(
                ChecklistItem(
                    id="01HWA00000000000000000CI01",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    label="Duplicate position",
                    position=0,
                    requires_photo=False,
                    checked=False,
                )
            )
            with pytest.raises(IntegrityError):
                db_session.flush()
            db_session.rollback()
        finally:
            reset_current(token)


class TestCascadeAndRestrict:
    """FK cascade / SET NULL / RESTRICT behaviour."""

    def test_delete_occurrence_cascades_children(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="cascade-occ@example.com",
            display_name="CascadeOcc",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="cascade-occ-ws",
            name="CascadeOccWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPCA")
        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLCA",
                workspace_id=ws.id,
            )
            occ = _seed_occurrence(
                db_session,
                occurrence_id="01HWA00000000000000000OCCCA",
                workspace_id=ws.id,
                template_id=template.id,
                property_id=prop.id,
            )
            db_session.add(
                ChecklistItem(
                    id="01HWA00000000000000000CICA",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    label="Child",
                    position=0,
                    requires_photo=False,
                    checked=False,
                )
            )
            db_session.add(
                Evidence(
                    id="01HWA00000000000000000EVCA",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    kind="note",
                    note_md="Cascade note.",
                    created_at=_PINNED,
                    created_by_user_id=user.id,
                )
            )
            db_session.add(
                Comment(
                    id="01HWA00000000000000000CMCA",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    author_user_id=user.id,
                    body_md="Cascade comment.",
                    created_at=_PINNED,
                    attachments_json=[],
                )
            )
            db_session.flush()

            db_session.delete(occ)
            db_session.flush()

            assert (
                db_session.scalars(
                    select(ChecklistItem).where(ChecklistItem.occurrence_id == occ.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(Evidence).where(Evidence.occurrence_id == occ.id)
                ).all()
                == []
            )
            assert (
                db_session.scalars(
                    select(Comment).where(Comment.occurrence_id == occ.id)
                ).all()
                == []
            )
        finally:
            reset_current(token)

    def test_delete_template_with_occurrences_is_restricted(
        self, db_session: Session
    ) -> None:
        """``occurrence.template_id`` is RESTRICT — FK violation on delete.

        The test issues a raw DELETE (not the ORM cascade path) so
        SQLite enforces ON DELETE RESTRICT at flush time. An ORM-level
        ``session.delete(template)`` would first walk relationships
        and try to null dependents; with no relationship declared on
        these v1 models the ORM hands the DELETE straight through,
        so the RESTRICT constraint fires at the DB.
        """
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="restrict-tpl@example.com",
            display_name="RestrictTpl",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="restrict-tpl-ws",
            name="RestrictTplWS",
            owner_user_id=user.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPRT")
        token = set_current(_ctx_for(ws, user.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLRT",
                workspace_id=ws.id,
            )
            _seed_occurrence(
                db_session,
                occurrence_id="01HWA00000000000000000OCCRT",
                workspace_id=ws.id,
                template_id=template.id,
                property_id=prop.id,
            )

            # justification: raw DELETE straddles the tenant filter's
            # rewrite path (the filter only rewrites Select / Update /
            # Delete ORM statements built against Table objects) and
            # the RESTRICT check is what we're exercising. A ctx
            # block keeps the filter quiet on the surrounding reads.
            with pytest.raises(IntegrityError), tenant_agnostic():
                db_session.execute(
                    text("DELETE FROM task_template WHERE id = :id"),
                    {"id": template.id},
                )
            db_session.rollback()
        finally:
            reset_current(token)

    def test_delete_user_sets_pointers_null(self, db_session: Session) -> None:
        """Deleting a user nulls every actor pointer on tasks rows."""
        clock = FrozenClock(_PINNED)
        actor = bootstrap_user(
            db_session,
            email="actor@example.com",
            display_name="Actor",
            clock=clock,
        )
        owner = bootstrap_user(
            db_session,
            email="owner@example.com",
            display_name="Owner",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="null-user-ws",
            name="NullUserWS",
            owner_user_id=owner.id,
            clock=clock,
        )
        prop = _seed_property(db_session, property_id="01HWA00000000000000000PRPNU")
        token = set_current(_ctx_for(ws, owner.id))
        try:
            template = _seed_template(
                db_session,
                template_id="01HWA00000000000000000TPLNU",
                workspace_id=ws.id,
            )
            occ = Occurrence(
                id="01HWA00000000000000000OCCNU",
                workspace_id=ws.id,
                template_id=template.id,
                property_id=prop.id,
                assignee_user_id=actor.id,
                completed_by_user_id=actor.id,
                reviewer_user_id=actor.id,
                starts_at=_PINNED,
                ends_at=_PINNED_END,
                state="pending",
                created_at=_PINNED,
            )
            db_session.add(occ)
            # Flush occurrence first so the FK target exists before
            # SQLAlchemy's UoW walks the rest. With no declared
            # relationship() between occurrence → {evidence, comment}
            # the UoW insertion order follows ``session.add`` order,
            # which on SQLite can trip ON INSERT FK enforcement if a
            # child row beats the parent into the same flush batch.
            db_session.flush()
            db_session.add(
                Evidence(
                    id="01HWA00000000000000000EVNU",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    kind="note",
                    note_md="Null the actor.",
                    created_at=_PINNED,
                    created_by_user_id=actor.id,
                )
            )
            db_session.add(
                Comment(
                    id="01HWA00000000000000000CMNU",
                    workspace_id=ws.id,
                    occurrence_id=occ.id,
                    author_user_id=actor.id,
                    body_md="Null the author.",
                    created_at=_PINNED,
                    attachments_json=[],
                )
            )
            db_session.flush()
        finally:
            reset_current(token)

        # Deleting a :class:`User` row is inherently cross-tenant —
        # it's a platform-level op, not scoped to any workspace.
        # justification: user is tenant-agnostic; no ctx to pin.
        with tenant_agnostic():
            loaded_actor = db_session.get(User, actor.id)
            assert loaded_actor is not None
            db_session.delete(loaded_actor)
            db_session.flush()

        # Expire the identity map so subsequent reads re-fetch from
        # the DB; ON DELETE SET NULL is a DB-side side effect that
        # the ORM doesn't observe through the in-memory copy.
        db_session.expire_all()

        token = set_current(_ctx_for(ws, owner.id))
        try:
            loaded_occ = db_session.get(Occurrence, "01HWA00000000000000000OCCNU")
            assert loaded_occ is not None
            assert loaded_occ.assignee_user_id is None
            assert loaded_occ.completed_by_user_id is None
            assert loaded_occ.reviewer_user_id is None
            loaded_ev = db_session.get(Evidence, "01HWA00000000000000000EVNU")
            assert loaded_ev is not None
            assert loaded_ev.created_by_user_id is None
            loaded_comment = db_session.get(Comment, "01HWA00000000000000000CMNU")
            assert loaded_comment is not None
            assert loaded_comment.author_user_id is None
        finally:
            reset_current(token)


class TestTenantFilter:
    """All seven tasks tables are workspace-scoped under the filter."""

    @pytest.mark.parametrize("model", [TaskTemplate, ChecklistTemplateItem, Schedule])
    def test_template_layer_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[TaskTemplate] | type[ChecklistTemplateItem] | type[Schedule],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__

    @pytest.mark.parametrize(
        "model",
        [Occurrence, ChecklistItem, Evidence, Comment],
    )
    def test_occurrence_layer_read_without_ctx_raises(
        self,
        filtered_factory: sessionmaker[Session],
        model: type[Occurrence] | type[ChecklistItem] | type[Evidence] | type[Comment],
    ) -> None:
        with (
            filtered_factory() as session,
            pytest.raises(TenantFilterMissing) as exc,
        ):
            session.execute(select(model))
        assert exc.value.table == model.__tablename__
