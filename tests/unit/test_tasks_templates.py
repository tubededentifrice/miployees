"""Unit tests for :mod:`app.domain.tasks.templates`.

Exercises the CRUD service surface against an in-memory SQLite
engine built via ``Base.metadata.create_all()`` — no alembic, no
tenant filter, just the ORM round-trip + the pure-Python DTO
validators. This keeps the unit-test budget tight; the integration
shard (``tests/integration/tasks/test_templates.py``) will cover
the full migration + tenant-filter + cross-backend path once it
lands.

Covers:

* :class:`TaskTemplateCreate` / :class:`TaskTemplateUpdate`
  validation: every scope-consistency branch, checklist-key
  uniqueness, inventory positivity, field length caps.
* Error-class hierarchy: 404 → ``LookupError``, 422 →
  ``ValueError`` / ``ScopeInconsistent``, 409 →
  :class:`TemplateInUseError`.
* :class:`TaskTemplateView`: frozen + slotted, pure value type.
* CRUD round-trip: create → read → list (with each filter) →
  update → soft-delete; every mutation writes one ``audit_log``
  row with the expected action + diff shape.
* In-use guard: ``delete`` rejects a template referenced by a
  live ``schedule.template_id`` and surfaces the offending ids.
* ``WorkspaceContext`` is the only source of ``workspace_id`` —
  a row inserted in workspace A is invisible from workspace B.

See ``docs/specs/06-tasks-and-scheduling.md`` §"Task template",
§"Checklist template shape".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.adapters.db.tasks.models import Schedule, TaskTemplate
from app.adapters.db.workspace.models import Workspace
from app.domain.tasks.templates import (
    ChecklistRRULEError,
    ChecklistTemplateItem,
    ChecklistTemplateItemPayload,
    ScopeInconsistent,
    TaskTemplateCreate,
    TaskTemplateNotFound,
    TaskTemplateUpdate,
    TaskTemplateView,
    TemplateInUseError,
    create,
    delete,
    expand_checklist_for_task,
    list_templates,
    read,
    read_many,
    reorder_checklist,
    update,
    validate_checklist_template,
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

    The tasks schema carries foreign keys into ``user`` / ``property``
    / ``workspace`` — without those models loaded on the shared
    ``Base.metadata``, :meth:`Base.metadata.create_all` raises
    :class:`~sqlalchemy.exc.NoReferencedTableError`. Mirrors the
    discovery loop in :mod:`migrations.env`.
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
            # Only swallow "this context has no models module" —
            # any other import error is a real bug.
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``.

    We deliberately do NOT run alembic here: the service is tested
    against the ORM schema only. Integration-level migration + tenant-
    filter coverage lives in the integration shard.
    """
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
    """Insert a minimum-field :class:`Workspace` row and return its id.

    The tasks ORM enforces a FK from ``task_template.workspace_id`` to
    ``workspace.id``; every test that inserts a template needs a
    parent row to satisfy the constraint.
    """
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


def _ctx(
    workspace_id: str, *, slug: str = "ws", actor: str | None = None
) -> WorkspaceContext:
    """Build a :class:`WorkspaceContext` pinned to ``workspace_id``."""
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor or "01HWA00000000000000000USR1",
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _minimal_payload(**overrides: object) -> dict[str, object]:
    """Return a plain dict of sensible-default template fields.

    Tests that exercise a specific field pass ``**overrides``; the
    helper keeps boilerplate out of the test bodies and makes
    diff-readability easy ("this test differs only in `priority`").
    The dict form is reused to build either :class:`TaskTemplateCreate`
    or :class:`TaskTemplateUpdate` from the same base — both DTOs
    share the exact same field shape.
    """
    base: dict[str, object] = {
        "name": "Weekly kitchen deep clean",
        "description_md": "Full sweep of the kitchen.",
        "duration_minutes": 45,
        "priority": "normal",
        "photo_evidence": "optional",
    }
    base.update(overrides)
    return base


def _minimal_body(**overrides: object) -> TaskTemplateCreate:
    """:class:`TaskTemplateCreate` constructed from :func:`_minimal_payload`."""
    return TaskTemplateCreate.model_validate(_minimal_payload(**overrides))


def _minimal_update(**overrides: object) -> TaskTemplateUpdate:
    """:class:`TaskTemplateUpdate` constructed from :func:`_minimal_payload`."""
    return TaskTemplateUpdate.model_validate(_minimal_payload(**overrides))


# ---------------------------------------------------------------------------
# Error-class hierarchy
# ---------------------------------------------------------------------------


class TestErrorTypes:
    """Each error subclasses the right stdlib parent for router mapping."""

    def test_not_found_is_lookup_error(self) -> None:
        """404 path: routers catch ``LookupError`` at the HTTP boundary."""
        assert issubclass(TaskTemplateNotFound, LookupError)

    def test_scope_inconsistent_is_value_error(self) -> None:
        """422 path: ``ValueError`` maps to unprocessable-entity."""
        assert issubclass(ScopeInconsistent, ValueError)

    def test_template_in_use_is_value_error(self) -> None:
        """409 path: ``TemplateInUseError`` is a ``ValueError`` subclass.

        The router differentiates 409 from 422 by the concrete
        ``TemplateInUseError`` type (it's a dedicated handler);
        base-class membership keeps the fallback 422 path correct.
        """
        assert issubclass(TemplateInUseError, ValueError)

    def test_errors_are_distinct(self) -> None:
        classes = {TaskTemplateNotFound, ScopeInconsistent, TemplateInUseError}
        assert len(classes) == 3


class TestTemplateInUseErrorPayload:
    """Payload carries the offending ids for UI rendering."""

    def test_schedule_ids_are_preserved(self) -> None:
        err = TemplateInUseError(
            template_id="tpl-1",
            schedule_ids=("sched-1", "sched-2"),
        )
        assert err.template_id == "tpl-1"
        assert err.schedule_ids == ("sched-1", "sched-2")
        assert err.stay_lifecycle_rule_ids == ()

    def test_message_counts_consumers(self) -> None:
        err = TemplateInUseError(
            template_id="tpl-1",
            schedule_ids=("sched-1", "sched-2"),
        )
        assert "2 schedule" in str(err)

    def test_sequence_is_coerced_to_tuple(self) -> None:
        """The stored payload is immutable even when the caller passes a list."""
        err = TemplateInUseError(
            template_id="tpl-1",
            schedule_ids=["sched-1"],
        )
        assert isinstance(err.schedule_ids, tuple)
        assert err.schedule_ids == ("sched-1",)

    def test_stay_lifecycle_ids_slot_exists(self) -> None:
        """The field is always present — even in the v1 empty case."""
        err = TemplateInUseError(template_id="tpl-1", schedule_ids=["s"])
        assert err.stay_lifecycle_rule_ids == ()


# ---------------------------------------------------------------------------
# TaskTemplateView invariants
# ---------------------------------------------------------------------------


class TestTaskTemplateView:
    """``TaskTemplateView`` is a frozen, slotted value type."""

    def _view(self) -> TaskTemplateView:
        return TaskTemplateView(
            id="tpl",
            workspace_id="ws",
            name="Sample",
            description_md="",
            role_id=None,
            duration_minutes=30,
            property_scope="any",
            listed_property_ids=(),
            area_scope="any",
            listed_area_ids=(),
            checklist_template_json=(),
            photo_evidence="disabled",
            linked_instruction_ids=(),
            priority="normal",
            inventory_consumption_json={},
            llm_hints_md=None,
            created_at=_PINNED,
            deleted_at=None,
        )

    def test_view_is_slotted(self) -> None:
        """Slotted frozen dataclasses reject new attributes at runtime."""
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
# DTO validators
# ---------------------------------------------------------------------------


class TestPropertyScopeValidation:
    """Every ``property_scope`` branch is gated."""

    def test_any_rejects_ids(self) -> None:
        """``property_scope='any'`` forbids any ids in the list."""
        with pytest.raises(ValidationError) as exc:
            TaskTemplateCreate(
                name="t",
                property_scope="any",
                listed_property_ids=["p1"],
            )
        assert "property_scope='any'" in str(exc.value)

    def test_any_with_empty_list_accepted(self) -> None:
        dto = TaskTemplateCreate(name="t", property_scope="any", listed_property_ids=[])
        assert dto.property_scope == "any"

    def test_one_requires_exactly_one_id(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplateCreate(
                name="t",
                property_scope="one",
                listed_property_ids=["p1", "p2"],
            )
        assert "exactly one" in str(exc.value)

    def test_one_with_empty_list_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(
                name="t",
                property_scope="one",
                listed_property_ids=[],
            )

    def test_one_with_single_id_accepted(self) -> None:
        dto = TaskTemplateCreate(
            name="t",
            property_scope="one",
            listed_property_ids=["p1"],
        )
        assert dto.listed_property_ids == ["p1"]

    def test_listed_requires_non_empty_list(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplateCreate(
                name="t",
                property_scope="listed",
                listed_property_ids=[],
            )
        assert "non-empty" in str(exc.value)

    def test_listed_rejects_duplicates(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplateCreate(
                name="t",
                property_scope="listed",
                listed_property_ids=["p1", "p1"],
            )
        assert "duplicate" in str(exc.value)


class TestAreaScopeValidation:
    """Area scope follows the same rules as property scope, plus ``derived``."""

    def test_any_rejects_ids(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(
                name="t",
                area_scope="any",
                listed_area_ids=["a1"],
            )

    def test_one_requires_exactly_one(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(
                name="t",
                area_scope="one",
                listed_area_ids=["a1", "a2"],
            )

    def test_listed_requires_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(
                name="t",
                area_scope="listed",
                listed_area_ids=[],
            )

    def test_derived_rejects_ids(self) -> None:
        """``derived`` pulls ids from the stay context — a list is dead weight."""
        with pytest.raises(ValidationError) as exc:
            TaskTemplateCreate(
                name="t",
                area_scope="derived",
                listed_area_ids=["a1"],
            )
        assert "derived" in str(exc.value)

    def test_derived_with_empty_list_accepted(self) -> None:
        dto = TaskTemplateCreate(name="t", area_scope="derived", listed_area_ids=[])
        assert dto.area_scope == "derived"

    def test_rejects_duplicate_area_ids(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(
                name="t",
                area_scope="listed",
                listed_area_ids=["a1", "a1"],
            )


class TestChecklistValidation:
    """Checklist items are structurally validated."""

    def test_key_uniqueness_enforced(self) -> None:
        """Two items may not share a ``key`` within one template."""
        with pytest.raises(ValidationError) as exc:
            TaskTemplateCreate(
                name="t",
                checklist_template_json=[
                    {"key": "sweep", "text": "Sweep"},
                    {"key": "sweep", "text": "Sweep again"},
                ],
            )
        assert "duplicate" in str(exc.value).lower()

    def test_unique_keys_accepted(self) -> None:
        dto = TaskTemplateCreate(
            name="t",
            checklist_template_json=[
                {"key": "sweep", "text": "Sweep"},
                {"key": "mop", "text": "Mop"},
            ],
        )
        assert len(dto.checklist_template_json) == 2

    def test_empty_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(
                name="t",
                checklist_template_json=[{"key": "", "text": "x"}],
            )

    def test_key_must_be_stable_slug(self) -> None:
        for key in ("CleanFridge", "clean-fridge", "clean fridge", "x" * 65):
            with pytest.raises(ValidationError):
                TaskTemplateCreate(
                    name="t",
                    checklist_template_json=[{"key": key, "text": "x"}],
                )

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(
                name="t",
                checklist_template_json=[{"key": "k", "text": ""}],
            )

    def test_extra_fields_rejected(self) -> None:
        """``extra='forbid'`` blocks typos in the checklist payload."""
        with pytest.raises(ValidationError):
            TaskTemplateCreate(
                name="t",
                checklist_template_json=[{"key": "k", "text": "t", "unknown": "field"}],
            )

    def test_rrule_and_dtstart_optional(self) -> None:
        """RRULE + dtstart are optional; the default is plain non-recurring."""
        dto = TaskTemplateCreate(
            name="t",
            checklist_template_json=[{"key": "k", "text": "t"}],
        )
        item = dto.checklist_template_json[0]
        assert item.rrule is None
        assert item.dtstart_local is None

    def test_invalid_rrule_rejected_at_write_time(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplateCreate(
                name="t",
                checklist_template_json=[
                    {"key": "deep_clean", "text": "Deep clean", "rrule": "NOPE=1"}
                ],
            )

        assert "NOPE=1" in str(exc.value)


class TestInventoryValidation:
    """Inventory consumption map must have positive integer values."""

    def test_positive_qty_accepted(self) -> None:
        dto = TaskTemplateCreate(
            name="t", inventory_consumption_json={"sku-1": 2, "sku-2": 5}
        )
        assert dto.inventory_consumption_json == {"sku-1": 2, "sku-2": 5}

    def test_zero_qty_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            TaskTemplateCreate(name="t", inventory_consumption_json={"sku-1": 0})
        assert "positive" in str(exc.value)

    def test_negative_qty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(name="t", inventory_consumption_json={"sku-1": -1})


class TestBodyFieldLimits:
    """Length + range caps are enforced on the body."""

    def test_name_min_length(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(name="")

    def test_name_max_length(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(name="x" * 201)

    def test_duration_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(name="t", duration_minutes=0)

    def test_duration_capped_at_one_day(self) -> None:
        """> 24 h is almost always a typo (minutes vs hours mix-up)."""
        with pytest.raises(ValidationError):
            TaskTemplateCreate(name="t", duration_minutes=24 * 60 + 1)

    def test_extra_fields_on_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate(name="t", unknown_field="x")  # type: ignore[call-arg]

    def test_photo_evidence_enum(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate.model_validate(
                {"name": "t", "photo_evidence": "sometimes"}
            )

    def test_priority_enum(self) -> None:
        with pytest.raises(ValidationError):
            TaskTemplateCreate.model_validate({"name": "t", "priority": "meh"})


class TestUnicodeAndEmoji:
    """Names and descriptions accept the full UTF-8 range."""

    def test_emoji_name(self) -> None:
        dto = TaskTemplateCreate(name="Clean the kitchen ")
        assert "" in dto.name

    def test_mixed_script_name(self) -> None:
        dto = TaskTemplateCreate(name="Ménage — 清掃 — clean")
        assert "Ménage" in dto.name


# ---------------------------------------------------------------------------
# ChecklistTemplateItemPayload
# ---------------------------------------------------------------------------


class TestChecklistItemPayload:
    """The checklist item DTO validates the §06 shape."""

    def test_all_fields_populated(self) -> None:
        item = ChecklistTemplateItemPayload(
            key="clean_fridge",
            text="Clean the fridge",
            required=True,
            guest_visible=False,
            rrule="FREQ=MONTHLY;BYMONTHDAY=1",
            dtstart_local="2026-01-01",
        )
        assert item.required is True
        assert item.rrule is not None
        assert item.dtstart_local == date(2026, 1, 1)

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChecklistTemplateItemPayload(key="k", text="t", something="else")  # type: ignore[call-arg]


class TestChecklistTemplateHelpers:
    """Checklist editor helpers validate, expand, and reorder §06 items."""

    def test_validate_returns_typed_items_and_rejects_duplicate_keys(self) -> None:
        items = validate_checklist_template(
            [
                {"key": "wipe_counters", "text": "Wipe counters"},
                ChecklistTemplateItem(key="clean_fridge", text="Clean fridge"),
            ]
        )

        assert [item.key for item in items] == ["wipe_counters", "clean_fridge"]
        with pytest.raises(ScopeInconsistent):
            validate_checklist_template(
                [
                    {"key": "wipe_counters", "text": "Wipe counters"},
                    {"key": "wipe_counters", "text": "Again"},
                ]
            )

    def test_normalises_rrule_body_and_exposes_typed_error(self) -> None:
        item = ChecklistTemplateItem(
            key="monthly",
            text="Monthly",
            rrule="RRULE:FREQ=MONTHLY;BYMONTHDAY=1",
        )

        assert item.rrule == "FREQ=MONTHLY;BYMONTHDAY=1"
        with pytest.raises(ChecklistRRULEError) as exc:
            validate_checklist_template(
                [{"key": "bad", "text": "Bad", "rrule": "NOPE=1"}]
            )
        assert exc.value.rrule == "NOPE=1"

    def test_expand_checklist_filters_monthly_every_n_weeks_and_six_months(
        self,
    ) -> None:
        items = [
            {"key": "always", "text": "Always"},
            {
                "key": "monthly",
                "text": "Monthly",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "dtstart_local": "2026-01-01",
            },
            {
                "key": "fortnightly",
                "text": "Fortnightly",
                "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO",
                "dtstart_local": "2026-01-05",
            },
            {
                "key": "six_month",
                "text": "Six-month service",
                "rrule": "FREQ=MONTHLY;INTERVAL=6;BYMONTHDAY=1",
                "dtstart_local": "2026-01-01",
            },
        ]

        jan_first = expand_checklist_for_task(
            items,
            scheduled_for_local=date(2026, 1, 1),
            is_ad_hoc=False,
        )
        jan_fifth = expand_checklist_for_task(
            items,
            scheduled_for_local=date(2026, 1, 5),
            is_ad_hoc=False,
        )
        jan_twelfth = expand_checklist_for_task(
            items,
            scheduled_for_local=date(2026, 1, 12),
            is_ad_hoc=False,
        )
        jul_first = expand_checklist_for_task(
            items,
            scheduled_for_local=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
            is_ad_hoc=False,
        )

        assert [item.key for item in jan_first] == ["always", "monthly", "six_month"]
        assert [item.key for item in jan_fifth] == ["always", "fortnightly"]
        assert [item.key for item in jan_twelfth] == ["always"]
        assert [item.key for item in jul_first] == ["always", "monthly", "six_month"]

    def test_expand_checklist_uses_resolved_anchor_when_item_omits_dtstart(
        self,
    ) -> None:
        items = [
            {"key": "always", "text": "Always"},
            {
                "key": "fortnightly",
                "text": "Fortnightly",
                "rrule": "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO",
            },
        ]

        included = expand_checklist_for_task(
            items,
            scheduled_for_local=date(2026, 1, 19),
            is_ad_hoc=False,
            dtstart_local=date(2026, 1, 5),
        )
        skipped = expand_checklist_for_task(
            items,
            scheduled_for_local=date(2026, 1, 12),
            is_ad_hoc=False,
            dtstart_local=date(2026, 1, 5),
        )

        assert [item.key for item in included] == ["always", "fortnightly"]
        assert [item.key for item in skipped] == ["always"]

    def test_ad_hoc_expansion_includes_every_item(self) -> None:
        items = [
            {"key": "always", "text": "Always"},
            {
                "key": "monthly",
                "text": "Monthly",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "dtstart_local": "2026-01-01",
            },
        ]

        expanded = expand_checklist_for_task(
            items,
            scheduled_for_local=date(2026, 1, 2),
            is_ad_hoc=True,
        )

        assert [item.key for item in expanded] == ["always", "monthly"]

    def test_reorder_preserves_item_payloads_by_key(self) -> None:
        items = [
            ChecklistTemplateItem(key="wipe", text="Wipe", required=True),
            ChecklistTemplateItem(key="mop", text="Mop", guest_visible=True),
        ]

        reordered = reorder_checklist(items, ["mop", "wipe"])

        assert [item.key for item in reordered] == ["mop", "wipe"]
        assert reordered[0].guest_visible is True
        assert reordered[1].required is True
        with pytest.raises(ScopeInconsistent):
            reorder_checklist(items, ["mop"])
        with pytest.raises(ScopeInconsistent):
            reorder_checklist(items, ["mop", "mop"])
        with pytest.raises(ScopeInconsistent):
            reorder_checklist(items, ["mop", "unknown"])


# ---------------------------------------------------------------------------
# CRUD round-trip
# ---------------------------------------------------------------------------


class TestCreate:
    """:func:`create` inserts a row, returns a view, and writes an audit entry."""

    def test_happy_path(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="happy-create")
        ctx = _ctx(ws, slug="happy-create")
        body = _minimal_body()
        clock = FrozenClock(_PINNED)

        view = create(session, ctx, body=body, clock=clock)

        assert view.id  # ULID generated
        assert view.workspace_id == ws
        assert view.name == body.name
        assert view.created_at == _PINNED
        assert view.deleted_at is None

    def test_writes_audit_row(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="audit-create")
        ctx = _ctx(ws, slug="audit-create")
        clock = FrozenClock(_PINNED)

        view = create(session, ctx, body=_minimal_body(), clock=clock)

        audit = session.scalars(
            select(AuditLog).where(AuditLog.entity_id == view.id)
        ).one()
        assert audit.action == "create"
        assert audit.entity_kind == "task_template"
        assert audit.actor_id == ctx.actor_id
        assert audit.workspace_id == ws
        assert isinstance(audit.diff, dict)
        assert "after" in audit.diff
        assert audit.diff["after"]["name"] == view.name

    def test_resolves_workspace_from_ctx_not_payload(self, session: Session) -> None:
        """``workspace_id`` comes from the context; the payload has no such field.

        This regression guard proves a caller cannot route into
        workspace B by lying on the request body — the DTO doesn't
        even accept ``workspace_id``, and the service pulls it from
        the context exclusively.
        """
        ws_a = _bootstrap_workspace(session, slug="ctx-a")
        ws_b = _bootstrap_workspace(session, slug="ctx-b")
        ctx = _ctx(ws_a, slug="ctx-a")

        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))

        assert view.workspace_id == ws_a
        # The row is in workspace A only — workspace B sees nothing.
        ctx_b = _ctx(ws_b, slug="ctx-b")
        assert list_templates(session, ctx_b) == []

    def test_body_cannot_override_workspace_id(self) -> None:
        """``workspace_id`` is not a field on the DTO — extra='forbid' blocks it."""
        with pytest.raises(ValidationError):
            TaskTemplateCreate(  # type: ignore[call-arg]
                name="t",
                workspace_id="evil-workspace",
            )

    def test_legacy_columns_are_written(self, session: Session) -> None:
        """Legacy cd-chd columns are filled so existing NOT-NULLs stay green.

        The v1 slice pinned ``title`` / ``default_duration_min`` as
        NOT NULL. Until a follow-up drops them, the service mirrors
        the new values onto the legacy columns so INSERTs stay legal.
        """
        ws = _bootstrap_workspace(session, slug="legacy-cols")
        ctx = _ctx(ws, slug="legacy-cols")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))

        row = session.scalars(
            select(TaskTemplate).where(TaskTemplate.id == view.id)
        ).one()
        assert row.title == view.name
        assert row.default_duration_min == view.duration_minutes


class TestRead:
    """:func:`read` returns the live template or raises 404."""

    def test_happy_path(self, session: Session) -> None:
        """Read after create returns the same row content.

        The equality check ignores ``created_at`` / ``deleted_at``
        because SQLite strips the tzinfo on round-trip (our
        integration shard on Postgres proves the full aware-UTC
        contract). Every other field is compared verbatim.
        """
        ws = _bootstrap_workspace(session, slug="read-happy")
        ctx = _ctx(ws, slug="read-happy")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))

        loaded = read(session, ctx, template_id=view.id)
        assert loaded.id == view.id
        assert loaded.workspace_id == view.workspace_id
        assert loaded.name == view.name
        assert loaded.description_md == view.description_md
        assert loaded.duration_minutes == view.duration_minutes
        assert loaded.property_scope == view.property_scope
        assert loaded.listed_property_ids == view.listed_property_ids
        assert loaded.priority == view.priority
        assert loaded.photo_evidence == view.photo_evidence
        assert loaded.deleted_at is None

    def test_unknown_id_raises(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="read-404")
        ctx = _ctx(ws, slug="read-404")
        with pytest.raises(TaskTemplateNotFound):
            read(session, ctx, template_id="01HWA00000000000000NONE01")

    def test_cross_workspace_hidden(self, session: Session) -> None:
        """Workspace A cannot read workspace B's template."""
        ws_a = _bootstrap_workspace(session, slug="xws-a")
        ws_b = _bootstrap_workspace(session, slug="xws-b")
        ctx_a = _ctx(ws_a, slug="xws-a")
        ctx_b = _ctx(ws_b, slug="xws-b")
        view = create(session, ctx_a, body=_minimal_body(), clock=FrozenClock(_PINNED))

        with pytest.raises(TaskTemplateNotFound):
            read(session, ctx_b, template_id=view.id)

    def test_soft_deleted_hidden_by_default(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="read-deleted")
        ctx = _ctx(ws, slug="read-deleted")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))

        with pytest.raises(TaskTemplateNotFound):
            read(session, ctx, template_id=view.id)

    def test_soft_deleted_surfaced_with_flag(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="read-deleted-flag")
        ctx = _ctx(ws, slug="read-deleted-flag")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))

        loaded = read(session, ctx, template_id=view.id, include_deleted=True)
        assert loaded.id == view.id
        assert loaded.deleted_at is not None


class TestReadMany:
    """:func:`read_many` bulk-fetches ids for sidecar payloads (cd-dzte)."""

    def test_empty_input_returns_empty(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="rm-empty")
        ctx = _ctx(ws, slug="rm-empty")
        assert read_many(session, ctx, template_ids=[]) == []

    def test_returns_only_requested_ids(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="rm-subset")
        ctx = _ctx(ws, slug="rm-subset")
        clock = FrozenClock(_PINNED)
        a = create(session, ctx, body=_minimal_body(name="A"), clock=clock)
        b = create(session, ctx, body=_minimal_body(name="B"), clock=clock)
        create(session, ctx, body=_minimal_body(name="C"), clock=clock)

        rows = read_many(session, ctx, template_ids=[a.id, b.id])
        assert {r.id for r in rows} == {a.id, b.id}

    def test_unknown_id_silently_skipped(self, session: Session) -> None:
        """A stale schedule pointing at an unknown id leaves a hole, not a 404."""
        ws = _bootstrap_workspace(session, slug="rm-unknown")
        ctx = _ctx(ws, slug="rm-unknown")
        clock = FrozenClock(_PINNED)
        a = create(session, ctx, body=_minimal_body(name="Live"), clock=clock)

        rows = read_many(session, ctx, template_ids=[a.id, "01HWA00000000000000NONE01"])
        assert [r.id for r in rows] == [a.id]

    def test_cross_workspace_hidden(self, session: Session) -> None:
        """Workspace A's id is invisible from workspace B even when bulk-fetched."""
        ws_a = _bootstrap_workspace(session, slug="rm-a")
        ws_b = _bootstrap_workspace(session, slug="rm-b")
        ctx_a = _ctx(ws_a, slug="rm-a")
        ctx_b = _ctx(ws_b, slug="rm-b")
        a = create(session, ctx_a, body=_minimal_body(), clock=FrozenClock(_PINNED))

        rows = read_many(session, ctx_b, template_ids=[a.id])
        assert rows == []

    def test_soft_deleted_hidden_by_default(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="rm-deleted")
        ctx = _ctx(ws, slug="rm-deleted")
        a = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        delete(session, ctx, template_id=a.id, clock=FrozenClock(_PINNED))

        rows = read_many(session, ctx, template_ids=[a.id])
        assert rows == []

    def test_soft_deleted_surfaced_with_flag(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="rm-deleted-flag")
        ctx = _ctx(ws, slug="rm-deleted-flag")
        a = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        delete(session, ctx, template_id=a.id, clock=FrozenClock(_PINNED))

        rows = read_many(session, ctx, template_ids=[a.id], include_deleted=True)
        assert [r.id for r in rows] == [a.id]
        assert rows[0].deleted_at is not None

    def test_duplicate_ids_collapse(self, session: Session) -> None:
        """Asking for the same id twice returns one row (set semantics)."""
        ws = _bootstrap_workspace(session, slug="rm-dup")
        ctx = _ctx(ws, slug="rm-dup")
        a = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))

        rows = read_many(session, ctx, template_ids=[a.id, a.id, a.id])
        assert [r.id for r in rows] == [a.id]


class TestList:
    """:func:`list_templates` honours every advertised filter."""

    def test_empty_workspace_returns_empty_list(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-empty")
        ctx = _ctx(ws, slug="list-empty")
        assert list_templates(session, ctx) == []

    def test_ordering_is_stable(self, session: Session) -> None:
        """Rows come back in ``created_at`` ascending order, id tiebreaker."""
        ws = _bootstrap_workspace(session, slug="list-order")
        ctx = _ctx(ws, slug="list-order")
        clock = FrozenClock(_PINNED)
        first = create(session, ctx, body=_minimal_body(name="A"), clock=clock)
        second = create(session, ctx, body=_minimal_body(name="B"), clock=clock)

        rows = list_templates(session, ctx)
        assert [r.id for r in rows] == sorted([first.id, second.id])

    def test_q_filters_by_name_substring(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-q-name")
        ctx = _ctx(ws, slug="list-q-name")
        # Give both rows a bland description so the ``q`` match is
        # purely driven by the name column.
        create(
            session,
            ctx,
            body=_minimal_body(name="Deep clean kitchen", description_md="blurb"),
            clock=FrozenClock(_PINNED),
        )
        create(
            session,
            ctx,
            body=_minimal_body(name="Sweep pool deck", description_md="blurb"),
            clock=FrozenClock(_PINNED),
        )

        rows = list_templates(session, ctx, q="kitchen")
        assert [r.name for r in rows] == ["Deep clean kitchen"]

    def test_q_filters_by_description_substring(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-q-desc")
        ctx = _ctx(ws, slug="list-q-desc")
        create(
            session,
            ctx,
            body=_minimal_body(name="A", description_md="contains KITCHEN work"),
            clock=FrozenClock(_PINNED),
        )
        create(
            session,
            ctx,
            body=_minimal_body(name="B", description_md="pool only"),
            clock=FrozenClock(_PINNED),
        )

        rows = list_templates(session, ctx, q="kitchen")
        assert [r.name for r in rows] == ["A"]

    def test_q_is_case_insensitive(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-q-ci")
        ctx = _ctx(ws, slug="list-q-ci")
        create(
            session,
            ctx,
            body=_minimal_body(name="Kitchen clean"),
            clock=FrozenClock(_PINNED),
        )

        rows = list_templates(session, ctx, q="KITCHEN")
        assert len(rows) == 1

    def test_q_whitespace_only_does_not_filter(self, session: Session) -> None:
        """An all-whitespace ``q`` returns every row (noop filter)."""
        ws = _bootstrap_workspace(session, slug="list-q-ws")
        ctx = _ctx(ws, slug="list-q-ws")
        create(session, ctx, body=_minimal_body(name="A"), clock=FrozenClock(_PINNED))
        create(session, ctx, body=_minimal_body(name="B"), clock=FrozenClock(_PINNED))

        rows = list_templates(session, ctx, q="   ")
        assert len(rows) == 2

    def test_role_id_filter(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-role")
        ctx = _ctx(ws, slug="list-role")
        create(
            session,
            ctx,
            body=_minimal_body(name="with role", role_id="role-1"),
            clock=FrozenClock(_PINNED),
        )
        create(
            session,
            ctx,
            body=_minimal_body(name="no role"),
            clock=FrozenClock(_PINNED),
        )

        rows = list_templates(session, ctx, role_id="role-1")
        assert [r.name for r in rows] == ["with role"]

    def test_deleted_filter_excludes_by_default(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-del-def")
        ctx = _ctx(ws, slug="list-del-def")
        live = create(
            session, ctx, body=_minimal_body(name="live"), clock=FrozenClock(_PINNED)
        )
        gone = create(
            session, ctx, body=_minimal_body(name="gone"), clock=FrozenClock(_PINNED)
        )
        delete(session, ctx, template_id=gone.id, clock=FrozenClock(_PINNED))

        rows = list_templates(session, ctx)
        assert [r.id for r in rows] == [live.id]

    def test_deleted_filter_returns_only_deleted(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="list-del-flag")
        ctx = _ctx(ws, slug="list-del-flag")
        create(
            session, ctx, body=_minimal_body(name="live"), clock=FrozenClock(_PINNED)
        )
        gone = create(
            session, ctx, body=_minimal_body(name="gone"), clock=FrozenClock(_PINNED)
        )
        delete(session, ctx, template_id=gone.id, clock=FrozenClock(_PINNED))

        rows = list_templates(session, ctx, deleted=True)
        assert [r.id for r in rows] == [gone.id]

    def test_cross_workspace_isolation(self, session: Session) -> None:
        ws_a = _bootstrap_workspace(session, slug="list-iso-a")
        ws_b = _bootstrap_workspace(session, slug="list-iso-b")
        ctx_a = _ctx(ws_a, slug="list-iso-a")
        ctx_b = _ctx(ws_b, slug="list-iso-b")
        create(session, ctx_a, body=_minimal_body(name="A"), clock=FrozenClock(_PINNED))
        create(session, ctx_b, body=_minimal_body(name="B"), clock=FrozenClock(_PINNED))

        rows_a = list_templates(session, ctx_a)
        assert [r.name for r in rows_a] == ["A"]


class TestUpdate:
    """:func:`update` replaces the mutable body, writes an audit with diff."""

    def test_happy_path(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="upd-happy")
        ctx = _ctx(ws, slug="upd-happy")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))

        new_body = _minimal_update(
            name="New name",
            description_md="New desc",
            priority="urgent",
            photo_evidence="required",
        )
        updated = update(
            session, ctx, template_id=view.id, body=new_body, clock=FrozenClock(_PINNED)
        )
        assert updated.name == "New name"
        assert updated.priority == "urgent"
        assert updated.photo_evidence == "required"

    def test_unknown_id_raises(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="upd-404")
        ctx = _ctx(ws, slug="upd-404")
        with pytest.raises(TaskTemplateNotFound):
            update(
                session,
                ctx,
                template_id="01HWA00000000000000NONE02",
                body=_minimal_update(),
                clock=FrozenClock(_PINNED),
            )

    def test_soft_deleted_row_not_updateable(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="upd-deleted")
        ctx = _ctx(ws, slug="upd-deleted")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))

        with pytest.raises(TaskTemplateNotFound):
            update(
                session,
                ctx,
                template_id=view.id,
                body=_minimal_update(name="after delete"),
                clock=FrozenClock(_PINNED),
            )

    def test_writes_audit_with_before_after(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="upd-audit")
        ctx = _ctx(ws, slug="upd-audit")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))

        update(
            session,
            ctx,
            template_id=view.id,
            body=_minimal_update(name="Renamed"),
            clock=FrozenClock(_PINNED),
        )

        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == view.id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
        update_row = next(a for a in audits if a.action == "update")
        assert isinstance(update_row.diff, dict)
        assert update_row.diff["before"]["name"] != update_row.diff["after"]["name"]
        assert update_row.diff["after"]["name"] == "Renamed"

    def test_update_validates_scope(self) -> None:
        """Update runs the same scope-consistency rules as create.

        Constructing an invalid :class:`TaskTemplateUpdate` raises at
        DTO-construction time, before the service is called — the
        same gate create uses.
        """
        with pytest.raises(ValidationError):
            TaskTemplateUpdate.model_validate(
                {
                    "name": "t",
                    "property_scope": "any",
                    "listed_property_ids": ["p1"],
                }
            )

    def test_cross_workspace_rejected(self, session: Session) -> None:
        ws_a = _bootstrap_workspace(session, slug="upd-iso-a")
        ws_b = _bootstrap_workspace(session, slug="upd-iso-b")
        ctx_a = _ctx(ws_a, slug="upd-iso-a")
        ctx_b = _ctx(ws_b, slug="upd-iso-b")
        view = create(session, ctx_a, body=_minimal_body(), clock=FrozenClock(_PINNED))

        with pytest.raises(TaskTemplateNotFound):
            update(
                session,
                ctx_b,
                template_id=view.id,
                body=_minimal_update(name="x"),
                clock=FrozenClock(_PINNED),
            )


class TestDelete:
    """:func:`delete` soft-deletes or refuses when live references exist."""

    def test_happy_path(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="del-happy")
        ctx = _ctx(ws, slug="del-happy")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))

        result = delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))
        assert result.deleted_at == _PINNED

    def test_is_soft_delete_not_hard(self, session: Session) -> None:
        """The row survives in the DB with ``deleted_at`` set."""
        ws = _bootstrap_workspace(session, slug="del-soft")
        ctx = _ctx(ws, slug="del-soft")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))

        row = session.scalars(
            select(TaskTemplate).where(TaskTemplate.id == view.id)
        ).one()
        assert row.deleted_at is not None

    def test_delete_is_idempotent_at_soft_level(self, session: Session) -> None:
        """Soft-deleted rows look 404 to the service — another delete raises 404."""
        ws = _bootstrap_workspace(session, slug="del-idem")
        ctx = _ctx(ws, slug="del-idem")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))

        with pytest.raises(TaskTemplateNotFound):
            delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))

    def test_writes_audit(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="del-audit")
        ctx = _ctx(ws, slug="del-audit")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))

        audits = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_id == view.id)
            .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        ).all()
        delete_row = next(a for a in audits if a.action == "delete")
        assert isinstance(delete_row.diff, dict)
        assert delete_row.diff["before"]["deleted_at"] is None
        assert delete_row.diff["after"]["deleted_at"] is not None

    def test_unknown_id_raises_not_in_use(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="del-404")
        ctx = _ctx(ws, slug="del-404")
        with pytest.raises(TaskTemplateNotFound):
            delete(
                session,
                ctx,
                template_id="01HWA00000000000000NONE03",
                clock=FrozenClock(_PINNED),
            )

    def test_refuses_when_schedule_references_template(self, session: Session) -> None:
        """A live ``schedule.template_id`` reference blocks soft-delete."""
        ws = _bootstrap_workspace(session, slug="del-in-use")
        ctx = _ctx(ws, slug="del-in-use")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        schedule_id = new_ulid()
        session.add(
            Schedule(
                id=schedule_id,
                workspace_id=ws,
                template_id=view.id,
                property_id=None,
                rrule_text="FREQ=WEEKLY;BYDAY=MO",
                dtstart=_PINNED,
                until=None,
                assignee_user_id=None,
                assignee_role=None,
                enabled=True,
                next_generation_at=None,
                created_at=_PINNED,
            )
        )
        session.flush()

        with pytest.raises(TemplateInUseError) as exc:
            delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))
        assert exc.value.template_id == view.id
        assert schedule_id in exc.value.schedule_ids

    def test_refusal_does_not_mark_deleted(self, session: Session) -> None:
        """A refused delete leaves ``deleted_at`` NULL — the row stays live."""
        ws = _bootstrap_workspace(session, slug="del-refuse")
        ctx = _ctx(ws, slug="del-refuse")
        view = create(session, ctx, body=_minimal_body(), clock=FrozenClock(_PINNED))
        session.add(
            Schedule(
                id=new_ulid(),
                workspace_id=ws,
                template_id=view.id,
                property_id=None,
                rrule_text="FREQ=WEEKLY;BYDAY=MO",
                dtstart=_PINNED,
                until=None,
                assignee_user_id=None,
                assignee_role=None,
                enabled=True,
                next_generation_at=None,
                created_at=_PINNED,
            )
        )
        session.flush()

        with pytest.raises(TemplateInUseError):
            delete(session, ctx, template_id=view.id, clock=FrozenClock(_PINNED))

        row = session.scalars(
            select(TaskTemplate).where(TaskTemplate.id == view.id)
        ).one()
        assert row.deleted_at is None

    def test_cross_workspace_rejected(self, session: Session) -> None:
        ws_a = _bootstrap_workspace(session, slug="del-iso-a")
        ws_b = _bootstrap_workspace(session, slug="del-iso-b")
        ctx_a = _ctx(ws_a, slug="del-iso-a")
        ctx_b = _ctx(ws_b, slug="del-iso-b")
        view = create(session, ctx_a, body=_minimal_body(), clock=FrozenClock(_PINNED))

        with pytest.raises(TaskTemplateNotFound):
            delete(
                session,
                ctx_b,
                template_id=view.id,
                clock=FrozenClock(_PINNED),
            )


class TestChecklistRoundTrip:
    """Checklist payloads survive the round-trip through JSON + view."""

    def test_items_are_preserved(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="chk-rt")
        ctx = _ctx(ws, slug="chk-rt")
        body = _minimal_body(
            checklist_template_json=[
                {
                    "key": "sweep",
                    "text": "Sweep",
                    "required": True,
                    "guest_visible": False,
                },
                {"key": "mop", "text": "Mop", "required": False, "guest_visible": True},
            ]
        )
        view = create(session, ctx, body=body, clock=FrozenClock(_PINNED))
        loaded = read(session, ctx, template_id=view.id)

        assert len(loaded.checklist_template_json) == 2
        assert loaded.checklist_template_json[0].key == "sweep"
        assert loaded.checklist_template_json[0].required is True
        assert loaded.checklist_template_json[1].guest_visible is True

    def test_rrule_survives_round_trip(self, session: Session) -> None:
        ws = _bootstrap_workspace(session, slug="chk-rrule")
        ctx = _ctx(ws, slug="chk-rrule")
        body = _minimal_body(
            checklist_template_json=[
                {
                    "key": "deep",
                    "text": "Deep clean fridge",
                    "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                    "dtstart_local": "2026-01-01",
                }
            ]
        )
        view = create(session, ctx, body=body, clock=FrozenClock(_PINNED))
        loaded = read(session, ctx, template_id=view.id)

        item = loaded.checklist_template_json[0]
        assert item.rrule == "FREQ=MONTHLY;BYMONTHDAY=1"
        assert item.dtstart_local == date(2026, 1, 1)
