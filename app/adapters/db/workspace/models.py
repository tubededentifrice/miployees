"""Workspace + UserWorkspace + WorkRole + UserWorkRole + WorkEngagement
SQLAlchemy models.

v1 slice of §02's ``workspaces`` and ``user_workspace`` schemas —
enough columns to unblock downstream DB + auth work:

* cd-w92 (identity: ``users`` / ``passkey_credential`` / ``session``)
* cd-i6u (places: ``property`` / ``property_workspace``)
* cd-3i5 (self-serve signup)
* cd-jpa (membership lifecycle)

The richer ``workspaces`` surface from §02 —
``verification_state``, ``signup_ip``, ``default_language`` /
``_currency`` / ``_country`` / ``_locale``, ``created_via``,
``created_by_user_id`` — is deferred to cd-n6p (owner settings) and
cd-055 (signup quotas) and lands via follow-up migrations without
breaking the v1 public surface. The ``settings_json`` column already
lands here (cd-jdhm) so the recovery kill-switch has a canonical
home; cd-n6p populates the rest of the owner-facing settings keys
against the same column.

``user_workspace.user_id`` is a **soft reference** (no FK) because
the ``users`` table lands with cd-w92. Once cd-w92 ships a later
migration may promote this into a real FK if the schema review agrees;
for now the composite PK + the FK on ``workspace_id`` is enough to
keep referential drift visible.

cd-5kv4 adds two more workspace-scoped tables to the same package
(both registered alongside ``user_workspace``):

* :class:`WorkRole` — the per-workspace job catalogue (``maid``,
  ``cook``, ``driver``, …) per §05 "Work role".
* :class:`UserWorkRole` — links a user to a work role within one
  workspace, with per-assignment overrides per §05 "User work role".

cd-4saj lands the third §05 sibling in the same package:

* :class:`WorkEngagement` — the per-(user, workspace) employment
  relationship that carries the pay pipeline (§02 "work_engagement",
  §22 "Engagement kinds"). The soft-ref columns
  ``supplier_org_id`` / ``pay_destination_id`` /
  ``reimbursement_destination_id`` are plain :class:`str` with no FK
  declared because the ``organization`` and ``pay_destination``
  tables do not exist yet — **cd-0ro4** (filed alongside cd-4saj)
  is the follow-up that promotes these columns into real FKs once
  the parent tables land.

These all land here rather than under a dedicated ``employees``
package because their FKs target ``workspace.id`` and the upcoming
employees domain service (cd-dv2) is the only consumer; a sibling
package would duplicate the registration plumbing without a clean
ownership story.

See ``docs/specs/02-domain-model.md`` §"workspaces",
§"user_workspace", §"work_engagement";
``docs/specs/05-employees-and-roles.md`` §"Work role" / §"User work
role"; ``docs/specs/22-clients-and-vendors.md`` §"Engagement kinds";
``docs/specs/01-architecture.md`` §"Workspace addressing" and
§"Tenant filter enforcement".
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = [
    "UserWorkRole",
    "UserWorkspace",
    "WorkEngagement",
    "WorkRole",
    "Workspace",
]


# Allowed plan values, enforced by a CHECK constraint. Matches §02
# "workspaces" — the quota gate (§15) reads ``plan`` to pick caps.
_PLAN_VALUES: tuple[str, ...] = ("free", "pro", "enterprise", "unlimited")

# Allowed membership-source values, enforced by a CHECK constraint.
# A ``user_workspace`` row is derived from at least one upstream
# grant (§02 "user_workspace"); the ``source`` column records which
# upstream is responsible so the refresh worker can prune the row
# when every upstream revokes.
_SOURCE_VALUES: tuple[str, ...] = (
    "workspace_grant",
    "property_grant",
    "org_grant",
    "work_engagement",
)

# Allowed ``work_engagement.engagement_kind`` values — enforced by a
# CHECK constraint. Matches §22 "Engagement kinds": ``payroll`` is
# the default (direct-employment pipeline), ``contractor`` is a
# self-invoicing individual, ``agency_supplied`` routes payment
# through a supplier organisation. Switching kinds mid-engagement is
# gated by the §22 domain rules.
_ENGAGEMENT_KIND_VALUES: tuple[str, ...] = (
    "payroll",
    "contractor",
    "agency_supplied",
)


class Workspace(Base):
    """One row per tenant.

    The ``id`` is a ULID (string); the ``slug`` is the globally unique
    URL label (``<host>/w/<slug>/...``). v1 ships every SaaS workspace
    on ``plan='free'``; self-hosters typically assign ``unlimited`` via
    ``crewday admin workspace set-plan``. ``quota_json`` is the
    materialised snapshot actually enforced — keeps operator overrides
    explicit (§02 "Plan + quota").
    """

    __tablename__ = "workspace"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    plan: Mapped[str] = mapped_column(String, nullable=False, default="free")
    # ``quota_json`` is a flat mapping of cap-name → limit (e.g.
    # ``users_max``, ``storage_bytes``); the outer ``Any`` is scoped to
    # SQLAlchemy's JSON column type — callers writing a typed payload
    # should use a TypedDict locally and coerce into this column.
    quota_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    # ``settings_json`` is the flat map of ``dotted.key → value`` holding
    # concrete workspace defaults for every registered setting (§02
    # "Settings cascade"). cd-jdhm lands the column so the self-service
    # recovery kill-switch (``auth.self_service_recovery_enabled``) has
    # a canonical home; cd-n6p wires owner-facing writes for the
    # **non-base** setting catalog (the four named base columns below
    # — timezone / locale / currency, plus ``name`` — are first-class
    # columns per §02 "workspaces" base columns rather than dotted
    # keys on this map). Defaulted to ``{}`` so callers never need to
    # coalesce when reading a missing key.
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # Owner-mutable identity-level base columns (cd-n6p). §02
    # "workspaces" lists them as first-class workspace columns rather
    # than dotted keys on ``settings_json``; the owner-only update path
    # in :mod:`app.services.workspace.settings_service` writes them
    # under capability gating + audit trail. Server defaults match the
    # cd-n6p migration so an existing row materialises a coherent
    # value on read; the service always writes explicit values on
    # PATCH.
    default_timezone: Mapped[str] = mapped_column(
        String, nullable=False, default="UTC", server_default="UTC"
    )
    default_locale: Mapped[str] = mapped_column(
        String, nullable=False, default="en", server_default="en"
    )
    default_currency: Mapped[str] = mapped_column(
        String, nullable=False, default="USD", server_default="USD"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Mutation timestamp — bumped on every basics edit so SSE
    # subscribers can refresh the workspace picker after an owner
    # renames the workspace or changes the default formatting (§14).
    # NOT NULL at rest: the cd-n6p migration backfills existing rows
    # from ``created_at`` so readers never coalesce defensively.
    # ``server_default = CURRENT_TIMESTAMP`` so a fresh INSERT that
    # does not name the column (the pattern in unit-test fixtures
    # predating cd-n6p) still lands a coherent value; the domain
    # service always writes an explicit value, so the server default
    # is purely a safety net.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    # ``owner_onboarded_at`` flips when the first-run wizard completes;
    # the welcome UI keys off it so quota banners know whether to show
    # getting-started hints vs. upgrade prompts.
    owner_onboarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "plan IN ('" + "', '".join(_PLAN_VALUES) + "')",
            name="plan",
        ),
        # Defence-in-depth shape check — the full ISO-4217 narrowing
        # lives in :mod:`app.util.currency`; the CHECK only catches
        # ``LENGTH != 3`` so a corrupt write without service mediation
        # cannot land an empty string or ``EURO``. Mirrors the
        # property table's ``country`` CHECK shape.
        CheckConstraint(
            "LENGTH(default_currency) = 3",
            name="default_currency_shape",
        ),
    )


class UserWorkspace(Base):
    """Derived membership junction: one row per (user, workspace).

    Populated by the role-grant refresh worker (later task) every time
    an upstream ``role_grant`` / ``work_engagement`` /
    ``property_workspace`` row changes. The composite PK guarantees
    uniqueness; the FK on ``workspace_id`` with ``ON DELETE CASCADE``
    makes workspace hard-delete sweep the junction too. ``user_id`` is
    a soft reference until cd-w92 lands ``users``.

    Registered as workspace-scoped in ``app/adapters/db/workspace/__init__.py``:
    every SELECT auto-filters on ``workspace_id`` through the ORM
    tenant filter.
    """

    __tablename__ = "user_workspace"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "source IN ('" + "', '".join(_SOURCE_VALUES) + "')",
            name="source",
        ),
        # Composite index on workspace_id alone speeds the
        # "list members of workspace" path; the composite PK already
        # covers "list workspaces of user" on its leading column.
        Index("ix_user_workspace_workspace", "workspace_id"),
    )


class WorkRole(Base):
    """Per-workspace job catalogue row (maid, cook, driver, …).

    One row per (workspace, key). The ``key`` slug is editable — §05
    "Work role" records that renames audit-log as ``work_role.rekey``
    but does not block the write — so the unique is on
    ``(workspace_id, key)`` rather than ``key`` alone, and not an
    immutable surrogate. Starter roles (``maid``, ``cook``, …) seed
    on first boot as regular rows; nothing here pins them.

    ``default_settings_json`` holds provisioning hints the owner can
    copy into a new ``work_engagement.settings_override_json`` when
    first assigning this role (§05 "Recommended role defaults" /
    §05 "Worker settings"). It is **not** a second runtime resolver —
    the cascade from §02 "Settings cascade" is the only live
    resolver. Defaults to ``{}`` so callers never coalesce on read.

    ``icon_name`` is a Lucide PascalCase name (e.g. ``BrushCleaning``,
    ``Wrench``) — see §14 "Icons". Server-defaulted to the empty
    string so pre-existing rows and callers that omit it never land
    ``NULL``. The column is NOT NULL at rest; a tight shape here
    keeps the UI free of the "icon may be missing" conditional.

    Registered as workspace-scoped in
    ``app/adapters/db/workspace/__init__.py``: every SELECT auto-
    filters on ``workspace_id`` through the ORM tenant filter.

    **Soft delete.** ``deleted_at`` carries the retirement timestamp;
    live rows have ``NULL``. Archive semantics (§05 "Archive /
    reinstate") live in the domain layer — this column is the
    marker, not the lifecycle driver. The
    ``(workspace_id, deleted_at)`` index backs the live-list hot
    path (``WHERE workspace_id = ? AND deleted_at IS NULL``).

    See ``docs/specs/05-employees-and-roles.md`` §"Work role",
    ``docs/specs/02-domain-model.md`` §"People, work roles,
    engagements".
    """

    __tablename__ = "work_role"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``key`` is the stable slug (``maid``, ``cook``). Editable —
    # §05 calls out that a rename fires ``work_role.rekey`` in the
    # audit log but is not blocked. Uniqueness is scoped to the
    # workspace so two workspaces may independently own a ``maid``.
    key: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description_md: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    # Flat map of setting dotted-key → value. The outer ``Any`` is
    # scoped to SQLAlchemy's JSON column type — callers writing a
    # typed payload should use a TypedDict locally and coerce into
    # this column (same rationale as ``Workspace.quota_json``).
    default_settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # Lucide PascalCase name. Empty-string default keeps the column
    # NOT NULL without forcing every seeder / importer to invent an
    # icon up front — the UI resolves an empty value to its neutral
    # fallback.
    icon_name: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "key",
            name="uq_work_role_workspace_key",
        ),
        # "List live roles for this workspace" hot path. Leading
        # ``workspace_id`` carries the tenant filter; trailing
        # ``deleted_at`` lets the planner skip tombstones without a
        # second pass.
        Index(
            "ix_work_role_workspace_deleted",
            "workspace_id",
            "deleted_at",
        ),
    )


class UserWorkRole(Base):
    """Links a user to a :class:`WorkRole` within a workspace.

    The same user can hold several work roles in the same workspace
    (cook + driver with different rates) and can hold a matching role
    in more than one workspace (``maid`` in workspace A, generalist
    in workspace B). §05 "User work role" is the spec of record —
    this row is the storage.

    ``pay_rule_id`` is a **soft reference** (plain :class:`str`, no
    FK) because the ``pay_rule`` table does not exist yet (cd-ea7 is
    pending). Once it lands a follow-up migration may promote this
    into a real FK; the column name and nullability stay stable so
    domain callers are not disturbed. The same soft-ref pattern is
    used on :class:`~app.adapters.db.authz.models.RoleGrant.scope_property_id`
    and :class:`~app.adapters.db.places.models.Property.owner_user_id`.

    **Invariant (domain-enforced).** Every active ``UserWorkRole``
    row must carry the same ``workspace_id`` as the row's
    ``work_role_id`` — a user cannot borrow a work-role definition
    across workspaces (§05 "User work role" Invariant). Expressing
    the check in portable DDL would need a trigger or a per-backend
    assertion, so it lives in the domain layer (cd-dv2 employees
    service) rather than the schema. The FK on ``work_role_id``
    cascades on delete so a hard-deleted work role sweeps every
    user link with it; soft deletes (``deleted_at``) leave the row
    in place.

    **Invariant (domain-enforced).** If the user holds a ``role_grant``
    with ``grant_role = 'worker'`` on this workspace, they must have
    ≥ 1 active :class:`UserWorkRole` row here (§05 "User work role"
    Invariant). This is a write-path rule in the membership / grant
    services, not a DB check.

    Registered as workspace-scoped via ``user_work_role`` in the
    package's ``__init__``; every SELECT auto-filters on
    ``workspace_id`` through the ORM tenant filter.

    See ``docs/specs/05-employees-and-roles.md`` §"User work role",
    ``docs/specs/02-domain-model.md`` §"People, work roles,
    engagements".
    """

    __tablename__ = "user_work_role"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # ``user_id`` is a soft reference until the ``user`` FK is
    # promoted alongside the broader tenancy-join refactor; matches
    # the ``user_workspace.user_id`` rationale above.
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    work_role_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("work_role.id", ondelete="CASCADE"),
        nullable=False,
    )
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Soft reference to the future ``pay_rule`` table (cd-ea7).
    # Nullable — most workspaces inherit the engagement-level rule.
    pay_rule_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # §05 identity key: the same (user, workspace, role) can only
        # start once on a given date. A rehire on a different day
        # mints a fresh row, so the history is linear and the active
        # row is the one whose ``ended_on`` is NULL (or in the
        # future). The domain layer enforces "at most one active
        # row per (user, workspace, role)".
        UniqueConstraint(
            "user_id",
            "workspace_id",
            "work_role_id",
            "started_on",
            name="uq_user_work_role_identity",
        ),
        # "Every role this user holds in this workspace" — the
        # employees surface and the settings-cascade resolver both
        # filter here. Leading ``workspace_id`` carries the tenant
        # filter.
        Index(
            "ix_user_work_role_workspace_user",
            "workspace_id",
            "user_id",
        ),
        # "Every user who holds this role in this workspace" — the
        # candidate-pool path (cd-8luu) walks this index to assemble
        # §06's step-2 pool.
        Index(
            "ix_user_work_role_workspace_role",
            "workspace_id",
            "work_role_id",
        ),
    )


class WorkEngagement(Base):
    """Per-(user, workspace) employment relationship (§02 ``work_engagement``).

    Carries the pay pipeline that used to sit on ``employee`` in v0.
    A user who holds a ``worker`` grant on a workspace and draws
    compensation for it has **exactly one active** ``work_engagement``
    row there; a user may stack historical archived engagements in
    the same workspace and multiple active engagements across
    different workspaces.

    **Soft-ref columns (no FK yet).** ``supplier_org_id``,
    ``pay_destination_id``, and ``reimbursement_destination_id`` are
    plain :class:`str` columns rather than :class:`~sqlalchemy.ForeignKey`
    relations because the ``organization`` and ``pay_destination``
    tables do not exist yet. **cd-0ro4** (filed alongside cd-4saj)
    is the follow-up task that promotes these columns into real FKs
    once the parent tables land; the column names and nullability
    stay stable so domain callers are undisturbed. Same soft-ref
    pattern as
    :class:`~app.adapters.db.authz.models.RoleGrant.scope_property_id`
    and :attr:`UserWorkRole.pay_rule_id`.

    **CHECK: engagement_kind enum.** Matches §22 "Engagement kinds"
    — ``payroll`` / ``contractor`` / ``agency_supplied``. A bad value
    is a data bug; the CHECK rejects it before the ORM sees it.

    **CHECK: supplier pairing.** §02 records "``supplier_org_id``
    required iff ``engagement_kind = 'agency_supplied'``" — both
    directions. ``agency_supplied`` without a supplier is a
    half-wired pipeline; ``payroll`` / ``contractor`` carrying a
    supplier reference is a UX bug waiting to happen (the UI would
    surface supplier details for a direct-employment row). The CHECK
    enforces both halves.

    **Partial UNIQUE** on ``(user_id, workspace_id) WHERE
    archived_on IS NULL`` — exactly one active engagement per
    (user, workspace). Two active rows is the invariant violation;
    active + archived rows co-exist happily (the archive history is
    linear). SQLite 3.8+ supports partial indexes; the Alembic op
    uses the ``sqlite_where`` / ``postgresql_where`` kwargs so the
    same DDL lands on both backends. The ORM declaration mirrors
    the dialect-specific kwargs so ``Base.metadata.create_all``
    (the unit-test path) and Alembic agree on shape.

    **Hot-path indexes.** ``(workspace_id, user_id)`` backs the
    "what engagements does this user hold here?" view; ``(workspace_id,
    archived_on)`` backs the "who is currently engaged in this
    workspace?" scan (the manager's roster view). Leading
    ``workspace_id`` keeps the tenant filter on a local column.

    **Pay-pipeline reference.** §09 rows (``pay_rule``, ``payslip``,
    ``booking``, ``expense_claim``) reference
    ``work_engagement_id`` rather than ``user_id`` directly, so the
    same person in different workspaces accrues and bills
    independently.

    Registered as workspace-scoped in
    ``app/adapters/db/workspace/__init__.py``: every SELECT auto-
    filters on ``workspace_id`` through the ORM tenant filter.

    See ``docs/specs/02-domain-model.md`` §"work_engagement",
    ``docs/specs/05-employees-and-roles.md`` §"Work engagement",
    ``docs/specs/22-clients-and-vendors.md`` §"Engagement kinds",
    ``docs/specs/09-time-payroll-expenses.md`` §"Pay rule" /
    §"Payslip".
    """

    __tablename__ = "work_engagement"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # Soft reference to ``users.id`` until the broader tenancy-join
    # refactor lands; matches the ``user_workspace.user_id`` /
    # ``UserWorkRole.user_id`` rationale above.
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    engagement_kind: Mapped[str] = mapped_column(String, nullable=False)
    # Soft reference to the future ``organization`` table (cd-4saj
    # follow-up). Required when ``engagement_kind = 'agency_supplied'``
    # (enforced by CHECK); NULL otherwise.
    supplier_org_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft reference to the future ``pay_destination`` table (cd-4saj
    # follow-up). Default payout target for payslips / vendor
    # invoices on this engagement.
    pay_destination_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Soft reference — default target for expense reimbursements.
    # NULL falls back to :attr:`pay_destination_id` at payout time
    # (§09 "Expense claim").
    reimbursement_destination_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    started_on: Mapped[date] = mapped_column(Date, nullable=False)
    # Engagement end — archives the pay pipeline, not the user.
    # NULL = active; the partial UNIQUE below pivots on this column.
    archived_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Manager-visible notes. Empty-string default keeps the column
    # NOT NULL without forcing every seeder / API caller to thread
    # ``notes_md=""`` through.
    notes_md: Mapped[str] = mapped_column(
        String, nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "engagement_kind IN ('" + "', '".join(_ENGAGEMENT_KIND_VALUES) + "')",
            name="engagement_kind",
        ),
        # §02 "work_engagement": ``supplier_org_id`` required iff
        # ``engagement_kind = 'agency_supplied'``. Expressed as a
        # biconditional so both half-wired shapes (agency without
        # supplier, non-agency with supplier) fail at the DB.
        CheckConstraint(
            "(engagement_kind = 'agency_supplied' "
            "AND supplier_org_id IS NOT NULL) "
            "OR (engagement_kind != 'agency_supplied' "
            "AND supplier_org_id IS NULL)",
            name="supplier_org_pairing",
        ),
        # §02 "work_engagement": at most one active engagement per
        # (user, workspace). Archived rows are free to co-exist.
        # Partial index — SQLite 3.8+ and PG both honour the ``WHERE``
        # predicate; the dialect-specific kwargs pass through to the
        # DDL emitter on the matching backend.
        Index(
            "uq_work_engagement_user_workspace_active",
            "user_id",
            "workspace_id",
            unique=True,
            sqlite_where=text("archived_on IS NULL"),
            postgresql_where=text("archived_on IS NULL"),
        ),
        # "What engagements does this user hold in this workspace?"
        # Leading ``workspace_id`` carries the tenant filter.
        Index(
            "ix_work_engagement_workspace_user",
            "workspace_id",
            "user_id",
        ),
        # "Who is currently engaged in this workspace?" The manager
        # roster filters on ``archived_on IS NULL``; trailing
        # ``archived_on`` lets the planner skip archived rows.
        Index(
            "ix_work_engagement_workspace_archived",
            "workspace_id",
            "archived_on",
        ),
    )
