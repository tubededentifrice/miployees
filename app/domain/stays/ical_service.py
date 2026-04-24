"""``ical_feed`` registration + probe + lifecycle service.

The polling worker is a separate concern (cd-d48). This service owns
everything up to the point the poller would run:

* **Register** a feed against a property. Validates the URL through
  the :class:`~app.adapters.ical.ports.IcalValidator` port, auto-
  detects the provider, envelope-encrypts the URL, and inserts the
  row with ``enabled=False``. If the probe comes back as a
  parseable VCALENDAR we flip ``enabled=True`` in the same
  transaction.
* **Probe** an existing feed. Re-runs validation + fetch; updates
  ``last_polled_at`` and ``last_error`` (stubbed out below since the
  v1 ORM doesn't carry ``last_error`` yet — see "Spec drift" note
  below). Flips ``enabled=True`` on the first successful probe.
* **Update** an existing feed's URL and/or provider. Swapping the URL
  re-runs the full validate-encrypt-probe path; swapping just the
  provider override skips the probe.
* **Disable / delete** — ``disable_feed`` clears ``enabled`` but
  keeps the row; ``delete_feed`` drops it outright. §04 does not
  (yet) carry a soft-delete column on ``ical_feed``, so "delete"
  is a hard delete — the reservations the feed seeded survive via
  the ``ical_feed_id`` SET NULL cascade.
* **List** — returns a DTO that **never** includes the plaintext
  URL, only a host-prefix preview so the manager UI can render
  "Airbnb feed for ``xxxx.airbnb.com``" without round-tripping the
  secret through HTTP.

**Audit.** Every mutation writes one row via :func:`app.audit.write_audit`.
The URL is redacted to host-only in the audit diff — §15 forbids
plaintext secrets in the audit stream, and the envelope-encrypted
ciphertext would be noise.

**Spec drift notes.**

* The ORM only carries the v1 slice (``url`` / ``provider`` /
  ``last_polled_at`` / ``last_etag`` / ``enabled``). §04 adds
  ``unit_id``, ``poll_cadence``, and ``last_error``. This service
  stubs the ``last_error`` surface (see :data:`_LAST_ERROR_HINT`)
  so the shape lands once the column does; filed as a Beads task
  on top of cd-1ai (``last_error`` column + migration).
* The ``provider`` CHECK constraint allows ``airbnb | vrbo |
  booking | custom``. The auto-detect emits ``gcal`` and
  ``generic`` from :mod:`app.adapters.ical.providers`; the service
  collapses both to ``"custom"`` before the row write. The
  provider-override DTO also maps any non-v1 slug to ``"custom"``
  at the boundary, so the CHECK never fires.

**Port wiring.** The service takes an
:class:`~app.adapters.ical.ports.IcalValidator`, a
:class:`~app.adapters.ical.ports.ProviderDetector`, and an
:class:`~app.adapters.storage.envelope.EnvelopeEncryptor` by DI on
each call. Production wires the concrete adapters; tests pass stubs.

See ``docs/specs/04-properties-and-stays.md`` §"iCal feed",
``docs/specs/02-domain-model.md`` §"ical_feed",
``docs/specs/15-security-privacy.md`` §"Secret envelope" / §"SSRF".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.stays.models import IcalFeed
from app.adapters.ical.ports import (
    IcalProvider,
    IcalValidation,
    IcalValidationError,
    IcalValidator,
    ProviderDetector,
)
from app.adapters.storage.ports import EnvelopeEncryptor
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "IcalFeedCreate",
    "IcalFeedNotFound",
    "IcalFeedUpdate",
    "IcalFeedView",
    "IcalProbeResult",
    "IcalProviderOverride",
    "IcalUrlInvalid",
    "delete_feed",
    "disable_feed",
    "get_plaintext_url",
    "list_feeds",
    "probe_feed",
    "register_feed",
    "update_feed",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The HKDF ``purpose`` label the envelope helper uses. Different
# callers (property wifi password, workspace SMTP secret, ...) pick
# different purposes so their ciphertexts can't decrypt each other's
# plaintext. Locked at registration time — a purpose change would
# invalidate every persisted URL.
_URL_PURPOSE = "ical-feed-url"
_MAX_URL_LEN = 2048
_LAST_ERROR_HINT = "not_persisted_v1"  # §04 column lands with follow-up

# The v1 DB CHECK only allows these four provider slugs; the
# auto-detect's richer taxonomy (``gcal`` / ``generic``) collapses
# to ``custom`` on write. A spec-drift follow-up tracks the CHECK
# widening.
_DbProvider = Literal["airbnb", "vrbo", "booking", "custom"]
_DB_PROVIDERS: frozenset[str] = frozenset({"airbnb", "vrbo", "booking", "custom"})


# Provider override accepted at the service boundary — callers pass a
# public slug, the service coerces to the DB slug before write.
IcalProviderOverride = IcalProvider


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IcalFeedNotFound(LookupError):
    """The feed doesn't exist in the caller's workspace.

    404-equivalent. Mirrors :class:`app.domain.places.property_service.
    PropertyNotFound`: a feed linked only to workspace A is invisible
    to workspace B; we don't distinguish "wrong workspace" from
    "really missing".
    """


class IcalUrlInvalid(ValueError):
    """URL validation failed.

    422-equivalent. Carries the §04 error ``code`` and the underlying
    message so the router can render a structured response. The
    caller is responsible for ensuring the message doesn't contain
    the URL itself when surfaced into audit / logs (the domain
    service strips to host-only before persisting).
    """

    __slots__ = ("code",)

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class IcalFeedCreate(BaseModel):
    """Body for :func:`register_feed`.

    ``provider_override`` is optional — when ``None`` the service
    auto-detects from the URL host. The DTO caps the URL length to
    ``_MAX_URL_LEN`` so a pathological caller can't push
    multi-megabyte strings through the envelope path.
    """

    model_config = ConfigDict(extra="forbid")

    property_id: str = Field(..., min_length=1, max_length=64)
    url: str = Field(..., min_length=10, max_length=_MAX_URL_LEN)
    provider_override: IcalProviderOverride | None = None


class IcalFeedUpdate(BaseModel):
    """Body for :func:`update_feed`.

    All fields optional — the service diffs against the stored row
    and only re-runs validation / probe on URL changes. Swapping
    just the provider override is a cheap metadata flip that does
    not re-hit the network.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, min_length=10, max_length=_MAX_URL_LEN)
    provider_override: IcalProviderOverride | None = None


@dataclass(frozen=True, slots=True)
class IcalFeedView:
    """Read projection — safe to return to any caller.

    ``url_preview`` is the public, non-secret form: scheme + host,
    no path or query (both of which frequently carry the provider's
    secret token). ``url_plaintext`` is deliberately **not** on
    this DTO — the only legal way to reach the plaintext is through
    :func:`get_plaintext_url`, which is the poller's entry point.
    """

    id: str
    workspace_id: str
    property_id: str
    provider: _DbProvider
    provider_override: IcalProviderOverride | None
    url_preview: str
    enabled: bool
    last_polled_at: datetime | None
    last_etag: str | None
    last_error: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IcalProbeResult:
    """Outcome of a :func:`probe_feed` call.

    ``parseable_ics`` is the gate the service uses to flip
    ``enabled=True`` on the first successful probe. ``error_code``
    is the §04 vocabulary (``ical_url_*``); populated only when
    the probe failed.
    """

    feed_id: str
    ok: bool
    parseable_ics: bool
    error_code: str | None
    polled_at: datetime


# ---------------------------------------------------------------------------
# Service API
# ---------------------------------------------------------------------------


def register_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    body: IcalFeedCreate,
    validator: IcalValidator,
    detector: ProviderDetector,
    envelope: EnvelopeEncryptor,
    clock: Clock | None = None,
) -> IcalFeedView:
    """Register a new feed for ``property_id``.

    Pipeline:

    1. Validate the URL via the SSRF-guarded :class:`IcalValidator`.
    2. Auto-detect the provider unless ``body.provider_override`` is
       set.
    3. Encrypt the canonicalised URL via the envelope port.
    4. Insert the row with ``enabled`` mirroring
       ``validation.parseable_ics`` — a probe that comes back with a
       real VCALENDAR envelope lights the feed immediately; anything
       less (e.g. a non-ICS body) lands disabled for the operator
       to investigate.
    5. Write one ``ical_feed.register`` audit row with host-only URL.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    try:
        validation = validator.validate(body.url)
    except IcalValidationError as exc:
        raise IcalUrlInvalid(exc.code, str(exc)) from exc

    # Provider override wins when present; fall through to auto-
    # detection only when the override is absent. Skipping the detect
    # call in the override path keeps the service side-effect-free on
    # that branch (detector stubs in tests see zero calls).
    effective_provider: IcalProvider = (
        body.provider_override
        if body.provider_override is not None
        else detector.detect(validation.url)
    )
    db_provider = _to_db_provider(effective_provider)

    ciphertext = envelope.encrypt(validation.url.encode("utf-8"), purpose=_URL_PURPOSE)

    row = IcalFeed(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        property_id=body.property_id,
        url=_ciphertext_to_str(ciphertext),
        provider=db_provider,
        last_polled_at=now,
        last_etag=None,
        enabled=validation.parseable_ics,
        created_at=now,
    )
    session.add(row)
    session.flush()

    view = _row_to_view(
        row,
        validation=validation,
        provider_override=body.provider_override,
        last_error=None,
    )
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=row.id,
        action="register",
        diff={"after": _view_to_diff_dict(view)},
        clock=resolved_clock,
    )
    return view


def update_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    body: IcalFeedUpdate,
    validator: IcalValidator,
    detector: ProviderDetector,
    envelope: EnvelopeEncryptor,
    clock: Clock | None = None,
) -> IcalFeedView:
    """Mutate an existing feed.

    ``url`` set → re-validate + re-encrypt + re-probe; enabled flips
    to match the new probe's ``parseable_ics``.
    ``provider_override`` set → swap the stored provider slug
    without re-probing (cheap metadata flip).

    At least one of the two must be set; an empty body raises
    :class:`ValueError` (422) — there's no such thing as a no-op
    update, and silently succeeding would be an audit surprise.
    """
    if body.url is None and body.provider_override is None:
        raise ValueError("update_feed requires at least one of url / provider_override")

    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, feed_id=feed_id)
    before_view = _row_to_view(
        row, validation=None, provider_override=None, last_error=None
    )

    validation: IcalValidation | None = None
    if body.url is not None:
        try:
            validation = validator.validate(body.url)
        except IcalValidationError as exc:
            raise IcalUrlInvalid(exc.code, str(exc)) from exc
        ciphertext = envelope.encrypt(
            validation.url.encode("utf-8"), purpose=_URL_PURPOSE
        )
        row.url = _ciphertext_to_str(ciphertext)
        row.last_polled_at = now
        row.enabled = validation.parseable_ics

    if body.provider_override is not None:
        # Override wins; auto-detect only runs when the override is
        # absent. When both ``url`` and ``provider_override`` are
        # set, the override still wins — matches :func:`register_feed`.
        row.provider = _to_db_provider(body.provider_override)
    elif validation is not None:
        # URL changed but override is absent — re-run auto-detect on
        # the new URL.
        row.provider = _to_db_provider(detector.detect(validation.url))

    session.flush()
    after_view = _row_to_view(
        row,
        validation=validation,
        provider_override=body.provider_override,
        last_error=None,
    )
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=row.id,
        action="update",
        diff={
            "before": _view_to_diff_dict(before_view),
            "after": _view_to_diff_dict(after_view),
        },
        clock=resolved_clock,
    )
    return after_view


def disable_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    clock: Clock | None = None,
) -> IcalFeedView:
    """Flip ``enabled=False`` on a feed without dropping the row.

    The row survives so the reservation history keyed off
    ``ical_feed_id`` stays navigable; the poller simply skips
    disabled feeds.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, feed_id=feed_id)
    before_view = _row_to_view(
        row, validation=None, provider_override=None, last_error=None
    )
    row.enabled = False
    session.flush()
    after_view = _row_to_view(
        row, validation=None, provider_override=None, last_error=None
    )
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=row.id,
        action="disable",
        diff={
            "before": _view_to_diff_dict(before_view),
            "after": _view_to_diff_dict(after_view),
        },
        clock=resolved_clock,
    )
    return after_view


def delete_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    clock: Clock | None = None,
) -> IcalFeedView:
    """Hard-delete the feed row.

    §02 "ical_feed" does not carry a ``deleted_at`` column; deleting
    is a plain DELETE. Reservations survive via the
    ``reservation.ical_feed_id`` ``SET NULL`` cascade (§02
    "reservation"). If v2 adds a soft-delete column this path
    switches to stamping ``deleted_at`` and leaves the audit shape
    intact.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    row = _load_row(session, ctx, feed_id=feed_id)
    before_view = _row_to_view(
        row, validation=None, provider_override=None, last_error=None
    )
    session.delete(row)
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=before_view.id,
        action="delete",
        diff={"before": _view_to_diff_dict(before_view)},
        clock=resolved_clock,
    )
    return before_view


def probe_feed(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    validator: IcalValidator,
    envelope: EnvelopeEncryptor,
    clock: Clock | None = None,
) -> IcalProbeResult:
    """Re-run validation + fetch against the stored URL.

    Used by both the operator's "test this feed" button (future API)
    and the first-success gate — a newly-registered feed lands
    ``enabled=False`` if the probe body didn't look like an ICS
    envelope; a later probe can flip it on.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    now = resolved_clock.now()
    row = _load_row(session, ctx, feed_id=feed_id)

    plaintext_url = get_plaintext_url(session, ctx, feed_id=feed_id, envelope=envelope)
    try:
        validation = validator.validate(plaintext_url)
    except IcalValidationError as exc:
        row.last_polled_at = now
        # last_error would be persisted here once §04's column lands;
        # see _LAST_ERROR_HINT + follow-up Beads task.
        session.flush()
        write_audit(
            session,
            ctx,
            entity_kind="ical_feed",
            entity_id=row.id,
            action="probe",
            diff={"ok": False, "error_code": exc.code, "polled_at": now.isoformat()},
            clock=resolved_clock,
        )
        return IcalProbeResult(
            feed_id=row.id,
            ok=False,
            parseable_ics=False,
            error_code=exc.code,
            polled_at=now,
        )

    row.last_polled_at = now
    if validation.parseable_ics and not row.enabled:
        # First-success gate: flip ``enabled`` only when we've seen a
        # real VCALENDAR. A non-parseable body leaves ``enabled``
        # alone (if it was true we keep it true; if false we keep it
        # false pending a real body).
        row.enabled = True
    session.flush()
    write_audit(
        session,
        ctx,
        entity_kind="ical_feed",
        entity_id=row.id,
        action="probe",
        diff={
            "ok": True,
            "parseable_ics": validation.parseable_ics,
            "polled_at": now.isoformat(),
        },
        clock=resolved_clock,
    )
    return IcalProbeResult(
        feed_id=row.id,
        ok=True,
        parseable_ics=validation.parseable_ics,
        error_code=None,
        polled_at=now,
    )


def list_feeds(
    session: Session,
    ctx: WorkspaceContext,
    *,
    property_id: str | None = None,
) -> Sequence[IcalFeedView]:
    """Enumerate feeds in the caller's workspace.

    Optional ``property_id`` filter. Ordered by ``created_at`` then
    ``id`` for a stable tie-break. The plaintext URL is **never**
    materialised — the view carries only the non-secret
    ``url_preview``.
    """
    stmt = select(IcalFeed).where(IcalFeed.workspace_id == ctx.workspace_id)
    if property_id is not None:
        stmt = stmt.where(IcalFeed.property_id == property_id)
    stmt = stmt.order_by(IcalFeed.created_at.asc(), IcalFeed.id.asc())
    rows = session.scalars(stmt).all()
    return [
        _row_to_view(row, validation=None, provider_override=None, last_error=None)
        for row in rows
    ]


def get_plaintext_url(
    session: Session,
    ctx: WorkspaceContext,
    *,
    feed_id: str,
    envelope: EnvelopeEncryptor,
) -> str:
    """Return the decrypted URL for ``feed_id``.

    The **only** legal plaintext reach. The poller (cd-d48) calls
    this inside its fetch loop; the operator-facing HTTP layer must
    never surface the result. An operator UI that wants to show the
    URL should copy through a signed, short-lived echo path instead
    — the plaintext URL can carry vendor secret tokens.

    Raises :class:`IcalFeedNotFound` for unknown / wrong-workspace
    ids; ciphertext corruption surfaces as
    :class:`app.adapters.storage.envelope.EnvelopeDecryptError`.
    """
    row = _load_row(session, ctx, feed_id=feed_id)
    plaintext = envelope.decrypt(_str_to_ciphertext(row.url), purpose=_URL_PURPOSE)
    return plaintext.decode("utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_row(session: Session, ctx: WorkspaceContext, *, feed_id: str) -> IcalFeed:
    """Workspace-scoped loader. Raises :class:`IcalFeedNotFound` on miss."""
    stmt = select(IcalFeed).where(
        IcalFeed.id == feed_id,
        IcalFeed.workspace_id == ctx.workspace_id,
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise IcalFeedNotFound(feed_id)
    return row


def _to_db_provider(provider: IcalProvider) -> _DbProvider:
    """Collapse the full ``IcalProvider`` taxonomy to the v1 DB slug.

    ``gcal`` / ``generic`` → ``"custom"``. A spec-drift follow-up
    tracks widening the DB CHECK to carry the richer set.
    """
    if provider in ("airbnb", "vrbo", "booking"):
        return provider  # narrowed by the Literal overlap.
    return "custom"


def _ciphertext_to_str(ciphertext: bytes) -> str:
    """Encode ciphertext bytes for the ``ical_feed.url`` TEXT column.

    The column is TEXT (v1 slice — §02 adds a real ``BYTEA`` column
    once the full ``secret_envelope`` row lands). We latin-1 encode
    the raw bytes, which is the canonical "1:1 byte-to-codepoint"
    text mapping — every byte value ``0..255`` maps to exactly one
    Unicode codepoint so round-tripping through TEXT is lossless.
    """
    return ciphertext.decode("latin-1")


def _str_to_ciphertext(stored: str) -> bytes:
    """Inverse of :func:`_ciphertext_to_str`."""
    return stored.encode("latin-1")


def _row_to_view(
    row: IcalFeed,
    *,
    validation: IcalValidation | None,
    provider_override: IcalProviderOverride | None,
    last_error: str | None,
) -> IcalFeedView:
    """Project an :class:`IcalFeed` row into the safe read shape.

    ``validation`` is passed through only during register / update so
    the returned view carries a fresh ``url_preview`` for the
    caller; reads that don't have a validation handy fall back to
    decrypting is NOT OK here — the list path explicitly must not
    round-trip plaintext. Instead the view carries ``"(encrypted)"``
    when we can't derive a preview without decryption.
    """
    preview: str
    if validation is not None:
        preview = _host_only_preview(validation.url)
    else:
        # List / disable / delete — we don't decrypt. This keeps the
        # read path free of envelope dependencies. Operators who want
        # the public preview can trigger a probe (which has a
        # validation in hand) or hit a dedicated "reveal" endpoint
        # that goes through :func:`get_plaintext_url` with an
        # elevated capability.
        preview = "(encrypted)"
    return IcalFeedView(
        id=row.id,
        workspace_id=row.workspace_id,
        property_id=row.property_id,
        provider=_narrow_db_provider(row.provider),
        provider_override=provider_override,
        url_preview=preview,
        enabled=row.enabled,
        last_polled_at=row.last_polled_at,
        last_etag=row.last_etag,
        last_error=last_error,
        created_at=row.created_at,
    )


def _narrow_db_provider(value: str) -> _DbProvider:
    """Narrow a loaded DB string to the :data:`_DbProvider` literal.

    The CHECK constraint on ``ical_feed.provider`` already rejects
    anything else; the narrow surfaces schema drift as a loud
    :class:`ValueError` rather than silently returning junk.
    """
    if value in _DB_PROVIDERS:
        # Literal narrowing happens via the equality checks below;
        # the frozenset membership is for fast rejection on garbage.
        if value == "airbnb":
            return "airbnb"
        if value == "vrbo":
            return "vrbo"
        if value == "booking":
            return "booking"
        if value == "custom":
            return "custom"
    raise ValueError(f"unknown ical_feed.provider {value!r} on loaded row")


def _host_only_preview(url: str) -> str:
    """Return ``scheme://host`` — strip path and query (often secret)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def _view_to_diff_dict(view: IcalFeedView) -> dict[str, Any]:
    """Flatten an :class:`IcalFeedView` into a JSON-safe audit payload.

    Intentionally omits anything URL-derived that could carry the
    plaintext secret. ``url_preview`` is already host-only; still
    passes through the audit writer's redactor for defence in
    depth.
    """
    return {
        "id": view.id,
        "workspace_id": view.workspace_id,
        "property_id": view.property_id,
        "provider": view.provider,
        "provider_override": view.provider_override,
        "url_preview": view.url_preview,
        "enabled": view.enabled,
        "last_polled_at": (
            view.last_polled_at.isoformat() if view.last_polled_at else None
        ),
        "last_etag": view.last_etag,
        "last_error": view.last_error,
        "created_at": view.created_at.isoformat(),
    }
