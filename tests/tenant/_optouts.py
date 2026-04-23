"""Explicit tenant-scope opt-outs for the cross-tenant regression matrix.

Surfaces listed here are **intentionally** not covered by the
per-workspace isolation tests because they are deployment-scoped
(operate on the whole deployment) or identity-scoped (operate on a
single human across every workspace they belong to). Every entry MUST
carry a ``# justification:`` comment explaining why the surface is
genuinely tenant-agnostic.

The parity gate in :mod:`tests.tenant.test_repository_parity` and
:mod:`tests.tenant.test_http_surface` consults this registry: a new
route, repository method, or event that doesn't get a matching case
and isn't listed here fails the gate loudly.

See ``docs/specs/17-testing-quality.md`` §"Cross-tenant regression
test" — "Gaps fail the gate; the fix is either to extend the tenant
test suite or to explain why the surface is genuinely tenant-agnostic
(deployment-scope, identity-scope — both recorded as explicit
opt-outs in ``tests/tenant/_optouts.py`` with a ``# justification:``
comment)."
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# HTTP route opt-outs
# ---------------------------------------------------------------------------
#
# Matched by ``path`` (FastAPI pattern, e.g. ``/healthz``,
# ``/api/v1/invite/accept``). A path NOT starting with ``/w/{slug}/``
# is deployment-scoped by construction; the tenancy middleware
# skips them (see :data:`app.tenancy.middleware.SKIP_PATHS` and
# :func:`app.tenancy.middleware._is_skip_path`), so a cross-tenant
# probe on them is meaningless by design.
HTTP_PATH_OPTOUTS: frozenset[str] = frozenset(
    {
        # justification: bare-host ops probes — deployment-scoped, no
        # workspace context exists on these paths (§16 "Healthchecks").
        "/healthz",
        "/readyz",
        "/version",
        # justification: bare-host identity surface — signup / magic /
        # recovery / invite flows run before any workspace is chosen
        # (§03 "Self-serve signup").
        "/api/v1/auth/passkey/login/start",
        "/api/v1/auth/passkey/login/finish",
        "/api/v1/invite/accept",
        "/api/v1/invite/complete",
        "/api/v1/invite/{invite_id}/confirm",
        # justification: OpenAPI + docs are documentation surfaces,
        # deployment-scoped and read-only (§12 "Base URL").
        "/api/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        # justification: SPA catch-all serves the web bundle; it
        # intentionally handles every non-API GET that didn't match a
        # real route (§14 "SPA fallback") and is not a tenant surface.
        "/{full_path:path}",
    }
)


# ---------------------------------------------------------------------------
# Repository method opt-outs
# ---------------------------------------------------------------------------
#
# Matched by the fully-qualified name ``app.domain.<ctx>.<module>.<fn>``.
# These entries name domain-layer public functions that DO take a
# :class:`~app.tenancy.WorkspaceContext` AND are therefore picked up
# by :func:`tests.tenant.test_repository_parity._discover_repository_methods`
# but are **intentionally exempt** from the cross-tenant case because
# the function's semantics cross workspaces by design.
#
# Functions that do NOT take a ctx (e.g. ``consume_invite_token``,
# ``list_workspaces_for_user``, ``switch_session_workspace``) are
# invisible to discovery by construction — they don't need listing
# here. The gate only asks "every ctx-taking function is covered OR
# explicitly opted out"; non-ctx functions are out of scope.
REPOSITORY_METHOD_OPTOUTS: frozenset[str] = frozenset(
    # justification: (reserved — today every ctx-taking domain
    # function is covered by the ORM-filter seam proven by
    # TestScopedRowIsolation. Entries land here when a new
    # ctx-taking function genuinely crosses workspaces by design,
    # e.g. cross-workspace property-share read paths.)
    set[str]()
)


# ---------------------------------------------------------------------------
# Event kind opt-outs
# ---------------------------------------------------------------------------
#
# Matched by the event ``name`` ClassVar (e.g. ``task.created``).
# Every concrete :class:`~app.events.registry.Event` subclass today
# carries a ``workspace_id`` field on its payload, so the default
# position is "no opt-outs" — the set is kept empty here but lives
# in this module as the single registration seam for the future.
EVENT_NAME_OPTOUTS: frozenset[str] = frozenset(
    # justification: (reserved — no deployment-scoped events exist
    # in the v1 slice; every event kind carries a workspace_id).
    set[str]()
)


# ---------------------------------------------------------------------------
# Worker-job opt-outs
# ---------------------------------------------------------------------------
#
# Matched by the fully-qualified function name. Today only
# :func:`app.worker.tasks.generator.generate_task_occurrences` is on
# the registry; it IS workspace-scoped so no opt-out is needed.
WORKER_JOB_OPTOUTS: frozenset[str] = frozenset(
    # justification: (reserved — deployment-scoped worker jobs
    # like ``rotate_audit_log`` or ``refresh_exchange_rates`` will
    # land here with an explicit justification when they ship).
    set[str]()
)


__all__ = [
    "EVENT_NAME_OPTOUTS",
    "HTTP_PATH_OPTOUTS",
    "REPOSITORY_METHOD_OPTOUTS",
    "WORKER_JOB_OPTOUTS",
]
