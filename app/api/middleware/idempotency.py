"""``Idempotency-Key`` middleware — persisted replay cache per spec §12.

The middleware sits *after* the tenancy middleware (so the caller's
``token_id`` is available via :attr:`request.state.actor_identity`)
and *before* the handler. It:

1. Bypasses every request that is not a ``POST`` — spec §12
   "Idempotency" only contracts the header on POST-mutating routes.
2. Bypasses requests without an ``Idempotency-Key`` header — the
   middleware is opt-in per-request; clients retrying must repeat
   the same key.
3. Bypasses an explicit allow-list of **exempt paths**. The only
   route in v1 is ``POST /payslips/{id}/payout_manifest`` (spec §11
   "interactive-session-only"): its response is not cached in the
   idempotency store; the header is accepted but ignored. A replay
   re-executes, re-audits, and re-decrypts from the current secret
   store. Extra exempt routes are added to the module-level
   :data:`EXEMPT_PATH_PATTERNS` tuple, not dispersed across files.
4. Bypasses requests whose ``request.state.actor_identity`` does
   not carry a ``token_id`` — the replay cache is keyed by
   ``(token_id, key)``, so a cookie-session request cannot
   participate. Session-authenticated POSTs still succeed; they
   simply forgo the replay guarantee.
5. Reads the inbound request body once, hashes it (sha256 of the
   canonical JSON; non-JSON bodies fall back to the raw bytes so
   file uploads still work), and looks up
   ``(token_id, idempotency_key)`` in the :class:`IdempotencyKey`
   table.

   - **Hit, matching body hash:** the stored response is replayed
     verbatim with an ``Idempotency-Replay: true`` header attached.
   - **Hit, mismatching body hash:** the middleware returns a
     409 ``idempotency_conflict`` problem+json response inline —
     :class:`BaseHTTPMiddleware` swallows exceptions, so we
     render the envelope here instead of ``raise``-ing through
     FastAPI's exception handlers. The downstream handler never runs.
   - **Miss:** the handler runs against the cached request body
     (Starlette's ``_CachedRequest`` replays it to the downstream
     app verbatim), then the middleware opens a short-lived UoW
     and persists the response before returning. Any unhandled
     exception from the handler bubbles up unchanged — the cache
     only records a completed response.

Concurrent retries are serialised by the UNIQUE ``(token_id, key)``
constraint on :class:`IdempotencyKey`: the second writer catches
:class:`~sqlalchemy.exc.IntegrityError`, re-reads the winning row's
cached response, and replays it instead of double-executing the
handler. A follow-up request with the same key but a different body
always hits 409 ``idempotency_conflict``.

**TTL sweep.** :func:`prune_expired_idempotency_keys` deletes every
row older than :data:`IDEMPOTENCY_TTL_HOURS`. The APScheduler seam
in :mod:`app.worker.scheduler` registers this callable as the
``idempotency_sweep`` daily job (cd-j9l7) via
:func:`~app.worker.scheduler.register_jobs`; operators running a
split worker container hit the same code path through the shared
registration function. The callable is still exported publicly so
it can also be invoked manually from a CLI or from a cron outside
the app process on deployments that prefer external scheduling.

See ``docs/specs/12-rest-api.md`` §"Idempotency",
``docs/specs/11-llm-and-agents.md`` §"interactive-session-only",
and ``docs/specs/02-domain-model.md`` §"idempotency_key".
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.adapters.db.ops.models import IdempotencyKey
from app.adapters.db.session import make_uow
from app.api.errors import problem_response
from app.domain.errors import IdempotencyConflict
from app.tenancy.current import tenant_agnostic
from app.tenancy.middleware import ACTOR_STATE_ATTR, ActorIdentity
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "EXEMPT_PATH_PATTERNS",
    "IDEMPOTENCY_HEADER",
    "IDEMPOTENCY_REPLAY_HEADER",
    "IDEMPOTENCY_TTL_HOURS",
    "IdempotencyMiddleware",
    "canonical_body_hash",
    "is_exempt_path",
    "prune_expired_idempotency_keys",
]


_log = logging.getLogger(__name__)


# Request header name carrying the client-supplied idempotency key.
# Case-insensitive on the wire; we normalise to this spelling when
# reading.
IDEMPOTENCY_HEADER: Final[str] = "Idempotency-Key"

# Response header the middleware adds on a cache hit so clients can
# tell a replayed response from a fresh one (e.g. for metric
# reporting). Not named in spec §12 today — it's a common convention
# (Stripe, IETF draft ``draft-ietf-httpapi-idempotency-key-header``)
# we adopt here; tracked for spec pickup under the cd-z6fk spec
# follow-up.
IDEMPOTENCY_REPLAY_HEADER: Final[str] = "Idempotency-Replay"

# TTL for persisted cache rows. Spec §12 pins 24 h.
IDEMPOTENCY_TTL_HOURS: Final[int] = 24

# Subset of response headers the middleware replays on a cache hit.
# Most headers (``Set-Cookie``, ``Content-Security-Policy``,
# ``Strict-Transport-Security``, …) are re-stamped by downstream
# middleware on every response and must NOT be captured in the
# replay cache — serving a stale CSP nonce or a stale session cookie
# would be a regression. We persist the narrow subset that encodes
# the *payload*'s identity: content metadata + ETag + Location for
# 201s.
_REPLAYABLE_RESPONSE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "content-language",
        "etag",
        "location",
        "last-modified",
        "cache-control",
    }
)


# Routes explicitly exempt from the idempotency cache. Matching is
# regex-based so path parameters (``{id}`` → any non-slash segment)
# resolve correctly. Keep the list short; every entry is justified
# by a spec reference in its comment. The optional prefix group
# swallows the three shapes the same route can take in the v1
# URL tree:
#
# * ``/w/<slug>/api/v1/...`` — canonical workspace-scoped form.
# * ``/api/v1/...`` — bare-host form (not used for payslips in v1
#   but pinned here so adding a bare-host mutating route later
#   doesn't require a regex churn).
# * bare ``/...`` — used by tests that mount the route outside the
#   usual workspace prefix.
#
# - ``/payslips/{id}/payout_manifest`` — spec §11
#   "interactive-session-only": streams decrypted account numbers
#   JIT; not cached so a replay re-decrypts from the *current*
#   secret store and can legitimately return 410 once secrets are
#   purged. The header is accepted but ignored.
EXEMPT_PATH_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^(?:/w/[^/]+)?(?:/api/v1)?/payslips/[^/]+/payout_manifest/?$"),
)


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    """Immutable snapshot of a persisted response ready for replay."""

    status: int
    body: bytes
    headers: dict[str, str]


def is_exempt_path(path: str) -> bool:
    """Return ``True`` if the path is on the exempt allow-list.

    Exposed publicly so tests can pin the exemption set without
    going through the middleware.
    """
    return any(pattern.match(path) for pattern in EXEMPT_PATH_PATTERNS)


def canonical_body_hash(body: bytes) -> str:
    """Return the sha256 hex digest of ``body``'s canonical form.

    Strategy:

    * Empty body → hash of the empty string. A retry with the same
      empty body matches; a retry with a non-empty body does not.
    * JSON body (parses cleanly) → sha256 of ``json.dumps(data,
      sort_keys=True, separators=(',', ':'))`` encoded as UTF-8.
      This collapses whitespace / key-order differences so a client
      that reformats the JSON between attempts still hits the cache.
    * Anything else → sha256 of the raw bytes. File uploads and
      other non-JSON payloads fall through here, which is the only
      stable hash we can offer — the client must send byte-identical
      bodies on retry for the cache to hit.

    The short-name implementation detail (two branches, one
    fallback) is deliberately pure so it can be pinned under unit
    tests without the middleware.
    """
    if not body:
        return hashlib.sha256(b"").hexdigest()
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Not valid JSON (or not decodable as text). Fall back to
        # hashing the raw bytes — the client must send byte-
        # identical bodies for the retry to match.
        return hashlib.sha256(body).hexdigest()
    canonical = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _collect_replayable_headers(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Narrow a response's headers to the subset we persist.

    Keys are lower-cased on the way in so the stored map is stable
    across frameworks that emit mixed-case header names. The
    replay path re-emits them verbatim; HTTP header names are
    case-insensitive so downstream clients handle the lower-case
    spelling identically to their original.
    """
    out: dict[str, str] = {}
    for key, value in headers:
        lowered = key.lower()
        if lowered in _REPLAYABLE_RESPONSE_HEADERS:
            out[lowered] = value
    return out


async def _read_request_body(request: Request) -> bytes:
    """Return the inbound body bytes.

    Starlette's :class:`BaseHTTPMiddleware` wraps the request in a
    :class:`starlette.middleware.base._CachedRequest` which caches
    the body on ``_body`` the first time ``await request.body()``
    is called, then replays it verbatim to the downstream handler
    via ``wrapped_receive``. We simply read once and rely on that
    cache — no manual ``_receive`` surgery required.
    """
    return await request.body()


def _extract_token_id(request: Request) -> str | None:
    """Return the caller's ``token_id`` from request state, or ``None``.

    The tenancy middleware stashes the resolved
    :class:`ActorIdentity` on ``request.state`` under
    :data:`~app.tenancy.middleware.ACTOR_STATE_ATTR`. Session-
    authenticated requests carry an actor with
    ``token_id is None``; anonymous requests (bare-host skip paths,
    unauthenticated routes) leave the attribute unset — in either
    case the idempotency middleware has no token to key on and
    short-circuits.
    """
    actor = getattr(request.state, ACTOR_STATE_ATTR, None)
    if not isinstance(actor, ActorIdentity):
        return None
    return actor.token_id


def _read_cached(
    db_session: DbSession, *, token_id: str, key: str
) -> IdempotencyKey | None:
    """Return the existing cache row for ``(token_id, key)`` or ``None``.

    justification: idempotency_key is a deployment-wide table (no
    workspace_id column); the tenancy filter would refuse the read
    without an explicit bypass. The predicate is the ``(token_id,
    key)`` pair — authorisation, not tenancy.
    """
    with tenant_agnostic():
        return db_session.scalars(
            select(IdempotencyKey)
            .where(
                IdempotencyKey.token_id == token_id,
                IdempotencyKey.key == key,
            )
            .limit(1)
        ).first()


def _persist_cached(
    db_session: DbSession,
    *,
    token_id: str,
    key: str,
    status: int,
    body_hash: str,
    body: bytes,
    headers: dict[str, str],
    created_at: datetime,
) -> None:
    """Insert a new cache row and commit.

    The caller drives the ``(token_id, key)`` race-winning semantics
    via :class:`~sqlalchemy.exc.IntegrityError`; this helper only
    owns the write. The UoW's commit/rollback is the caller's
    responsibility — we explicitly ``commit()`` here because this
    middleware opens its own short-lived UoW per request.

    justification: idempotency_key is deployment-wide; insertion
    bypasses the tenancy filter the same way the read does.
    """
    row = IdempotencyKey(
        id=new_ulid(),
        token_id=token_id,
        key=key,
        status=status,
        body_hash=body_hash,
        body=body,
        headers=headers,
        created_at=created_at,
    )
    with tenant_agnostic():
        db_session.add(row)
        db_session.commit()


def _replay(cached: _CachedResponse) -> Response:
    """Return a :class:`Response` that replays a cached entry verbatim."""
    response = Response(
        content=cached.body,
        status_code=cached.status,
        headers=cached.headers,
    )
    response.headers[IDEMPOTENCY_REPLAY_HEADER] = "true"
    return response


def _conflict_response(
    request: Request,
    *,
    key: str,
    stored_body_hash: str,
) -> Response:
    """Build the RFC 7807 ``idempotency_conflict`` 409 response.

    We render it inline here (rather than ``raise IdempotencyConflict(...)``)
    because :class:`BaseHTTPMiddleware` swallows exceptions raised in
    a dispatch and routes them through the default 500 handler —
    FastAPI's registered exception handlers only cover routes. Taking
    the short path through :func:`~app.api.errors.problem_response`
    keeps the envelope byte-identical to every other 409 the app
    emits.
    """
    exc = IdempotencyConflict(
        "idempotency key reused with a different body",
        extra={
            "idempotency_key": key,
            "stored_body_hash": stored_body_hash,
        },
    )
    return problem_response(
        request,
        status=409,
        type_name=exc.type_name,
        title=exc.title,
        detail=exc.detail,
        extra=dict(exc.extra) if exc.extra else None,
    )


@runtime_checkable
class _Streamable(Protocol):
    """Duck-typed shape of Starlette's streaming-body responses.

    Matches :class:`starlette.responses.StreamingResponse` and the
    private :class:`starlette.middleware.base._StreamingResponse`
    returned by :class:`BaseHTTPMiddleware.call_next`; both expose
    ``body_iterator`` as a mutable async-iterator slot.
    """

    body_iterator: AsyncIterator[bytes]


async def _single_chunk_iterator(
    buffered: bytes,
) -> AsyncIterator[bytes]:
    """Yield ``buffered`` once as an async byte stream."""
    yield buffered


async def _collect_response_body(response: Response) -> bytes:
    """Return the full body bytes of a Starlette :class:`Response`.

    Starlette's :class:`BaseHTTPMiddleware` always hands
    ``call_next`` a ``_StreamingResponse`` whose body is exposed as
    an async ``body_iterator`` — even when the downstream handler
    returned a plain :class:`JSONResponse`. We consume that
    iterator into a buffer, then *replace* the iterator with a
    replay shim so the response can still stream out to the caller
    once we return it. Non-streaming responses
    (``response.body`` is ``bytes``) are returned verbatim without
    touching the iterator.
    """
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        return body

    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        # Refuse to cache a response we cannot replay identically.
        raise TypeError(
            f"cannot persist idempotency cache entry for response of type "
            f"{type(response).__name__}; only byte-bodied or streaming "
            f"responses are supported"
        )

    chunks: list[bytes] = []
    async for chunk in iterator:
        # ASGI-message dicts (e.g. pathsend) would mean this is a
        # send-passthrough stream, not a body stream — we cannot
        # safely replay those. Bail so the caller can skip caching.
        if isinstance(chunk, dict):
            raise TypeError(
                "cannot persist idempotency cache entry for ASGI-message "
                "streaming response"
            )
        chunks.append(chunk)
    buffered = b"".join(chunks)

    # Re-arm the response so downstream ASGI dispatch streams the
    # buffered bytes out to the caller. ``body_iterator`` is the
    # documented Starlette seam on streaming responses; mypy only
    # sees the base :class:`Response` type here, which does not
    # declare it. We rebind through the duck-typed :class:`_Streamable`
    # protocol so the assignment stays type-safe.
    assert isinstance(response, _Streamable), (
        f"streaming body iterator missing on {type(response).__name__}"
    )
    response.body_iterator = _single_chunk_iterator(buffered)
    return buffered


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Persist POST response bodies keyed by ``(token_id, Idempotency-Key)``.

    Registered *after* :class:`~app.tenancy.middleware.WorkspaceContextMiddleware`
    in the factory so ``request.state.actor_identity`` is already
    populated. The middleware is a no-op for every request that
    isn't a ``POST`` carrying an ``Idempotency-Key`` header on a
    non-exempt path — those fall straight through to ``call_next``.

    The constructor accepts an optional :class:`~app.util.clock.Clock`
    so tests can pin ``created_at`` without monkeypatching
    ``datetime.now``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(app)
        self._clock = clock if clock is not None else SystemClock()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method.upper() != "POST":
            return await call_next(request)

        key = request.headers.get(IDEMPOTENCY_HEADER)
        if not key:
            return await call_next(request)

        path = request.url.path
        if is_exempt_path(path):
            # Exempt routes are caller-safe to retry anyway; we
            # accept the header without validating or persisting so
            # a client that always stamps the header doesn't get a
            # surprise error on the exempt surface.
            return await call_next(request)

        token_id = _extract_token_id(request)
        if token_id is None:
            # No token on this request → nothing to key on. Pass
            # through; a session-authenticated POST without a token
            # cannot use the replay cache.
            return await call_next(request)

        body = await _read_request_body(request)
        body_hash = canonical_body_hash(body)

        # --- Cache lookup ---
        with make_uow() as db_session:
            assert isinstance(db_session, DbSession)
            existing = _read_cached(db_session, token_id=token_id, key=key)
            if existing is not None:
                if existing.body_hash != body_hash:
                    return _conflict_response(
                        request,
                        key=key,
                        stored_body_hash=existing.body_hash,
                    )
                return _replay(
                    _CachedResponse(
                        status=existing.status,
                        body=bytes(existing.body),
                        headers=dict(existing.headers),
                    )
                )

        # --- Miss: run the handler, persist the response ---
        response = await call_next(request)

        # Skip caching of server-error (5xx) responses. An
        # ``Idempotency-Key`` whose first attempt hit a transient
        # failure (DB timeout, upstream flap) must be safe to retry —
        # persisting the 5xx would pin every subsequent retry to the
        # same failure for 24 h, defeating the retry mechanism.
        # Success (2xx) and client-error (4xx) responses are cached
        # as normal: the client's retry semantics treat those as
        # terminal and the replay surfaces the terminal state. This
        # matches Stripe/AWS-style idempotency-key behaviour; spec §12
        # is silent on status-class policy but "replays return the
        # stored response" implicitly presumes a terminal response
        # was stored, not a transient failure.
        if response.status_code >= 500:
            _log.info(
                "idempotency: skipping 5xx response",
                extra={
                    "event": "idempotency.skip_server_error",
                    "path": path,
                    "status": response.status_code,
                },
            )
            return response

        # Only persist successful responses and problem+json error
        # envelopes — anything else (streaming, file, unknown) we
        # pass through without caching. Callers get no replay
        # guarantee on those routes, which is strictly safer than
        # caching a response we cannot replay identically.
        try:
            response_body = await _collect_response_body(response)
        except TypeError:
            _log.warning(
                "idempotency: skipping non-byte response body",
                extra={
                    "event": "idempotency.skip_noncacheable",
                    "path": path,
                    "status": response.status_code,
                },
            )
            return response

        replay_headers = _collect_replayable_headers(response.headers.items())
        now = self._clock.now()

        with make_uow() as db_session:
            assert isinstance(db_session, DbSession)
            try:
                _persist_cached(
                    db_session,
                    token_id=token_id,
                    key=key,
                    status=response.status_code,
                    body_hash=body_hash,
                    body=response_body,
                    headers=replay_headers,
                    created_at=now,
                )
            except IntegrityError:
                # Concurrent retry won the race. Roll back our
                # failed insert, re-read the winning row, and replay
                # its cached response — matching the single-writer
                # path so clients cannot tell which call "won".
                db_session.rollback()
                existing = _read_cached(db_session, token_id=token_id, key=key)
                if existing is None:
                    # Unique violation but the row is gone — the only
                    # plausible cause is the TTL sweeper firing
                    # mid-race, which is vanishingly rare. Return the
                    # fresh response rather than pretending we cached
                    # nothing.
                    _log.warning(
                        "idempotency: integrity conflict but row missing on re-read",
                        extra={
                            "event": "idempotency.race_row_missing",
                            "path": path,
                            "token_id": token_id,
                        },
                    )
                    return response
                if existing.body_hash != body_hash:
                    # Two different requests with the same key arrived
                    # concurrently. Surface the same 409 the single-
                    # writer path would have emitted.
                    return _conflict_response(
                        request,
                        key=key,
                        stored_body_hash=existing.body_hash,
                    )
                return _replay(
                    _CachedResponse(
                        status=existing.status,
                        body=bytes(existing.body),
                        headers=dict(existing.headers),
                    )
                )

        return response


def prune_expired_idempotency_keys(
    *,
    db_session: DbSession | None = None,
    now: datetime | None = None,
    ttl: timedelta | None = None,
) -> int:
    """Delete every :class:`IdempotencyKey` row older than the TTL.

    The payload for the daily ``idempotency_sweep`` scheduled job
    (spec §12 "Idempotency" — TTL 24 h). The APScheduler wiring
    lives in :mod:`app.worker.scheduler` (cd-j9l7); operators who
    prefer external cron can invoke this callable from a CLI
    instead. The function opens its own UoW when ``db_session`` is
    ``None`` so it is safe to call from outside a request.

    Returns the number of rows deleted for logging / metric purposes.

    justification: idempotency_key is a deployment-wide table; the
    sweeper deliberately bypasses the tenancy filter.
    """
    cutoff_now = now if now is not None else datetime.now(UTC)
    cutoff_ttl = ttl if ttl is not None else timedelta(hours=IDEMPOTENCY_TTL_HOURS)
    cutoff = cutoff_now - cutoff_ttl

    if db_session is not None:
        return _prune_in_session(db_session, cutoff)

    with make_uow() as owned_session:
        assert isinstance(owned_session, DbSession)
        deleted = _prune_in_session(owned_session, cutoff)
        owned_session.commit()
        return deleted


def _prune_in_session(db_session: DbSession, cutoff: datetime) -> int:
    """Core DELETE — split so callers can drive the UoW themselves."""
    with tenant_agnostic():
        result = db_session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff)
        )
    # ``Session.execute`` returns ``Result[Any]`` in the public
    # type stubs; bulk-DML paths actually return a CursorResult with
    # a concrete ``rowcount``. The narrow cast here is precise, not
    # defensive — a non-cursor result would mean SQLAlchemy's DELETE
    # seam regressed and we want the failure to be loud.
    assert isinstance(result, CursorResult)
    return result.rowcount or 0
