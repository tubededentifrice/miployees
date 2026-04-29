"""Messaging context router — web-push subscription + VAPID key surface.

Mounted by the app factory under ``/w/<slug>/api/v1/messaging``. All
routes require an active :class:`~app.tenancy.WorkspaceContext`.

Routes (cd-0bnz):

* ``GET  /notifications/push/vapid-key`` — return the workspace's
  VAPID public key so the browser's service worker can subscribe.
  Cached in-process for 5 minutes per workspace against the
  monotonic clock.
* ``POST /notifications/push/subscribe`` — register the browser's
  ``PushSubscription.toJSON()`` payload for the caller. Idempotent
  on ``(user_id, endpoint)``.
* ``POST /notifications/push/unsubscribe`` — remove the caller's
  subscription for a given endpoint. Idempotent: returns 204 even
  when the row was already gone.

The handlers are thin: parse the DTO, call the domain service, map
typed errors to RFC 7807 error bodies. The UoW
(:func:`app.api.deps.db_session`) owns the transaction boundary; the
domain code never commits itself.

Native-app (FCM / APNS) push-token registration is a separate
reserved surface under ``/me/push-tokens`` that returns 501 until the
native app ships (§12 "Device push tokens", §10 "v1 scope note"). The
web-push surface lives here because the browser's subscription model
is workspace-scoped today — a future promotion to identity scope
matches the §02 ``user_push_token`` roadmap.

See ``docs/specs/10-messaging-notifications.md`` §"Channels" →
§"Agent-message delivery" (tier 2 semantics);
``docs/specs/12-rest-api.md`` §"Messaging" (endpoint surface);
``docs/specs/02-domain-model.md`` §"user_push_token".
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from threading import Lock
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.messaging.models import Notification, PushToken
from app.adapters.db.messaging.repositories import SqlAlchemyPushTokenRepository
from app.api.deps import current_workspace_context, db_session
from app.api.messaging.channels import build_channels_router
from app.api.messaging.messages import build_messages_router
from app.audit import write_audit
from app.domain.messaging.push_tokens import (
    MAX_ENDPOINT_LEN,
    EndpointNotAllowed,
    EndpointSchemeInvalid,
    PushSubscribe,
    PushTokenView,
    VapidNotConfigured,
    get_vapid_public_key,
    list_for_user,
    register,
    unregister,
)
from app.events.bus import EventBus
from app.tenancy import WorkspaceContext
from app.util.clock import SystemClock

__all__ = [
    "BulkMarkReadRequest",
    "NotificationListResponse",
    "NotificationPatchRequest",
    "NotificationPayload",
    "PushTokenPayload",
    "PushTokenUnavailableRequest",
    "PushUnsubscribe",
    "VapidKeyPayload",
    "build_messaging_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


class PushUnsubscribe(BaseModel):
    """Request body for ``POST /notifications/push/unsubscribe``.

    Narrowed shape — only ``endpoint`` is needed. Defined at module
    level (not inside :func:`build_messaging_router`) so FastAPI's
    body-parsing introspection recognises the type as a request body
    rather than forwarding it to query parameters; a closure-scoped
    class loses its ``__module__`` signal and FastAPI falls back to
    query-param inference.

    ``extra='forbid'`` matches the subscribe DTO in
    :mod:`app.domain.messaging.push_tokens` so a typo'd payload
    (``{"url": "..."}``) is rejected with 422 instead of silently
    treating the call as a request to unsubscribe an empty endpoint.
    The ``endpoint`` length cap mirrors :data:`MAX_ENDPOINT_LEN` so
    the subscribe / unsubscribe shapes share one contract — a value
    that registers must also be deletable through this surface.
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=1, max_length=MAX_ENDPOINT_LEN)


class BulkMarkReadRequest(BaseModel):
    """Request body for ``POST /notifications:mark-read``."""

    model_config = ConfigDict(extra="forbid")

    ids: list[str] = Field(min_length=1, max_length=100)


class NotificationPatchRequest(BaseModel):
    """Request body for ``PATCH /notifications/{id}``."""

    model_config = ConfigDict(extra="forbid")

    read: bool = True


class PushTokenUnavailableRequest(BaseModel):
    """Reserved native-app push token registration body.

    Native FCM/APNS delivery is not provisioned in this slice, so the
    endpoint always returns ``501 push_unavailable`` after validating
    the basic shape. Web push remains live through ``/subscribe``.
    """

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(pattern="^(android|ios)$")
    token: str = Field(min_length=1, max_length=4096)
    device_label: str | None = Field(default=None, max_length=120)
    app_version: str | None = Field(default=None, max_length=64)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PushTokenPayload(BaseModel):
    """HTTP projection of :class:`~app.domain.messaging.push_tokens.PushTokenView`.

    A Pydantic model (rather than re-exporting the frozen dataclass)
    so FastAPI's OpenAPI generator emits a named component schema
    the SPA can pattern-match on. Mirrors the read shape of the
    domain view one-to-one — no filtering, no derived fields.
    """

    id: str
    workspace_id: str
    user_id: str
    endpoint: str
    created_at: datetime
    last_used_at: datetime | None
    user_agent: str | None

    @classmethod
    def from_view(cls, view: PushTokenView) -> PushTokenPayload:
        """Copy a :class:`PushTokenView` into its HTTP payload shape."""
        return cls(
            id=view.id,
            workspace_id=view.workspace_id,
            user_id=view.user_id,
            endpoint=view.endpoint,
            created_at=view.created_at,
            last_used_at=view.last_used_at,
            user_agent=view.user_agent,
        )


class NotificationPayload(BaseModel):
    """HTTP projection of a caller-visible notification row."""

    id: str
    workspace_id: str
    recipient_user_id: str
    kind: str
    subject: str
    body_md: str | None
    payload: dict[str, object]
    read_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Notification) -> NotificationPayload:
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            recipient_user_id=row.recipient_user_id,
            kind=row.kind,
            subject=row.subject,
            body_md=row.body_md,
            payload=dict(row.payload_json),
            read_at=row.read_at,
            created_at=row.created_at,
        )


class NotificationListResponse(BaseModel):
    """Collection envelope for notification list reads."""

    data: list[NotificationPayload]
    next_cursor: str | None
    has_more: bool
    total_estimate: int


class VapidKeyPayload(BaseModel):
    """Response body for ``GET /notifications/push/vapid-key``.

    Single ``key`` field carrying the base64url-encoded public key.
    The SPA feeds this verbatim to ``pushManager.subscribe({
    applicationServerKey })``.
    """

    key: str


# ---------------------------------------------------------------------------
# VAPID key cache — per-workspace, 5-minute TTL on monotonic clock
# ---------------------------------------------------------------------------


# Cache TTL in seconds. 5 minutes matches the task's acceptance
# criterion and the web-push key's change cadence: the operator
# rotates via CLI, and a small staleness window is acceptable
# because the key is used at subscription time (one call per
# browser install, not per push delivery).
_VAPID_CACHE_TTL_SECONDS: float = 300.0


# Process-local cache: ``workspace_id → (expiry_monotonic, key)``.
# Module-level mutable state is intentional — one cache entry per
# workspace, no cross-process invalidation in v1 (operator rotation
# is rare and tolerates a 5-minute window; the native-app delivery
# path does not read this cache).
#
# Uses :func:`time.monotonic` so an NTP jump or a deployment
# running across a DST transition cannot retcon the TTL.
_vapid_cache: dict[str, tuple[float, str]] = {}
_vapid_cache_lock: Lock = Lock()


# Injectable monotonic clock for tests. Production path calls
# :func:`time.monotonic` directly; tests pass a callable that
# returns a controlled float. A simple ``Callable[[], float]`` seam
# avoids pulling in a full :class:`~app.util.clock.Clock` Protocol
# here — the real clock would return wall-time datetimes, not
# monotonic floats, and we specifically need monotonic semantics.
_MonotonicFn = Callable[[], float]


def _reset_vapid_cache_for_tests() -> None:
    """Drop every cached VAPID key.

    Exposed for tests that exercise the cache TTL — each test
    needs a clean slate so a leaked entry from a peer test doesn't
    leak through the ``hit`` path of a fresh scenario. Not part
    of the public router API.
    """
    with _vapid_cache_lock:
        _vapid_cache.clear()


def _cached_vapid_key(
    session: Session,
    ctx: WorkspaceContext,
    *,
    monotonic: _MonotonicFn,
) -> str:
    """Return the workspace's VAPID public key, caching for 5 minutes.

    Cache is per-``workspace_id`` on the shared module-level dict.
    A thread safely enters the section via :data:`_vapid_cache_lock`
    — the lock span is tight (dict read + optional DB read on miss),
    so contention is negligible at v1 scale.

    Raises :class:`VapidNotConfigured` on a miss when the setting
    is absent; the caught/remapped version is the router's 503
    path.
    """
    now_monotonic = monotonic()
    key_cached: str | None = None
    with _vapid_cache_lock:
        cached = _vapid_cache.get(ctx.workspace_id)
        if cached is not None:
            expiry, value = cached
            if now_monotonic < expiry:
                key_cached = value
    if key_cached is not None:
        return key_cached

    # Cache miss (or expired) — read from the DB, then repopulate.
    # We release the lock during the DB read so a slow SELECT does
    # not stall other workspaces' lookups; the worst-case outcome
    # is two concurrent misses both fetching the same value, which
    # is harmless.
    value = get_vapid_public_key(SqlAlchemyPushTokenRepository(session), ctx)
    with _vapid_cache_lock:
        _vapid_cache[ctx.workspace_id] = (
            now_monotonic + _VAPID_CACHE_TTL_SECONDS,
            value,
        )
    return value


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _http_for_push_error(exc: Exception) -> HTTPException:
    """Map a push-domain error to the router's HTTP response shape.

    Keeps the mapping centralised so every route returns the same
    ``{"error": "<code>"}`` envelope for the same domain type.
    Matches the convention in :mod:`app.api.v1.time`.
    """
    if isinstance(exc, EndpointSchemeInvalid):
        # Literal 422 — Starlette renamed
        # ``HTTP_422_UNPROCESSABLE_ENTITY`` →
        # ``HTTP_422_UNPROCESSABLE_CONTENT`` in 2024 and emits a
        # deprecation warning on the old name. The integer is stable.
        # (Same trick in :func:`app.api.v1.time._http_for_shift_error`.)
        return HTTPException(
            status_code=422,
            detail={"error": "endpoint_scheme_invalid", "message": str(exc)},
        )
    if isinstance(exc, EndpointNotAllowed):
        return HTTPException(
            status_code=422,
            detail={"error": "endpoint_not_allowed", "message": str(exc)},
        )
    if isinstance(exc, VapidNotConfigured):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "vapid_not_configured"},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "internal"},
    )


def _notification_or_404(
    session: Session,
    ctx: WorkspaceContext,
    notification_id: str,
) -> Notification:
    row = session.scalars(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.workspace_id == ctx.workspace_id,
            Notification.recipient_user_id == ctx.actor_id,
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "notification_not_found"},
        )
    return row


def _mark_notification(
    session: Session,
    ctx: WorkspaceContext,
    row: Notification,
    *,
    read: bool,
) -> Notification:
    now = SystemClock().now()
    before = row.read_at
    if read and row.read_at is None:
        row.read_at = now
    elif not read and row.read_at is not None:
        row.read_at = None
    if row.read_at != before:
        write_audit(
            session,
            ctx,
            entity_kind="notification",
            entity_id=row.id,
            action="messaging.notification.read_state_changed",
            diff={"read": row.read_at is not None},
            via="api",
        )
        session.flush()
    return row


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_messaging_router(
    *,
    monotonic: _MonotonicFn | None = None,
    event_bus: EventBus | None = None,
) -> APIRouter:
    """Build the messaging router with an injectable monotonic clock.

    ``monotonic`` defaults to :func:`time.monotonic`. Tests inject a
    controlled callable so they can drive the cache TTL deterministically
    without patching the module globals on every scenario.
    """
    _monotonic = monotonic if monotonic is not None else time.monotonic

    r = APIRouter(tags=["messaging"])
    r.include_router(build_channels_router())
    r.include_router(build_messages_router(event_bus=event_bus))

    @r.get(
        "/notifications",
        response_model=NotificationListResponse,
        operation_id="messaging.notifications.list",
        summary="List the caller's notifications",
    )
    def list_notifications(
        ctx: _Ctx,
        session: _Db,
        unread_only: bool = Query(default=False),
        cursor: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> NotificationListResponse:
        stmt = (
            select(Notification)
            .where(
                Notification.workspace_id == ctx.workspace_id,
                Notification.recipient_user_id == ctx.actor_id,
            )
            .order_by(Notification.id.desc())
            .limit(limit + 1)
        )
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        if cursor is not None:
            stmt = stmt.where(Notification.id < cursor)
        rows = list(session.scalars(stmt).all())
        has_more = len(rows) > limit
        page = rows[:limit]
        total_stmt = (
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.workspace_id == ctx.workspace_id,
                Notification.recipient_user_id == ctx.actor_id,
            )
        )
        if unread_only:
            total_stmt = total_stmt.where(Notification.read_at.is_(None))
        return NotificationListResponse(
            data=[NotificationPayload.from_row(row) for row in page],
            next_cursor=page[-1].id if has_more and page else None,
            has_more=has_more,
            total_estimate=session.scalar(total_stmt) or 0,
        )

    @r.get(
        "/notifications/{notification_id}",
        response_model=NotificationPayload,
        operation_id="messaging.notifications.get",
        summary="Get one notification",
    )
    def get_notification(
        notification_id: Annotated[str, Path(min_length=1)],
        ctx: _Ctx,
        session: _Db,
    ) -> NotificationPayload:
        return NotificationPayload.from_row(
            _notification_or_404(session, ctx, notification_id)
        )

    @r.patch(
        "/notifications/{notification_id}",
        response_model=NotificationPayload,
        operation_id="messaging.notifications.update",
        summary="Mark a notification read or unread",
    )
    def patch_notification(
        notification_id: Annotated[str, Path(min_length=1)],
        body: NotificationPatchRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> NotificationPayload:
        row = _notification_or_404(session, ctx, notification_id)
        return NotificationPayload.from_row(
            _mark_notification(session, ctx, row, read=body.read)
        )

    @r.post(
        "/notifications:mark-read",
        response_model=NotificationListResponse,
        operation_id="messaging.notifications.mark_read",
        summary="Bulk-mark notifications as read",
    )
    def post_mark_read(
        body: BulkMarkReadRequest,
        ctx: _Ctx,
        session: _Db,
    ) -> NotificationListResponse:
        rows = session.scalars(
            select(Notification)
            .where(
                Notification.workspace_id == ctx.workspace_id,
                Notification.recipient_user_id == ctx.actor_id,
                Notification.id.in_(body.ids),
            )
            .order_by(Notification.id.desc())
        ).all()
        for row in rows:
            _mark_notification(session, ctx, row, read=True)
        return NotificationListResponse(
            data=[NotificationPayload.from_row(row) for row in rows],
            next_cursor=None,
            has_more=False,
            total_estimate=len(rows),
        )

    @r.get(
        "/notifications/push/vapid-key",
        response_model=VapidKeyPayload,
        operation_id="messaging.get_vapid_public_key",
        summary="Get the web-push VAPID public key for the workspace",
    )
    def get_vapid_key(
        ctx: _Ctx,
        session: _Db,
    ) -> VapidKeyPayload:
        """Return the base64url VAPID public key (cached 5 minutes)."""
        try:
            key = _cached_vapid_key(session, ctx, monotonic=_monotonic)
        except VapidNotConfigured as exc:
            raise _http_for_push_error(exc) from exc
        return VapidKeyPayload(key=key)

    @r.post(
        "/notifications/push/subscribe",
        response_model=PushTokenPayload,
        status_code=status.HTTP_201_CREATED,
        operation_id="messaging.register_push_subscription",
        summary="Register (or refresh) the caller's web-push subscription",
    )
    def post_subscribe(
        body: PushSubscribe,
        ctx: _Ctx,
        session: _Db,
    ) -> PushTokenPayload:
        """Idempotent upsert of a browser's PushSubscription for the caller.

        Always returns 201 — the same status on a fresh subscribe and
        on a benign re-subscribe (browser re-running the service
        worker on page load). The response body always carries the
        current row, so a client that replays the call gets an
        up-to-date view either way. Returning 201 uniformly sidesteps
        a race where a parallel test reads the "was it new" signal
        from the status code; the signal lives on the DB (one row
        per (user, endpoint)) and the audit ledger (one row per
        initial subscribe) instead.
        """
        try:
            view = register(
                SqlAlchemyPushTokenRepository(session),
                ctx,
                endpoint=body.endpoint,
                p256dh=body.keys.p256dh,
                auth=body.keys.auth,
                user_agent=body.ua,
            )
        except (EndpointNotAllowed, EndpointSchemeInvalid) as exc:
            raise _http_for_push_error(exc) from exc
        return PushTokenPayload.from_view(view)

    @r.get(
        "/notifications/push/tokens",
        response_model=list[PushTokenPayload],
        operation_id="messaging.push_tokens.list",
        summary="List the caller's registered web-push tokens",
    )
    def get_push_tokens(
        ctx: _Ctx,
        session: _Db,
    ) -> list[PushTokenPayload]:
        views = list_for_user(SqlAlchemyPushTokenRepository(session), ctx)
        return [PushTokenPayload.from_view(view) for view in views]

    @r.post(
        "/notifications/push/tokens",
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        operation_id="messaging.push_tokens.register_native_unavailable",
        summary="Reserved native-app push token registration surface",
    )
    def post_native_push_token(
        body: PushTokenUnavailableRequest,
        ctx: _Ctx,
    ) -> None:
        del body
        del ctx
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={"error": "push_unavailable"},
        )

    @r.delete(
        "/notifications/push/tokens/{token_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="messaging.push_tokens.delete",
        summary="Delete one of the caller's web-push tokens",
    )
    def delete_push_token(
        token_id: Annotated[str, Path(min_length=1)],
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        row = session.scalars(
            select(PushToken).where(
                PushToken.id == token_id,
                PushToken.workspace_id == ctx.workspace_id,
                PushToken.user_id == ctx.actor_id,
            )
        ).one_or_none()
        if row is not None:
            endpoint_host = urlparse(row.endpoint).hostname
            session.delete(row)
            write_audit(
                session,
                ctx,
                entity_kind="push_token",
                entity_id=token_id,
                action="messaging.push_token.deleted",
                diff={"endpoint_host": endpoint_host},
                via="api",
            )
            session.flush()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @r.post(
        "/notifications/push/unsubscribe",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="messaging.unregister_push_subscription",
        summary="Remove the caller's web-push subscription for a given endpoint",
    )
    def post_unsubscribe(
        body: PushUnsubscribe,
        ctx: _Ctx,
        session: _Db,
    ) -> Response:
        """Idempotent: returns 204 whether the row existed or not."""
        unregister(SqlAlchemyPushTokenRepository(session), ctx, endpoint=body.endpoint)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return r


# Production router. The default instance uses :func:`time.monotonic`
# as its clock seam. Tests that need to drive the cache TTL build
# their own via :func:`build_messaging_router` with an injected
# callable.
router: APIRouter = build_messaging_router()
