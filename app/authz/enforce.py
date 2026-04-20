"""Permission enforcement — canonical authority check for routers.

Every protected HTTP endpoint answers the same question: *"may this
actor perform this action on this scope?"*. :func:`require` is the
one place that knows how. Routers declare the gate via the
:func:`Permission` FastAPI dependency factory; service-layer callers
(background workers, CLI, agent) call :func:`require` directly.

Resolution order — implements §02 "Permission resolution" verbatim:

1. **Action existence.** Unknown ``action_key`` → :class:`UnknownActionKey`
   (caller bug, maps to 422 ``unknown_action_key``).
2. **Scope kind validation.** ``scope_kind`` not in the action's
   ``valid_scope_kinds`` → :class:`InvalidScope` (caller bug, 422).
3. **Root-only gate.** Actions flagged ``root_only`` never flow
   through the rule walk. Members of the scope's ``owners`` group are
   allowed; everyone else is denied, regardless of any rule.
4. **Scope walk.** Ask the :class:`PermissionRuleRepository` for
   matching rules on each scope in ``[scope, … containing scopes]``
   (most-specific first). For each scope in order: any ``deny`` on
   that scope denies (the "deny within a scope beats allow within
   the same scope" rule from §02) — except on
   ``root_protected_deny`` actions where owners are immune to deny,
   so owner-targeted denies are masked before the group is tallied.
   Otherwise, any ``allow`` on that scope allows. Otherwise, walk
   continues to the next scope.
5. **Default_allow fallback.** If the walk didn't decide, the caller
   is allowed iff they are a member of any system group listed in
   ``ActionSpec.default_allow``.
6. Otherwise: :class:`PermissionDenied`.

Each deny emits one structured log line for the "who can do this?"
debug surface. The message is pure English; the decision data rides
the ``extra`` dict so log aggregators can filter without parsing.

``permission_rule`` seam — v1 reality:

The v1 schema does NOT ship the ``permission_rule`` table (deferred
per ``app.adapters.db.authz.models`` docstring). The resolver is
structured around a :class:`PermissionRuleRepository` Protocol so
the enforcement logic is complete today; the backing table lands in a
follow-up PR that provides a SQL adapter implementing the same
Protocol. Until then, callers pass
:class:`EmptyPermissionRuleRepository` (or rely on the default), the
walk reads zero rows, and the resolver falls through to
``default_allow`` immediately. **The enforcer's contract doesn't
change when the table lands** — only the adapter.

See ``docs/specs/02-domain-model.md`` §"Permission resolution" and
``docs/specs/05-employees-and-roles.md`` §"Action catalog".
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import current_workspace_context, db_session
from app.authz.membership import UnknownSystemGroup, is_member_of
from app.authz.owners import is_owner_member
from app.domain.identity._action_catalog import (
    ACTION_CATALOG,
    ActionSpec,
)
from app.tenancy import WorkspaceContext

__all__ = [
    "CatalogDrift",
    "EmptyPermissionRuleRepository",
    "InvalidScope",
    "Permission",
    "PermissionCheck",
    "PermissionDenied",
    "PermissionRuleRepository",
    "RuleEffect",
    "RuleRow",
    "UnknownActionKey",
    "require",
    "validate_catalog_integrity",
]


_log = logging.getLogger(__name__)


RuleEffect = Literal["allow", "deny"]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PermissionDenied(RuntimeError):
    """The caller was denied the requested action.

    Router code maps this to HTTP 403 ``permission_denied``. Service
    callers may re-raise or translate depending on context. The
    message carries the ``action_key`` for log legibility; structured
    fields (``action_key``, ``scope_kind``, ``scope_id``, ``actor_id``)
    ride on the log ``extra`` dict emitted by :func:`require`.
    """


class UnknownActionKey(RuntimeError):
    """The resolver was asked about an ``action_key`` not in the catalog.

    Not a permission decision — a caller bug. Router code maps to
    HTTP 422 ``unknown_action_key`` (internal misuse surfaced to the
    developer, not an auth failure).
    """


class InvalidScope(RuntimeError):
    """The action's catalog entry forbids the requested ``scope_kind``.

    Router code maps to HTTP 422 ``invalid_scope_kind``. Example:
    ``workspace.archive`` only accepts ``scope_kind='workspace'``;
    passing ``'property'`` is a caller bug, not an auth denial.
    """


class CatalogDrift(RuntimeError):
    """A ``permission_rule`` row references an ``action_key`` not in the catalog.

    Raised by :func:`validate_catalog_integrity` at application boot.
    The intent is to fail fast when the catalog shrinks (a spec edit
    removes a key) without first migrating the affected rule rows —
    a deny-by-default would otherwise silently change behaviour.
    """


# ---------------------------------------------------------------------------
# Rule-repository seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleRow:
    """Minimal projection of a ``permission_rule`` row.

    Carries only the fields the resolver needs; the full row shape
    stays in the adapter. ``subject_matches_actor`` is pre-computed
    by the repository so the resolver doesn't reach into group
    membership tables itself — a future adapter may satisfy the match
    via a SQL join, an in-memory cache, or a denormalised materialised
    view without changing this contract.
    """

    rule_id: str
    scope_kind: str
    scope_id: str
    effect: RuleEffect


class PermissionRuleRepository(Protocol):
    """Read-side Protocol for ``permission_rule`` lookups.

    Implementations fetch every active rule that *could* match a
    ``(user, action_key, scope-chain)`` triple. Subject matching
    (user-subject vs group-subject + membership expansion) happens on
    the adapter side so the resolver only sees rules that already
    apply to the actor.

    The scope chain is explicit: ``scope_kind`` + ``scope_id`` is the
    target scope; ``ancestor_scope_ids`` lists the scopes to walk in
    most-specific-first order — the target scope first, then each
    ancestor (§02 "Permission resolution" #4: `[property, workspace]`
    when ``S`` is a property, `[workspace]` when ``S`` is a
    workspace, etc.). The adapter returns rows drawn from any scope
    in that list, and the resolver walks them scope-by-scope.

    **Ordering.** Adapters MUST return rows grouped scope-by-scope in
    most-specific-first order (i.e. the order of ``ancestor_scope_ids``).
    Within a single scope the order is irrelevant — the resolver
    scans every row on a scope before deciding (§02 "Deny within a
    scope beats allow within the same scope"), so a deny and an
    allow on the same scope collapse deterministically regardless of
    adapter ordering.
    """

    def rules_for(
        self,
        session: Session,
        *,
        workspace_id: str,
        user_id: str,
        action_key: str,
        scope_kind: str,
        scope_id: str,
        ancestor_scope_ids: Sequence[tuple[str, str]],
    ) -> Sequence[RuleRow]:
        """Return active rules matching the actor on the scope chain.

        ``ancestor_scope_ids`` is ``[(kind, id), …]`` in
        most-specific-first order — the target scope first, followed
        by each containing scope (§02 "Permission resolution" #4).
        The adapter is expected to filter on active rows
        (``revoked_at IS NULL``), action-key match, and subject match
        (user-direct or group-membership-expanded).
        """
        ...


class EmptyPermissionRuleRepository:
    """No-op :class:`PermissionRuleRepository` — returns no rules.

    The v1 schema doesn't ship the ``permission_rule`` table yet, so
    the enforcer ships with this as its default repo. The effect: the
    scope-walk step of §02 "Permission resolution" is structurally
    exercised but always produces zero matches, and the resolver falls
    through to ``default_allow``. Tests pin this explicitly; the
    future SQL-backed adapter replaces it without touching
    :func:`require`.
    """

    def rules_for(
        self,
        session: Session,
        *,
        workspace_id: str,
        user_id: str,
        action_key: str,
        scope_kind: str,
        scope_id: str,
        ancestor_scope_ids: Sequence[tuple[str, str]],
    ) -> Sequence[RuleRow]:
        """Return an empty tuple — no ``permission_rule`` rows exist in v1."""
        return ()


# Process-wide default — tests override via an explicit ``rule_repo``
# kwarg, not by mutating this. Keeping it a module-level instance
# avoids allocating an empty repo per-request.
_DEFAULT_RULE_REPO: PermissionRuleRepository = EmptyPermissionRuleRepository()


# ---------------------------------------------------------------------------
# Check + resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PermissionCheck:
    """What the caller wants to do.

    Used by :func:`Permission` and the handful of service-layer
    callers that want to queue a check alongside other work. The
    resolver accepts ``(action_key, scope_kind, scope_id)`` directly
    too; the dataclass exists as a one-liner for tests and log-line
    serialisation.
    """

    action_key: str
    scope_kind: str
    scope_id: str


def _build_scope_chain(
    scope_kind: str,
    scope_id: str,
    workspace_id: str,
) -> tuple[tuple[str, str], ...]:
    """Return the most-specific-first scope chain for the walk.

    §02 "Permission resolution" #4: ``[property, workspace]`` when
    ``S`` is a property, ``[workspace]`` when ``S`` is a workspace,
    ``[organization]`` when ``S`` is an organization. ``deployment``
    has no containing scope.

    For v1 we don't yet have property-containing-workspace metadata
    in the repo (properties land with cd-i6u), so a property's
    containing workspace is assumed to be ``workspace_id`` from the
    active :class:`WorkspaceContext`. That's safe because every
    property-scope request flows through a workspace ctx; the
    property subtree always nests under its workspace.
    """
    if scope_kind == "property":
        return (
            ("property", scope_id),
            ("workspace", workspace_id),
        )
    return ((scope_kind, scope_id),)


def _group_rules_by_scope(
    rules: Sequence[RuleRow],
) -> list[tuple[tuple[str, str], list[RuleRow]]]:
    """Group ``rules`` by ``(scope_kind, scope_id)`` in emitted order.

    The adapter contract (see :class:`PermissionRuleRepository`) is
    most-specific-scope-first — within a scope the order is
    irrelevant because the resolver scans every row before making a
    decision. Rows for the same scope are expected to appear
    contiguously, but this helper is tolerant of interleaving: it
    keyed-dedups while preserving the first occurrence order of each
    scope.

    Returning a ``list[(scope_key, [row, …])]`` instead of an
    ``OrderedDict`` keeps the call site a flat ``for`` loop and makes
    the ordering guarantee visible in the type.
    """
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], list[RuleRow]] = {}
    for rule in rules:
        key = (rule.scope_kind, rule.scope_id)
        bucket = groups.get(key)
        if bucket is None:
            bucket = []
            groups[key] = bucket
            order.append(key)
        bucket.append(rule)
    return [(key, groups[key]) for key in order]


def _owner_on_scope_chain(
    session: Session,
    *,
    workspace_id: str,
    user_id: str,
    scope_chain: tuple[tuple[str, str], ...],
) -> bool:
    """Return ``True`` iff the user is an ``owners`` member of any scope on the chain.

    §02 "Permission resolution" #2 (root-only) and #3 (owners
    fast-path) both treat "owner of the workspace containing a
    property" as "owner of the property". v1 only stores owners
    membership at workspace scope (``owners@<workspace>``), so the
    check reduces to "is the user an owners member of ``workspace_id``".
    Property-scope owners groups land with a future schema update;
    the loop is kept in preparation for that so future adapters can
    extend the chain without touching callers.
    """
    for kind, _sid in scope_chain:
        if kind == "workspace" and is_owner_member(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
        ):
            return True
    return False


def _log_denied(
    *,
    action_key: str,
    scope_kind: str,
    scope_id: str,
    actor_id: str,
    workspace_id: str,
    reason: str,
) -> None:
    """Emit one structured warning per denied check.

    Keeping the message terse and pushing decision data onto
    ``extra`` lets aggregators filter on ``event=authz.denied``
    without parsing. ``reason`` is an internal hint (``root_only``,
    ``rule_deny``, ``no_match``) — not surfaced to the HTTP client.
    """
    _log.warning(
        "authz.denied",
        extra={
            "event": "authz.denied",
            "action_key": action_key,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "actor_id": actor_id,
            "workspace_id": workspace_id,
            "reason": reason,
        },
    )


def require(
    session: Session,
    ctx: WorkspaceContext,
    *,
    action_key: str,
    scope_kind: str,
    scope_id: str,
    rule_repo: PermissionRuleRepository | None = None,
) -> None:
    """Enforce the permission check or raise.

    Service-layer callers (workers, CLI, HTTP handlers that can't use
    the :func:`Permission` dep) invoke this directly. The router-side
    flow goes through :func:`Permission`.

    Returns ``None`` on allow. Raises:

    * :class:`UnknownActionKey` when ``action_key`` is not catalogued.
    * :class:`InvalidScope` when ``scope_kind`` is not in the
      action's ``valid_scope_kinds``.
    * :class:`PermissionDenied` otherwise on deny.

    ``rule_repo`` defaults to :class:`EmptyPermissionRuleRepository`
    (v1 has no ``permission_rule`` table yet). Tests and future
    adapters override it.
    """
    spec = ACTION_CATALOG.get(action_key)
    if spec is None:
        raise UnknownActionKey(action_key)

    if scope_kind not in spec.valid_scope_kinds:
        raise InvalidScope(
            f"action {action_key!r} does not accept scope_kind={scope_kind!r}"
        )

    repo = rule_repo if rule_repo is not None else _DEFAULT_RULE_REPO
    scope_chain = _build_scope_chain(
        scope_kind=scope_kind,
        scope_id=scope_id,
        workspace_id=ctx.workspace_id,
    )
    is_owner = _owner_on_scope_chain(
        session,
        workspace_id=ctx.workspace_id,
        user_id=ctx.actor_id,
        scope_chain=scope_chain,
    )

    # Step 2 — root-only gate.
    if spec.root_only:
        if is_owner:
            return
        _log_denied(
            action_key=action_key,
            scope_kind=scope_kind,
            scope_id=scope_id,
            actor_id=ctx.actor_id,
            workspace_id=ctx.workspace_id,
            reason="root_only",
        )
        raise PermissionDenied(action_key)

    # Step 3 + 4 — scope walk. Group the returned rules by their
    # ``(scope_kind, scope_id)`` in the order the adapter emitted them
    # (most-specific first). For each scope group, §02 "Deny within a
    # scope beats allow within the same scope" means: ANY deny on the
    # scope denies; otherwise ANY allow on the scope allows; otherwise
    # fall through to the next scope group.
    #
    # ``root_protected_deny`` immunity applies per-row: owners simply
    # ignore deny rows on those actions, so a same-scope [deny, allow]
    # pair collapses to an allow once the deny is masked.
    rules = repo.rules_for(
        session,
        workspace_id=ctx.workspace_id,
        user_id=ctx.actor_id,
        action_key=action_key,
        scope_kind=scope_kind,
        scope_id=scope_id,
        ancestor_scope_ids=scope_chain,
    )
    for _scope_key, scope_rules in _group_rules_by_scope(rules):
        has_deny = False
        has_allow = False
        for rule in scope_rules:
            if rule.effect == "deny":
                if spec.root_protected_deny and is_owner:
                    # Owner immunity on root-protected actions — this
                    # deny row does not count against the caller.
                    continue
                has_deny = True
            else:
                has_allow = True
        if has_deny:
            _log_denied(
                action_key=action_key,
                scope_kind=scope_kind,
                scope_id=scope_id,
                actor_id=ctx.actor_id,
                workspace_id=ctx.workspace_id,
                reason="rule_deny",
            )
            raise PermissionDenied(action_key)
        if has_allow:
            return
        # No effective row on this scope — fall through to the next
        # scope group.

    # Step 5 — default_allow fallback.
    for group_slug in spec.default_allow:
        try:
            if is_member_of(
                session,
                workspace_id=ctx.workspace_id,
                user_id=ctx.actor_id,
                group_slug=group_slug,
            ):
                return
        except UnknownSystemGroup:
            # A catalog entry referencing an unknown group slug is a
            # spec / code drift — surface it loudly, not as a deny.
            raise CatalogDrift(
                f"action {action_key!r} lists unknown default_allow group "
                f"{group_slug!r}"
            ) from None

    # Step 6 — no match, no default. Deny.
    _log_denied(
        action_key=action_key,
        scope_kind=scope_kind,
        scope_id=scope_id,
        actor_id=ctx.actor_id,
        workspace_id=ctx.workspace_id,
        reason="no_match",
    )
    raise PermissionDenied(action_key)


# ---------------------------------------------------------------------------
# FastAPI dependency factory
# ---------------------------------------------------------------------------


def _deny_to_http(action_key: str) -> HTTPException:
    """Map a domain :class:`PermissionDenied` into the HTTP 403 shape.

    Kept in one place so the router-facing error body stays
    consistent: every denied check returns the same
    ``{"error": "permission_denied", "action_key": "<key>"}`` detail.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "permission_denied", "action_key": action_key},
    )


def _misuse_to_http(error: str, action_key: str, detail: str) -> HTTPException:
    """Map a caller bug (unknown action / invalid scope) into HTTP 422.

    The detail shape matches §12's error envelope: one ``error`` code
    the client can switch on, plus human-readable context.
    """
    # Starlette / FastAPI renamed the 422 constant in 2024; the integer
    # literal keeps the call stable across versions without chasing
    # the deprecation warning.
    return HTTPException(
        status_code=422,
        detail={"error": error, "action_key": action_key, "message": detail},
    )


def Permission(
    action_key: str,
    *,
    scope_kind: str,
    scope_id_from_path: str | None = None,
    rule_repo: PermissionRuleRepository | None = None,
) -> Callable[..., None]:
    """Build a FastAPI dependency that enforces ``action_key``.

    Two wiring patterns — the caller picks at ``Depends()`` time:

    * **Workspace-scoped** — ``Permission("scope.view",
      scope_kind="workspace")``. The dep resolves ``scope_id`` from
      ``ctx.workspace_id`` automatically.
    * **Property-scoped** — ``Permission("tasks.create",
      scope_kind="property", scope_id_from_path="property_id")``. The
      dep reads ``request.path_params["property_id"]`` to get the
      target. The ancestor workspace comes from the ctx as usual.
      Organization-scope or deployment-scope endpoints pass the
      corresponding path-param name.

    The returned callable is the dependency; :class:`Depends` wires
    it into the route. Errors flow through :class:`HTTPException`:

    * :class:`UnknownActionKey` → 422 ``unknown_action_key``.
    * :class:`InvalidScope` → 422 ``invalid_scope_kind``.
    * :class:`PermissionDenied` → 403 ``permission_denied``.
    * Missing path param → 500 ``scope_id_unresolved`` (caller wired
      the dep incorrectly).

    ``rule_repo`` is threaded through so an app factory (cd-ika7) can
    inject a SQL-backed repo process-wide. Unit tests usually leave
    it ``None`` so the built-in empty repo applies.
    """

    def _dep(
        request: Request,
        ctx: Annotated[WorkspaceContext, Depends(current_workspace_context)],
        session: Annotated[Session, Depends(db_session)],
    ) -> None:
        if scope_id_from_path is None:
            # Default: workspace-scope gate. Non-workspace scope_kinds
            # without a path-param source are a wiring bug — fall
            # back to ctx.workspace_id for ``workspace`` only.
            if scope_kind == "workspace":
                scope_id = ctx.workspace_id
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "error": "scope_id_unresolved",
                        "message": (
                            f"Permission({action_key!r}) has scope_kind="
                            f"{scope_kind!r} but no scope_id_from_path set"
                        ),
                    },
                )
        else:
            raw = request.path_params.get(scope_id_from_path)
            if raw is None:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "error": "scope_id_unresolved",
                        "message": (
                            f"Permission({action_key!r}) expected path-param "
                            f"{scope_id_from_path!r} but none was provided"
                        ),
                    },
                )
            # ``path_params`` values arrive as strings from the
            # Starlette router; narrow explicitly to keep mypy happy.
            if not isinstance(raw, str):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={
                        "error": "scope_id_unresolved",
                        "message": (
                            f"Permission({action_key!r}) path-param "
                            f"{scope_id_from_path!r} is not a string"
                        ),
                    },
                )
            scope_id = raw

        try:
            require(
                session,
                ctx,
                action_key=action_key,
                scope_kind=scope_kind,
                scope_id=scope_id,
                rule_repo=rule_repo,
            )
        except UnknownActionKey as exc:
            raise _misuse_to_http("unknown_action_key", action_key, str(exc)) from exc
        except InvalidScope as exc:
            raise _misuse_to_http("invalid_scope_kind", action_key, str(exc)) from exc
        except PermissionDenied as exc:
            raise _deny_to_http(action_key) from exc

    return _dep


# ---------------------------------------------------------------------------
# Catalog integrity (startup hook seam)
# ---------------------------------------------------------------------------


def validate_catalog_integrity(
    session: Session | None = None,
    rule_repo: PermissionRuleRepository | None = None,
) -> None:
    """Assert every ``permission_rule`` row references a known ``action_key``.

    Intended as an application-startup check: the app factory
    (cd-ika7 — not wired yet) calls this once, fails fast on drift,
    and refuses to serve traffic until the mismatch is cleaned up.
    With the v1 :class:`EmptyPermissionRuleRepository` there is no
    ``permission_rule`` table, so this function always passes. The
    signature is stable — when the SQL adapter lands, the repo
    implementation will walk every active rule row (hence the
    :class:`Session` parameter) and raise :class:`CatalogDrift` on
    the first unknown key.

    ``session`` is ``None``-able in v1 because the built-in empty repo
    doesn't touch the DB; once the SQL adapter becomes the default,
    callers must pass a live session. Keeping the ``None`` default
    now means the boot-time sanity check (catalog-internal
    consistency) can run without a DB connection — useful for
    static-analysis and test bootstrap.

    TODO(cd-ika7): wire into :mod:`app.main` lifespan once the app
    factory lands.

    Also self-checks the in-memory catalog:

    * Every ``valid_scope_kind`` is in
      :data:`app.domain.identity._action_catalog.VALID_SCOPE_KINDS`.
    * Every ``default_allow`` slug is a recognised system group.

    A catalog-internal drift raises :class:`CatalogDrift` with a
    message pointing at the offending key.
    """
    # Lazy import keeps the module import graph tight — this helper
    # is only called at boot, not on the hot path.
    from app.domain.identity._action_catalog import VALID_SCOPE_KINDS

    known_groups: frozenset[str] = frozenset(
        {"owners", "managers", "all_workers", "all_clients"}
    )

    for key, spec in ACTION_CATALOG.items():
        _self_check_spec(spec, known_groups=known_groups, valid_kinds=VALID_SCOPE_KINDS)
        if key != spec.key:
            raise CatalogDrift(
                f"catalog key mismatch: map key {key!r} but spec.key {spec.key!r}"
            )

    # Without a rule table in v1, this is the whole check. Keeping
    # the ``rule_repo`` parameter plumbed means the future PR that
    # adds ``permission_rule`` gets a turnkey integrity walker.
    _ = rule_repo


def _self_check_spec(
    spec: ActionSpec,
    *,
    known_groups: frozenset[str],
    valid_kinds: frozenset[str],
) -> None:
    """Raise :class:`CatalogDrift` on any structural problem in ``spec``.

    Extracted so :func:`validate_catalog_integrity` reads as a flat
    for-loop — every failure mode points at a single cause with a
    single message shape.
    """
    if not spec.valid_scope_kinds:
        raise CatalogDrift(f"{spec.key!r}: empty valid_scope_kinds")
    for kind in spec.valid_scope_kinds:
        if kind not in valid_kinds:
            raise CatalogDrift(
                f"{spec.key!r}: valid_scope_kinds={kind!r} is not a known scope kind"
            )
    for group in spec.default_allow:
        if group not in known_groups:
            raise CatalogDrift(
                f"{spec.key!r}: default_allow={group!r} is not a known system group"
            )
    if spec.root_only and spec.default_allow:
        # Root-only actions never consult ``default_allow`` — listing
        # any group is a spec error that would mislead future
        # readers.
        raise CatalogDrift(
            f"{spec.key!r}: root_only actions must have an empty default_allow"
        )
