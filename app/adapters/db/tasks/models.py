"""TaskTemplate / ChecklistTemplateItem / Schedule / Occurrence /
ChecklistItem / Evidence / Comment SQLAlchemy models.

v1 slice per cd-chd — sufficient for seeding the template → schedule
→ occurrence → {checklist / evidence / comment} chain that backs
the product's centre of gravity. The richer §02 / §06 surface
(``paused_at``, ``active_from`` / ``active_until``,
``checklist_snapshot_json``, ``photo_evidence`` as a three-value
enum, the fuller ``scheduled | cancelled | overdue`` state machine,
soft-delete ``deleted_at``) lands with follow-up tasks
(cd-0tg template CRUD, cd-4qr turnover auto-generation) without
breaking this migration's public write contract.

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
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = [
    "ChecklistItem",
    "ChecklistTemplateItem",
    "Comment",
    "Evidence",
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

# Allowed ``occurrence.state`` values. The v1 slice is the minimum
# chain the acceptance criterion asks for; the fuller §06 machine
# (``scheduled`` / ``cancelled`` / ``overdue``) lands with the
# domain-layer scheduler worker (cd-0tg + follow-ups).
_OCCURRENCE_STATE_VALUES: tuple[str, ...] = (
    "pending",
    "in_progress",
    "done",
    "skipped",
    "approved",
)

# Allowed ``evidence.kind`` values — the §02 evidence taxonomy. The
# matching ``required_evidence`` enum is a strict superset (it also
# carries ``none``, which is a template-level "no evidence
# required" marker rather than a stored-artefact kind).
_EVIDENCE_KIND_VALUES: tuple[str, ...] = ("photo", "note", "voice", "gps")


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
    at generation time — title, description, expected duration,
    evidence requirements, the embedded checklist payload. The
    ``checklist_template_json`` column is an ergonomic cache for the
    client-side editor; the authoritative per-item list lives on
    :class:`ChecklistTemplateItem` rows.
    """

    __tablename__ = "task_template"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    # Markdown body rendered in the template detail view. Plain text
    # is a legal subset, so unit tests can write unescaped strings.
    description_md: Mapped[str] = mapped_column(String, nullable=False, default="")
    default_duration_min: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    required_evidence: Mapped[str] = mapped_column(
        String, nullable=False, default="none"
    )
    photo_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Nullable — a template without a default persona lets the
    # schedule / ad-hoc creator pick per-occurrence.
    default_assignee_role: Mapped[str | None] = mapped_column(String, nullable=True)
    # Flat list of checklist-item payloads. The outer ``Any`` is
    # scoped to SQLAlchemy's JSON column type — callers writing a
    # typed payload should use a TypedDict locally and coerce into
    # this column. The authoritative per-item list lives on
    # :class:`ChecklistTemplateItem`; this JSON field mirrors it for
    # the client editor.
    checklist_template_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

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
        Index("ix_task_template_workspace", "workspace_id"),
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
    # Raw iCalendar RRULE body (e.g. ``FREQ=WEEKLY;BYDAY=MO,TH``).
    # The domain layer parses + evaluates against the parent
    # property's timezone.
    rrule_text: Mapped[str] = mapped_column(String, nullable=False)
    dtstart: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assignee_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
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
    )


class Occurrence(Base):
    """A materialised unit of work derived from a schedule or ad-hoc.

    ``schedule_id`` is nullable: one-off tasks (quick-add, turnover
    bundles) have no parent schedule. ``template_id`` is ``RESTRICT``
    so a template that has produced occurrences can't be
    hard-deleted — history must survive. ``property_id`` cascades
    (property deletion sweeps its history); the actor pointers
    (``assignee_user_id``, ``completed_by_user_id``,
    ``reviewer_user_id``) all ``SET NULL`` so the rows outlive their
    actors.
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
    template_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("task_template.id", ondelete="RESTRICT"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    assignee_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"state IN ({_in_clause(_OCCURRENCE_STATE_VALUES)})",
            name="state",
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
    """Artefact attached to an :class:`Occurrence` — photo / note / voice / gps.

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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_EVIDENCE_KIND_VALUES)})",
            name="kind",
        ),
        Index("ix_evidence_workspace_occurrence", "workspace_id", "occurrence_id"),
    )


class Comment(Base):
    """Threaded markdown comment on an :class:`Occurrence`.

    The author pointer uses ``SET NULL`` so the thread survives a
    user-delete (important for audit). ``attachments_json`` is a
    list payload of blob-hash + filename pairs; the domain layer
    validates shape at write time. The
    ``(workspace_id, occurrence_id, created_at)`` index supports
    the per-thread read path — the expected query is "give me this
    occurrence's comments in order".
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

    __table_args__ = (
        Index(
            "ix_comment_workspace_occurrence_created",
            "workspace_id",
            "occurrence_id",
            "created_at",
        ),
    )
