"""FastAPI middleware that resolves ``/w/<slug>/...`` to a WorkspaceContext.

The middleware sits between the ASGI adapter and the routers. For every
request it either:

1. Matches a **bare-host skip path** (health probes, auth entry points,
   static assets, the SPA catch-all for ``/w/<slug>`` without a trailing
   segment, ...) and passes the request through without binding a
   :class:`~app.tenancy.WorkspaceContext`.
2. Parses ``/w/<slug>/...`` at the URL root, validates the slug via
   :func:`~app.tenancy.validate_slug`, resolves the actor (session cookie
   or bearer token), looks up ``workspace`` + ``user_workspace``
   membership, and binds a live :class:`WorkspaceContext` on the
   request-scoped ContextVar.
3. Returns **404** for every rejection path (unknown slug, reserved
   slug, consecutive-hyphen slug, no auth, membership miss, bearer
   token with a mismatched workspace, ...). Never **403** — per spec
   §01 "Workspace addressing": an enumerator gets the same response
   shape as a non-member, keeping the enumeration surface flat.

**Constant-time cross-tenant responses** (spec §15
"Constant-time cross-tenant responses"). Every rejection branch funnels
through :func:`_not_found`, which returns a byte-identical envelope.
Timing parity is handled by :func:`resolve_workspace` — a slug miss
pays a dummy ``user_workspace`` read so its wall-clock is comparable
to a membership-miss (which does one workspace lookup + one membership
lookup).

**Phase-0 stub** (cd-iwsv). When
:attr:`~app.config.Settings.phase0_stub_enabled` is ``True`` the
middleware additionally accepts the legacy test headers
``X-Test-Workspace-Id`` / ``X-Test-Actor-Id`` and synthesises a
manager-role context without any DB work. The flag defaults to
``False`` in production; tests flip it on explicitly. When the flag
is ``False`` the headers are ignored — the real resolver runs.

See ``docs/specs/01-architecture.md`` §"Workspace addressing",
§"WorkspaceContext" and §"Tenant filter enforcement";
``docs/specs/03-auth-and-tokens.md`` §"Sessions" and §"API tokens";
``docs/specs/15-security-privacy.md`` §"Constant-time cross-tenant
responses".

**Performance note.** Starlette's :class:`BaseHTTPMiddleware` wraps the
downstream app in a per-request task, which adds a small amount of
latency vs. a pure-ASGI implementation. For v1 this overhead is
acceptable — correctness + ergonomics beat the ~µs cost on our
request volumes. A future revisit can drop to pure ASGI if the
middleware stack grows.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.auth.session import (
    SESSION_COOKIE_NAME,
    SessionExpired,
    SessionInvalid,
    hash_cookie_value,
)
from app.auth.session import (
    validate as validate_session,
)
from app.auth.tokens import (
    InvalidToken,
    TokenExpired,
    TokenRevoked,
)
from app.auth.tokens import (
    verify as verify_token,
)
from app.authz.owners import is_owner_member
from app.config import Settings, get_settings
from app.tenancy.context import ActorGrantRole, WorkspaceContext
from app.tenancy.current import reset_current, set_current, tenant_agnostic
from app.tenancy.slug import InvalidSlug, validate_slug
from app.util.ulid import new_ulid

__all__ = [
    "CORRELATION_ID_HEADER",
    "SKIP_PATHS",
    "TEST_ACTOR_ID_HEADER",
    "TEST_WORKSPACE_ID_HEADER",
    "ActorIdentity",
    "WorkspaceContextMiddleware",
    "resolve_actor",
    "resolve_workspace",
]


# Outgoing + incoming correlation id header. Accepted from the client
# (echoed unchanged) and minted fresh otherwise. Case-insensitive at
# the HTTP layer; we normalise to this exact spelling on the response.
CORRELATION_ID_HEADER = "X-Request-Id"

# Phase-0 stub headers (cd-iwsv-gated). When
# ``settings.phase0_stub_enabled`` is ``False`` (production default)
# these are ignored and the real resolver runs.
TEST_WORKSPACE_ID_HEADER = "X-Test-Workspace-Id"
TEST_ACTOR_ID_HEADER = "X-Test-Actor-Id"

# Bearer-token header prefix. We look for exactly ``Bearer `` (one
# trailing space) per RFC 6750; case-insensitive on the scheme word
# itself.
_BEARER_PREFIX = "bearer "


# Bare-host skip paths, derived from ``docs/specs/01-architecture.md``
# §"Workspace addressing". A request is "skipped" iff its path equals
# one of these strings OR starts with one followed by ``/`` (a child
# segment). Keep this list in sync with the reverse-proxy routing
# table and the reserved-slug list in :mod:`app.tenancy.slug`.
SKIP_PATHS: frozenset[str] = frozenset(
    {
        # Ops probes + identity surface (§01 "Workspace addressing").
        "/healthz",
        "/readyz",
        "/version",
        "/signup",
        "/login",
        "/recover",
        "/select-workspace",
        # Bare-host OpenAPI + docs (§12 "Base URL").
        "/api/openapi.json",
        "/api/v1",
        "/docs",
        "/redoc",
        # Bare-host auth surface (§03 "Self-serve signup", §12). Both
        # magic-link and passkey routers live here; keep the siblings in
        # lock-step so future routers (webauthn/*) are obvious to add.
        "/auth/magic",
        "/auth/passkey",
        # Bare-host email-change landing (§14 "Public"). Carries a
        # magic-link token, has no workspace until the swap completes.
        "/me/email/verify",
        # Bare-host admin shell + API (§14 "Admin", §12 "Admin surface").
        "/admin",
        # Static assets + SPA chrome that the reverse proxy or FastAPI
        # may serve from the bare host (§14 "Shell chrome").
        "/static",
        "/assets",
        "/styleguide",
        "/unsupported",
    }
)


# Dummy identifiers used by :func:`resolve_workspace` to equalise the
# wall-clock between slug-miss and membership-miss paths. Both are
# 26-char Crockford-base32 strings so they parse as ULIDs at the
# schema level but will never collide with a real row.
_TIMING_DUMMY_WORKSPACE_ID: str = "00000000000000000000NOPE00"
_TIMING_DUMMY_USER_ID: str = "00000000000000000000NOPE01"


# Priority ordering for derived ``actor_grant_role`` (§05 "Roles &
# groups"). Higher value = more authority. ``manager`` wins when the
# same user holds multiple surface grants on a workspace.
_ROLE_PRIORITY: dict[str, int] = {
    "guest": 0,
    "client": 1,
    "worker": 2,
    "manager": 3,
}


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    """Resolved caller identity: the outcome of :func:`resolve_actor`.

    ``workspace_id`` is populated **only** for bearer-token callers
    whose token row names a workspace (§03 "API tokens"). Session
    cookies are tenant-agnostic at issue time (the user may switch
    workspaces mid-session), so the session branch leaves it ``None``
    and :func:`resolve_workspace` accepts the URL slug as the
    authoritative workspace.

    ``token_id`` and ``session_id`` are mutually exclusive — whichever
    pipeline produced the row stamps it. The unused one stays ``None``.
    Downstream audit wiring keys off the populated one.
    """

    user_id: str
    kind: Literal["user"]
    workspace_id: str | None
    token_id: str | None
    session_id: str | None


# ---------------------------------------------------------------------------
# Path helpers (unchanged vs. Phase-0)
# ---------------------------------------------------------------------------


def _is_skip_path(path: str) -> bool:
    """Return ``True`` if ``path`` is a bare-host route we pass through.

    Matches either the exact skip-path value (``/healthz``) or a child
    segment rooted at it (``/static/app.css``, ``/docs/swagger.json``).
    Deliberately does NOT match a longer path that merely starts with
    the same characters (``/signup-flow`` is scoped, not a child of
    ``/signup``).
    """
    if path in SKIP_PATHS:
        return True
    # Child-segment check: longest skip-path is ``/select-workspace`` —
    # a single startswith-with-separator pass is cheap.
    return any(path.startswith(f"{prefix}/") for prefix in SKIP_PATHS)


def _is_bare_w_path(path: str) -> bool:
    """Return ``True`` if ``path`` is a bare-host ``/w`` SPA catch-all.

    Matches ``/w``, ``/w/``, ``/w/<slug>``, and ``/w/<slug>/`` — i.e.
    anything that does not have a **non-empty** segment after the
    slug. Those requests are served by the SPA's catch-all, never by a
    scoped route, so the middleware must not attempt slug resolution on
    them (a mis-parse here would surface as a spurious 404 on the
    workspace-picker screen).
    """
    if path in ("/w", "/w/"):
        return True
    # ``_parse_scoped_path`` requires at least one non-empty segment
    # after the slug; when that's missing, treat it as a bare SPA hit.
    segments = path.split("/")
    # "/w/villa-sud" → ['', 'w', 'villa-sud'] (len=3)
    # "/w/villa-sud/" → ['', 'w', 'villa-sud', ''] (len=4, last='')
    if len(segments) < 4:
        return segments[:2] == ["", "w"]
    if segments[:2] != ["", "w"]:
        return False
    # Len >= 4 but the third (and all later) segments are empty ⇒ bare.
    return all(s == "" for s in segments[3:])


def _parse_scoped_path(path: str) -> str | None:
    """Extract ``<slug>`` from ``/w/<slug>/<rest>`` or ``None``.

    Returns ``None`` when the path does not have a non-empty segment
    after the slug — callers upstream have already excluded skip paths
    and bare-``/w`` paths, so this is purely the final "does the URL
    look scoped?" check.
    """
    segments = path.split("/")
    if len(segments) < 4:
        return None
    if segments[0] != "" or segments[1] != "w":
        return None
    slug = segments[2]
    if slug == "":
        return None
    # Must have at least one non-empty segment after the slug.
    if not any(s != "" for s in segments[3:]):
        return None
    return slug


def _not_found() -> JSONResponse:
    """Return the canonical 404 shape.

    Spec §15 mandates a byte-identical envelope across every
    rejection branch so an enumerator cannot tell "unknown slug" from
    "not a member of a known workspace" apart. The envelope is the
    shared ``{"error": "not_found", "detail": null}`` shape spec §15
    pins — not the Starlette default ``{"detail": "Not Found"}``.
    """
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": None})


# ---------------------------------------------------------------------------
# Actor resolution
# ---------------------------------------------------------------------------


def resolve_actor(
    request: Request,
    db_session: DbSession,
    settings: Settings,
) -> ActorIdentity | None:
    """Resolve the caller identity from the request's auth material.

    Resolution order:

    1. ``Authorization: Bearer <token>`` — pass to
       :func:`app.auth.tokens.verify`. Returns the token's user +
       workspace + key_id on success.
    2. ``Cookie: __Host-crewday_session=<value>`` — pass to
       :func:`app.auth.session.validate`. Returns the session's
       user_id on success; ``workspace_id`` is left ``None`` because
       the session itself is tenant-agnostic at issue time.

    Every failure (malformed token, unknown token, revoked token,
    expired token, bad session cookie, expired session, …) returns
    ``None``. The middleware then emits a 404 — per spec §01 we do
    not distinguish "bad credential" from "good credential but not a
    member" at the wire. ``settings`` is accepted to keep the
    resolver plumb-testable (``session.validate`` uses it for the
    pepper subkey).

    Raises nothing: unhandled exceptions bubble; the
    :class:`InvalidToken` / :class:`SessionInvalid` / ... tree is
    caught narrowly.
    """
    auth_header = request.headers.get("Authorization")
    if auth_header is not None and auth_header.lower().startswith(_BEARER_PREFIX):
        token_value = auth_header[len(_BEARER_PREFIX) :].strip()
        if token_value:
            try:
                verified = verify_token(db_session, token=token_value)
            except (InvalidToken, TokenExpired, TokenRevoked):
                # Every failure collapses to "no actor" — the middleware
                # cannot distinguish them at the wire without leaking.
                return None
            return ActorIdentity(
                user_id=verified.user_id,
                kind="user",
                workspace_id=verified.workspace_id,
                token_id=verified.key_id,
                session_id=None,
            )

    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value:
        try:
            user_id = validate_session(
                db_session,
                cookie_value=cookie_value,
                settings=settings,
            )
        except (SessionInvalid, SessionExpired):
            return None
        # The session row's PK is the sha256 of the cookie value; we
        # re-derive it locally so ``session_id`` carries the same
        # identifier ``audit_log`` uses, without a second DB read.
        return ActorIdentity(
            user_id=user_id,
            kind="user",
            workspace_id=None,
            token_id=None,
            session_id=hash_cookie_value(cookie_value),
        )

    return None


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------


def _derive_actor_grant_role(
    db_session: DbSession,
    *,
    workspace_id: str,
    user_id: str,
    is_owner: bool,
) -> ActorGrantRole:
    """Return the highest-priority surface grant the actor holds.

    Reads every ``role_grant`` row for the ``(user, workspace)`` pair
    (any ``scope_property_id``, including NULL workspace-wide) and
    picks the entry with the highest priority in
    :data:`_ROLE_PRIORITY`. If the actor holds no grants at all but
    **is** in the owners group, fall back to ``manager`` — owners
    always carry implicit manager authority (§02 "permission_group"
    §"owners"). Otherwise fall back to ``guest`` (a member whose
    upstream grants have all expired but whose ``user_workspace`` row
    hasn't been pruned yet).

    A richer derivation — picking the grant whose ``scope_property_id``
    matches the request path, walking permission-group capabilities,
    etc. — is cd-rpxd's scope. For v1 the workspace-wide highest-role
    pick is the safe default: every router today gates on explicit
    action-catalog membership at the service seam, so
    ``actor_grant_role`` is an audit-shape hint, not the authority.
    """
    stmt = select(RoleGrant.grant_role).where(
        RoleGrant.workspace_id == workspace_id,
        RoleGrant.user_id == user_id,
    )
    roles = set(db_session.scalars(stmt).all())
    if roles:
        # Deterministic pick by the role priority table.
        best = max(roles, key=lambda r: _ROLE_PRIORITY.get(r, -1))
        # Narrow to the literal set; an unknown value means the
        # ``grant_role`` CHECK drifted — fail closed by returning a
        # minimal-authority stub.
        if best in _ROLE_PRIORITY:
            # mypy can't narrow ``str`` to ``ActorGrantRole`` through
            # the dict-key membership; do the narrow explicitly.
            if best == "manager":
                return "manager"
            if best == "worker":
                return "worker"
            if best == "client":
                return "client"
            if best == "guest":
                return "guest"

    if is_owner:
        # Owners without an explicit ``manager`` grant still gate every
        # action the manager surface opens — matches the invariant that
        # a workspace always has at least one owners-group member who
        # can administer it.
        return "manager"
    return "guest"


def resolve_workspace(
    request_path: str,
    actor: ActorIdentity | None,
    db_session: DbSession,
    *,
    audit_correlation_id: str,
) -> WorkspaceContext | None:
    """Resolve the ``/w/<slug>/...`` path to a :class:`WorkspaceContext`.

    Returns ``None`` on every rejection path — slug not a valid URL
    shape, slug regex/reserved-list miss, slug not in the DB,
    actor missing, bearer-token workspace mismatch, or no
    ``user_workspace`` row. The caller turns that ``None`` into a
    constant-time 404.

    **Timing equalisation** (§15). The slug-miss branch does a dummy
    ``user_workspace`` read (against
    :data:`_TIMING_DUMMY_WORKSPACE_ID`) so its wall-clock matches the
    membership-miss branch, which does one workspace lookup plus one
    membership lookup. The dummy read is fed the well-known sentinel
    id so it cannot accidentally resolve a real row — and an attacker
    cannot probe its existence via this surface either.
    """
    slug = _parse_scoped_path(request_path)
    if slug is None:
        return None
    try:
        validate_slug(slug)
    except InvalidSlug:
        # Pay the dummy read so a malformed/reserved slug looks the
        # same on the wire as "real slug, actor not a member".
        _timing_equalise_dummy_read(db_session)
        return None

    # justification: workspace is the tenancy anchor and is deliberately
    # NOT registered as workspace-scoped — it must be readable before
    # a :class:`WorkspaceContext` exists (see
    # ``app/adapters/db/workspace/__init__.py``). The explicit ``slug``
    # predicate on the SELECT is the authorisation, not the tenant
    # filter.
    ws = db_session.scalars(
        select(Workspace).where(Workspace.slug == slug).limit(1)
    ).first()
    if ws is None:
        _timing_equalise_dummy_read(db_session)
        return None

    # An anonymous caller against a known workspace is rejected the
    # same way a non-member is. Still pay the membership read so the
    # two timings overlap.
    if actor is None:
        _timing_equalise_dummy_read(db_session, workspace_id=ws.id)
        return None

    # Bearer-token bound to a different workspace: §03 "API tokens"
    # "A scoped token used against the wrong workspace returns 404
    # workspace_out_of_scope" — the router enforces this at the seam,
    # but we also enforce it here so a misused token never gets past
    # the middleware with a live ctx.
    if actor.workspace_id is not None and actor.workspace_id != ws.id:
        _timing_equalise_dummy_read(db_session, workspace_id=ws.id)
        return None

    # ``user_workspace`` is workspace-scoped; callers outside a live
    # :class:`WorkspaceContext` must bypass the tenant filter with an
    # explicit justification. The SELECT's ``workspace_id`` predicate
    # is the authorisation; the filter would otherwise refuse the read.
    # justification: pre-context membership probe — the middleware is
    # resolving the caller's ctx and has nothing to install yet.
    with tenant_agnostic():
        membership = db_session.scalars(
            select(UserWorkspace)
            .where(
                UserWorkspace.workspace_id == ws.id,
                UserWorkspace.user_id == actor.user_id,
            )
            .limit(1)
        ).first()
    if membership is None:
        return None

    # Owners-group + role-grant reads. ``permission_group_member``,
    # ``permission_group``, and ``role_grant`` are all workspace-
    # scoped; the middleware has no ctx to install yet.
    # justification: pre-context authority probe — resolving the
    # caller's authority to build the :class:`WorkspaceContext`.
    with tenant_agnostic():
        is_owner = is_owner_member(
            db_session,
            workspace_id=ws.id,
            user_id=actor.user_id,
        )
        grant_role = _derive_actor_grant_role(
            db_session,
            workspace_id=ws.id,
            user_id=actor.user_id,
            is_owner=is_owner,
        )

    return WorkspaceContext(
        workspace_id=ws.id,
        workspace_slug=slug,
        actor_id=actor.user_id,
        actor_kind=actor.kind,
        actor_grant_role=grant_role,
        actor_was_owner_member=is_owner,
        audit_correlation_id=audit_correlation_id,
    )


def _timing_equalise_dummy_read(
    db_session: DbSession, *, workspace_id: str | None = None
) -> None:
    """Emit one ``user_workspace`` read for timing parity.

    The slug-miss + anonymous branches would otherwise finish in one
    (workspace) lookup while the membership-miss branch pays two
    (workspace + user_workspace). Paying a single sentinel read here
    closes the gap. We scope the predicate to the well-known
    :data:`_TIMING_DUMMY_WORKSPACE_ID` / :data:`_TIMING_DUMMY_USER_ID`
    pair — the row never exists, the read always returns ``None``,
    and an attacker cannot probe its existence via this surface.

    ``workspace_id`` can be overridden when a known workspace is
    already in scope (the "anonymous on a real slug" and "bearer
    mismatch on a real slug" branches) so the predicate shape mirrors
    the happy path's exact SQL.
    """
    probe_workspace_id = (
        workspace_id if workspace_id is not None else _TIMING_DUMMY_WORKSPACE_ID
    )
    # Sentinel read for wall-clock parity with the membership path.
    # justification: timing-equalisation — predicate is explicit and
    # the row will never exist; we need the lookup cost, not semantics.
    with tenant_agnostic():
        db_session.scalars(
            select(UserWorkspace)
            .where(
                UserWorkspace.workspace_id == probe_workspace_id,
                UserWorkspace.user_id == _TIMING_DUMMY_USER_ID,
            )
            .limit(1)
        ).first()


# ---------------------------------------------------------------------------
# Phase-0 stub (cd-iwsv): retained behind the settings flag
# ---------------------------------------------------------------------------


def _phase0_stub_context(
    request: Request,
    slug: str,
    correlation_id: str,
) -> WorkspaceContext | None:
    """Build a stubbed :class:`WorkspaceContext` from the test headers.

    Returns ``None`` when the ``X-Test-Workspace-Id`` header is
    missing — callers treat that as a 404 (indistinguishable from
    any other rejection).
    """
    workspace_id = request.headers.get(TEST_WORKSPACE_ID_HEADER)
    if workspace_id is None:
        return None
    actor_id = request.headers.get(TEST_ACTOR_ID_HEADER) or new_ulid()
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_tenancy_event(
    *,
    slug: str | None,
    workspace_id: str | None,
    actor_id: str | None,
    actor_kind: str | None,
    token_id: str | None,
    session_id: str | None,
    correlation_id: str,
    skip_path: bool,
    outcome: str,
) -> None:
    """Emit the structured ``tenancy.context`` log line.

    One call site per middleware branch — extracted so the log shape
    can only drift in one place. Aggregators filter on
    ``event=tenancy.context`` and can pivot on ``outcome`` and
    ``skip_path`` to separate bare-host traffic from scoped traffic,
    successful resolutions from 404s.
    """
    _log.info(
        "tenancy.context",
        extra={
            "event": "tenancy.context",
            "slug": slug,
            "workspace_id": workspace_id,
            "actor_id": actor_id,
            "actor_kind": actor_kind,
            "token_id": token_id,
            "session_id": session_id,
            "correlation_id": correlation_id,
            "skip_path": skip_path,
            "outcome": outcome,
        },
    )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class WorkspaceContextMiddleware(BaseHTTPMiddleware):
    """Resolve ``/w/<slug>/...`` to a :class:`WorkspaceContext`.

    Binds the context via :func:`app.tenancy.current.set_current` for
    the downstream handler and guarantees cleanup (``reset_current``)
    in a ``finally`` so a crashed handler cannot leak tenancy state
    into the next request served by the same worker task.

    The middleware opens a :class:`~app.adapters.db.session.UnitOfWorkImpl`
    per scoped request to run the resolver. Each UoW commits on a
    clean resolution and rolls back on an exception. The
    ``last_seen_at`` bump / session-refresh audit writes produced by
    :func:`app.auth.session.validate` need this UoW to land; the
    downstream handler opens its own UoW via
    :func:`app.api.deps.db_session` for its own business logic.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or new_ulid()

        # 1) Bare-host skip paths (health, signup, static, docs, ...)
        #    and requests that aren't scoped ``/w/<slug>/...`` at all.
        if (
            _is_skip_path(path)
            or _is_bare_w_path(path)
            or _parse_scoped_path(path) is None
        ):
            _log_tenancy_event(
                slug=None,
                workspace_id=None,
                actor_id=None,
                actor_kind=None,
                token_id=None,
                session_id=None,
                correlation_id=correlation_id,
                skip_path=True,
                outcome="skipped",
            )
            response = await call_next(request)
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response

        # 2) Scoped request — build the context or 404.
        settings = get_settings()
        ctx, outcome = self._resolve_context(request, settings, correlation_id)

        if ctx is None:
            _log_tenancy_event(
                slug=_parse_scoped_path(path),
                workspace_id=None,
                actor_id=None,
                actor_kind=None,
                token_id=None,
                session_id=None,
                correlation_id=correlation_id,
                skip_path=False,
                outcome=outcome,
            )
            response = _not_found()
            response.headers[CORRELATION_ID_HEADER] = correlation_id
            return response

        _log_tenancy_event(
            slug=ctx.workspace_slug,
            workspace_id=ctx.workspace_id,
            actor_id=ctx.actor_id,
            actor_kind=ctx.actor_kind,
            token_id=None,
            session_id=None,
            correlation_id=correlation_id,
            skip_path=False,
            outcome=outcome,
        )

        token = set_current(ctx)
        try:
            response = await call_next(request)
        finally:
            # Always restore — even if the downstream handler raised —
            # so the ContextVar does not leak into the next request
            # served by the same worker task.
            reset_current(token)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response

    def _resolve_context(
        self,
        request: Request,
        settings: Settings,
        correlation_id: str,
    ) -> tuple[WorkspaceContext | None, str]:
        """Run the Phase-0 stub OR the real resolver and report an outcome tag.

        Split out of :meth:`dispatch` so the DB session + UoW lifecycle
        lives in one place and :meth:`dispatch` stays a flat switchboard
        between "skip / resolved / 404".

        The outcome tag is purely for the log line — it has no effect
        on the wire response, which stays the shared 404 envelope on
        every rejection.
        """
        path = request.url.path
        slug = _parse_scoped_path(path)
        if slug is None:
            # Upstream already guarded; defensive only.
            return None, "not_scoped"

        # Phase-0 stub — guarded by the env flag. When off, the
        # ``X-Test-Workspace-Id`` header is simply ignored and the real
        # resolver runs against the DB.
        if settings.phase0_stub_enabled:
            try:
                validate_slug(slug)
            except InvalidSlug:
                return None, "slug_invalid_stub"
            ctx = _phase0_stub_context(request, slug, correlation_id)
            if ctx is None:
                return None, "stub_header_missing"
            return ctx, "stub_resolved"

        # Real resolver path. Open a UoW so domain calls that flush
        # audit rows (``session.refreshed`` on sliding refresh, the
        # ``last_used_at`` bump on a live token) actually land.
        with make_uow() as db_session:
            assert isinstance(db_session, DbSession)
            actor = resolve_actor(request, db_session, settings)
            ctx = resolve_workspace(
                path,
                actor,
                db_session,
                audit_correlation_id=correlation_id,
            )
            if ctx is None:
                if actor is None:
                    return None, "anon_or_unresolved"
                return None, "membership_miss"
            return ctx, "resolved"
