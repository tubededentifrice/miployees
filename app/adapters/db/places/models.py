"""Property / Unit / Area / PropertyWorkspace / PropertyClosure models.

v1 slice per cd-i6u. The richer §02 / §04 surface (structured
``address_json``, ``kind`` / ``client_org_id`` / ``owner_user_id``
on ``property``; ``unit.default_checkin_time`` /
``welcome_overrides_json`` / ``settings_override_json``;
``area.kind`` / ``unit_id`` / ``parent_area``; extended
``property_workspace.share_guest_identity`` / ``invite_id`` /
``added_via`` / ``added_by_user_id``; etc.) is deferred to cd-8u5
(property domain service) and follow-up migrations without
breaking this migration's public write contract.

The `property` row itself is **NOT** workspace-scoped — the same
villa can belong to several workspaces through the
``property_workspace`` junction (§02 "Villa belongs to many
workspaces"). Adapters that need a workspace-filtered property list
MUST join through ``property_workspace``; see the package
docstring for the tenancy contract on ``unit`` / ``area`` /
``property_closure``.

See ``docs/specs/02-domain-model.md`` §"property_workspace",
``docs/specs/04-properties-and-stays.md`` §"Property" / §"Unit" /
§"Area".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``user.id`` /
# ``user_work_role.id`` / ``workspace.id`` / ``pay_rule.id`` FKs
# below resolve against ``Base.metadata`` only if the target
# packages have been imported, so we register them here as a side
# effect.
from app.adapters.db.identity import models as _identity_models  # noqa: F401
from app.adapters.db.payroll import models as _payroll_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = [
    "Area",
    "Property",
    "PropertyClosure",
    "PropertyWorkRoleAssignment",
    "PropertyWorkspace",
    "Unit",
]


# Allowed ``property_workspace.membership_role`` values, enforced by a
# CHECK constraint. Matches §02 "property_workspace" —
# ``owner_workspace`` is the governance anchor (at most one per
# property), ``managed_workspace`` is operational access granted by
# the owner, ``observer_workspace`` is read-only.
_MEMBERSHIP_ROLE_VALUES: tuple[str, ...] = (
    "owner_workspace",
    "managed_workspace",
    "observer_workspace",
)

# Allowed ``property.kind`` values — drives default lifecycle rule +
# area seeding behaviour (§04 "`kind` semantics"). The CHECK on the
# column enforces the enum; the domain layer narrows the loaded
# string to a :class:`Literal` on read.
_PROPERTY_KIND_VALUES: tuple[str, ...] = (
    "residence",
    "vacation",
    "str",
    "mixed",
)

# Allowed ``unit.type`` values for the v1 slice. §04 speaks of a
# free-form unit kind ("Room 1", "Apt 3B"); the column here carries
# the physical-kind taxonomy (apartment / studio / room / bungalow /
# villa / other). A tighter spec-matched enum lands with cd-8u5.
_UNIT_TYPE_VALUES: tuple[str, ...] = (
    "apartment",
    "studio",
    "room",
    "bungalow",
    "villa",
    "other",
)


class Property(Base):
    """A physical place the workspace operates in.

    The v1 slice (cd-i6u) landed ``id`` / ``address`` / ``timezone``
    / ``lat`` / ``lon`` / ``tags_json`` / ``created_at``. cd-8u5
    added the richer §02 / §04 surface the manager UI and the
    property domain service need:

    * ``name`` — human-visible display name.
    * ``kind`` — lifecycle-seeding enum (``residence | vacation |
      str | mixed``).
    * ``address_json`` — canonical structured address; ``country``
      inside it is back-filled on write (§04 "`address_json`
      canonical shape").
    * ``country`` — ISO-3166-1 alpha-2 country code.
    * ``locale`` / ``default_currency`` — optional per-property
      overrides; inherit workspace defaults when ``NULL``.
    * ``client_org_id`` / ``owner_user_id`` — soft references to
      ``organization`` (cd-t8m) and ``users``.
    * ``welcome_defaults_json`` / ``property_notes_md`` — JSON blob
      + staff-visible notes.
    * ``updated_at`` / ``deleted_at`` — mutation + soft-delete
      timestamps.

    The table is **NOT** workspace-scoped: the same row may link to
    several workspaces through :class:`PropertyWorkspace`. Services
    that need a workspace-filtered property list MUST join through
    the junction; see the package docstring.
    """

    __tablename__ = "property"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Display name ("Villa Sud", "Apt 3B"). Nullable at the DB layer
    # so the cd-8u5 migration can backfill from ``address`` without
    # a two-step tighten; the domain service always writes a non-
    # blank value on insert.
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Lifecycle-seeding enum. CHECK-enforced via ``ck_property_kind``.
    # Server default ``residence`` (most conservative seed) so legacy
    # rows keep working; the service narrows to a :class:`Literal`
    # on read.
    kind: Mapped[str] = mapped_column(String, nullable=False, default="residence")
    # v1 stores the postal address as a single text blob. cd-8u5
    # keeps ``address`` as the rendered single-line form for legacy
    # adapters and adds ``address_json`` for the canonical shape.
    address: Mapped[str] = mapped_column(String, nullable=False)
    # Canonical structured address — ``line1`` / ``line2`` / ``city``
    # / ``state_province`` / ``postal_code`` / ``country``. Empty
    # object for legacy rows; the service back-fills ``country`` in
    # both directions on write.
    address_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # ISO-3166-1 alpha-2 country code. Authoritative source is
    # ``address_json.country`` when present; the back-fill keeps both
    # columns in sync on every write.
    country: Mapped[str] = mapped_column(String, nullable=False, default="XX")
    # BCP-47 locale tag; nullable = inherit workspace language +
    # property country at render time (§04 "Property" — locale field).
    locale: Mapped[str | None] = mapped_column(String, nullable=True)
    # ISO-4217 currency override; nullable = inherit workspace
    # ``default_currency``.
    default_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    # IANA timezone (e.g. ``Europe/Paris``). Every timestamp that is
    # "local to this place" — stay check-in/out, task occurrence —
    # resolves through this column.
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Soft reference to ``organization.id`` (cd-t8m). NULL = the
    # workspace is its own employer (§04 "Billing client").
    client_org_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft reference to ``users.id`` — display-only "owner of record"
    # pointer. Authorisation is governed by ``property_workspace`` +
    # the workspace's ``owners`` group, never by this column.
    owner_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Free-form labels a workspace uses to group properties (e.g.
    # ``["riviera", "off-season"]``). The list shape is declared on
    # the mapped annotation so callers writing a typed payload don't
    # need an ``Any`` cast; the DB column is a plain JSON blob.
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Welcome-page payload (§04 "Welcome defaults"). Empty object
    # when unset; the guest welcome page merges unit overrides over
    # this blob.
    welcome_defaults_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # Internal staff-visible notes (§04 "Property" — property_notes_md).
    property_notes_md: Mapped[str] = mapped_column(String, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Mutation timestamp — bumped on every domain-service update.
    # Nullable for the cd-8u5 migration's cheap backfill path; the
    # service always writes it on insert + update.
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Soft-delete marker; live rows carry ``NULL``. The service's
    # default list excludes non-null rows.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('" + "', '".join(_PROPERTY_KIND_VALUES) + "')",
            name="kind",
        ),
        Index("ix_property_deleted", "deleted_at"),
    )


class PropertyWorkspace(Base):
    """Junction row binding a property to a workspace.

    The composite PK ``(property_id, workspace_id)`` lets the same
    physical property belong to several workspaces at once.
    ``workspace_id`` is what the ORM tenant filter
    (:mod:`app.tenancy.orm_filter`) pins to the active
    :class:`~app.tenancy.WorkspaceContext`, so reads of this junction
    are naturally scoped to the caller's workspace.

    ``membership_role`` expresses how the workspace relates to the
    property — owner / managed / observer (§02 "Villa belongs to
    many workspaces"). The v1 slice defaults new rows to
    ``owner_workspace``; the CHECK constraint enforces the enum.
    """

    __tablename__ = "property_workspace"

    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    membership_role: Mapped[str] = mapped_column(
        String, nullable=False, default="owner_workspace"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "membership_role IN ('" + "', '".join(_MEMBERSHIP_ROLE_VALUES) + "')",
            name="membership_role",
        ),
        # Composite PK already covers "workspaces for this property";
        # these indexes speed the sibling lookup directions.
        Index("ix_property_workspace_workspace", "workspace_id"),
        Index("ix_property_workspace_property", "property_id"),
    )


class Unit(Base):
    """Bookable subdivision of a property.

    v1 slice: ``id`` / ``property_id`` / ``label`` / ``type`` /
    ``capacity`` / ``created_at``. The richer §04 columns
    (``default_checkin_time``, ``welcome_overrides_json``,
    ``settings_override_json``, ``ordinal``) land with cd-8u5.
    Workspace isolation is enforced by joining through
    :class:`PropertyWorkspace` — the package docstring spells out
    why ``unit`` itself stays unregistered.
    """

    __tablename__ = "unit"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    # Physical-kind taxonomy; a tighter spec-matched enum lands with
    # cd-8u5. CHECK enforces the v1 set.
    type: Mapped[str] = mapped_column(String, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "type IN ('" + "', '".join(_UNIT_TYPE_VALUES) + "')",
            name="type",
        ),
        Index("ix_unit_property", "property_id"),
    )


class Area(Base):
    """Subdivision of a property — kitchen, pool, garden, etc.

    v1 slice: ``id`` / ``property_id`` / ``label`` / ``icon`` /
    ``ordering`` / ``created_at``. The §04 ``unit_id`` (for
    unit-scoped areas), ``kind`` enum and ``parent_area`` self-FK
    land with cd-8u5.
    """

    __tablename__ = "area"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    # ``icon`` is the lucide icon slug the UI renders next to the
    # label (e.g. ``"utensils"``, ``"waves"``). Nullable — areas
    # without a canonical icon just render the label.
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    # ``ordering`` is the integer walk-order hint (§04 "Auto-seeded
    # areas"); lower values render first.
    ordering: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (Index("ix_area_property", "property_id"),)


class PropertyClosure(Base):
    """Blackout window on a property — renovation, owner-stay, etc.

    v1 slice: ``id`` / ``property_id`` / ``starts_at`` / ``ends_at``
    / ``reason`` / ``created_by_user_id`` / ``created_at``. The
    CHECK ``ends_after_starts`` guards against zero-or-negative-
    length windows (a closure that covers no time is a data bug,
    not a legitimate operational state).

    ``created_by_user_id`` is nullable + ``ON DELETE SET NULL`` so
    history survives the actor's deletion; every other FK cascades
    on the parent property's delete.
    """

    __tablename__ = "property_closure"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ends_after_starts"),
        Index("ix_property_closure_property_starts", "property_id", "starts_at"),
    )


class PropertyWorkRoleAssignment(Base):
    """Per-property pinning of a :class:`UserWorkRole` (cd-e4m3).

    Pins a :class:`~app.adapters.db.workspace.models.UserWorkRole` to
    a specific :class:`Property`. The absence of any assignment row
    leaves the user's role **workspace-wide** (a "generalist" —
    eligible for every property in the workspace, per §05 "Property
    work role assignment"). One or more rows narrow eligibility to
    those properties only.

    A single (user_work_role, property) pair is **uniquely
    identified** by an active row here — variation in *when* the
    user works that property (e.g. Mon mornings vs. Mon afternoons)
    is expressed by the multi-slot :ref:`schedule_ruleset`
    referenced via :attr:`schedule_ruleset_id`, not by stacking
    multiple ``property_work_role_assignment`` rows. That makes the
    partial ``UNIQUE (user_work_role_id, property_id) WHERE
    deleted_at IS NULL`` the natural identity key.

    **Tenancy.** The table carries a denormalised ``workspace_id``
    column even though the parent ``user_work_role`` already encodes
    the workspace. This matches the
    :class:`~app.adapters.db.workspace.models.WorkEngagement` /
    :class:`~app.adapters.db.workspace.models.UserWorkRole`
    pattern — the ORM tenant filter rides a local column rather
    than threading a join through the parent on every read. The
    package's ``__init__`` registers the table so a bare SELECT
    without a :class:`~app.tenancy.WorkspaceContext` raises
    :class:`~app.tenancy.orm_filter.TenantFilterMissing`.

    **Domain-enforced invariants** (write-path; not expressed in
    DDL):

    1. ``workspace_id`` must equal the parent ``user_work_role``'s
       ``workspace_id``. Cross-workspace borrowing is already
       blocked by §05 "User work role"; the redundancy is explicit
       here so a future bulk-loader can't slip a row through.
    2. ``property_id`` must point at a property that is linked to
       ``workspace_id`` through a live ``property_workspace`` row —
       a workspace cannot pin a role to a property it doesn't
       operate. Validated at write time by the future API service
       (cd-za6n).

    **Soft references** (no FK declared):

    * ``schedule_ruleset_id`` — the ``schedule_ruleset`` table
      does not yet exist (§06 "Schedule ruleset (per-property
      rota)"; landing in a sibling task). Plain :class:`str` until
      the table lands; a follow-up migration may promote it into a
      real FK without disturbing domain callers (same pattern as
      :attr:`~app.adapters.db.workspace.models.UserWorkRole.pay_rule_id`).

    **Real foreign keys** (already in the schema):

    * ``user_work_role_id`` → ``user_work_role.id`` ``ON DELETE
      CASCADE`` — hard-deleting a user_work_role sweeps every
      assignment row.
    * ``property_id`` → ``property.id`` ``ON DELETE CASCADE`` —
      hard-deleting the property sweeps the row (matching the
      sibling :class:`Unit` / :class:`Area` / :class:`PropertyClosure`
      cascade).
    * ``workspace_id`` → ``workspace.id`` ``ON DELETE CASCADE``.
    * ``property_pay_rule_id`` → ``pay_rule.id`` ``ON DELETE SET
      NULL`` — losing the pay rule drops the override but keeps the
      assignment alive (the engagement-level rule re-applies).

    See ``docs/specs/05-employees-and-roles.md`` §"Property work
    role assignment", ``docs/specs/02-domain-model.md`` §"People,
    work roles, engagements", and
    ``docs/specs/06-tasks-and-scheduling.md`` §"Schedule ruleset
    (per-property rota)".
    """

    __tablename__ = "property_work_role_assignment"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Denormalised tenancy column — the ORM tenant filter rides this
    # local column rather than threading a join through
    # ``user_work_role`` on every read. Always equal to the parent
    # ``user_work_role.workspace_id`` (write-path invariant; see the
    # class docstring).
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_work_role_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user_work_role.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Soft reference to the future ``schedule_ruleset`` table (§06).
    # NULL = no rota declared — eligibility falls back to
    # ``user_weekly_availability`` alone (§05 "Property work role
    # assignment").
    schedule_ruleset_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Per-property rate override. NULL = inherit the engagement-level
    # rule. ``ON DELETE SET NULL`` so losing the pay rule doesn't
    # nuke the assignment — the engagement rule re-applies.
    property_pay_rule_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("pay_rule.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Soft-delete tombstone; live rows carry NULL. The partial UNIQUE
    # below excludes tombstoned rows so a re-pin after an archive
    # mints a fresh row without colliding with the historical one.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Identity key — one live row per (user_work_role, property).
        # Variation in *when* the user works the property is
        # expressed by ``schedule_ruleset_slot`` rows under the
        # referenced ruleset, not by stacking multiple assignments.
        # Tombstoned rows are excluded so an archive + re-pin works.
        Index(
            "uq_property_work_role_assignment_role_property_active",
            "user_work_role_id",
            "property_id",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # "List live assignments for this workspace" hot path —
        # leading ``workspace_id`` carries the tenant filter;
        # trailing ``deleted_at`` lets the planner skip tombstones.
        Index(
            "ix_property_work_role_assignment_workspace_deleted",
            "workspace_id",
            "deleted_at",
        ),
        # "Every assignment of this user_work_role" — the employees
        # surface walks this index to display per-user property
        # narrowings.
        Index(
            "ix_property_work_role_assignment_workspace_user_work_role",
            "workspace_id",
            "user_work_role_id",
        ),
        # "Every assignment at this property" — the property's
        # workforce panel walks this index to list the workers
        # operating the place.
        Index(
            "ix_property_work_role_assignment_workspace_property",
            "workspace_id",
            "property_id",
        ),
    )
