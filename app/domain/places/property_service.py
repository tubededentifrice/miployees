"""``property`` CRUD service with multi-belonging bootstrap.

The :class:`Property` row is the physical place the workspace
operates in — a villa, apartment, yacht, etc. This module is the
only place that inserts, updates, soft-deletes, or reads property
rows at the domain layer (§01 "Handlers are thin").

Public surface:

* **DTOs** — Pydantic v2 models for each write shape
  (:class:`PropertyCreate`, :class:`PropertyUpdate`) plus the read
  projection :class:`PropertyView` and the structured
  :class:`AddressPayload`. Shape-level validation (``kind`` enum,
  country-code length, address_json structure) runs inside the DTO
  so the same rule fires for HTTP + Python callers.
* **Service functions** — :func:`create_property`,
  :func:`update_property`, :func:`soft_delete_property`,
  :func:`list_properties`, :func:`get_property`. Every function
  takes a :class:`~app.tenancy.WorkspaceContext` as its first
  argument; workspace scoping flows through the
  :class:`~app.adapters.db.places.models.PropertyWorkspace`
  junction, never from the caller's payload.
* **Errors** — :class:`PropertyNotFound`,
  :class:`AddressCountryMismatch`. Each subclasses the stdlib
  parent the router's error map points at (``LookupError`` → 404,
  ``ValueError`` → 422). ``_resolve_country`` also raises a plain
  :class:`ValueError` when neither the body nor the existing row
  carries a country on a create (same 422 surface, different
  message).

**Multi-belonging bootstrap.** :func:`create_property` inserts one
``property`` row and one ``property_workspace`` row with
``membership_role = 'owner_workspace'`` in a single flush. The
junction row's ``label`` defaults to ``property.name`` but may be
overridden per call. Subsequent workspaces attach to the property
via the :mod:`property_workspace` invite flow (cd-hsk) — this
service never mints ``managed_workspace`` / ``observer_workspace``
rows directly.

**Workspace scoping.** Reads and writes filter via
``property_workspace`` joined on ``workspace_id = ctx.workspace_id``.
A property whose junction row does not include the caller's
workspace is invisible — ``get`` / ``update`` / ``soft_delete``
raise :class:`PropertyNotFound` (404) rather than 403, matching
the §01 "tenant surface is not enumerable" stance.

**Address-country back-fill.** On every write (create + update) the
service keeps ``property.country`` and ``address_json.country`` in
sync, per §04 "`address_json` canonical shape":

* ``address_json.country`` present → copied to ``property.country``.
* ``property.country`` present but ``address_json.country`` empty →
  back-filled into ``address_json.country``.
* Both present and mismatched → :class:`AddressCountryMismatch`
  (422); callers must reconcile deliberately.

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation
writes one :mod:`app.audit` row in the same transaction.

See ``docs/specs/04-properties-and-stays.md`` §"Property" /
§"`address_json` canonical shape" / §"Multi-belonging",
``docs/specs/02-domain-model.md`` §"property" /
§"property_workspace".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.adapters.db.places.models import Property, PropertyWorkspace
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AddressCountryMismatch",
    "AddressPayload",
    "PropertyCreate",
    "PropertyKind",
    "PropertyNotFound",
    "PropertyUpdate",
    "PropertyView",
    "create_property",
    "get_property",
    "list_properties",
    "soft_delete_property",
    "update_property",
]


# ---------------------------------------------------------------------------
# Enums (string literals — keep parity with the DB CHECK constraint)
# ---------------------------------------------------------------------------


PropertyKind = Literal["residence", "vacation", "str", "mixed"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PropertyNotFound(LookupError):
    """The requested property does not exist in the caller's workspace.

    404-equivalent. Raised by :func:`get_property`,
    :func:`update_property`, and :func:`soft_delete_property` when
    the id is unknown, soft-deleted (unless ``include_deleted`` is
    set), or not linked to the caller's workspace via
    ``property_workspace``. Matches §01 "tenant surface is not
    enumerable" — we deliberately do not distinguish
    "wrong-workspace" from "really missing".
    """


class AddressCountryMismatch(ValueError):
    """``address_json.country`` disagrees with ``property.country``.

    422-equivalent. The back-fill rule keeps the two columns in sync
    automatically when only one is set; a payload that sets both to
    different values is almost always a caller bug the service
    should surface rather than silently pick a winner.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


# Caps chosen to keep the DB + audit payload bounded without being
# restrictive in practice.
_MAX_NAME_LEN = 200
_MAX_ADDRESS_LEN = 500
_MAX_NOTES_LEN = 20_000
_MAX_ID_LEN = 64
_MAX_TIMEZONE_LEN = 64
_MAX_CURRENCY_LEN = 3
_MAX_LOCALE_LEN = 35
_MAX_TAGS = 50
_MAX_TAG_LEN = 100
# ISO-3166-1 alpha-2: exactly two characters. The migration defaults
# legacy rows to ``XX`` (placeholder) so the domain layer enforces
# "two letters" without over-validating against the official list —
# that check would go stale as ISO adds codes.
_COUNTRY_LEN = 2


class AddressPayload(BaseModel):
    """Canonical structured address — mirrors §04 "`address_json`".

    Every field nullable (for partial addresses the manager hasn't
    filled in yet) except ``country``, which is the anchor the
    back-fill rule keys on. A payload with ``country`` set but
    nothing else is legal — the DB stores the JSON verbatim.

    The country code is narrowed to two upper-case letters here so
    the back-fill logic never has to case-normalise downstream.
    """

    model_config = ConfigDict(extra="forbid")

    line1: str | None = Field(default=None, max_length=_MAX_ADDRESS_LEN)
    line2: str | None = Field(default=None, max_length=_MAX_ADDRESS_LEN)
    city: str | None = Field(default=None, max_length=_MAX_ADDRESS_LEN)
    state_province: str | None = Field(default=None, max_length=_MAX_ADDRESS_LEN)
    postal_code: str | None = Field(default=None, max_length=_MAX_ADDRESS_LEN)
    country: str | None = Field(
        default=None, min_length=_COUNTRY_LEN, max_length=_COUNTRY_LEN
    )

    @model_validator(mode="after")
    def _normalise_country(self) -> AddressPayload:
        """Upper-case ``country`` so the back-fill comparison is stable."""
        if self.country is not None:
            upper = self.country.upper()
            if upper != self.country:
                # Pydantic frozen-model bypass: re-bind the attribute
                # via ``object.__setattr__`` since the model is not
                # frozen but the default assignment path skips the
                # validator on re-entry.
                object.__setattr__(self, "country", upper)
            if not self.country.isalpha():
                raise ValueError(
                    f"country must be two ASCII letters (ISO-3166-1 alpha-2); "
                    f"got {self.country!r}"
                )
        return self


class _PropertyBody(BaseModel):
    """Shared mutable body of the create + update DTOs.

    Held as a private base so the ``model_validator`` that enforces
    the ``address_json.country`` ↔ ``country`` relation runs on
    both. Pydantic v2's ``model_validator`` decorates the parent
    class once and every subclass inherits the rule.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=_MAX_NAME_LEN)
    kind: PropertyKind = "residence"
    address: str = Field(..., min_length=1, max_length=_MAX_ADDRESS_LEN)
    address_json: AddressPayload = Field(default_factory=AddressPayload)
    # ISO-3166-1 alpha-2. Two letters, upper-cased by the validator
    # for stable comparison inside the back-fill.
    country: str | None = Field(
        default=None, min_length=_COUNTRY_LEN, max_length=_COUNTRY_LEN
    )
    locale: str | None = Field(default=None, max_length=_MAX_LOCALE_LEN)
    default_currency: str | None = Field(default=None, max_length=_MAX_CURRENCY_LEN)
    timezone: str = Field(..., min_length=1, max_length=_MAX_TIMEZONE_LEN)
    lat: float | None = None
    lon: float | None = None
    client_org_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    owner_user_id: str | None = Field(default=None, max_length=_MAX_ID_LEN)
    tags_json: list[str] = Field(default_factory=list, max_length=_MAX_TAGS)
    welcome_defaults_json: dict[str, Any] = Field(default_factory=dict)
    property_notes_md: str = Field(default="", max_length=_MAX_NOTES_LEN)

    @model_validator(mode="after")
    def _normalise_and_validate(self) -> _PropertyBody:
        """Normalise ``country`` / ``default_currency`` and reject bad tags.

        The ``address_json.country`` ↔ ``country`` mismatch check
        runs in :func:`_resolve_country` at service time — it depends
        on the row's existing state for the update path, so the DTO
        can't encode it.
        """
        if self.country is not None:
            upper = self.country.upper()
            if upper != self.country:
                object.__setattr__(self, "country", upper)
            if not self.country.isalpha():
                raise ValueError(
                    f"country must be two ASCII letters (ISO-3166-1 alpha-2); "
                    f"got {self.country!r}"
                )
        if self.default_currency is not None:
            upper = self.default_currency.upper()
            if upper != self.default_currency:
                object.__setattr__(self, "default_currency", upper)
            if len(self.default_currency) != _MAX_CURRENCY_LEN:
                raise ValueError(
                    f"default_currency must be ISO-4217 (3 letters); "
                    f"got {self.default_currency!r}"
                )
            if not self.default_currency.isalpha():
                raise ValueError(
                    f"default_currency must be three ASCII letters; "
                    f"got {self.default_currency!r}"
                )
        # Defend the tag list shape: no duplicates, no blanks, no
        # runaway strings (caps at _MAX_TAG_LEN).
        if len(set(self.tags_json)) != len(self.tags_json):
            raise ValueError("tags_json must not contain duplicates")
        for tag in self.tags_json:
            if not tag or not tag.strip():
                raise ValueError("tags_json must not contain blank entries")
            if len(tag) > _MAX_TAG_LEN:
                raise ValueError(
                    f"tags_json entry exceeds {_MAX_TAG_LEN} characters: {tag!r}"
                )
        if not self.name.strip():
            raise ValueError("name must be a non-blank string")
        if not self.address.strip():
            raise ValueError("address must be a non-blank string")
        if not self.timezone.strip():
            raise ValueError("timezone must be a non-blank string")
        # Reject bogus timezones at the DTO boundary. §04 requires an
        # IANA zone (``Europe/Paris``); a bad value would otherwise
        # survive the write and silently coerce to UTC downstream at
        # task-generation time (see
        # ``app.domain.tasks.oneoff._resolve_property_zone``) — far
        # from the caller. :class:`zoneinfo.ZoneInfo` is the same
        # lookup the worker uses, so parity between write + read is
        # guaranteed.
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"timezone must be a valid IANA zone (e.g. 'Europe/Paris'); "
                f"got {self.timezone!r}"
            ) from exc
        return self


class PropertyCreate(_PropertyBody):
    """Request body for property create.

    The caller-supplied junction ``label`` defaults to the property's
    ``name`` when omitted — the mock UI surfaces the label per
    property card (§04 "Multi-belonging") so workspaces that host
    the same physical villa under different internal names can keep
    their own wording without touching the canonical row.
    """

    label: str | None = Field(default=None, max_length=_MAX_NAME_LEN)


class PropertyUpdate(_PropertyBody):
    """Request body for property update.

    v1 treats update as a full replacement of the mutable body —
    the spec does not (yet) call for per-field PATCH. Callers send
    the full desired state; the service diffs against the current
    row, writes through, and records the before/after diff in the
    audit log.
    """


@dataclass(frozen=True, slots=True)
class PropertyView:
    """Immutable read projection of a ``property`` row.

    Returned by every service read + write. A frozen / slotted
    dataclass (not a Pydantic model) because reads carry audit-
    sensitive fields (``deleted_at``, ``created_at``) that are
    managed by the service, not the caller's payload. Keeping the
    read shape separate from the write shape removes the accidental
    "echo the DB timestamp back to the client, accept it on
    round-trip" class of bug.
    """

    id: str
    name: str
    kind: PropertyKind
    address: str
    address_json: dict[str, Any]
    country: str
    locale: str | None
    default_currency: str | None
    timezone: str
    lat: float | None
    lon: float | None
    client_org_id: str | None
    owner_user_id: str | None
    tags_json: tuple[str, ...]
    welcome_defaults_json: dict[str, Any]
    property_notes_md: str
    created_at: datetime
    updated_at: datetime | None
    deleted_at: datetime | None


# ---------------------------------------------------------------------------
# Helpers — country back-fill
# ---------------------------------------------------------------------------


def _resolve_country(
    *,
    body_country: str | None,
    body_address_country: str | None,
    existing_country: str | None = None,
    existing_address_country: str | None = None,
) -> tuple[str, str]:
    """Resolve ``(property.country, address_json.country)`` from a write.

    The §04 back-fill rule:

    * both set, equal → keep both;
    * both set, not equal → :class:`AddressCountryMismatch`;
    * only ``address_json.country`` set → copy to ``property.country``;
    * only ``property.country`` set → back-fill ``address_json.country``;
    * neither set on the body → fall back to the existing row (update
      path) or raise :class:`ValueError` (create path — the migration
      default ``XX`` is only a legacy-row placeholder, not a legal
      write).

    Returns both columns with matching upper-case country codes.
    """
    # Normalise "empty string" to "absent" so a caller who sends the
    # field explicitly blank behaves the same as one who omits it.
    body = _blank_to_none(body_country)
    body_inner = _blank_to_none(body_address_country)
    existing = _blank_to_none(existing_country)
    existing_inner = _blank_to_none(existing_address_country)

    if body is not None and body_inner is not None:
        if body.upper() != body_inner.upper():
            raise AddressCountryMismatch(
                f"property.country={body!r} disagrees with "
                f"address_json.country={body_inner!r}; set one or "
                "make them equal"
            )
        resolved = body.upper()
        return resolved, resolved
    if body is not None:
        resolved = body.upper()
        return resolved, resolved
    if body_inner is not None:
        resolved = body_inner.upper()
        return resolved, resolved
    # Body silent — fall back to the existing row if present.
    if existing is not None:
        resolved = existing.upper()
        return resolved, resolved
    if existing_inner is not None:
        resolved = existing_inner.upper()
        return resolved, resolved
    raise ValueError(
        "country is required — set property.country or address_json.country"
    )


def _blank_to_none(value: str | None) -> str | None:
    """Treat an empty / whitespace-only string as ``None``.

    The back-fill rule has to distinguish "absent" from "explicitly
    blank"; pydantic's ``min_length=2`` on the country fields already
    rejects a blank on ingress, but the existing-row columns can be
    blank on a pre-migration backfill row, so we normalise here
    instead of trusting the caller.
    """
    if value is None:
        return None
    if not value.strip():
        return None
    return value


# ---------------------------------------------------------------------------
# Row ↔ view projection
# ---------------------------------------------------------------------------


def _row_to_view(row: Property) -> PropertyView:
    """Project a loaded :class:`Property` row into a read view."""
    return PropertyView(
        id=row.id,
        # ``name`` is nullable at the DB layer for the migration's
        # cheap backfill; the service always writes a non-blank value
        # on insert + update. A ``NULL`` here is a pre-migration row
        # — fall back to ``address`` so the UI still has something.
        name=row.name if row.name is not None else row.address,
        kind=_narrow_kind(row.kind),
        address=row.address,
        address_json=dict(row.address_json or {}),
        country=row.country,
        locale=row.locale,
        default_currency=row.default_currency,
        timezone=row.timezone,
        lat=row.lat,
        lon=row.lon,
        client_org_id=row.client_org_id,
        owner_user_id=row.owner_user_id,
        tags_json=tuple(row.tags_json or []),
        welcome_defaults_json=dict(row.welcome_defaults_json or {}),
        property_notes_md=row.property_notes_md or "",
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    )


def _narrow_kind(value: str) -> PropertyKind:
    """Narrow a loaded DB string to the :data:`PropertyKind` literal.

    The DB CHECK gate already rejects anything else; this helper
    exists purely to satisfy mypy's strict-Literal reading without
    a ``cast``. An unexpected value is loud rather than silent —
    a schema drift is worth a stack trace, not a default.
    """
    if value == "residence":
        return "residence"
    if value == "vacation":
        return "vacation"
    if value == "str":
        return "str"
    if value == "mixed":
        return "mixed"
    raise ValueError(f"unknown property.kind {value!r} on loaded row")


def _view_to_diff_dict(view: PropertyView) -> dict[str, Any]:
    """Flatten a :class:`PropertyView` into a JSON-safe audit payload."""
    return {
        "id": view.id,
        "name": view.name,
        "kind": view.kind,
        "address": view.address,
        "address_json": dict(view.address_json),
        "country": view.country,
        "locale": view.locale,
        "default_currency": view.default_currency,
        "timezone": view.timezone,
        "lat": view.lat,
        "lon": view.lon,
        "client_org_id": view.client_org_id,
        "owner_user_id": view.owner_user_id,
        "tags_json": list(view.tags_json),
        "welcome_defaults_json": dict(view.welcome_defaults_json),
        "property_notes_md": view.property_notes_md,
        "created_at": view.created_at.isoformat(),
        "updated_at": (
            view.updated_at.isoformat() if view.updated_at is not None else None
        ),
        "deleted_at": (
            view.deleted_at.isoformat() if view.deleted_at is not None else None
        ),
    }


# ---------------------------------------------------------------------------
# Row loader — workspace scoped via property_workspace join
# ---------------------------------------------------------------------------


def _load_row(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    include_deleted: bool = False,
) -> Property:
    """Load ``property_id`` scoped to the caller's workspace.

    The row is reached via :class:`PropertyWorkspace` joined on
    ``workspace_id = ctx.workspace_id`` — a property whose junction
    row does not include the caller's workspace is invisible.
    Soft-deleted rows are excluded unless ``include_deleted`` is set.

    The ORM tenant filter already constrains queries against
    ``property_workspace`` to ``ctx.workspace_id``; the explicit
    ``.where(PropertyWorkspace.workspace_id == ...)`` below is
    defence-in-depth against a misconfigured context — matches the
    convention in :mod:`app.domain.identity.role_grants` and
    :mod:`app.domain.tasks.templates`.
    """
    stmt = (
        select(Property)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(
            Property.id == property_id,
            PropertyWorkspace.workspace_id == ctx.workspace_id,
        )
    )
    if not include_deleted:
        stmt = stmt.where(Property.deleted_at.is_(None))
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise PropertyNotFound(property_id)
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_property(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    include_deleted: bool = False,
) -> PropertyView:
    """Return the property identified by ``property_id``.

    Raises :class:`PropertyNotFound` if the id is unknown, soft-
    deleted (unless ``include_deleted`` is set), or not linked to
    the caller's workspace.
    """
    row = _load_row(
        session, ctx, property_id=property_id, include_deleted=include_deleted
    )
    return _row_to_view(row)


def list_properties(
    session: Session,
    ctx: WorkspaceContext,
    *,
    q: str | None = None,
    kind: PropertyKind | None = None,
    deleted: bool = False,
) -> Sequence[PropertyView]:
    """Return every property linked to the caller's workspace.

    Ordered by ``created_at`` ascending with ``id`` as a stable
    tiebreaker inside the same millisecond. Filter semantics:

    * ``q`` — case-insensitive substring match against ``name`` and
      ``address``.
    * ``kind`` — strict equality on the ``kind`` column.
    * ``deleted`` — ``False`` (the default) returns only live rows;
      ``True`` returns only soft-deleted rows. No "both" mode —
      mixing active + retired rows in one list screen is an
      anti-pattern.
    """
    stmt = (
        select(Property)
        .join(PropertyWorkspace, PropertyWorkspace.property_id == Property.id)
        .where(PropertyWorkspace.workspace_id == ctx.workspace_id)
    )
    if deleted:
        stmt = stmt.where(Property.deleted_at.is_not(None))
    else:
        stmt = stmt.where(Property.deleted_at.is_(None))
    if kind is not None:
        stmt = stmt.where(Property.kind == kind)
    if q is not None and q.strip():
        # Portable case-insensitive match via SQL ``LOWER``. The
        # needle is bounded by pydantic caps on the DTO side; the
        # raw ``q`` here is the router-level query param that has
        # no such cap, so we trim + normalise before interpolation.
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Property.name).like(needle),
                func.lower(Property.address).like(needle),
            )
        )
    stmt = stmt.order_by(Property.created_at.asc(), Property.id.asc())
    rows = session.scalars(stmt).all()
    return [_row_to_view(row) for row in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_property(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: PropertyCreate,
    clock: Clock | None = None,
) -> PropertyView:
    """Insert a fresh ``property`` + bootstrap ``property_workspace``.

    Single flush — the property row and its ``owner_workspace``
    junction row land atomically. ``label`` defaults to
    ``body.name`` when the caller doesn't override.

    One ``property.create`` audit row is written; the ``after``
    diff carries the resolved view.

    Returns the full :class:`PropertyView` so the router can echo
    it back to the client without a second SELECT.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()

    # Resolve the country before the row is built — a mismatch is a
    # 422 that must fire *before* we flush anything.
    country, address_country = _resolve_country(
        body_country=body.country,
        body_address_country=body.address_json.country,
    )

    row = Property(
        id=new_ulid(),
        name=body.name,
        kind=body.kind,
        address=body.address,
        address_json=_address_with_country(body.address_json, address_country),
        country=country,
        locale=body.locale,
        default_currency=body.default_currency,
        timezone=body.timezone,
        lat=body.lat,
        lon=body.lon,
        client_org_id=body.client_org_id,
        owner_user_id=body.owner_user_id,
        tags_json=list(body.tags_json),
        welcome_defaults_json=dict(body.welcome_defaults_json),
        property_notes_md=body.property_notes_md,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )
    session.add(row)
    # Bootstrap the ``owner_workspace`` junction row in the same
    # flush (§04 "Multi-belonging"). The composite PK on
    # ``(property_id, workspace_id)`` makes this safe against a
    # concurrent attach from a sibling service.
    label = body.label if body.label is not None else body.name
    session.add(
        PropertyWorkspace(
            property_id=row.id,
            workspace_id=ctx.workspace_id,
            label=label,
            membership_role="owner_workspace",
            created_at=now,
        )
    )
    session.flush()

    view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="property",
        entity_id=row.id,
        action="create",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    return view


def update_property(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    body: PropertyUpdate,
    clock: Clock | None = None,
) -> PropertyView:
    """Replace the mutable body of ``property_id``.

    Workspace-scoped: a property id not linked to
    ``ctx.workspace_id`` raises :class:`PropertyNotFound` (404),
    matching the "wrong-workspace collapses to not-found" stance.

    Records one audit row with the full before/after diff so
    operators can reconstruct the change.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, property_id=property_id)
    before = _row_to_view(row)

    country, address_country = _resolve_country(
        body_country=body.country,
        body_address_country=body.address_json.country,
        existing_country=row.country,
        existing_address_country=(row.address_json or {}).get("country"),
    )

    row.name = body.name
    row.kind = body.kind
    row.address = body.address
    row.address_json = _address_with_country(body.address_json, address_country)
    row.country = country
    row.locale = body.locale
    row.default_currency = body.default_currency
    row.timezone = body.timezone
    row.lat = body.lat
    row.lon = body.lon
    row.client_org_id = body.client_org_id
    row.owner_user_id = body.owner_user_id
    row.tags_json = list(body.tags_json)
    row.welcome_defaults_json = dict(body.welcome_defaults_json)
    row.property_notes_md = body.property_notes_md
    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="property",
        entity_id=row.id,
        action="update",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


def soft_delete_property(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str,
    clock: Clock | None = None,
) -> PropertyView:
    """Soft-delete ``property_id`` by stamping ``deleted_at``.

    Workspace-scoped — a property not linked to ``ctx.workspace_id``
    raises :class:`PropertyNotFound`.

    Soft-delete is **row-local**: the property is hidden from the
    default list for every linked workspace, but the
    ``property_workspace`` junction rows survive. A follow-up
    (cd-hsk property↔workspace service) handles the inverse
    "retire the junction without retiring the property" flow.

    Records one ``property.delete`` audit row with the before /
    after diff. Returns the post-delete view so the router can
    echo the ``deleted_at`` timestamp back to the client.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, property_id=property_id)
    before = _row_to_view(row)
    row.deleted_at = now
    row.updated_at = now
    session.flush()
    after = _row_to_view(row)

    write_audit(
        session,
        ctx,
        entity_kind="property",
        entity_id=row.id,
        action="delete",
        diff={
            "before": _view_to_diff_dict(before),
            "after": _view_to_diff_dict(after),
        },
        clock=resolved_clock,
    )
    return after


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _address_with_country(address: AddressPayload, country: str) -> dict[str, Any]:
    """Return ``address`` as a dict with ``country`` pinned to ``country``.

    The back-fill normalises the column pair; this helper writes
    the normalised value back into the JSON payload so the row and
    the blob stay in sync. Empty fields are serialised as ``None``
    rather than dropped so the stored JSON carries the full canonical
    shape (§04 "`address_json` canonical shape").
    """
    return {
        "line1": address.line1,
        "line2": address.line2,
        "city": address.city,
        "state_province": address.state_province,
        "postal_code": address.postal_code,
        "country": country,
    }
