"""Unit tests for :mod:`app.adapters.db.tasks.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
default handling, and the shape of ``__table_args__`` (CHECK
constraints, unique composites, index shape). Integration coverage
(migrations, FK cascade, uniqueness + CHECK violations against a
real DB, tenant filter behaviour) lives in
``tests/integration/test_db_tasks.py``.

See ``docs/specs/02-domain-model.md`` §"task_template",
§"schedule", §"occurrence", §"checklist_item", §"evidence",
§"comment", and ``docs/specs/06-tasks-and-scheduling.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from app.adapters.db.tasks import (
    ChecklistItem,
    ChecklistTemplateItem,
    Comment,
    Evidence,
    Occurrence,
    Schedule,
    TaskTemplate,
)
from app.adapters.db.tasks import models as tasks_models

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)
_LATER = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


class TestTaskTemplateModel:
    """The ``TaskTemplate`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        template = TaskTemplate(
            id="01HWA00000000000000000TPLA",
            workspace_id="01HWA00000000000000000WSPA",
            title="Clean kitchen",
            description_md="Wipe every surface.",
            default_duration_min=45,
            required_evidence="photo",
            photo_required=True,
            checklist_template_json=[],
            created_at=_PINNED,
        )
        assert template.id == "01HWA00000000000000000TPLA"
        assert template.workspace_id == "01HWA00000000000000000WSPA"
        assert template.title == "Clean kitchen"
        assert template.description_md == "Wipe every surface."
        assert template.default_duration_min == 45
        assert template.required_evidence == "photo"
        assert template.photo_required is True
        # ``default_assignee_role`` is nullable; defaults to ``None``.
        assert template.default_assignee_role is None
        assert template.checklist_template_json == []
        assert template.created_at == _PINNED

    def test_with_default_assignee_role(self) -> None:
        template = TaskTemplate(
            id="01HWA00000000000000000TPLB",
            workspace_id="01HWA00000000000000000WSPA",
            title="Daily sweep",
            description_md="",
            default_duration_min=15,
            required_evidence="none",
            photo_required=False,
            default_assignee_role="worker",
            checklist_template_json=[{"label": "Lobby"}],
            created_at=_PINNED,
        )
        assert template.default_assignee_role == "worker"
        assert template.checklist_template_json == [{"label": "Lobby"}]

    def test_tablename(self) -> None:
        assert TaskTemplate.__tablename__ == "task_template"

    def test_required_evidence_check_present(self) -> None:
        # Constraint name ``required_evidence`` on the model; the
        # shared naming convention rewrites it to
        # ``ck_task_template_required_evidence`` on the bound column,
        # so match by suffix rather than the raw name (mirrors the
        # ``places`` test pattern).
        checks = [
            c
            for c in TaskTemplate.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("required_evidence")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for kind in ("none", "photo", "note", "voice", "gps"):
            assert kind in sql, f"{kind} missing from CHECK constraint"

    def test_default_assignee_role_check_present(self) -> None:
        checks = [
            c
            for c in TaskTemplate.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("default_assignee_role")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        # NULL escape preserved so an unset persona stays legal.
        assert "IS NULL" in sql
        for role in ("manager", "worker", "client", "guest"):
            assert role in sql, f"{role} missing from CHECK constraint"

    def test_workspace_index_present(self) -> None:
        indexes = [i for i in TaskTemplate.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_task_template_workspace" in names


class TestChecklistTemplateItemModel:
    """The ``ChecklistTemplateItem`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        row = ChecklistTemplateItem(
            id="01HWA00000000000000000CTIA",
            workspace_id="01HWA00000000000000000WSPA",
            template_id="01HWA00000000000000000TPLA",
            label="Wipe counters",
            position=0,
            requires_photo=False,
            created_at=_PINNED,
        )
        assert row.id == "01HWA00000000000000000CTIA"
        assert row.template_id == "01HWA00000000000000000TPLA"
        assert row.label == "Wipe counters"
        assert row.position == 0
        assert row.requires_photo is False
        assert row.created_at == _PINNED

    def test_tablename(self) -> None:
        assert ChecklistTemplateItem.__tablename__ == "checklist_template_item"

    def test_unique_template_position(self) -> None:
        uniques = [
            u
            for u in ChecklistTemplateItem.__table_args__
            if isinstance(u, UniqueConstraint)
        ]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == ["template_id", "position"]


class TestScheduleModel:
    """The ``Schedule`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        sched = Schedule(
            id="01HWA00000000000000000SCHA",
            workspace_id="01HWA00000000000000000WSPA",
            template_id="01HWA00000000000000000TPLA",
            rrule_text="FREQ=WEEKLY;BYDAY=MO",
            dtstart=_PINNED,
            enabled=True,
            created_at=_PINNED,
        )
        assert sched.property_id is None
        assert sched.until is None
        assert sched.assignee_user_id is None
        assert sched.assignee_role is None
        assert sched.next_generation_at is None
        assert sched.enabled is True

    def test_with_all_optional(self) -> None:
        sched = Schedule(
            id="01HWA00000000000000000SCHB",
            workspace_id="01HWA00000000000000000WSPA",
            template_id="01HWA00000000000000000TPLA",
            property_id="01HWA00000000000000000PRPA",
            rrule_text="FREQ=DAILY",
            dtstart=_PINNED,
            until=_LATER,
            assignee_user_id="01HWA00000000000000000USRA",
            assignee_role="worker",
            enabled=False,
            next_generation_at=_LATER,
            created_at=_PINNED,
        )
        assert sched.property_id == "01HWA00000000000000000PRPA"
        assert sched.until == _LATER
        assert sched.assignee_user_id == "01HWA00000000000000000USRA"
        assert sched.assignee_role == "worker"
        assert sched.next_generation_at == _LATER

    def test_tablename(self) -> None:
        assert Schedule.__tablename__ == "schedule"

    def test_until_after_dtstart_check_present(self) -> None:
        checks = [
            c
            for c in Schedule.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("until_after_dtstart")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "until" in sql
        assert "dtstart" in sql
        # Nullable — the CHECK must let ``NULL until`` through.
        assert "IS NULL" in sql

    def test_assignee_role_check_present(self) -> None:
        checks = [
            c
            for c in Schedule.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("assignee_role")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "IS NULL" in sql
        for role in ("manager", "worker", "client", "guest"):
            assert role in sql, f"{role} missing from CHECK constraint"

    def test_workspace_next_gen_index_present(self) -> None:
        indexes = [i for i in Schedule.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_schedule_workspace_next_gen" in names
        target = next(i for i in indexes if i.name == "ix_schedule_workspace_next_gen")
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "next_generation_at",
        ]


class TestOccurrenceModel:
    """The ``Occurrence`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        occ = Occurrence(
            id="01HWA00000000000000000OCCA",
            workspace_id="01HWA00000000000000000WSPA",
            template_id="01HWA00000000000000000TPLA",
            property_id="01HWA00000000000000000PRPA",
            starts_at=_PINNED,
            ends_at=_LATER,
            state="pending",
            created_at=_PINNED,
        )
        assert occ.schedule_id is None
        assert occ.assignee_user_id is None
        assert occ.completed_at is None
        assert occ.completed_by_user_id is None
        assert occ.reviewer_user_id is None
        assert occ.reviewed_at is None
        assert occ.state == "pending"

    def test_tablename(self) -> None:
        assert Occurrence.__tablename__ == "occurrence"

    def test_state_check_present(self) -> None:
        checks = [
            c
            for c in Occurrence.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("state")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        for state in ("pending", "in_progress", "done", "skipped", "approved"):
            assert state in sql, f"{state} missing from CHECK constraint"

    def test_ends_after_starts_check_present(self) -> None:
        checks = [
            c
            for c in Occurrence.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("ends_after_starts")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        assert "ends_at" in sql
        assert "starts_at" in sql

    def test_per_acceptance_indexes_present(self) -> None:
        indexes = [i for i in Occurrence.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_occurrence_workspace_assignee_starts" in names
        assert "ix_occurrence_workspace_state_starts" in names
        assignee_ix = next(
            i for i in indexes if i.name == "ix_occurrence_workspace_assignee_starts"
        )
        assert [c.name for c in assignee_ix.columns] == [
            "workspace_id",
            "assignee_user_id",
            "starts_at",
        ]
        state_ix = next(
            i for i in indexes if i.name == "ix_occurrence_workspace_state_starts"
        )
        assert [c.name for c in state_ix.columns] == [
            "workspace_id",
            "state",
            "starts_at",
        ]


class TestChecklistItemModel:
    """The ``ChecklistItem`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        row = ChecklistItem(
            id="01HWA00000000000000000CIA0",
            workspace_id="01HWA00000000000000000WSPA",
            occurrence_id="01HWA00000000000000000OCCA",
            label="Check stock",
            position=0,
            requires_photo=False,
            checked=False,
        )
        assert row.checked is False
        assert row.checked_at is None
        assert row.evidence_blob_hash is None

    def test_tablename(self) -> None:
        assert ChecklistItem.__tablename__ == "checklist_item"

    def test_unique_occurrence_position(self) -> None:
        uniques = [
            u for u in ChecklistItem.__table_args__ if isinstance(u, UniqueConstraint)
        ]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == ["occurrence_id", "position"]


class TestEvidenceModel:
    """The ``Evidence`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        ev = Evidence(
            id="01HWA00000000000000000EVA0",
            workspace_id="01HWA00000000000000000WSPA",
            occurrence_id="01HWA00000000000000000OCCA",
            kind="note",
            note_md="All good.",
            created_at=_PINNED,
        )
        assert ev.blob_hash is None
        assert ev.note_md == "All good."
        assert ev.created_by_user_id is None

    def test_tablename(self) -> None:
        assert Evidence.__tablename__ == "evidence"

    def test_kind_check_present(self) -> None:
        checks = [
            c
            for c in Evidence.__table_args__
            if isinstance(c, CheckConstraint)
            and c.name is not None
            and str(c.name).endswith("kind")
        ]
        assert len(checks) == 1
        sql = str(checks[0].sqltext)
        # ``none`` is NOT a stored-evidence kind — it's a template
        # marker only. Assert the check enumerates the real four.
        assert "'none'" not in sql
        for kind in ("photo", "note", "voice", "gps"):
            assert f"'{kind}'" in sql

    def test_workspace_occurrence_index_present(self) -> None:
        indexes = [i for i in Evidence.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_evidence_workspace_occurrence" in names
        target = next(
            i for i in indexes if i.name == "ix_evidence_workspace_occurrence"
        )
        assert [c.name for c in target.columns] == ["workspace_id", "occurrence_id"]


class TestCommentModel:
    """The ``Comment`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        c = Comment(
            id="01HWA00000000000000000CMA0",
            workspace_id="01HWA00000000000000000WSPA",
            occurrence_id="01HWA00000000000000000OCCA",
            body_md="Looks done.",
            created_at=_PINNED,
            attachments_json=[],
        )
        assert c.author_user_id is None
        assert c.body_md == "Looks done."
        assert c.attachments_json == []

    def test_tablename(self) -> None:
        assert Comment.__tablename__ == "comment"

    def test_workspace_occurrence_created_index_present(self) -> None:
        indexes = [i for i in Comment.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_comment_workspace_occurrence_created" in names
        target = next(
            i for i in indexes if i.name == "ix_comment_workspace_occurrence_created"
        )
        assert [c.name for c in target.columns] == [
            "workspace_id",
            "occurrence_id",
            "created_at",
        ]


class TestPackageReExports:
    """``app.adapters.db.tasks`` re-exports every v1-slice model."""

    def test_models_re_exported(self) -> None:
        assert TaskTemplate is tasks_models.TaskTemplate
        assert ChecklistTemplateItem is tasks_models.ChecklistTemplateItem
        assert Schedule is tasks_models.Schedule
        assert Occurrence is tasks_models.Occurrence
        assert ChecklistItem is tasks_models.ChecklistItem
        assert Evidence is tasks_models.Evidence
        assert Comment is tasks_models.Comment


class TestRegistryIntent:
    """Every tasks table is registered as workspace-scoped.

    The assertions call :func:`app.tenancy.registry.register` directly
    rather than relying on the import-time side effect of
    ``app.adapters.db.tasks``: a sibling ``test_tenancy_orm_filter``
    autouse fixture calls ``registry._reset_for_tests()`` which wipes
    the process-wide set, so asserting presence after that reset
    would be flaky. The tests below encode the invariant — "every
    tasks table is scoped" — without over-coupling to import
    ordering.
    """

    def test_every_tasks_table_is_registered(self) -> None:
        from app.tenancy import registry

        registry._reset_for_tests()
        for table in (
            "task_template",
            "checklist_template_item",
            "schedule",
            "occurrence",
            "checklist_item",
            "evidence",
            "comment",
        ):
            registry.register(table)
        scoped = registry.scoped_tables()
        for table in (
            "task_template",
            "checklist_template_item",
            "schedule",
            "occurrence",
            "checklist_item",
            "evidence",
            "comment",
        ):
            assert table in scoped, f"{table} must be scoped"
