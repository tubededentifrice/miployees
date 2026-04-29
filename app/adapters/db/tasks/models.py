"""TaskTemplate / ChecklistTemplateItem / Schedule / Occurrence /
ChecklistItem / Evidence / Comment SQLAlchemy models.

v1 slice per cd-chd, extended by cd-0tg (template CRUD) to carry
the full §02 / §06 task-template shape needed by the manager UI
(``name``, ``role_id``, ``duration_minutes``, ``property_scope`` +
``listed_property_ids``, ``area_scope`` + ``listed_area_ids``,
``photo_evidence`` three-value enum, ``priority``, ``linked_
instruction_ids``, ``inventory_effects_json``, ``llm_hints_md``,
``deleted_at``). The cd-chd slice's original columns (``title``,
``default_duration_min``, ``required_evidence``, ``photo_required``,
``default_assignee_role``) remain in place for backward compat with
the cd-chd integration tests; the domain service writes through to
both the old and new names until a follow-up migration drops the
legacy pair. Further §02 / §06 columns (``paused_at``, ``active_
from`` / ``active_until``, ``checklist_snapshot_json``, the fuller
``scheduled | cancelled | overdue`` state machine) land with cd-4qr
(turnover auto-generation) and later follow-ups.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. FK hygiene mirrors
the rest of the app:

* Template / schedule / occurrence references that power history
  (``occurrence.template_id``) use ``RESTRICT`` — a template that
  has produced occurrences cannot be hard-deleted without explicit
  intent.
* Cascading parents (``template → checklist_template_item``,
  ``occurrence → {checklist_item, evidence, comment}``) use
  ``CASCADE`` so sweeping the parent removes the children.
* ``schedule.property_id`` uses ``SET NULL`` — a schedule can be
  workspace-wide (``property_id IS NULL``) and losing its property
  drops it back to that state rather than nuking the schedule row.
* User pointers (``assignee_user_id``, ``completed_by_user_id``,
  ``reviewer_user_id``, ``created_by_user_id``, ``author_user_id``)
  all use ``SET NULL`` so history survives the actor's removal,
  matching the ``property_closure`` / ``role_grant`` convention.

See ``docs/specs/02-domain-model.md`` §"task_template",
§"schedule", §"occurrence", §"checklist_item", §"evidence",
§"comment", and ``docs/specs/06-tasks-and-scheduling.md``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``property.id`` / ``user.id``
# / ``workspace.id`` FKs below resolve against ``Base.metadata`` only
# if the target packages have been imported, so we register them here
# as a side effect.
from app.adapters.db.identity import models as _identity_models  # noqa: F401
from app.adapters.db.places import models as _places_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = [
    "ChecklistItem",
    "ChecklistTemplateItem",
    "Comment",
    "Evidence",
    "NlTaskPreview",
    "Occurrence",
    "Schedule",
    "TaskTemplate",
]


# Allowed ``task_template.required_evidence`` values, enforced by a
# CHECK constraint. ``none`` means the task can be marked done with
# no artefact; the other four require the matching evidence kind.
_REQUIRED_EVIDENCE_VALUES: tuple[str, ...] = (
    "none",
    "photo",
    "note",
    "voice",
    "gps",
)

# Allowed ``task_template.default_assignee_role`` and
# ``schedule.assignee_role`` values — the §02 persona enum. ``NULL``
# is a legal choice on both columns; the CHECK only fires on
# non-NULL writes.
_ASSIGNEE_ROLE_VALUES: tuple[str, ...] = (
    "manager",
    "worker",
    "client",
    "guest",
)

# Allowed ``occurrence.state`` values. The v1 slice pinned the
# minimum chain the cd-chd acceptance criterion asked for; cd-k4l
# widens the enum with ``scheduled`` (pre-materialisation state
# used by the scheduler worker as it stages future rows) and
# ``cancelled`` (set by :func:`app.domain.tasks.schedules.delete`
# when a parent schedule is soft-deleted). cd-hurw widens it with
# ``overdue``, the soft state the sweeper worker
# (:mod:`app.worker.tasks.overdue`) flips a task into when
# ``ends_at + grace`` is past — §06 "State machine" pins it as
# never-terminal.
_OCCURRENCE_STATE_VALUES: tuple[str, ...] = (
    "scheduled",
    "pending",
    "in_progress",
    "done",
    "skipped",
    "approved",
    "cancelled",
    "overdue",
)

# Allowed ``evidence.kind`` values — the §02 evidence taxonomy. The
# matching ``required_evidence`` enum is a strict superset (it also
# carries ``none``, which is a template-level "no evidence
# required" marker rather than a stored-artefact kind).
_EVIDENCE_KIND_VALUES: tuple[str, ...] = (
    "photo",
    "note",
    "voice",
    "gps",
    "checklist_snapshot",
)

# Allowed ``comment.kind`` values (cd-cfe4) — the §06 "Task notes are
# the agent inbox" taxonomy. ``user`` is a human author (worker /
# manager typing in the chat composer); ``agent`` is the embedded
# workspace agent speaking in the thread (carries an
# :attr:`Comment.llm_call_id`); ``system`` is an internal state-change
# marker emitted by the completion / assignment services (e.g.
# "marked done by Maya at 14:02"). Mirrors the ``Evidence.kind``
# CHECK-constraint pattern above.
_COMMENT_KIND_VALUES: tuple[str, ...] = ("user", "agent", "system")

# Allowed ``task_template.property_scope`` values (cd-0tg). Drives the
# "which properties does this template target" filter at generation
# time. ``any`` → workspace-wide; ``one`` → exactly one id in
# ``listed_property_ids``; ``listed`` → the enumerated set.
_PROPERTY_SCOPE_VALUES: tuple[str, ...] = ("any", "one", "listed")

# Allowed ``task_template.area_scope`` values (cd-0tg). Same shape as
# ``property_scope`` plus ``derived`` — used by stay-lifecycle
# bundles where the area comes from the stay at generation time.
_AREA_SCOPE_VALUES: tuple[str, ...] = ("any", "one", "listed", "derived")

# Allowed ``task_template.photo_evidence`` values (cd-0tg). Three-
# value enum replacing the v1 slice's ``required_evidence`` +
# ``photo_required`` pair. ``disabled`` hides the camera picker;
# ``optional`` shows it but accepts completions without a photo;
# ``required`` rejects completions that lack one.
_PHOTO_EVIDENCE_VALUES: tuple[str, ...] = ("disabled", "optional", "required")

# Allowed ``task_template.priority`` values (cd-0tg). Drives the
# manager's default sort and chip tone (see §06 "Task template").
_PRIORITY_VALUES: tuple[str, ...] = ("low", "normal", "high", "urgent")


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Kept as a tiny helper so the seven CHECK constraints below stay
    readable; it also matches the convention used by the sibling
    ``authz`` and ``places`` models.
    """
    return "'" + "', '".join(values) + "'"


class TaskTemplate(Base):
    """Re-usable blueprint for an occurrence.

    Carries the fields every spawned :class:`Occurrence` copies down
    at generation time — name, description, expected duration,
    evidence policy, the embedded checklist payload, and the scope
    shape that decides which properties / areas the template targets.
    The ``checklist_template_json`` column is an ergonomic cache for
    the client-side editor; the authoritative per-item list lives on
    :class:`ChecklistTemplateItem` rows.

    **cd-0tg** extends the cd-chd v1 slice with the richer §02 / §06
    surface (``name``, ``role_id``, ``duration_minutes``,
    ``property_scope`` + ``listed_property_ids``, ``area_scope`` +
    ``listed_area_ids``, ``photo_evidence``, ``priority``,
    ``linked_instruction_ids``, ``inventory_effects_json``,
    ``llm_hints_md``, ``deleted_at``). The legacy cd-chd columns
    (``title``, ``default_duration_min``, ``required_evidence``,
    ``photo_required``, ``default_assignee_role``) remain in place
    for backward compat; the CRUD service
    (:mod:`app.domain.tasks.templates`) writes through to both the
    old and new names until a follow-up migration drops the legacy
    pair.
    """

    __tablename__ = "task_template"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # cd-chd legacy display column. Kept nullable on the model side
    # because the DB column is still NOT NULL — the CRUD service
    # writes ``name`` into both fields until ``title`` is dropped.
    title: Mapped[str] = mapped_column(String, nullable=False)
    # cd-0tg spec display column. Nullable for backward compat with
    # rows inserted before this migration; new writes populate it.
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft reference to ``work_role.id`` (§05). The ``work_role``
    # table isn't in the schema yet, so no FK — plain String, matching
    # the ``scope_property_id`` convention on ``role_grant``.
    role_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Markdown body rendered in the template detail view. Plain text
    # is a legal subset, so unit tests can write unescaped strings.
    description_md: Mapped[str] = mapped_column(String, nullable=False, default="")
    # cd-chd legacy duration column. See ``duration_minutes`` below.
    default_duration_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    # cd-0tg spec duration column. Nullable for backward compat.
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # cd-chd legacy evidence-policy columns, superseded by
    # ``photo_evidence`` below.
    required_evidence: Mapped[str] = mapped_column(
        String, nullable=False, default="none"
    )
    photo_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # cd-chd legacy default-assignee column (enum, not FK). Superseded
    # by ``role_id`` above.
    default_assignee_role: Mapped[str | None] = mapped_column(String, nullable=True)
    # cd-0tg property-scope shape. ``any`` → workspace-wide; ``one``
    # → exactly one id in ``listed_property_ids``; ``listed`` → the
    # enumerated set. Server default ``'any'`` so existing rows
    # survive the migration.
    property_scope: Mapped[str] = mapped_column(String, nullable=False, default="any")
    # IDs targeted by ``property_scope``. Empty for ``any``, one-
    # element for ``one``, non-empty for ``listed`` — the domain
    # service enforces this consistency; the DB only holds the list.
    listed_property_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # cd-0tg area-scope shape. Same semantics as ``property_scope``
    # plus ``derived`` for stay-lifecycle bundles where the area
    # comes from the stay at generation time (§06).
    area_scope: Mapped[str] = mapped_column(String, nullable=False, default="any")
    listed_area_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Flat list of checklist-item payloads. The outer ``Any`` is
    # scoped to SQLAlchemy's JSON column type — callers writing a
    # typed payload should use a TypedDict locally and coerce into
    # this column. The authoritative per-item list lives on
    # :class:`ChecklistTemplateItem`; this JSON field mirrors it for
    # the client editor.
    checklist_template_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # cd-0tg three-value photo-evidence enum. Server default
    # ``'disabled'`` so existing rows survive the migration.
    photo_evidence: Mapped[str] = mapped_column(
        String, nullable=False, default="disabled"
    )
    # IDs of linked instructions (§07). Empty list by default.
    linked_instruction_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # cd-0tg priority enum. Server default ``'normal'``.
    priority: Mapped[str] = mapped_column(String, nullable=False, default="normal")
    # List of {item_ref, kind, qty} inventory effects (§08). JSON
    # rather than a join table for v1.
    inventory_effects_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Free-text hints the agent inbox (§06) passes to the LLM when
    # explaining the task.
    llm_hints_md: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft-delete marker. ``NULL`` means live; non-null means the
    # template is retired. The ``occurrence.template_id`` FK is
    # ``RESTRICT``, so history survives the soft-delete.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    @property
    def inventory_consumption_json(self) -> dict[str, int]:
        """Compatibility view over consume-only ``inventory_effects_json`` rows."""
        consumption: dict[str, int] = {}
        for entry in self.inventory_effects_json or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != "consume":
                continue
            item_ref = entry.get("item_ref")
            qty = entry.get("qty")
            if isinstance(item_ref, str) and isinstance(qty, int):
                consumption[item_ref] = qty
        return consumption

    @inventory_consumption_json.setter
    def inventory_consumption_json(self, value: dict[str, int]) -> None:
        self.inventory_effects_json = [
            {"item_ref": item_ref, "kind": "consume", "qty": qty}
            for item_ref, qty in value.items()
        ]

    __table_args__ = (
        CheckConstraint(
            f"required_evidence IN ({_in_clause(_REQUIRED_EVIDENCE_VALUES)})",
            name="required_evidence",
        ),
        CheckConstraint(
            "default_assignee_role IS NULL OR default_assignee_role IN "
            f"({_in_clause(_ASSIGNEE_ROLE_VALUES)})",
            name="default_assignee_role",
        ),
        CheckConstraint(
            f"property_scope IN ({_in_clause(_PROPERTY_SCOPE_VALUES)})",
            name="property_scope",
        ),
        CheckConstraint(
            f"area_scope IN ({_in_clause(_AREA_SCOPE_VALUES)})",
            name="area_scope",
        ),
        CheckConstraint(
            f"photo_evidence IN ({_in_clause(_PHOTO_EVIDENCE_VALUES)})",
            name="photo_evidence",
        ),
        CheckConstraint(
            f"priority IN ({_in_clause(_PRIORITY_VALUES)})",
            name="priority",
        ),
        Index("ix_task_template_workspace", "workspace_id"),
        # "list live templates" hot path — filter on
        # (workspace_id, deleted_at IS NULL) most of the time.
        Index("ix_task_template_workspace_deleted", "workspace_id", "deleted_at"),
    )


class ChecklistTemplateItem(Base):
    """A single checklist row attached to a :class:`TaskTemplate`.

    The ``workspace_id`` column is denormalised so the ORM tenant
    filter (:mod:`app.tenancy.orm_filter`) can enforce workspace
    boundaries on reads that only touch this table. The FK
    ``template_id → task_template.id`` cascades so deleting a
    template sweeps its checklist blueprint.
    """

    __tablename__ = "checklist_template_item"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    template_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("task_template.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    requires_photo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "position",
            name="uq_checklist_template_item_template_position",
        ),
        Index("ix_checklist_template_item_template", "template_id"),
    )


class Schedule(Base):
    """Recurrence rule that materialises :class:`Occurrence` rows.

    The RRULE body is stored as the raw iCalendar text
    (``rrule_text``); evaluation happens in the domain layer, which
    reads the parent property's IANA timezone to resolve local
    clock-wall timestamps. ``property_id`` is nullable — a schedule
    can be workspace-wide (``NULL`` → applies to every property the
    workspace governs, resolved per the §06 generator). The
    ``next_generation_at`` column is the scheduler worker's
    hot-path key, hence the ``(workspace_id, next_generation_at)``
    index.

    **cd-k4l** extends the cd-chd v1 slice with the full §06
    schedule shape used by the manager UI and the CRUD service
    (:mod:`app.domain.tasks.schedules`): ``name``, ``area_id``,
    ``backup_assignee_user_ids`` (ordered fallback list),
    ``dtstart_local`` (property-local ISO-8601 timestamp),
    ``duration_minutes``, ``rdate_local`` / ``exdate_local`` (line-
    separated ISO-8601 local timestamps), ``active_from`` /
    ``active_until`` (property-local active-range bounds),
    ``paused_at`` (pause marker; §"Pause vs active range" pins
    pause as the winning predicate), and ``deleted_at``
    (soft-delete). The legacy cd-chd columns (``rrule_text``,
    ``dtstart``, ``until``, ``assignee_user_id``, ``assignee_role``,
    ``enabled``, ``next_generation_at``) remain in place for
    backward compat; the CRUD service writes through to both the
    old and new names until a follow-up migration drops the legacy
    pair.
    """

    __tablename__ = "schedule"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    template_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("task_template.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable — a schedule without a property is workspace-wide.
    # ``ON DELETE SET NULL`` so losing the property drops the
    # schedule back to workspace-wide rather than nuking the row.
    property_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="SET NULL"),
        nullable=True,
    )
    # cd-k4l spec display column. Nullable for backward compat with
    # pre-migration rows; new writes populate it.
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    # cd-k4l area scope. ``NULL`` = schedule applies across the
    # property's areas. No FK yet (area FK is ``SET NULL`` elsewhere
    # and would orphan the schedule if dropped); the domain service
    # validates the area exists in the property at write time once
    # cd-sn26 lands. Plain ``String`` matches the ``scope_property_id``
    # convention on ``role_grant``.
    area_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Raw iCalendar RRULE body (e.g. ``FREQ=WEEKLY;BYDAY=MO,TH``).
    # The domain layer parses + evaluates against the parent
    # property's timezone.
    rrule_text: Mapped[str] = mapped_column(String, nullable=False)
    dtstart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # cd-k4l spec ``dtstart_local`` column. ISO-8601 string in the
    # property's local timezone (e.g. ``2026-04-20T09:00``). The
    # scheduler worker resolves to UTC via ``property.timezone`` at
    # occurrence-generation time. Nullable for backward compat;
    # new writes populate it.
    dtstart_local: Mapped[str | None] = mapped_column(String, nullable=True)
    until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # cd-k4l duration override. Nullable → fall back to the
    # template's ``duration_minutes``. Matches the §06 schedule
    # table.
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # cd-k4l RDATE / EXDATE line-separated ISO-8601 local timestamps.
    # Stored verbatim; the generator re-parses on use. Empty string
    # on create when the caller passed no overrides — the domain
    # layer treats empty as "no extra dates".
    rdate_local: Mapped[str] = mapped_column(String, nullable=False, default="")
    exdate_local: Mapped[str] = mapped_column(String, nullable=False, default="")
    # cd-k4l active-range columns. Property-local dates; the
    # generator filters occurrences to ``active_from ≤ date ≤
    # active_until`` before materialising. ``active_from`` is
    # required (``NULL`` would be ambiguous — "since when?" is a
    # mandatory answer for a live schedule); ``active_until`` is
    # nullable → open-ended weekly.
    active_from: Mapped[str | None] = mapped_column(String, nullable=True)
    active_until: Mapped[str | None] = mapped_column(String, nullable=True)
    # cd-k4l pause marker. ``NULL`` = live. ``paused_at`` wins over
    # ``active_from`` / ``active_until`` per §06 "Pause vs active
    # range" — a paused schedule never generates occurrences.
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # cd-k4l soft-delete marker. The template `delete()` in
    # :mod:`app.domain.tasks.templates` reads this column when
    # deciding whether a schedule still references the template;
    # matches the convention on :class:`TaskTemplate.deleted_at`.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assignee_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # cd-k4l ordered backup-assignee list. JSON list of user ids;
    # walked in order by the assignment algorithm (§06) when the
    # primary (``assignee_user_id``) is unavailable. Domain service
    # validates each entry holds a matching ``user_work_role`` at
    # write time, returning 422 ``backup_invalid_work_role`` on
    # failure (§06 "Backup list validation"). Empty by default.
    backup_assignee_user_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # Nullable — an unset role lets the generator resolve the
    # persona per-occurrence from the template's
    # ``default_assignee_role``.
    assignee_role: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # When the scheduler worker should next walk this schedule.
    # ``NULL`` means "not scheduled yet" — the generator treats a
    # freshly-inserted row as due immediately.
    next_generation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "until IS NULL OR until > dtstart",
            name="until_after_dtstart",
        ),
        CheckConstraint(
            "assignee_role IS NULL OR assignee_role IN "
            f"({_in_clause(_ASSIGNEE_ROLE_VALUES)})",
            name="assignee_role",
        ),
        # Scheduler hot path: "which schedules are due now?"
        Index("ix_schedule_workspace_next_gen", "workspace_id", "next_generation_at"),
        Index("ix_schedule_template", "template_id"),
        # cd-k4l list hot path: "live schedules in this workspace"
        # filters on ``(workspace_id, deleted_at IS NULL)``; the
        # composite index powers the manager's `/schedules` view.
        Index("ix_schedule_workspace_deleted", "workspace_id", "deleted_at"),
    )


class NlTaskPreview(Base):
    """Short-lived preview for natural-language task intake.

    ``POST /tasks/from_nl`` is dry-run-first: the LLM output and the
    deterministic resolver result are persisted here for up to 24 hours,
    then ``/tasks/from_nl/commit`` consumes the stored shape to create the
    task template and schedule. The table is workspace-scoped so previews
    cannot be committed across tenants.
    """

    __tablename__ = "nl_task_preview"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    original_text: Mapped[str] = mapped_column(String, nullable=False)
    resolved_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    assumptions_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    ambiguities_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_nl_task_preview_workspace_expires", "workspace_id", "expires_at"),
    )


class Occurrence(Base):
    """A materialised unit of work derived from a schedule or ad-hoc.

    ``schedule_id`` is nullable: one-off tasks (quick-add, turnover
    bundles) have no parent schedule. ``template_id`` is ``RESTRICT``
    so a template that has produced occurrences can't be
    hard-deleted — history must survive. ``property_id`` cascades
    (property deletion sweeps its history); the actor pointers
    (``assignee_user_id``, ``completed_by_user_id``,
    ``reviewer_user_id``, ``created_by_user_id``) all ``SET NULL``
    so the rows outlive their actors.

    **cd-0rf** extends the cd-22e slice with the §06 "Task row" columns
    the one-off creation service (:mod:`app.domain.tasks.oneoff`)
    needs to materialise an ad-hoc task without a parent schedule:
    ``title``, ``description_md``, ``priority``, ``photo_evidence``,
    ``duration_minutes``, ``area_id``, ``unit_id``,
    ``expected_role_id``, ``linked_instruction_ids``,
    ``inventory_consumption_json``, ``is_personal``, and
    ``created_by_user_id``. Every new column is nullable or carries
    a server default so rows inserted by cd-22e (the scheduler
    worker — which still copies only the minimum generator surface)
    stay legal; a follow-up Beads task teaches the generator to
    carry the full shape through from the template.
    """

    __tablename__ = "occurrence"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    schedule_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("schedule.id", ondelete="SET NULL"),
        nullable=True,
    )
    # RESTRICT — the template row carries the historical definition
    # of what this occurrence was; losing it would orphan every
    # record of completion. Callers must soft-delete the template
    # (column not in this slice; arrives with cd-0tg) to retire it.
    #
    # cd-0rf widens this to nullable so pure-ad-hoc one-off tasks
    # (no parent template) can land. The scheduler worker still
    # writes a template on every generator insert, so RESTRICT
    # semantics survive in practice for schedule-backed rows.
    template_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("task_template.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # cd-0rf widens this to nullable so personal one-off tasks
    # without a property anchor can land. The domain service
    # validates property ownership at write time when ``property_id``
    # is set; a ``NULL`` value flags a workspace-scoped personal
    # task (§06 "Self-created and personal tasks").
    property_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=True,
    )
    assignee_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # cd-22e property-local ISO-8601 timestamp mirroring ``starts_at``.
    # Stored as text (not ``DateTime``) for parity with
    # ``Schedule.dtstart_local``: the value is intentionally tz-naive
    # in the property frame, and routing it through a typed column
    # would invite implicit UTC coercion. Nullable for backward compat
    # with pre-cd-22e rows; the scheduler worker populates it on every
    # insert and the partial unique index below keys off it.
    scheduled_for_local: Mapped[str | None] = mapped_column(String, nullable=True)
    # cd-22e historical anchor: the property-local timestamp the
    # scheduler worker chose for this occurrence at generation time.
    # Immutable — even if the task is later rescheduled (pull-back,
    # manager edit), this field keeps the original pick so reports
    # can tell an SLA slip from a deliberate move. Seeded equal to
    # ``scheduled_for_local`` at creation time. Nullable for backward
    # compat with pre-cd-22e rows.
    originally_scheduled_for: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    # cd-hurw soft-overdue marker (§06 "State machine"). The sweeper
    # stamps this with ``now`` when it flips a row into ``overdue``;
    # any manual transition (start / complete / skip / cancel /
    # revert_overdue) clears it back to NULL. NULL therefore means
    # "not currently overdue", whether the row never slipped or
    # whether a manual transition recovered it.
    overdue_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewer_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # cd-k4l cancellation reason. Free-form text set alongside
    # ``state='cancelled'`` by the schedule-delete cascade
    # (``'schedule deleted'``), task-cancel flow, etc. Nullable on
    # every non-cancelled row; the domain layer enforces the
    # "cancelled ↔ reason" pairing at write time (per-spec: the
    # delete cascade populates ``'schedule deleted'`` verbatim).
    cancellation_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # cd-0rf §06 "Task row" columns. ``title`` / ``description_md``
    # are nullable on the migration side because the cd-22e
    # generator does not populate them; every ad-hoc write through
    # :mod:`app.domain.tasks.oneoff` fills them.
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    description_md: Mapped[str | None] = mapped_column(String, nullable=True)
    priority: Mapped[str] = mapped_column(String, nullable=False, default="normal")
    photo_evidence: Mapped[str] = mapped_column(
        String, nullable=False, default="disabled"
    )
    # Per-occurrence duration override. Rendered values fall back to
    # ``ends_at - starts_at`` when NULL.
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Soft pointers — no FK. Losing the area / unit / role must not
    # orphan the task row. The domain service validates existence at
    # write time once cd-sn26 widens area / unit CRUD.
    area_id: Mapped[str | None] = mapped_column(String, nullable=True)
    unit_id: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_role_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # §07 linked instructions + §08 SKU → qty inventory consumption.
    # Both copied down from the template on the ad-hoc path; the
    # authoritative per-task lists live on the parent template until
    # a manager amends the task itself.
    linked_instruction_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    inventory_consumption_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # §06 "Self-created and personal tasks". Default ``False`` —
    # only the quick-add UI flips it to ``True`` explicitly. The
    # visibility filter lives in the §15 read layer.
    is_personal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Origin actor — ``SET NULL`` on user delete so history survives.
    # Nullable on the migration side (cd-22e generator does not
    # populate); every ad-hoc write populates it.
    created_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"state IN ({_in_clause(_OCCURRENCE_STATE_VALUES)})",
            name="state",
        ),
        CheckConstraint(
            f"priority IN ({_in_clause(_PRIORITY_VALUES)})",
            name="priority",
        ),
        CheckConstraint(
            f"photo_evidence IN ({_in_clause(_PHOTO_EVIDENCE_VALUES)})",
            name="photo_evidence",
        ),
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        # Per-acceptance: "my tasks" sort by time (`/today` view).
        Index(
            "ix_occurrence_workspace_assignee_starts",
            "workspace_id",
            "assignee_user_id",
            "starts_at",
        ),
        # Per-acceptance: manager queues by state + time.
        Index(
            "ix_occurrence_workspace_state_starts",
            "workspace_id",
            "state",
            "starts_at",
        ),
        # cd-hurw sweeper hot path: "find every task in
        # scheduled / pending / in_progress whose ends_at + grace is
        # past" — the ``state IN (...)`` prefix is the selective leg,
        # and ``overdue_since`` trails so the sibling "already overdue,
        # skip" branch stays cheap on a per-tenant scan.
        Index(
            "ix_occurrence_workspace_state_overdue_since",
            "workspace_id",
            "state",
            "overdue_since",
        ),
        # cd-22e idempotency guard: two generator runs over the same
        # window must not materialise the same ``(schedule_id,
        # scheduled_for_local)`` twice. Scoped to ``schedule_id IS
        # NOT NULL`` via the dialect ``_where`` kwargs so one-off
        # tasks (no parent schedule) don't trip the unique constraint
        # on a ``NULL`` schedule_id. Both SQLite and PostgreSQL
        # respect partial unique indexes; the kwargs are dialect-
        # specific and SQLAlchemy passes them through to the DDL
        # emitter on the matching backend.
        Index(
            "uq_occurrence_schedule_scheduled_for_local",
            "schedule_id",
            "scheduled_for_local",
            unique=True,
            sqlite_where=text("schedule_id IS NOT NULL"),
            postgresql_where=text("schedule_id IS NOT NULL"),
        ),
    )


class ChecklistItem(Base):
    """Per-occurrence checklist row.

    Copied from the template's :class:`ChecklistTemplateItem` rows
    at generation time, then authoritative for the life of the
    occurrence — the worker checks items off against this table,
    not the template. ``evidence_blob_hash`` carries the content-
    addressed hash of the attached artefact (if any); the blob
    itself lives in the asset store per §21.
    """

    __tablename__ = "checklist_item"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("occurrence.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    requires_photo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    checked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evidence_blob_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "occurrence_id",
            "position",
            name="uq_checklist_item_occurrence_position",
        ),
        Index("ix_checklist_item_occurrence", "occurrence_id"),
    )


class Evidence(Base):
    """Artefact attached to an :class:`Occurrence`.

    ``blob_hash`` is the content-addressed pointer into the asset
    store and is ``NULL`` for the ``note`` kind (the note body lives
    in ``note_md``). Every write records ``created_by_user_id`` so
    the audit trail can reproduce who attached the artefact; the FK
    uses ``SET NULL`` so history survives the actor's removal.
    """

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("occurrence.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    blob_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # Markdown body for the ``note`` kind; ``NULL`` for every other
    # kind. The domain layer enforces the "note ↔ note_md" pairing
    # at write time.
    note_md: Mapped[str | None] = mapped_column(String, nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    checklist_snapshot_json: Mapped[list[Any] | None] = mapped_column(
        JSON, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_EVIDENCE_KIND_VALUES)})",
            name="kind",
        ),
        Index("ix_evidence_workspace_occurrence", "workspace_id", "occurrence_id"),
        Index("ix_evidence_workspace_deleted", "workspace_id", "deleted_at"),
    )


class Comment(Base):
    """Threaded markdown comment on an :class:`Occurrence` — the agent inbox.

    The author pointer uses ``SET NULL`` so the thread survives a
    user-delete (important for audit). ``attachments_json`` is a
    list payload of blob-hash + filename pairs; the domain layer
    validates shape at write time. The
    ``(workspace_id, occurrence_id, created_at)`` index supports
    the per-thread read path — the expected query is "give me this
    occurrence's comments in order".

    **cd-cfe4** extends the cd-chd v1 slice with the §06 "Task notes
    are the agent inbox" shape: ``kind`` (``user | agent | system``)
    separates human authors from the embedded workspace agent and
    from internal state-change markers; ``mentioned_user_ids`` is
    the resolved-at-write-time list of ``@mention`` targets the §10
    messaging fanout reads; ``edited_at`` and ``deleted_at`` are
    soft-state timestamps (the domain service enforces the 5-minute
    edit window and soft-delete semantics); ``llm_call_id`` is a
    soft pointer into ``llm_call`` (no FK yet — the table is not in
    the schema) so an agent message carries the LLM call that
    produced it.
    """

    __tablename__ = "comment"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    occurrence_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("occurrence.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    body_md: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Flat list of ``{blob_hash, filename, …}`` payloads. The outer
    # ``Any`` is scoped to SQLAlchemy's JSON column type — callers
    # writing a typed payload should use a TypedDict locally and
    # coerce into this column.
    attachments_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # cd-cfe4 message kind. Server default ``'user'`` so pre-cd-cfe4
    # rows survive the migration; the domain service writes through
    # on every new insert.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="user")
    # Resolved ``@mention`` user ids (§06 "Task notes are the agent
    # inbox"). Populated by the domain service at write time;
    # consumed by the §10 messaging fanout for the offline-mention
    # email. Empty list on agent / system messages (they don't
    # mention humans).
    mentioned_user_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # cd-cfe4 edit marker. ``NULL`` = never edited. The domain
    # service only allows :func:`app.domain.tasks.comments.edit_comment`
    # within the 5-minute grace window on ``kind='user'`` rows; agent
    # / system messages never flip this column.
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # cd-cfe4 soft-delete marker. ``NULL`` = live. :func:`list_comments`
    # hides non-null rows for every reader except workspace owners,
    # so moderation history survives without bleeding into the
    # worker / manager thread view.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Soft pointer to the :class:`llm_call` row that produced this
    # message. NULL for ``user`` / ``system`` rows, populated for
    # ``agent`` rows when the domain service knows the call id. No
    # FK — the ``llm_call`` table's lifecycle is independent of
    # comment retention (an archived LLM call should not sweep the
    # comment thread).
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_COMMENT_KIND_VALUES)})",
            name="kind",
        ),
        Index(
            "ix_comment_workspace_occurrence_created",
            "workspace_id",
            "occurrence_id",
            "created_at",
        ),
    )
