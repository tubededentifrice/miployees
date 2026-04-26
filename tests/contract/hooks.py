"""Schemathesis custom checks + hooks (cd-3j25).

Loaded by ``schemathesis run`` via ``SCHEMATHESIS_HOOKS=tests.contract.hooks``.
Three invariants are enforced, mirroring
``docs/specs/17-testing-quality.md`` §"API contract":

1. **Authorization presence.** Every non-public path must carry an
   ``Authorization: Bearer …`` header on the prepared request.
2. **Idempotency-Key round-trip.** When the OpenAPI operation declares
   an ``Idempotency-Key`` header parameter, a follow-up call with the
   same key must return the cached response (same status + body
   bytes).
3. **ETag round-trip.** When the response schema declares an ``ETag``
   header, a follow-up GET with ``If-None-Match: <etag>`` must return
   304.

Public-path matching is regex-based and intentionally narrow — every
entry in :data:`_PUBLIC_PATTERNS` is justified inline so a future
reviewer can grep for it. The list covers the unauthenticated
bootstraps the ops surface needs (`/healthz`, `/readyz`, `/version`,
``/api/openapi.json``, ``/docs``, ``/redoc``) and the auth entry
points that mint the very session a Bearer token is issued from
(magic-link, signup, passkey login, dev-login). Adding a route that
genuinely accepts unauthenticated traffic later means extending this
list with a one-line justification, not silencing the check.

The Idempotency-Key + ETag checks short-circuit when the operation
or response schema doesn't declare the relevant header, so a hook
firing on a route that doesn't use the feature is a no-op rather than
a false positive.
"""

from __future__ import annotations

import os
import re
from typing import Any, Final

import schemathesis
from schemathesis import Case, CheckContext, Response

__all__ = [
    "check_authorization_present",
    "check_etag_round_trip",
    "check_idempotency_round_trip",
    "constrain_workspace_slug",
]

# Workspace slug the runner seeds via ``scripts/_schemathesis_seed.py``.
# Schemathesis would otherwise generate random unicode slugs that 404
# on the workspace-membership lookup; pinning the slug to the seeded
# row makes every ``/w/<slug>/api/v1/...`` path resolvable. Override
# via the ``CREWDAY_SCHEMATHESIS_SLUG`` env var when running against
# a non-default seed.
_WORKSPACE_SLUG: Final[str] = os.environ.get(
    "CREWDAY_SCHEMATHESIS_SLUG", "schemathesis"
)


# ---------------------------------------------------------------------------
# Public path allowlist
# ---------------------------------------------------------------------------

# Routes that legitimately accept unauthenticated traffic. Match against
# the request path (``case.path`` resolved against the schema base);
# anchored with ``^`` + ``$`` to avoid accidentally exempting a longer
# path that happens to start with a public prefix.
#
# Justifications mirror the §03 + §16 spec — every line below is a
# documented bypass, not a TODO.
_PUBLIC_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Ops probes — §16 "Healthchecks". Must stay reachable without a
    # token so the load balancer + the container orchestrator can
    # liveness/readiness-poll.
    re.compile(r"^/healthz$"),
    re.compile(r"^/readyz$"),
    re.compile(r"^/version$"),
    # OpenAPI surface — §12 "Base URL". The schema document is public
    # by design (the SPA reads it for client codegen + `/docs` renders
    # it for human reviewers).
    re.compile(r"^/api/openapi\.json$"),
    re.compile(r"^/docs(?:/.*)?$"),
    re.compile(r"^/redoc(?:/.*)?$"),
    # Magic-link bootstrap — §03 "Magic links". The /request endpoint
    # is intentionally unauthenticated (the user has no session yet);
    # /consume turns the link into a session, also pre-auth.
    re.compile(r"^/api/v1/auth/magic/request$"),
    re.compile(r"^/api/v1/auth/magic/consume$"),
    # Passkey login — §03 "Passkey login". Both the start + finish
    # halves must be reachable pre-session because the whole point is
    # to mint the session.
    re.compile(r"^/api/v1/auth/passkey/login/start$"),
    re.compile(r"^/api/v1/auth/passkey/login/finish$"),
    # Passkey signup-flow registration (cd-ju0q transitional surface).
    # Both halves bootstrap the very session that would carry a token.
    re.compile(r"^/api/v1/auth/passkey/signup/register/start$"),
    re.compile(r"^/api/v1/auth/passkey/signup/register/finish$"),
    # Self-serve signup — §03 "Self-serve signup". Mounted at the
    # bare-host ``/signup`` prefix (NOT ``/auth/signup``) — the form
    # is public by design.
    re.compile(r"^/api/v1/signup(?:/.*)?$"),
    # Invite acceptance — §03 "Invites". The invite token (or the
    # signed invite_id) is the auth primitive; the endpoint can't
    # require a Bearer token because the invitee has no other
    # credential yet. Routes are mounted at ``/invite`` and
    # ``/invites`` (one per OpenAPI router — see
    # ``app/api/v1/auth/invite.py``):
    #   * POST /invite/accept           — invite-id branch (existing user
    #     and new user paths share the same accept handler)
    #   * POST /invite/{invite_id}/confirm — confirm pending acceptance
    #   * POST /invite/complete         — complete signup-via-invite
    #   * GET  /invites/{token}         — introspect invite by token
    #   * POST /invites/{token}/accept  — redeem invite by token
    re.compile(r"^/api/v1/invite/accept$"),
    re.compile(r"^/api/v1/invite/[^/]+/confirm$"),
    re.compile(r"^/api/v1/invite/complete$"),
    re.compile(r"^/api/v1/invites/[^/]+$"),
    re.compile(r"^/api/v1/invites/[^/]+/accept$"),
    # Recovery codes — §03 "Recovery". Same shape as magic-link: the
    # endpoint mints a session from a pre-issued recovery code, so a
    # Bearer token is the wrong primitive here. Mounted at
    # ``/recover`` (NOT ``/auth/recovery``) — see
    # ``app/api/v1/auth/recovery.py::build_recovery_router``.
    re.compile(r"^/api/v1/recover(?:/.*)?$"),
    # Email-change confirmation — §03 "Email change". Confirms a
    # signed token sent to the new (verify) or previous (revert)
    # address; both flows are pre-session by design and mounted at
    # ``/auth/email/{verify,revert}`` — see
    # ``app/api/v1/auth/email_change.py``. The companion
    # ``/me/email/change_request`` endpoint stays authed (it lives
    # under ``/me`` and requires the caller's session) and is
    # deliberately NOT in the allowlist.
    re.compile(r"^/api/v1/auth/email/verify$"),
    re.compile(r"^/api/v1/auth/email/revert$"),
)


def _is_public_path(path: str) -> bool:
    """Return ``True`` when ``path`` matches the public allowlist.

    Strips a trailing slash so ``/healthz/`` matches ``/healthz``;
    schemathesis sometimes emits both forms depending on how the
    schema declared the path.
    """
    normalised = path.rstrip("/") or "/"
    return any(p.match(normalised) for p in _PUBLIC_PATTERNS)


# ---------------------------------------------------------------------------
# Header lookup helpers
# ---------------------------------------------------------------------------


def _header(response: Response, name: str) -> str | None:
    """Return ``response.headers[name]`` case-insensitively, or ``None``."""
    target = name.lower()
    for key, value in response.headers.items():
        if key.lower() == target:
            # ``Response.headers`` may carry list-valued entries (the
            # ``requests`` adapter collapses to ``str`` but ASGI keeps
            # lists). Normalise to a plain str for the check.
            if isinstance(value, list):
                return value[0] if value else None
            return value
    return None


def _request_header(response: Response, name: str) -> str | None:
    """Read a request header from the prepared request behind ``response``.

    The :class:`schemathesis.Response` wrapper exposes the upstream
    :class:`requests.PreparedRequest` via ``response.request``; we
    walk its headers case-insensitively. Returns ``None`` when the
    header isn't present so the caller can decide failure semantics.
    """
    request = response.request
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return str(value) if value is not None else None
    return None


def _operation_declares_header(case: Case, header_name: str) -> bool:
    """Return ``True`` when the operation declares ``header_name`` as a parameter.

    Walks ``case.operation.headers`` (schemathesis-parsed parameters)
    plus the raw ``parameters`` list on the OpenAPI op definition to
    catch schemas that put the header on the path level. Match is
    case-insensitive — header names are case-insensitive on the wire.
    """
    target = header_name.lower()

    # Schemathesis parses parameters into ``operation.headers`` /
    # ``operation.path_parameters`` / etc. on construction.
    op = case.operation
    parsed = getattr(op, "headers", None)
    if parsed is not None:
        for param in parsed:
            name = getattr(param, "name", None)
            if isinstance(name, str) and name.lower() == target:
                return True

    # Fallback: walk the raw ``parameters`` list on the operation
    # definition. This catches schemas that the parser flattened
    # differently (path-level parameters merged into the operation).
    raw_def = getattr(op, "definition", None)
    raw_resolved: dict[str, Any] | None = (
        raw_def.raw if raw_def is not None and hasattr(raw_def, "raw") else None
    )
    if isinstance(raw_resolved, dict):
        for param in raw_resolved.get("parameters", []) or []:
            if (
                isinstance(param, dict)
                and param.get("in") == "header"
                and isinstance(param.get("name"), str)
                and param["name"].lower() == target
            ):
                return True

    return False


def _response_declares_header(case: Case, status_code: int, header_name: str) -> bool:
    """Return ``True`` when the matched response schema declares ``header_name``.

    Looks up the response definition for ``status_code`` (or the
    ``default`` slot) on the OpenAPI op and checks its ``headers``
    map. Match is case-insensitive.
    """
    target = header_name.lower()
    op = case.operation
    raw_def = getattr(op, "definition", None)
    raw: dict[str, Any] | None = (
        raw_def.raw if raw_def is not None and hasattr(raw_def, "raw") else None
    )
    if not isinstance(raw, dict):
        return False
    responses = raw.get("responses") or {}
    if not isinstance(responses, dict):
        return False
    # Try the exact status code first, then the ``default`` slot.
    candidates: list[Any] = []
    exact = responses.get(str(status_code))
    if exact is not None:
        candidates.append(exact)
    default = responses.get("default")
    if default is not None:
        candidates.append(default)
    for resp in candidates:
        if not isinstance(resp, dict):
            continue
        headers = resp.get("headers") or {}
        if not isinstance(headers, dict):
            continue
        for name in headers:
            if isinstance(name, str) and name.lower() == target:
                return True
    return False


# ---------------------------------------------------------------------------
# Schema patch: declare missing ``{slug}`` path parameter
# ---------------------------------------------------------------------------


@schemathesis.hook
def before_load_schema(
    ctx: schemathesis.HookContext, raw_schema: dict[str, Any]
) -> None:
    """Inject the ``{slug}`` path parameter on every workspace-scoped path.

    Schemathesis refuses to test an operation whose path template
    references a placeholder the OpenAPI document does not declare as
    a parameter. FastAPI's sub-app composition omits the
    ``{slug}`` parameter from the merged document — every
    ``/w/{slug}/api/v1/...`` op is missing the path-level entry. We
    repair the schema in-place so the gate has something to test;
    the long-term fix is to surface the parameter in
    :func:`app.api.factory.create_app`'s OpenAPI customiser, tracked
    by a cd-3j25 follow-up Beads task.
    """
    paths = raw_schema.get("paths")
    if not isinstance(paths, dict):
        return
    slug_param: dict[str, Any] = {
        "name": "slug",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "minLength": 1, "maxLength": 64},
    }
    for path, methods in paths.items():
        if not isinstance(methods, dict) or "{slug}" not in path:
            continue
        # Path-level ``parameters`` is the right home — applies to
        # every method on the path. If the path already has a
        # parameters list, append unless ``slug`` is already declared.
        existing = methods.get("parameters")
        if existing is None:
            methods["parameters"] = [slug_param.copy()]
            continue
        if not isinstance(existing, list):
            continue
        if any(
            isinstance(p, dict) and p.get("name") == "slug" and p.get("in") == "path"
            for p in existing
        ):
            continue
        existing.append(slug_param.copy())


# ---------------------------------------------------------------------------
# Path-parameter constraints
# ---------------------------------------------------------------------------


@schemathesis.hook("map_path_parameters")
def constrain_workspace_slug(
    ctx: schemathesis.HookContext, path_parameters: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Pin the ``{slug}`` path parameter to the seeded workspace slug.

    Workspace-scoped routes live under ``/w/{slug}/api/v1/...``;
    schemathesis would otherwise generate random unicode slugs that
    404 against the workspace-membership lookup before any handler
    code runs. Pinning the slug to the seeded value (``schemathesis``
    by default, overridable via ``CREWDAY_SCHEMATHESIS_SLUG``) means
    the gate exercises the real handlers rather than the tenancy 404
    branch on every request.

    Other path params (``{id}``, ``{user_id}``, etc.) are left to
    schemathesis — those are tested for "does the handler 404 cleanly
    on a missing resource", which is a separate (also valuable)
    contract.

    ``path_parameters`` is ``None`` for operations that declare no
    path parameters (e.g. ``auth.me.get``); the hook short-circuits
    in that case rather than tripping a ``TypeError`` in the
    membership test.
    """
    if path_parameters is None:
        return None
    if "slug" in path_parameters:
        path_parameters["slug"] = _WORKSPACE_SLUG
    return path_parameters


# ---------------------------------------------------------------------------
# Custom checks
# ---------------------------------------------------------------------------


@schemathesis.check
def check_authorization_present(
    ctx: CheckContext, response: Response, case: Case
) -> None:
    """Assert ``Authorization: Bearer …`` rides every non-public request.

    Reads the prepared request behind ``response`` (rather than the
    OpenAPI parameter declaration) so the check fires even on routes
    that don't declare the header — the spec's contract is that
    *every* non-public request carries the bearer, not just the
    documented ones.

    Public routes (``/healthz``, ``/api/openapi.json``,
    auth bootstraps, …) are exempt via :func:`_is_public_path`.
    """
    # ``case.formatted_path`` interpolates path parameters; falls back
    # to ``case.path`` when the schemathesis version doesn't expose it.
    path: str = (
        getattr(case, "formatted_path", None) or getattr(case, "path", None) or ""
    )
    if _is_public_path(path):
        return

    auth = _request_header(response, "Authorization")
    if auth is None or not auth.lower().startswith("bearer "):
        raise AssertionError(
            f"Authorization Bearer header missing on non-public path "
            f"{path!r} (got {auth!r}); add the path to the public "
            "allowlist in tests/contract/hooks.py if it is genuinely "
            "unauthenticated, otherwise the request is leaking past "
            "the auth gate."
        )


@schemathesis.check
def check_idempotency_round_trip(
    ctx: CheckContext, response: Response, case: Case
) -> None:
    """Assert a second call with the same Idempotency-Key replays the response.

    No-op when:

    * the operation does not declare ``Idempotency-Key`` as a header
      parameter (the route opts in via OpenAPI);
    * the request did not actually carry an ``Idempotency-Key`` header
      (negative-data shrinking can drop optional headers);
    * the first response is a 5xx — the cache is only populated on
      terminal 2xx/4xx, replaying a 5xx would test something
      different (and the cache rules say the row is not written).
    """
    if not _operation_declares_header(case, "Idempotency-Key"):
        return

    sent_key = _request_header(response, "Idempotency-Key")
    if sent_key is None:
        return

    if response.status_code >= 500:
        return

    # Replay the same case — schemathesis ``Case.call`` re-derives the
    # prepared request from the case's data, so passing the same case
    # produces an identical body. We pin the same ``Idempotency-Key``
    # header (and any ``Authorization`` header the first call carried)
    # so the second call lands on the same cache row.
    extra_headers: dict[str, Any] = {"Idempotency-Key": sent_key}
    auth = _request_header(response, "Authorization")
    if auth is not None:
        extra_headers["Authorization"] = auth

    try:
        replay = case.call(headers=extra_headers)
    except OSError as exc:  # pragma: no cover
        # Transport failures aren't a contract violation — just log
        # via the assertion message so the failure is visible without
        # tripping the suite. ConnectionError covers asgi-style retry
        # storms in tests.
        raise AssertionError(
            f"Idempotency-Key replay raised on follow-up call "
            f"({type(exc).__name__}): {exc}"
        ) from exc

    if replay.status_code != response.status_code:
        raise AssertionError(
            f"Idempotency-Key replay returned status "
            f"{replay.status_code} (first call was "
            f"{response.status_code}); the cache should serve a byte-"
            f"identical response on key reuse."
        )

    # Body-bytes equality — the cache stores the literal body, so
    # replay should be byte-for-byte. Tolerate whitespace differences
    # only when both bodies are JSON; otherwise demand identity.
    first_body = response.content
    replay_body = replay.content
    if first_body != replay_body:
        raise AssertionError(
            f"Idempotency-Key replay body differs from first response "
            f"on {case.method} {case.path}; cache served "
            f"{len(replay_body)} bytes, first call returned "
            f"{len(first_body)} bytes."
        )


@schemathesis.check
def check_etag_round_trip(ctx: CheckContext, response: Response, case: Case) -> None:
    """Assert ``GET → If-None-Match → 304`` round-trip on routes with ETag.

    No-op when:

    * the method isn't GET (ETag round-trip is a read-only contract);
    * the response schema doesn't declare an ``ETag`` header for the
      observed status code;
    * the response didn't actually emit an ``ETag`` header — the
      schema declares the header optional, so a missing header is a
      separate concern that the response_headers_conformance check
      already covers.
    """
    if case.method.upper() != "GET":
        return
    if not _response_declares_header(case, response.status_code, "ETag"):
        return
    etag = _header(response, "ETag")
    if etag is None:
        return

    # Replay the GET with ``If-None-Match: <etag>``. Auth is forwarded
    # so the second call lands on the same surface — the ETag cache
    # is per-(token, resource) on §12 mutating routes, so dropping
    # auth would let the server return a fresh body.
    extra_headers: dict[str, Any] = {"If-None-Match": etag}
    auth = _request_header(response, "Authorization")
    if auth is not None:
        extra_headers["Authorization"] = auth

    try:
        replay = case.call(headers=extra_headers)
    except OSError as exc:  # pragma: no cover
        raise AssertionError(
            f"ETag replay raised on If-None-Match follow-up "
            f"({type(exc).__name__}): {exc}"
        ) from exc

    if replay.status_code != 304:
        raise AssertionError(
            f"ETag round-trip on {case.method} {case.path} returned "
            f"{replay.status_code}; expected 304 Not Modified for "
            f"If-None-Match: {etag!r}. ETag declared in the response "
            f"schema implies the route honours conditional requests."
        )
