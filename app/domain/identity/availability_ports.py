"""Identity context — repository + capability seams for availability rows (cd-r5j2).

Defines the seams :mod:`app.domain.identity.user_availability_overrides`
(and, transitively cd-2upg, :mod:`app.domain.identity.user_leaves`) use
to read and write availability rows in
:mod:`app.adapters.db.availability.models` plus to enforce action-catalog
capabilities — without importing SQLAlchemy model classes or
:mod:`app.authz`.

Spec: ``docs/specs/01-architecture.md`` §"Boundary rules" rule 4 —
each context defines its own repository port in its public surface
(``app/domain/<context>/ports.py`` — split into a sibling
``availability_ports.py`` here so the existing ``ports.py`` stays
focused on the authz primitives it already declares
(:class:`PermissionGroupRepository` / :class:`RoleGrantRepository`)).

Two seams live here:

* :class:`UserAvailabilityOverrideRepository` — CRUD on the
  ``user_availability_override`` table plus the per-user weekly
  pattern lookup the §06 hybrid-approval calculator needs. Returns
  immutable :class:`UserAvailabilityOverrideRow` /
  :class:`UserWeeklyAvailabilityRow` projections so the domain never
  sees an ORM row.
* :class:`CapabilityChecker` — workspace-scoped authz probe for the
  `availability_overrides.*` (and, with cd-2upg, ``leaves.*``) action
  keys. Wraps :func:`app.authz.require` at the adapter layer so the
  domain service does not transitively pull
  :mod:`app.adapters.db.authz.models` via :mod:`app.authz.membership`
  / :mod:`app.authz.owners` (the cd-7qxh stopgap rationale).

**Why a separate repo per table (not one shared
``AvailabilityRepository``).** ``user_availability_override`` and
``user_leave`` share an adapter package (both live in
:mod:`app.adapters.db.availability.models`) but have distinct
state machines (override carries ``approval_required`` + the weekly-
pattern lookup; leave carries date-range + category filters). Per-
table repos keep each Protocol surface focused — the override repo
exposes the weekly-pattern lookup the approval calculator needs, and
the future leave repo (cd-2upg) keeps its own filter shape. Both SA
concretions live side-by-side in
:mod:`app.adapters.db.availability.repositories` so the file count
does not balloon.

**Why a single :class:`CapabilityChecker` for both modules.** The
authz check shape is identical across availability services
(workspace-scope ``require`` + non-raising ``has`` probe); two
identical Protocols would duplicate the seam contract. The checker
is constructed by the router with the same ``(session, ctx)`` pair
that drives the rest of the service call.

The repo-shaped value objects (:class:`UserAvailabilityOverrideRow`,
:class:`UserWeeklyAvailabilityRow`) mirror the domain's matching
read projections (the
``UserAvailabilityOverrideView`` returned by the override service
plus the calculator's ``weekly`` input). They live on the seam so
the SA adapter has a domain-owned shape to project ORM rows into
without importing the service module that produces the view (which
would create a circular dependency between
:mod:`app.domain.identity.user_availability_overrides` and this
module).

Protocols are deliberately **not** ``runtime_checkable``:
structural compatibility is checked statically by mypy. Runtime
``isinstance`` against these protocols would mask typos and invite
duck-typing shortcuts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal, Protocol

from sqlalchemy.orm import Session

__all__ = [
    "CapabilityChecker",
    "SeamPermissionDenied",
    "UserAvailabilityOverrideRepository",
    "UserAvailabilityOverrideRow",
    "UserWeeklyAvailabilityRow",
]


# ---------------------------------------------------------------------------
# Seam exceptions
# ---------------------------------------------------------------------------


class SeamPermissionDenied(Exception):
    """Raised by :meth:`CapabilityChecker.require` for a denied capability.

    A seam-level analogue of :class:`app.authz.PermissionDenied` so the
    domain service can ``except`` on this without importing
    :mod:`app.authz` (the transitive walk via
    :mod:`app.authz.membership` / :mod:`app.authz.owners` is what the
    cd-7qxh stopgap was tagged to bypass). The SA-backed checker in
    :mod:`app.adapters.db.availability.repositories` translates the
    underlying authz exception into this seam-level one before
    raising.

    Domain services re-raise this as their own context-specific
    ``PermissionDenied`` shape (e.g.
    :class:`~app.domain.identity.user_availability_overrides.UserAvailabilityOverridePermissionDenied`)
    so the router's error map stays narrow — one domain exception
    type per 403 envelope.
    """


# ---------------------------------------------------------------------------
# Row shapes (value objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserAvailabilityOverrideRow:
    """Immutable projection of a ``user_availability_override`` row.

    Mirrors the shape of
    :class:`app.domain.identity.user_availability_overrides.UserAvailabilityOverrideView`;
    declared here so the Protocol surface does not depend on the
    service module (which itself imports this seam).
    """

    id: str
    workspace_id: str
    user_id: str
    date: date
    available: bool
    starts_local: time | None
    ends_local: time | None
    reason: str | None
    approval_required: bool
    approved_at: datetime | None
    approved_by: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class UserWeeklyAvailabilityRow:
    """Immutable projection of a ``user_weekly_availability`` row.

    The §06 hybrid-approval calculator
    (:func:`~app.domain.identity.user_availability_overrides._compute_approval_required`)
    consumes this shape — it only needs the hours pair to decide
    whether the override widens, narrows, adds or removes the worker's
    coverage for the date's weekday. ``id`` / ``workspace_id`` /
    ``user_id`` / ``weekday`` / ``updated_at`` round out the shape so
    the seam can grow more readers later (e.g. an "edit your weekly
    pattern" surface) without re-shaping the value object.
    """

    id: str
    workspace_id: str
    user_id: str
    weekday: int
    starts_local: time | None
    ends_local: time | None
    updated_at: datetime


# ---------------------------------------------------------------------------
# UserAvailabilityOverrideRepository
# ---------------------------------------------------------------------------


class UserAvailabilityOverrideRepository(Protocol):
    """Read + write seam for ``user_availability_override`` and the weekly lookup.

    The repo carries an open SQLAlchemy ``Session`` so domain callers
    that also need :func:`app.audit.write_audit` (which still takes a
    concrete ``Session`` today) can thread the same UoW without
    holding a second seam. The accessor drops once the audit writer
    gains its own Protocol port.

    Every method honours the workspace-scoping invariant: the SA
    concretion always pins reads + writes to the ``workspace_id``
    passed by the caller, mirroring the ORM tenant filter as
    defence-in-depth (a misconfigured filter must fail loud).

    The repo never commits outside what the underlying statements
    require — the caller's UoW owns the transaction boundary (§01
    "Key runtime invariants" #3). Mutating methods flush so the
    caller's next read (and the audit writer's FK reference to
    ``entity_id``) sees the new row.
    """

    @property
    def session(self) -> Session:
        """Return the underlying SQLAlchemy session.

        Exposed for callers that need to thread the same UoW through
        :func:`app.audit.write_audit` (which still takes a concrete
        ``Session`` today). Drops when the audit writer gains its
        own Protocol port.
        """
        ...

    # -- Reads -----------------------------------------------------------

    def get(
        self,
        *,
        workspace_id: str,
        override_id: str,
        include_deleted: bool = False,
    ) -> UserAvailabilityOverrideRow | None:
        """Return the row or ``None`` when invisible to the caller.

        Defence-in-depth pins the lookup to ``workspace_id`` even
        though the ORM tenant filter already narrows the read; a
        misconfigured filter must fail loud, not silently.
        ``include_deleted=True`` skips the ``deleted_at IS NULL``
        predicate — used by the soft-delete + tombstone-aware paths.
        """
        ...

    def list(
        self,
        *,
        workspace_id: str,
        limit: int,
        after_id: str | None = None,
        user_id: str | None = None,
        status: Literal["approved", "pending"] | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> Sequence[UserAvailabilityOverrideRow]:
        """Return up to ``limit + 1`` live rows for the workspace, ``id ASC``.

        The caller (a cursor-paginated router) asks for ``limit + 1``
        so the :func:`~app.api.pagination.paginate` helper can compute
        ``has_more`` without a second query. Tombstones (``deleted_at
        IS NOT NULL``) are always filtered out — the live-list path
        is the only consumer of this method.

        ``status='approved'`` narrows to ``approved_at IS NOT NULL``;
        ``status='pending'`` narrows to ``approved_at IS NULL``.
        ``from_date`` / ``to_date`` are inclusive bounds on ``date``.
        """
        ...

    def find_weekly_pattern(
        self,
        *,
        workspace_id: str,
        user_id: str,
        weekday: int,
    ) -> UserWeeklyAvailabilityRow | None:
        """Return the weekly pattern row for ``(user, weekday)`` or ``None``.

        A user with no row at all for that weekday is treated as "off"
        by the §06 hybrid-approval calculator
        (:func:`~app.domain.identity.user_availability_overrides._compute_approval_required`)
        — same surface as a row with both ``starts_local`` and
        ``ends_local`` null. Centralising the lookup here keeps the
        approval calculator free of SQLAlchemy concerns.
        """
        ...

    # -- Writes ----------------------------------------------------------

    def insert(
        self,
        *,
        override_id: str,
        workspace_id: str,
        user_id: str,
        date: date,
        available: bool,
        starts_local: time | None,
        ends_local: time | None,
        reason: str | None,
        approval_required: bool,
        approved_at: datetime | None,
        approved_by: str | None,
        now: datetime,
    ) -> UserAvailabilityOverrideRow:
        """Insert a new ``user_availability_override`` row and return its projection.

        Flushes so the caller's next read (and the audit writer's
        FK reference to ``entity_id``) sees the new row. ``now`` is
        the caller's clock-resolved insertion time — used for both
        ``created_at`` and ``updated_at``.
        """
        ...

    def update_fields(
        self,
        *,
        workspace_id: str,
        override_id: str,
        available: bool | None = None,
        starts_local: time | None = None,
        ends_local: time | None = None,
        reason: str | None = None,
        clear_starts_local: bool = False,
        clear_ends_local: bool = False,
        clear_reason: bool = False,
        now: datetime,
    ) -> UserAvailabilityOverrideRow:
        """Apply the explicit-sparse partial update and return the refreshed projection.

        ``available`` ``None`` is treated as "unchanged" (the column is
        non-nullable so there is no semantic "clear" for it); a sent
        ``True``/``False`` lands on the row. The ``starts_local`` /
        ``ends_local`` / ``reason`` columns are nullable; the explicit
        ``clear_*`` flags distinguish "send JSON null to clear" from
        "field omitted from PATCH" (matching the service's
        ``model_fields_set`` walk). Stamps ``updated_at = now`` and
        flushes when something actually changed.

        Caller has already confirmed the row exists, applied the
        BOTH-OR-NEITHER + ``ends > starts`` invariants, and filtered
        zero-delta calls — this method is a pure SA write.
        """
        ...

    def stamp_approved(
        self,
        *,
        workspace_id: str,
        override_id: str,
        approved_by: str,
        now: datetime,
    ) -> UserAvailabilityOverrideRow:
        """Stamp ``approved_at`` + ``approved_by`` + ``updated_at`` and flush.

        Caller is responsible for the state-machine guard (the row
        must be pending). Returns the refreshed projection.
        """
        ...

    def soft_delete(
        self,
        *,
        workspace_id: str,
        override_id: str,
        reason: str | None = None,
        now: datetime,
    ) -> UserAvailabilityOverrideRow:
        """Stamp ``deleted_at`` + ``updated_at`` and return the tombstoned projection.

        ``reason`` is set to the post-rejection text when the caller
        is :func:`~app.domain.identity.user_availability_overrides.reject_override`
        (which folds the rejection rationale into the existing
        ``reason``); set to ``None`` for the canonical
        :func:`~app.domain.identity.user_availability_overrides.delete_override`
        withdraw path which leaves ``reason`` intact. Caller has
        already confirmed the row exists via :meth:`get`.

        The SA concretion only writes ``reason`` when the caller
        passes a non-``None`` value — this matches the service-layer
        "preserve the worker's original explanation" rule.
        """
        ...


# ---------------------------------------------------------------------------
# CapabilityChecker
# ---------------------------------------------------------------------------


class CapabilityChecker(Protocol):
    """Workspace-scoped action-catalog probe used by the availability services.

    Wraps the canonical :func:`app.authz.require` enforcement so
    callers don't transitively pull :mod:`app.adapters.db.authz.models`
    via the authz module's membership / owners walk (the cd-7qxh
    stopgap rationale). The SA-backed concretion lives in
    :mod:`app.adapters.db.availability.repositories`; tests substitute
    fakes.

    Both methods are pinned at construction time to a single
    ``(session, workspace_id, actor)`` triple — the underlying
    :func:`require` call always uses ``scope_kind='workspace'`` and
    ``scope_id=ctx.workspace_id`` because every action key the
    availability services check is workspace-scoped. A future caller
    that needs property-scope checks can extend this Protocol; today's
    callers don't.

    A misconfigured action catalog (unknown key, invalid scope) is a
    server-side bug, not a denial — the SA concretion lets those
    errors propagate as :class:`RuntimeError` rather than
    :class:`SeamPermissionDenied` so the router surfaces 500, not 403.
    """

    def require(self, action_key: str) -> None:
        """Enforce the named capability or raise :class:`SeamPermissionDenied`.

        Callers re-raise the seam exception as their own context-
        specific 403 type so the router's error map stays narrow.
        """
        ...

    def has(self, action_key: str) -> bool:
        """Return ``True`` iff the caller holds the named capability.

        Non-raising probe used by the auto-approve trigger and other
        "is this caller a manager?" branches inside the service. A
        catalog misconfiguration still raises :class:`RuntimeError`
        (the bool surface only reflects denial-vs-grant; bugs are
        bugs).
        """
        ...
