"""Permissions HTTP router — ``/permissions/{action_catalog,resolved}``.

Spec §12 "Users / work roles / settings":

```
GET    /permissions/action_catalog
GET    /permissions/resolved   ?user_id=…&action_key=…&scope_kind=…&scope_id=…
GET    /permissions/resolved/self?action_key=…&scope_kind=…&scope_id=…
```

Mounted inside the ``/w/<slug>/api/v1`` tree by the app factory.
Both routes are read-only.

* ``/permissions/action_catalog`` is the static, compile-time
  catalogue of every action key the resolver knows about. Workers,
  managers, and owners all need to read it (the agent UI renders
  per-action capabilities), so the gate is ``scope.view``
  (``default_allow=("owners", "managers", "all_workers",
  "all_clients")``).
* ``/permissions/resolved`` answers "would user U be allowed action A
  on scope S?" by walking the resolver in a non-raising mode and
  returning the structured decision. The route is governance-sensitive:
  it can reveal who has access to what, so it gates on ``audit_log.view``
  (default-allow owners + managers, ``root_protected_deny``).
* ``/permissions/resolved/self`` resolves the current actor's own
  permission and is the route-guard seam. It is intentionally not gated
  by ``audit_log.view``; the workspace context already authenticates the
  actor, and the endpoint never accepts an arbitrary ``user_id``.

See ``docs/specs/02-domain-model.md`` §"Permission resolution" and
``docs/specs/05-employees-and-roles.md`` §"Action catalog".
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.authz import (
    EmptyPermissionRuleRepository,
    InvalidScope,
    PermissionRuleRepository,
    UnknownActionKey,
    is_member_of,
    is_owner_member,
)
from app.authz.dep import Permission
from app.authz.membership import UnknownSystemGroup
from app.domain.identity._action_catalog import ACTION_CATALOG, ActionSpec
from app.tenancy import WorkspaceContext

__all__ = [
    "ActionCatalogEntryResponse",
    "ActionCatalogResponse",
    "ResolvedPermissionResponse",
    "build_permissions_router",
    "router",
]


_Ctx = Annotated[WorkspaceContext, Depends(current_workspace_context)]
_Db = Annotated[Session, Depends(db_session)]


ResolvedEffectLiteral = Literal["allow", "deny"]
SourceLayerLiteral = Literal["root_only", "default_allow", "no_match"]


# ---------------------------------------------------------------------------
# Wire-facing shapes
# ---------------------------------------------------------------------------


class ActionCatalogEntryResponse(BaseModel):
    """One entry in :class:`ActionCatalogResponse`.

    Mirrors :class:`app.domain.identity._action_catalog.ActionSpec` on
    the wire. ``valid_scope_kinds`` and ``default_allow`` are emitted
    as lists (pydantic serialises them as JSON arrays); the source
    catalog stores them as tuples for immutability.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    valid_scope_kinds: list[str]
    default_allow: list[str]
    root_only: bool
    root_protected_deny: bool


class ActionCatalogResponse(BaseModel):
    """Response shape for ``GET /permissions/action_catalog``.

    The catalog is static (compiled in at process start), so the
    response carries every entry in one shot — no pagination needed.
    A ``count`` field surfaces alongside ``entries`` for UI affordances
    that want to render a header without iterating the list.
    """

    entries: list[ActionCatalogEntryResponse]
    count: int


class ResolvedPermissionResponse(BaseModel):
    """Response shape for ``GET /permissions/resolved``.

    ``effect`` is the resolver's verdict; ``source_layer`` names which
    step (§02 "Permission resolution") produced it:

    * ``root_only`` — the action is root-only and the verdict is the
      owners-membership lookup.
    * ``default_allow`` — the resolver fell through to the action's
      ``default_allow`` list (the v1 hot path while the
      ``permission_rule`` table is absent).
    * ``no_match`` — neither rules nor defaults matched; the deny is
      the spec's "deny by default" §02 #6.

    ``matched_groups`` carries every system-group slug the user is a
    member of and that contributed to the verdict (empty when the
    user is not a member of any system group, or when the verdict
    was decided at the root-only step).

    ``source_rule_id`` is reserved for the cd-dzp follow-up that
    ships the ``permission_rule`` table; it is always ``None`` in v1
    because the empty rule repo never returns a rule.
    """

    effect: ResolvedEffectLiteral
    source_layer: SourceLayerLiteral
    source_rule_id: str | None
    matched_groups: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec_to_response(spec: ActionSpec) -> ActionCatalogEntryResponse:
    return ActionCatalogEntryResponse(
        key=spec.key,
        valid_scope_kinds=list(spec.valid_scope_kinds),
        default_allow=list(spec.default_allow),
        root_only=spec.root_only,
        root_protected_deny=spec.root_protected_deny,
    )


def _resolve_decision(
    session: Session,
    ctx: WorkspaceContext,
    *,
    user_id: str,
    action_key: str,
    scope_kind: str,
    scope_id: str,
    rule_repo: PermissionRuleRepository | None = None,
) -> ResolvedPermissionResponse:
    """Run the resolver and project the decision onto the wire shape.

    The resolver itself raises on deny; we re-implement the trace in
    parallel so the wire shape carries the structured verdict the SPA
    + admin UI need ("why is this denied?"). Until the ``permission_rule``
    table ships, the trace is small enough to inline; the cd-dzp
    follow-up replaces this body with a richer walker that surfaces
    matched rule rows.
    """
    spec = ACTION_CATALOG.get(action_key)
    if spec is None:
        raise UnknownActionKey(action_key)
    if scope_kind not in spec.valid_scope_kinds:
        raise InvalidScope(
            f"action {action_key!r} does not accept scope_kind={scope_kind!r}"
        )

    repo = rule_repo if rule_repo is not None else EmptyPermissionRuleRepository()
    # ``ctx.workspace_id`` pins the workspace anchor for owners /
    # default-allow lookups; the user_id under inspection differs
    # from ``ctx.actor_id`` (the actor is the *querier*, not the
    # subject of the resolved decision).
    is_owner = is_owner_member(
        session,
        workspace_id=ctx.workspace_id,
        user_id=user_id,
    )

    # Step 2 — root-only gate.
    if spec.root_only:
        return ResolvedPermissionResponse(
            effect="allow" if is_owner else "deny",
            source_layer="root_only",
            source_rule_id=None,
            matched_groups=["owners"] if is_owner else [],
        )

    # Step 3 + 4 — scope walk. v1 ships an empty repo so the walk is
    # always a fall-through; the cd-dzp follow-up replaces this with
    # the real walker.
    scope_chain: tuple[tuple[str, str], ...] = (
        (("property", scope_id), ("workspace", ctx.workspace_id))
        if scope_kind == "property"
        else ((scope_kind, scope_id),)
    )
    rules = repo.rules_for(
        session,
        workspace_id=ctx.workspace_id,
        user_id=user_id,
        action_key=action_key,
        scope_kind=scope_kind,
        scope_id=scope_id,
        ancestor_scope_ids=scope_chain,
    )
    # When the repo returns rows (future), surface the first matching
    # rule's id. Today this iterates an empty tuple.
    for rule in rules:
        if rule.effect == "deny":
            if spec.root_protected_deny and is_owner:
                continue
            return ResolvedPermissionResponse(
                effect="deny",
                source_layer="default_allow",  # rule-driven deny lands here in v2
                source_rule_id=rule.rule_id,
                matched_groups=[],
            )
        return ResolvedPermissionResponse(
            effect="allow",
            source_layer="default_allow",
            source_rule_id=rule.rule_id,
            matched_groups=[],
        )

    # Step 5 — default_allow fallback.
    matched: list[str] = []
    for slug in spec.default_allow:
        try:
            if is_member_of(
                session,
                workspace_id=ctx.workspace_id,
                user_id=user_id,
                group_slug=slug,
            ):
                matched.append(slug)
        except UnknownSystemGroup as exc:
            # A catalog entry referencing an unknown slug is a spec
            # drift; the resolver normally raises CatalogDrift here,
            # but for the read-only ``/resolved`` surface we surface
            # the offending slug verbatim so an operator can trace
            # the drift without reading process logs.
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "catalog_drift",
                    "message": (
                        f"action {action_key!r} lists unknown default_allow "
                        f"group {slug!r}"
                    ),
                },
            ) from exc
    if matched:
        return ResolvedPermissionResponse(
            effect="allow",
            source_layer="default_allow",
            source_rule_id=None,
            matched_groups=matched,
        )

    # Step 6 — no match → deny.
    return ResolvedPermissionResponse(
        effect="deny",
        source_layer="no_match",
        source_rule_id=None,
        matched_groups=[],
    )


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


_UserIdQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=64,
        description="User id whose permissions should be resolved.",
    ),
]
_ActionKeyQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=128,
        description="Action key from §05 to resolve against.",
    ),
]
_ScopeKindQuery = Annotated[
    Literal["workspace", "property", "organization", "deployment"],
    Query(description="Scope kind on which to evaluate the action."),
]
_ScopeIdQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=64,
        description=(
            "Scope id matching ``scope_kind``. For ``workspace`` this is "
            "typically the caller's workspace id; for ``property`` it is "
            "the property ULID."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_permissions_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` wired for permission introspection."""
    api = APIRouter(prefix="/permissions", tags=["identity", "permissions"])

    catalog_gate = Depends(Permission("scope.view", scope_kind="workspace"))
    resolve_gate = Depends(Permission("audit_log.view", scope_kind="workspace"))

    @api.get(
        "/action_catalog",
        response_model=ActionCatalogResponse,
        operation_id="permissions.action_catalog",
        summary="Read the static action catalog (§05)",
        dependencies=[catalog_gate],
        openapi_extra={
            "x-cli": {
                "group": "permissions",
                "verb": "action-catalog",
                "summary": "Read the action catalog",
                "mutates": False,
            },
        },
    )
    def action_catalog() -> ActionCatalogResponse:
        """Return every :class:`ActionSpec` from the compile-time catalog.

        The catalog is static across the process lifetime; clients can
        cache the response indefinitely (no ETag / max-age headers in
        v1 — the route is cheap and the catalog only changes on a
        deploy).
        """
        entries = [_spec_to_response(spec) for spec in ACTION_CATALOG.values()]
        return ActionCatalogResponse(entries=entries, count=len(entries))

    @api.get(
        "/resolved",
        response_model=ResolvedPermissionResponse,
        operation_id="permissions.resolved",
        summary='"Would user U be allowed action A on scope S?"',
        dependencies=[resolve_gate],
        openapi_extra={
            "x-cli": {
                "group": "permissions",
                "verb": "resolve",
                "summary": "Resolve a permission for a user / action / scope",
                "mutates": False,
            },
        },
    )
    def resolved(
        ctx: _Ctx,
        session: _Db,
        user_id: _UserIdQuery,
        action_key: _ActionKeyQuery,
        scope_kind: _ScopeKindQuery,
        scope_id: _ScopeIdQuery,
    ) -> ResolvedPermissionResponse:
        """Run the resolver and return the structured verdict.

        The resolver itself raises on deny; we re-walk the same
        decision tree non-raising and emit the structured payload the
        SPA + admin UI consume.
        """
        try:
            return _resolve_decision(
                session,
                ctx,
                user_id=user_id,
                action_key=action_key,
                scope_kind=scope_kind,
                scope_id=scope_id,
            )
        except UnknownActionKey as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "unknown_action_key",
                    "action_key": action_key,
                    "message": str(exc),
                },
            ) from exc
        except InvalidScope as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_scope_kind",
                    "action_key": action_key,
                    "message": str(exc),
                },
            ) from exc

    @api.get(
        "/resolved/self",
        response_model=ResolvedPermissionResponse,
        operation_id="permissions.resolved_self",
        summary='"Would the current actor be allowed action A on scope S?"',
    )
    def resolved_self(
        ctx: _Ctx,
        session: _Db,
        action_key: _ActionKeyQuery,
        scope_kind: _ScopeKindQuery,
        scope_id: _ScopeIdQuery,
    ) -> ResolvedPermissionResponse:
        """Resolve the current actor's own permission for route guards.

        This is deliberately narrower than ``/permissions/resolved``:
        callers cannot inspect another user's permissions, so the route
        does not require the governance-only ``audit_log.view`` action.
        """
        try:
            return _resolve_decision(
                session,
                ctx,
                user_id=ctx.actor_id,
                action_key=action_key,
                scope_kind=scope_kind,
                scope_id=scope_id,
            )
        except UnknownActionKey as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "unknown_action_key",
                    "action_key": action_key,
                    "message": str(exc),
                },
            ) from exc
        except InvalidScope as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_scope_kind",
                    "action_key": action_key,
                    "message": str(exc),
                },
            ) from exc

    return api


# Module-level router for the v1 app factory's eager import.
router = build_permissions_router()
