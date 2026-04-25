"""IcalFeed / Reservation / StayBundle SQLAlchemy models.

v1 slice per cd-1b2 — sufficient for seeding the external-calendar →
reservation → turnover-bundle chain that drives property turnover.
cd-ewd7 extends ``ical_feed`` to the full §04 shape (``unit_id``,
``poll_cadence``, ``last_error``, and the widened ``provider``
enum including ``gcal``). Richer ``reservation`` columns
(``nightly_rate_cents``, ``guest_kind``, ``unit_id``) and the
``stay_bundle_state`` enum still land with later domain-layer
follow-ups without breaking this migration's public write contract.

Every table carries a ``workspace_id`` column and is registered as
workspace-scoped via the package's ``__init__``. FK hygiene mirrors
the rest of the app:

* Cascading parents (``property → ical_feed``, ``property →
  reservation``, ``reservation → stay_bundle``) use ``CASCADE`` so
  sweeping a parent sweeps its descendants.
* ``reservation.ical_feed_id`` uses ``SET NULL`` — a reservation
  captured from iCal outlives the feed's deletion (think: agency
  swaps provider, but the booking remains real work).
* ``ical_feed.unit_id`` uses ``SET NULL`` — feeds outlive unit churn
  (renaming / merging units shouldn't delete the poller config).

See ``docs/specs/02-domain-model.md`` §"reservation", §"ical_feed",
§"stay_bundle", and ``docs/specs/04-properties-and-stays.md``
§"Stay (reservation)" / §"iCal feed".
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

# Cross-package FK targets — see :mod:`app.adapters.db` package
# docstring for the load-order contract. ``property.id`` / ``unit.id``
# / ``workspace.id`` FKs below resolve against ``Base.metadata`` only
# if the target packages have been imported, so we register them here
# as a side effect.
from app.adapters.db.places import models as _places_models  # noqa: F401
from app.adapters.db.workspace import models as _workspace_models  # noqa: F401

__all__ = ["DEFAULT_POLL_CADENCE", "IcalFeed", "Reservation", "StayBundle"]


# Allowed ``ical_feed.provider`` values, enforced by a CHECK
# constraint. Matches §04 "Supported providers": the four
# hosted-platform slugs (``airbnb | vrbo | booking | gcal``) plus
# the ``generic`` fallback. cd-ewd7 widened the set from the v1
# ``airbnb | vrbo | booking | custom`` shape so the detector's
# output can land in the DB directly (no ``gcal/generic → custom``
# collapse at the service boundary). ``custom`` stays in the
# accept set so v1-era rows (written before cd-ewd7) remain
# readable — the service does not mint new ``custom`` rows; it
# stores the detector's ``generic`` result verbatim.
_PROVIDER_VALUES: tuple[str, ...] = (
    "airbnb",
    "vrbo",
    "booking",
    "gcal",
    "generic",
    "custom",
)

# Allowed ``reservation.status`` values — the v1 lifecycle. The
# fuller §04 machine (``tentative | confirmed | in_house |
# checked_out | cancelled``) maps onto this simpler set at the
# domain boundary and lands with cd-1ai.
_STATUS_VALUES: tuple[str, ...] = (
    "scheduled",
    "checked_in",
    "completed",
    "cancelled",
)

# Allowed ``reservation.source`` values — the v1 ingestion channels.
# The §02 ``stay_source`` enum is a superset; the simpler three-value
# set here lands the shape the current API needs.
_SOURCE_VALUES: tuple[str, ...] = ("ical", "manual", "api")

# Allowed ``stay_bundle.kind`` values, enforced by a CHECK
# constraint. Matches §04 "Stay task bundles" — three canonical
# rule types the scheduler materialises against a reservation.
_BUNDLE_KIND_VALUES: tuple[str, ...] = ("turnover", "welcome", "deep_clean")


def _in_clause(values: tuple[str, ...]) -> str:
    """Render a ``col IN ('a', 'b', …)`` CHECK body fragment.

    Mirrors the helper in sibling ``tasks`` / ``places`` modules so
    the enum CHECK constraints below stay readable.
    """
    return "'" + "', '".join(values) + "'"


#: Default per-feed polling cron. §04 "iCal feed" pins this to
#: ``*/15 * * * *`` — every fifteen minutes is a sensible default
#: that respects upstream rate limits yet catches a same-day
#: cancellation inside the turnover window. Public so the domain
#: service and the poller (cd-d48) share a single source of truth.
DEFAULT_POLL_CADENCE: str = "*/15 * * * *"


class IcalFeed(Base):
    """External calendar URL the poller ingests reservations from.

    Carries the full §04 "iCal feed" shape after cd-ewd7:

    * ``url`` — the operator-supplied iCal endpoint (envelope-
      encrypted at the domain layer; TEXT here).
    * ``provider`` — canonical channel enum.
    * ``unit_id`` — the unit this feed populates. When NULL, stays
      land at the property level and the manager maps to a unit
      manually. ``SET NULL`` on unit deletion — feeds outlive unit
      churn (rename / merge / delete shouldn't lose the poller config).
    * ``poll_cadence`` — per-feed cron (default ``*/15 * * * *``).
      The poller (cd-d48) reads this to drive APScheduler.
    * ``last_polled_at`` / ``last_etag`` — conditional-GET plumbing.
    * ``last_error`` — the most recent §04 ``ical_url_*`` error code,
      cleared on a successful probe.
    * ``enabled`` — operator kill switch.

    FK cascades on ``property_id`` so deleting the property sweeps
    its feeds.
    """

    __tablename__ = "ical_feed"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``NULL`` means the feed is property-scoped (no unit mapping);
    # the domain-layer upsert falls back to ``(property_id, source,
    # external_id)`` per §04 "iCal feed". ``SET NULL`` so a unit
    # delete doesn't cascade into the feed row.
    unit_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("unit.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The operator-supplied iCal URL. §04's SSRF guard
    # (``ical_url_insecure_scheme`` / ``ical_url_private_address``)
    # runs in the domain layer, not at the DB.
    url: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    # Per-feed cron expression — populated with the §04 default on
    # insert unless the operator overrides. Free-form TEXT because
    # the parser (APScheduler / croniter) owns validation; the DB
    # is not the right place to reject a malformed cron.
    poll_cadence: Mapped[str] = mapped_column(
        String, nullable=False, default=DEFAULT_POLL_CADENCE
    )
    # ``NULL`` means the feed has never been polled — a fresh
    # registration. The poller treats a null value as "due now".
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last ``ETag`` seen on a 200; the next poll sends it as
    # ``If-None-Match`` to save bandwidth.
    last_etag: Mapped[str | None] = mapped_column(String, nullable=True)
    # Last §04 error code (``ical_url_timeout``,
    # ``ical_url_private_address``, …). ``NULL`` when the most
    # recent probe succeeded — the domain layer clears on success
    # so the operator UI can surface a live-vs-stale indicator.
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"provider IN ({_in_clause(_PROVIDER_VALUES)})",
            name="provider",
        ),
        Index("ix_ical_feed_workspace_property", "workspace_id", "property_id"),
        # The poller's due-work scan is keyed on ``unit_id`` when
        # upserting stays by ``(unit_id, source, external_id)``; a
        # plain index keeps that lookup cheap on a mostly-NULL column.
        Index("ix_ical_feed_unit", "unit_id"),
    )


class Reservation(Base):
    """A booked stay — either ingested from iCal or entered manually.

    The v1 slice carries the minimum needed to generate turnover
    work: check-in / check-out instants (both ``DateTime(timezone=
    True)`` — resolved UTC at rest), guest identity hints, the
    lifecycle status enum, and the ``external_uid`` used to
    idempotently re-ingest the same VEVENT. The ``(ical_feed_id,
    external_uid)`` uniqueness is what makes a re-poll safe: the
    next poll upserts on the pair rather than inserting a duplicate.

    When ``ical_feed_id IS NULL`` the reservation was manual or API;
    both Postgres and SQLite treat NULLs as distinct in unique
    indexes by default, so manual entries with the same
    ``external_uid`` never collide. OK for v1 — the domain layer
    will own the richer §04 "uniqueness by (unit, source, external)"
    rule when cd-1ai lands.

    ``raw_summary`` / ``raw_description`` preserve the upstream
    VEVENT body so downstream parsers can re-analyse without
    re-polling.
    """

    __tablename__ = "reservation"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    property_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("property.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable + ``SET NULL``: a reservation outlives its feed if the
    # feed is deleted (agency swap, manual recapture). The domain
    # layer's re-ingest path doesn't depend on the feed surviving.
    ical_feed_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("ical_feed.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Provider UID (Airbnb HMAC id, VRBO reservation id, etc.). Kept
    # as plain text — callers never parse it.
    external_uid: Mapped[str] = mapped_column(String, nullable=False)
    check_in: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    check_out: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    guest_name: Mapped[str | None] = mapped_column(String, nullable=True)
    guest_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="scheduled")
    source: Mapped[str] = mapped_column(String, nullable=False, default="ical")
    # Raw VEVENT body kept verbatim so the domain layer can re-parse
    # without another HTTP fetch.
    raw_summary: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_description: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_in_clause(_STATUS_VALUES)})",
            name="status",
        ),
        CheckConstraint(
            f"source IN ({_in_clause(_SOURCE_VALUES)})",
            name="source",
        ),
        CheckConstraint("check_out > check_in", name="check_out_after_check_in"),
        # Idempotent re-poll: the upsert path targets this composite.
        UniqueConstraint(
            "ical_feed_id",
            "external_uid",
            name="uq_reservation_feed_external_uid",
        ),
        # Per-acceptance: "reservations for this property in time order".
        Index("ix_reservation_property_check_in", "property_id", "check_in"),
    )


class StayBundle(Base):
    """Group of tasks materialised against a :class:`Reservation`.

    One bundle per (reservation, rule) pair — see §04 "Stay task
    bundles". The v1 slice persists the ``kind`` (``turnover`` /
    ``welcome`` / ``deep_clean``) and a JSON payload with template
    refs + metadata the scheduler uses to spawn occurrences.
    Cascades on the parent reservation so a cancelled booking
    sweeps its unstarted work.
    """

    __tablename__ = "stay_bundle"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    reservation_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("reservation.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    # Flat list of ``{template_id, metadata, …}`` payloads. The outer
    # ``Any`` is scoped to SQLAlchemy's JSON column type — callers
    # writing a typed payload should use a TypedDict locally and
    # coerce into this column. The domain layer validates shape at
    # write time.
    tasks_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"kind IN ({_in_clause(_BUNDLE_KIND_VALUES)})",
            name="kind",
        ),
        Index("ix_stay_bundle_reservation", "reservation_id"),
    )
