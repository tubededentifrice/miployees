"""Web-push subscription registration + VAPID key management (cd-0bnz).

The :class:`~app.adapters.db.messaging.models.PushToken` row is the
per-(user, endpoint) handle the §10 "Agent-message delivery" worker
walks when pushing a notification to a browser. This module owns the
self-service registration / un-registration surface behind
``/w/<slug>/api/v1/messaging/notifications/push/...`` plus the VAPID
public-key lookup.

Public surface:

* **DTOs** — :class:`PushSubscribeKeys` + :class:`PushSubscribe` are
  the ``PushSubscription.toJSON()`` shape the browser produces;
  :class:`PushTokenView` is the frozen read projection the router
  returns.
* **Service functions** — :func:`register`, :func:`unregister`,
  :func:`get_vapid_public_key`, :func:`list_for_user`. Every function
  takes ``session`` + :class:`~app.tenancy.WorkspaceContext` as its
  first two positional arguments; the ``workspace_id`` and
  ``user_id`` default from ``ctx``, never from the caller's payload
  (v1 invariant §01).
* **Errors** — :class:`PushTokenNotFound` (never raised by the
  public surface — unregister is always a no-op on miss — but
  exported so tests can reference the type), :class:`EndpointNotAllowed`
  (SSRF-mitigation reject), :class:`EndpointSchemeInvalid` (non-https
  endpoint reject), :class:`VapidNotConfigured` (workspace settings
  missing the public key).

**Endpoint validation.** The browser hands us an opaque URL chosen
by the push service (FCM, Mozilla autopush, Apple web.push). We
pin the allowed origin set to the three mainline providers to
dodge SSRF amplification — a malicious caller that registers a
``https://attacker.example/sink`` endpoint would otherwise turn
the eventual push-delivery worker into a bounce amplifier. The
scheme must be ``https``; userinfo / non-443 ports are rejected
defensively; query strings are allowed because providers use them
(e.g. FCM ``?auth=...``).

**Transaction boundary.** The service never calls
``session.commit()``; the caller's Unit-of-Work owns transaction
boundaries (§01 "Key runtime invariants" #3). Every mutation writes
one :mod:`app.audit` row in the same transaction; ``p256dh`` /
``auth`` / ``endpoint`` flow through the audit writer's redaction
seam because they are PII-adjacent (the ``user_agent`` snapshot too).

**Authz.** All three endpoints are self-scoped: the caller
registers their own device, un-registers their own, and (where
exposed) reads their own subscriptions. No new catalog entry —
the ``ctx.actor_id`` is always the ``user_id`` we write / read.

See ``docs/specs/10-messaging-notifications.md`` §"Channels" →
§"Agent-message delivery" (tier 2 push semantics),
``docs/specs/02-domain-model.md`` §"user_push_token",
``docs/specs/12-rest-api.md`` §"Messaging" (this module's router
surface is the web-push counterpart to the §"Device push tokens"
reserved native-app stub).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.messaging.models import PushToken
from app.adapters.db.workspace.models import Workspace
from app.audit import write_audit
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "MAX_ENDPOINT_LEN",
    "SETTINGS_KEY_VAPID_PUBLIC",
    "EndpointNotAllowed",
    "EndpointSchemeInvalid",
    "PushSubscribe",
    "PushSubscribeKeys",
    "PushTokenNotFound",
    "PushTokenView",
    "VapidNotConfigured",
    "get_vapid_public_key",
    "list_for_user",
    "register",
    "unregister",
    "validate_endpoint",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Allow-list of mainline web-push provider origins. The browser's
# ``PushSubscription.endpoint`` is an opaque URL chosen by the push
# service; the three values below cover Chrome / Chromium / Edge
# (FCM), Firefox (Mozilla autopush), and Safari / iOS (Apple web.push).
# An endpoint whose hostname is NOT one of these is rejected at
# registration time — an attacker that bypasses this gate could
# coerce the push-delivery worker into probing an arbitrary HTTPS
# endpoint (SSRF amplification via bounce signals).
#
# New providers land by widening this frozenset in a reviewable diff;
# we deliberately do NOT source the list from settings so a
# compromised admin cannot weaken the SSRF gate without a code change.
_PUSH_ENDPOINT_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        # Chrome / Chromium / Edge — Firebase Cloud Messaging web push.
        "fcm.googleapis.com",
        # Firefox — Mozilla autopush.
        "updates.push.services.mozilla.com",
        # Safari / iOS — Apple web.push.
        "web.push.apple.com",
    }
)


# Workspace ``settings_json`` key for the VAPID public key. The
# Web Push protocol requires the browser's Service Worker to know
# the applicationServerKey (== VAPID public key) at subscription time
# so it can embed it in the encrypted push payload that reaches the
# push service. The key is per-workspace so a deployment hosting
# multiple tenants can rotate them independently; rotation is CLI-
# driven (out of scope for this module, tracked as a follow-up).
SETTINGS_KEY_VAPID_PUBLIC = "messaging.push.vapid_public_key"


# Size caps on the incoming subscription payload. Browsers produce
# base64url blobs of bounded length (p256dh ~88 chars after padding
# strip, auth ~22 chars, endpoint varies by provider but rarely
# exceeds 2048); the caps below are generous so a future
# provider-quirk longer endpoint still lands without a schema
# change. ``_MAX_UA_LEN`` mirrors sibling tables' freeform text
# column caps.
# Public so the router-side ``PushUnsubscribe`` DTO uses the same
# cap as the subscribe shape — the endpoint length contract is
# uniform across the surface.
MAX_ENDPOINT_LEN = 4_096
_MAX_KEY_LEN = 256
_MAX_UA_LEN = 512


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PushTokenNotFound(LookupError):
    """The requested push token does not exist in the caller's workspace.

    Not raised by the public surface today — :func:`unregister` is
    idempotent and returns ``None`` on miss — but exported so tests
    and future read / DELETE paths can reference the type without
    re-inventing it.
    """


class EndpointSchemeInvalid(ValueError):
    """The subscription ``endpoint`` is not a plain ``https://`` URL.

    422-equivalent. The Web Push protocol mandates HTTPS transport;
    an ``http://`` or ``ws://`` endpoint is a caller bug (a dev fake
    bypassing the browser) or an attacker probing for a downgrade.
    Reject early so the audit trail records the reject rather than
    a silent pass.
    """


class EndpointNotAllowed(ValueError):
    """The subscription ``endpoint`` hostname is not in the allow-list.

    422-equivalent. See :data:`_PUSH_ENDPOINT_ALLOWED_HOSTS` for the
    rationale — the gate is SSRF mitigation, not a feature toggle;
    new providers are added by code change, not at runtime.
    """


class VapidNotConfigured(RuntimeError):
    """The workspace has no ``messaging.push.vapid_public_key`` setting.

    503-equivalent. The operator has not yet provisioned a VAPID
    keypair for this workspace. Surfaced as a distinct error so the
    SPA can show "push not available yet" rather than a generic 500.
    """


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class PushSubscribeKeys(BaseModel):
    """The ``keys`` sub-object in a browser ``PushSubscription.toJSON()``.

    Matches the Web Push JSON shape:
    ``{"endpoint": "...", "keys": {"p256dh": "...", "auth": "..."}}``.
    A separate model (rather than two flat fields on the parent DTO)
    so the HTTP body binds to the exact browser-produced shape — a
    caller that sends ``p256dh`` / ``auth`` at the top level gets a
    422 for a misplaced key rather than silently missing the
    encryption material.
    """

    model_config = ConfigDict(extra="forbid")

    p256dh: str = Field(min_length=1, max_length=_MAX_KEY_LEN)
    auth: str = Field(min_length=1, max_length=_MAX_KEY_LEN)


class PushSubscribe(BaseModel):
    """Request body for ``POST /notifications/push/subscribe``.

    Matches the browser's ``PushSubscription.toJSON()`` envelope.
    Endpoint validation (https, allowed host, no userinfo, port 443)
    fires in the service layer via :func:`validate_endpoint` so the
    same rule applies to Python callers invoking :func:`register`
    directly.
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)
    keys: PushSubscribeKeys
    # Browser ``User-Agent`` snapshot. Optional — a curl test caller
    # may omit it; the SPA sends ``navigator.userAgent`` verbatim.
    ua: str | None = Field(default=None, max_length=_MAX_UA_LEN)


@dataclass(frozen=True, slots=True)
class PushTokenView:
    """Immutable read projection of a ``push_token`` row.

    A frozen / slotted dataclass (not a Pydantic model) because reads
    carry PII-adjacent columns (``endpoint`` opaque id, ``user_agent``
    snapshot) managed by the service, not the caller's payload. The
    router wraps this in a :class:`pydantic.BaseModel` response shape
    so OpenAPI emits a named component.
    """

    id: str
    workspace_id: str
    user_id: str
    endpoint: str
    created_at: datetime
    last_used_at: datetime | None
    user_agent: str | None


# ---------------------------------------------------------------------------
# Row ↔ view projection
# ---------------------------------------------------------------------------


def _row_to_view(row: PushToken) -> PushTokenView:
    """Project a loaded :class:`PushToken` row into a read view."""
    return PushTokenView(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        endpoint=row.endpoint,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        user_agent=row.user_agent,
    )


# ---------------------------------------------------------------------------
# Endpoint validation
# ---------------------------------------------------------------------------


def validate_endpoint(endpoint: str) -> None:
    """Assert ``endpoint`` is a plain https URL in the provider allow-list.

    Raises :class:`EndpointSchemeInvalid` for non-https / malformed
    URLs; :class:`EndpointNotAllowed` when the scheme is right but
    the host is not one of the three mainline web-push providers.

    Tightening rules beyond the task description:

    * Userinfo (``https://user:pass@host/...``) is rejected — the
      Web Push subscription ids browsers produce never carry
      credentials embedded in the URL, so userinfo signals either a
      caller bug or an attacker trying to exfil state through the
      eventual HTTP probe.
    * Non-443 explicit ports are rejected — the three allow-listed
      providers all serve on the default. A request for
      ``https://fcm.googleapis.com:8443/...`` would be a deliberate
      attempt to pivot the push worker into a non-standard port
      against the same host; reject. An explicit ``:443`` is
      equivalent to the implicit default and is accepted.
    * Query is allowed — FCM uses ``?auth=...`` on some endpoints,
      and a defensive reject would break live browsers.
    * Fragment is rejected — the Web Push subscription URL the
      browser produces never carries one, so a fragment signals
      either a caller bug or an attacker trying to slip routing
      hints past the SSRF gate (the eventual HTTP probe strips
      fragments, so the discrepancy could be exploited to register
      one URL and have the worker probe a different effective
      target).
    """
    # ``urlparse`` tolerates a lot; we explicitly assert scheme +
    # netloc + hostname before trusting any of its fields.
    parsed = urlparse(endpoint)

    if parsed.scheme != "https":
        raise EndpointSchemeInvalid(f"endpoint scheme {parsed.scheme!r} is not https")

    # ``netloc`` == "" means ``urlparse`` could not resolve a host at
    # all (e.g. ``https:///path``); treat as scheme invalid rather
    # than allow-list reject so the operator sees the right error.
    if not parsed.netloc:
        raise EndpointSchemeInvalid(f"endpoint has no host: {endpoint!r}")

    # ``username`` is populated when the URL carries ``user[:pass]@``;
    # reject because a legitimate push endpoint never does. Note that
    # ``urlparse`` returns the empty string (not ``None``) for an
    # empty userinfo segment like ``https://@host/`` or
    # ``https://:@host/`` — both shapes are caller bugs and rejected
    # by the ``is not None`` test.
    if parsed.username is not None or parsed.password is not None:
        raise EndpointSchemeInvalid("endpoint must not carry userinfo (user:pass@...)")

    # Fragment rejected — see the function docstring. The parsed
    # ``fragment`` is the empty string when no ``#`` is present, so
    # we test for truthiness rather than ``is not None``.
    if parsed.fragment:
        raise EndpointSchemeInvalid("endpoint must not carry a fragment (#...)")

    # Port handling: ``urlparse`` returns ``None`` when the URL uses
    # the scheme default (443 for https). An explicit non-443 port
    # is rejected; 443 explicit is equivalent to implicit and
    # accepted.
    try:
        port = parsed.port
    except ValueError as exc:
        # Malformed port fragment (e.g. ``:abc``). ``urlparse``
        # raises :class:`ValueError` on access in 3.12+. Surface
        # as scheme-invalid since the URL shape is broken.
        raise EndpointSchemeInvalid(f"endpoint port is malformed: {exc!s}") from exc
    if port is not None and port != 443:
        raise EndpointSchemeInvalid(f"endpoint port {port!r} is not 443")

    host = parsed.hostname
    if host is None:
        # Defensive — ``netloc`` passed but ``hostname`` is None
        # only on a corner case (URL with brackets but no host). Map
        # to scheme-invalid for consistency with the ``no host``
        # branch above.
        raise EndpointSchemeInvalid(f"endpoint has no resolvable host: {endpoint!r}")

    if host.lower() not in _PUSH_ENDPOINT_ALLOWED_HOSTS:
        raise EndpointNotAllowed(
            f"endpoint host {host!r} is not in the web-push provider allow-list"
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _find_existing(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    endpoint: str,
) -> PushToken | None:
    """Return the existing ``(user_id, endpoint)`` row in this workspace or ``None``.

    Scoped to ``ctx.workspace_id`` for tenant hygiene even though the
    ORM tenant filter already applies — matches the defence-in-depth
    pattern in :mod:`app.domain.time.shifts`.
    """
    stmt = select(PushToken).where(
        PushToken.workspace_id == ctx.workspace_id,
        PushToken.user_id == user_id,
        PushToken.endpoint == endpoint,
    )
    return session.scalars(stmt).one_or_none()


def register(
    session: Session,
    ctx: WorkspaceContext,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None = None,
    clock: Clock | None = None,
) -> PushTokenView:
    """Upsert a push subscription for the caller and return the view.

    Idempotent on ``(user_id, endpoint)``: a second call with the
    same pair returns the pre-existing row without writing a
    duplicate audit entry (the browser re-registers on every page
    load for freshness; the row insert is only interesting the first
    time).

    ``user_id`` is always ``ctx.actor_id`` — web-push registration
    is strictly self-service.

    Raises:

    * :class:`EndpointSchemeInvalid` — non-https / malformed URL.
    * :class:`EndpointNotAllowed` — host not in the provider
      allow-list.
    """
    validate_endpoint(endpoint)

    user_id = ctx.actor_id

    existing = _find_existing(
        session,
        ctx,
        user_id=user_id,
        endpoint=endpoint,
    )
    if existing is not None:
        # Idempotent no-op: the browser re-subscribes on every
        # service-worker activation. Refresh the encryption material
        # in case the browser rotated p256dh / auth against the same
        # endpoint — the spec allows this. ``user_agent`` is
        # refreshed too (a new browser build on the same install
        # keeps the same endpoint but bumps the UA string).
        #
        # We DO NOT write an audit row here — a benign refresh on a
        # page reload is not an interesting audit signal; a stream
        # of identical rows would dilute the ledger. The initial
        # subscribe is the audit-worthy event.
        changed = False
        if existing.p256dh != p256dh:
            existing.p256dh = p256dh
            changed = True
        if existing.auth != auth:
            existing.auth = auth
            changed = True
        if user_agent is not None and existing.user_agent != user_agent:
            existing.user_agent = user_agent
            changed = True
        if changed:
            session.flush()
        return _row_to_view(existing)

    now = (clock if clock is not None else SystemClock()).now()
    row = PushToken(
        id=new_ulid(),
        workspace_id=ctx.workspace_id,
        user_id=user_id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        user_agent=user_agent,
        created_at=now,
        last_used_at=None,
    )
    session.add(row)
    session.flush()

    view = _row_to_view(row)
    write_audit(
        session,
        ctx,
        entity_kind="push_token",
        entity_id=row.id,
        action="messaging.push.subscribed",
        # Deliberately do NOT log ``p256dh`` / ``auth`` / the full
        # endpoint — these are PII-adjacent (the endpoint uniquely
        # identifies the browser install). The audit row carries
        # the row id + user id + UA; the raw encryption material
        # stays in the ``push_token`` row only.
        diff={
            "user_id": user_id,
            "user_agent": user_agent,
            "endpoint_host": urlparse(endpoint).hostname,
        },
        clock=clock,
    )
    return view


def unregister(
    session: Session,
    ctx: WorkspaceContext,
    *,
    endpoint: str,
    clock: Clock | None = None,
) -> None:
    """Delete the caller's subscription for ``endpoint`` if it exists.

    Idempotent: returns ``None`` whether the row existed or not.
    Only writes an audit row when a row was actually removed — a
    second un-register on the same endpoint is a benign browser
    housekeeping call and not an audit-worthy event.

    ``user_id`` is always ``ctx.actor_id`` — web-push
    un-registration is strictly self-service.
    """
    user_id = ctx.actor_id
    existing = _find_existing(
        session,
        ctx,
        user_id=user_id,
        endpoint=endpoint,
    )
    if existing is None:
        # Idempotent no-op. No audit row — there's no state change.
        return

    removed_id = existing.id
    removed_host = urlparse(existing.endpoint).hostname
    session.delete(existing)
    session.flush()

    write_audit(
        session,
        ctx,
        entity_kind="push_token",
        entity_id=removed_id,
        action="messaging.push.unsubscribed",
        diff={
            "user_id": user_id,
            "endpoint_host": removed_host,
        },
        clock=clock,
    )


def get_vapid_public_key(
    session: Session,
    ctx: WorkspaceContext,
) -> str:
    """Return the VAPID public key for the caller's workspace.

    Reads ``workspace.settings_json[SETTINGS_KEY_VAPID_PUBLIC]``.
    Raises :class:`VapidNotConfigured` when the key is absent or
    not a non-empty string — the caller's SPA cannot subscribe
    without it and the operator needs to provision the keypair.

    Caching is NOT done at this layer. The router caches for 5 min
    per workspace against the monotonic clock; caching here would
    make a rotation-via-CLI invisible to in-flight sessions that
    already hold a domain-level cached value.
    """
    stmt = select(Workspace.settings_json).where(Workspace.id == ctx.workspace_id)
    payload = session.scalars(stmt).one_or_none()
    if payload is None:
        # The ctx should not exist without a workspace row (the
        # tenancy middleware resolves slug → id from the same table);
        # treat as not-configured for a stable caller error surface.
        raise VapidNotConfigured(f"workspace {ctx.workspace_id!r} has no settings row")
    # ``settings_json`` is a flat dict — see
    # :class:`~app.adapters.db.workspace.models.Workspace` docstring.
    # Defensive isinstance per the recovery-helper pattern in
    # ``app/auth/recovery.py``.
    if not isinstance(payload, dict):
        raise VapidNotConfigured(
            f"workspace {ctx.workspace_id!r} settings payload is not a dict"
        )
    value = payload.get(SETTINGS_KEY_VAPID_PUBLIC)
    if not isinstance(value, str) or not value:
        raise VapidNotConfigured(
            f"workspace {ctx.workspace_id!r} is missing setting "
            f"{SETTINGS_KEY_VAPID_PUBLIC!r}"
        )
    return value


def list_for_user(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str | None = None,
) -> tuple[PushTokenView, ...]:
    """Return every push token for ``user_id`` (defaults to caller).

    Self-only in v1 — cross-user listing would need a new catalog
    action (``messaging.push.view_others``) and we defer that until
    the manager surface actually needs it (P3 in the Beads task).
    Passing a non-``None`` ``user_id`` that differs from
    ``ctx.actor_id`` raises :class:`PermissionError`.
    """
    target_user_id = user_id if user_id is not None else ctx.actor_id
    if target_user_id != ctx.actor_id:
        # Matches the §01 "tenant surface is not enumerable" rule
        # for a self-only surface: raise PermissionError so the
        # router maps to 403.
        raise PermissionError("listing another user's push tokens is not supported")

    stmt = (
        select(PushToken)
        .where(
            PushToken.workspace_id == ctx.workspace_id,
            PushToken.user_id == target_user_id,
        )
        .order_by(PushToken.created_at.asc(), PushToken.id.asc())
    )
    rows = session.scalars(stmt).all()
    return tuple(_row_to_view(row) for row in rows)
