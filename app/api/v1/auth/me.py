"""``GET /api/v1/auth/me`` — identity bootstrap for the SPA.

Bare-host route, tenant-agnostic (runs before a workspace is picked).
The SPA's :mod:`authStore` hits this on every load to decide whether
the cookied user is still live; a 401 drops the store into the
unauthenticated state and bounces the visitor to ``/login``, a 200
seeds ``useAuth()`` with the user + their available workspaces.

Response shape matches :class:`AuthMe` in
``app/web/src/auth/types.ts``. The :class:`AvailableWorkspace` inner
shape matches ``app/web/src/types/auth.ts`` and surfaces every
workspace the caller has a :class:`RoleGrant` on.

Owner detection: users who hold a ``manager`` surface grant on any
workspace, or who are a member of any ``owners`` permission group,
map their grant as ``manager`` in the response (the governance
anchor is already encoded by the ``manager`` surface, per §03 —
``owner`` is no longer a grant-role value in v1).

**Defaults on absent columns.** The v1 :class:`Workspace` row does
not yet carry ``timezone`` / ``default_currency`` / ``default_country``
/ ``default_locale`` (cd-n6p adds them). Until then we emit sensible
defaults so the SPA's typed ``Workspace`` contract is honoured
without a brittle ``null`` field. This is documented as a known
drift on cd-h2t0; the defaults match the deployment's locale bias
and can be overridden once the columns land.

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions",
``docs/specs/14-web-frontend.md`` §"Workspace selector", and
``docs/specs/12-rest-api.md`` §"Auth".
"""

from __future__ import annotations

import logging
from datetime import UTC
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.api.deps import db_session
from app.auth import session as auth_session
from app.tenancy import tenant_agnostic

__all__ = [
    "AuthMeResponse",
    "AvailableWorkspaceResponse",
    "WorkspaceSummary",
    "WorkspaceSwitcherEntry",
    "build_me_router",
    "build_me_workspaces_router",
]


_log = logging.getLogger(__name__)

_Db = Annotated[Session, Depends(db_session)]


# Defaults used until the v1 workspace row carries the real columns
# (cd-n6p). Kept here rather than in settings because they are
# serialisation defaults, not deploy-tunable policy.
_DEFAULT_TIMEZONE: str = "UTC"
_DEFAULT_CURRENCY: str = "EUR"
_DEFAULT_COUNTRY: str = "FR"
_DEFAULT_LOCALE: str = "en"


class WorkspaceSummary(BaseModel):
    """Subset of :class:`Workspace` surfaced in the ``/auth/me`` envelope.

    Mirrors ``app/web/src/types/core.ts`` ``Workspace``. ``id`` carries
    the URL slug rather than the DB ULID — the SPA's
    :func:`slugFor` helper currently reads the ``id`` field as the
    URL component, and the workspace chooser builds links as
    ``/w/{id}/...``. Returning the slug here keeps the chooser
    working without a follow-up shape migration on the frontend.
    """

    id: str = Field(..., description="Workspace URL slug.")
    name: str
    timezone: str
    default_currency: str
    default_country: str
    default_locale: str


class AvailableWorkspaceResponse(BaseModel):
    """One entry of :attr:`AuthMeResponse.available_workspaces`.

    ``grant_role`` is the caller's highest-privilege surface grant on
    this workspace. ``binding_org_id`` and ``source`` are carried for
    type-parity with the SPA contract; v1 always emits
    ``source='workspace_grant'`` and ``binding_org_id=None`` (the org
    binding and non-workspace-scoped grants land in follow-ups).
    """

    workspace: WorkspaceSummary
    grant_role: str | None
    binding_org_id: str | None
    source: str


class AuthMeResponse(BaseModel):
    """Body of ``GET /api/v1/auth/me``.

    Matches :class:`AuthMe` in ``app/web/src/auth/types.ts``. The SPA
    expects a flat envelope — no nested ``user`` — because the field
    set is small enough to inline.
    """

    user_id: str
    display_name: str
    email: str
    available_workspaces: list[AvailableWorkspaceResponse]
    current_workspace_id: str | None


class WorkspaceSwitcherEntry(BaseModel):
    """One entry of ``GET /api/v1/me/workspaces``.

    Dedicated switcher payload, distinct from
    :class:`AvailableWorkspaceResponse` so the surface can evolve
    independently. The richer shape carries:

    * ``workspace_id`` — the DB ULID (the slug is also surfaced for
      URL-building convenience).
    * ``slug`` — URL component for the workspace selector links.
    * ``name`` — display name.
    * ``current_role`` — caller's resolved surface role on the workspace
      (``manager`` collapses owners-group members per §03; the value is
      ``"owner"`` only when the caller still carries the legacy ``owner``
      grant_role, which v1 no longer mints — see :func:`AuthMeResponse`'s
      handling).
    * ``last_seen_at`` — ISO-8601 UTC timestamp from the most recent
      :class:`Session` row scoped to ``(user_id, workspace_id)``;
      ``None`` when the user has never picked the workspace yet.
    * ``settings_override`` — a light projection of the workspace's
      :attr:`Workspace.settings_json` so the SPA's switcher can render
      per-workspace branding without a follow-up settings round-trip.
      An empty dict on a workspace with no overrides — never ``None``.

    Spec §12 "Auth": ``GET /api/v1/me/workspaces`` (cd-y5z3).
    """

    workspace_id: str
    slug: str
    name: str
    current_role: str | None
    last_seen_at: str | None
    settings_override: dict[str, Any]


def _client_headers(request: Request) -> tuple[str, str]:
    """Return ``(ua, accept_language)`` for :func:`auth_session.validate`.

    Kept together because the fingerprint gate reads both. Empty
    strings are fine — :func:`validate` skips the fingerprint check
    when the caller supplies neither header.
    """
    return (
        request.headers.get("user-agent", ""),
        request.headers.get("accept-language", ""),
    )


def _load_available_workspaces(
    session: Session, *, user_id: str
) -> list[AvailableWorkspaceResponse]:
    """Return every workspace the user has a :class:`RoleGrant` on.

    Collapses multiple grants on the same workspace onto the
    highest-privilege one (manager > worker > client > guest). Users
    in an ``owners`` permission group are surfaced as ``manager`` —
    §03 collapses governance onto the manager surface in v1.
    """
    # justification: ``role_grant`` and ``workspace`` are tenancy
    # anchors themselves; this lookup runs before a WorkspaceContext
    # exists (auth/me is bare-host), so the ORM tenant filter has
    # nothing to apply.
    with tenant_agnostic():
        rows = session.execute(
            select(RoleGrant, Workspace)
            .join(Workspace, Workspace.id == RoleGrant.workspace_id)
            .where(RoleGrant.user_id == user_id)
        ).all()

        owners_workspace_ids = set(
            session.scalars(
                select(PermissionGroup.workspace_id)
                .join(
                    PermissionGroupMember,
                    PermissionGroupMember.group_id == PermissionGroup.id,
                )
                .where(PermissionGroupMember.user_id == user_id)
                .where(PermissionGroup.slug == "owners")
            ).all()
        )

    # Surface-role precedence (highest → lowest). ``None`` sorts
    # last so an unrecognised value never shadows a known grant.
    _RANK: dict[str, int] = {
        "manager": 0,
        "admin": 0,
        "worker": 1,
        "client": 2,
        "guest": 3,
    }

    best: dict[str, tuple[int, RoleGrant, Workspace]] = {}
    for grant, workspace in rows:
        rank = _RANK.get(grant.grant_role, 99)
        existing = best.get(workspace.id)
        if existing is None or rank < existing[0]:
            best[workspace.id] = (rank, grant, workspace)

    out: list[AvailableWorkspaceResponse] = []
    for ws_id, (_rank, grant, workspace) in best.items():
        role = grant.grant_role
        if ws_id in owners_workspace_ids and role != "manager":
            # Owners-group member without a manager surface grant is
            # still governance-authoritative; surface as manager so
            # the SPA routes to the manager landing.
            role = "manager"
        out.append(
            AvailableWorkspaceResponse(
                workspace=WorkspaceSummary(
                    id=workspace.slug,
                    name=workspace.name,
                    timezone=_DEFAULT_TIMEZONE,
                    default_currency=_DEFAULT_CURRENCY,
                    default_country=_DEFAULT_COUNTRY,
                    default_locale=_DEFAULT_LOCALE,
                ),
                grant_role=role,
                binding_org_id=None,
                source="workspace_grant",
            )
        )
    return out


def _load_switcher_entries(
    session: Session, *, user_id: str
) -> list[WorkspaceSwitcherEntry]:
    """Return one :class:`WorkspaceSwitcherEntry` per workspace ``user_id`` is in.

    Drives ``GET /api/v1/me/workspaces``. Joins the derived
    :class:`UserWorkspace` junction with :class:`Workspace` to get the
    slug + name + ``settings_json``; resolves ``current_role`` from the
    same precedence ladder used by :func:`_load_available_workspaces`
    (manager > worker > client > guest, with owners-group members
    surfaced as ``manager`` per §03); resolves ``last_seen_at`` from
    the most recent :class:`Session` row for the
    ``(user_id, workspace_id)`` pair.

    No PII enters the response — only workspace metadata + the caller's
    own role + their own session activity.
    """
    # justification: identity-bootstrap query; the user spans multiple
    # workspaces and the ORM tenant filter would narrow inappropriately.
    with tenant_agnostic():
        memberships = session.execute(
            select(UserWorkspace, Workspace)
            .join(Workspace, Workspace.id == UserWorkspace.workspace_id)
            .where(UserWorkspace.user_id == user_id)
            .order_by(Workspace.slug.asc())
        ).all()

        if not memberships:
            return []

        workspace_ids = [ws.id for _, ws in memberships]

        # Pre-load every grant + owners-group membership in two cheap
        # queries so the per-workspace loop below stays O(1) — avoids
        # N + 1 queries on a switcher payload that the SPA hits on
        # every load. ``RoleGrant.scope_property_id IS NULL`` filters
        # to workspace-scope grants only; property-pinned grants do
        # not promote the holder to a workspace-level role surface.
        grants = session.execute(
            select(RoleGrant.workspace_id, RoleGrant.grant_role)
            .where(RoleGrant.user_id == user_id)
            .where(RoleGrant.workspace_id.in_(workspace_ids))
            .where(RoleGrant.scope_property_id.is_(None))
        ).all()

        owners_workspace_ids = set(
            session.scalars(
                select(PermissionGroup.workspace_id)
                .join(
                    PermissionGroupMember,
                    PermissionGroupMember.group_id == PermissionGroup.id,
                )
                .where(PermissionGroupMember.user_id == user_id)
                .where(PermissionGroup.workspace_id.in_(workspace_ids))
                .where(PermissionGroup.slug == "owners")
                .where(PermissionGroup.system.is_(True))
            ).all()
        )

        # Most-recent ``last_seen_at`` per workspace — single GROUP BY
        # so the worst case is one round-trip for any number of
        # workspaces. ``Session.workspace_id`` is nullable; we filter
        # those rows out (they belong to the "no workspace picked yet"
        # state, not to any specific workspace).
        last_seen_rows = session.execute(
            select(
                SessionRow.workspace_id,
                sa_func.max(SessionRow.last_seen_at).label("last_seen_at"),
            )
            .where(SessionRow.user_id == user_id)
            .where(SessionRow.workspace_id.in_(workspace_ids))
            .group_by(SessionRow.workspace_id)
        ).all()

    last_seen_by_ws: dict[str, str] = {}
    for row in last_seen_rows:
        ws_id = row[0]
        last_seen = row[1]
        if ws_id is None or last_seen is None:
            continue
        # SQLite drops tzinfo on a ``DateTime(timezone=True)`` column
        # roundtrip; Postgres preserves it. Force UTC so the emitted
        # ISO-8601 string always carries an explicit ``+00:00`` /
        # ``Z`` offset (§02 "Time is UTC at rest, local for display").
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=UTC)
        last_seen_by_ws[ws_id] = last_seen.isoformat()

    # Surface-role precedence (highest → lowest). ``None`` sorts last
    # so an unrecognised value never shadows a known grant. Mirrors
    # the table in :func:`_load_available_workspaces`.
    _RANK: dict[str, int] = {
        "manager": 0,
        "admin": 0,
        "worker": 1,
        "client": 2,
        "guest": 3,
    }
    best_role_by_ws: dict[str, tuple[int, str]] = {}
    for ws_id, role in grants:
        rank = _RANK.get(role, 99)
        existing = best_role_by_ws.get(ws_id)
        if existing is None or rank < existing[0]:
            best_role_by_ws[ws_id] = (rank, role)

    out: list[WorkspaceSwitcherEntry] = []
    for _, workspace in memberships:
        rank_role = best_role_by_ws.get(workspace.id)
        role = rank_role[1] if rank_role is not None else None
        if workspace.id in owners_workspace_ids and role != "manager":
            # Owners-group member without a manager surface grant is
            # still governance-authoritative; surface as manager so
            # the SPA routes to the manager landing.
            role = "manager"

        # ``settings_json`` is the workspace's flat dotted-key map. We
        # pass it through verbatim so the switcher can render per-
        # workspace overrides without a follow-up call. The column is
        # NOT NULL so the dict is always present; copy defensively so
        # a downstream mutation can't bleed back into the ORM-managed
        # row.
        settings_override = dict(workspace.settings_json or {})

        out.append(
            WorkspaceSwitcherEntry(
                workspace_id=workspace.id,
                slug=workspace.slug,
                name=workspace.name,
                current_role=role,
                last_seen_at=last_seen_by_ws.get(workspace.id),
                settings_override=settings_override,
            )
        )
    return out


def build_me_router() -> APIRouter:
    """Return the router that serves ``GET /api/v1/auth/me``.

    Built as a factory (matching the other auth-router builders in
    this package) so the app factory keeps a uniform wiring seam and
    tests can mount the endpoint against an isolated FastAPI
    instance.
    """
    # Tags: ``identity`` surfaces every identity-adjacent operation
    # under one OpenAPI section (spec §01 context map + §12 Auth);
    # ``auth`` stays for fine-grained client filtering.
    router = APIRouter(prefix="/auth", tags=["identity", "auth"])

    @router.get(
        "/me",
        response_model=AuthMeResponse,
        operation_id="auth.me.get",
        summary="Return the authenticated user + their available workspaces",
        openapi_extra={
            # Singleton endpoint: "whoami" is the spec's verb (§13
            # ``crewday auth whoami``). The bare heuristic would
            # classify a GET without a trailing ``{id}`` as ``list``;
            # pin the CLI surface so the committed ``_surface.json``
            # does not drift on the heuristic alone.
            "x-cli": {
                "group": "auth",
                "verb": "whoami",
                "summary": "Show the authenticated user + their workspaces",
                "mutates": False,
            },
        },
    )
    def get_me(
        request: Request,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias="crewday_session"),
        ] = None,
    ) -> AuthMeResponse:
        """Validate the session cookie, hydrate user + workspaces.

        Returns 401 when the cookie is absent or rejected by
        :func:`auth_session.validate`. The SPA's
        :mod:`auth.onUnauthorized` seam routes every 401 to the store
        reset + login bounce.
        """
        cookie_value = session_cookie_primary or session_cookie_dev
        if not cookie_value:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_required"},
            )
        ua, accept_language = _client_headers(request)
        try:
            user_id = auth_session.validate(
                session,
                cookie_value=cookie_value,
                ua=ua,
                accept_language=accept_language,
            )
        except (auth_session.SessionInvalid, auth_session.SessionExpired) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_invalid"},
            ) from exc

        with tenant_agnostic():
            user = session.get(User, user_id)
        if user is None:
            # Session row references a user that was hard-deleted
            # between validate and this lookup. Treat as unauth.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_invalid"},
            )

        return AuthMeResponse(
            user_id=user.id,
            display_name=user.display_name,
            email=user.email,
            available_workspaces=_load_available_workspaces(session, user_id=user.id),
            current_workspace_id=None,
        )

    return router


def build_me_workspaces_router() -> APIRouter:
    """Return the router that serves ``GET /api/v1/me/workspaces``.

    Bare-host (tenant-agnostic) — the SPA hits this from the workspace
    switcher to populate the picker. Distinct from
    ``GET /auth/me``'s ``available_workspaces`` because the switcher
    needs a richer projection (``last_seen_at`` per workspace,
    ``settings_override``) that would be wasteful payload to ship on
    every authenticated load. Built as a separate router (not a
    second route on :func:`build_me_router`) because the prefix differs
    (``/me`` vs ``/auth``); both routers are mounted by the app factory
    on the bare-host ``/api/v1`` prefix.

    See ``docs/specs/12-rest-api.md`` §"Auth" — ``GET /api/v1/me/workspaces``.
    """
    # Tags: ``identity`` surfaces this under the same OpenAPI section
    # as ``/auth/me`` (spec §01 context map + §12 Auth); ``auth`` keeps
    # fine-grained client filtering symmetrical with the sibling
    # ``/auth/me`` route. ``workspaces`` is added so SPA-side filters
    # ("which endpoints power the switcher?") have a stable handle.
    router = APIRouter(prefix="/me", tags=["identity", "auth", "workspaces"])

    @router.get(
        "/workspaces",
        response_model=list[WorkspaceSwitcherEntry],
        operation_id="auth.me.workspaces.list",
        summary="Return the caller's workspaces (switcher payload)",
        openapi_extra={
            # Pin the CLI surface; the bare heuristic would classify a
            # bare GET as ``list``, which is correct here, but spelling
            # it explicitly future-proofs the committed surface JSON
            # against a heuristic change.
            "x-cli": {
                "group": "auth",
                "verb": "workspaces",
                "summary": "List the workspaces the caller can switch into",
                "mutates": False,
            },
        },
    )
    def list_my_workspaces(
        request: Request,
        session: _Db,
        session_cookie_primary: Annotated[
            str | None,
            Cookie(alias=auth_session.SESSION_COOKIE_NAME),
        ] = None,
        session_cookie_dev: Annotated[
            str | None,
            Cookie(alias="crewday_session"),
        ] = None,
    ) -> list[WorkspaceSwitcherEntry]:
        """Validate the session cookie, return the switcher payload.

        Returns 401 when the cookie is absent or rejected by
        :func:`auth_session.validate`. Returns ``[]`` when the user
        has no workspace memberships (a freshly-signed-up user before
        their first invite accept) — never 404, because the caller
        successfully authenticated; the absence of memberships is
        legitimate state, not a missing resource.
        """
        cookie_value = session_cookie_primary or session_cookie_dev
        if not cookie_value:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_required"},
            )
        ua, accept_language = _client_headers(request)
        try:
            user_id = auth_session.validate(
                session,
                cookie_value=cookie_value,
                ua=ua,
                accept_language=accept_language,
            )
        except (auth_session.SessionInvalid, auth_session.SessionExpired) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_invalid"},
            ) from exc

        with tenant_agnostic():
            user = session.get(User, user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "session_invalid"},
            )

        return _load_switcher_entries(session, user_id=user.id)

    return router
