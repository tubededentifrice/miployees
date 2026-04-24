"""Workspace + UserWorkspace SQLAlchemy models.

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

See ``docs/specs/02-domain-model.md`` §"workspaces",
§"user_workspace"; ``docs/specs/01-architecture.md`` §"Workspace
addressing" and §"Tenant filter enforcement".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = ["UserWorkspace", "Workspace"]


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
    # a canonical home; cd-n6p is the task that wires owner-facing
    # writes for the broader setting catalog. Defaulted to ``{}`` so
    # callers never need to coalesce when reading a missing key.
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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
