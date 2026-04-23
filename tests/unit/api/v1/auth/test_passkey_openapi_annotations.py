"""Regression guard for the passkey routers' OpenAPI annotations.

Spec §12 "OpenAPI" requires every route to carry:

* an ``operation_id`` following the ``{group}.{verb}`` convention
  (§12 "operationId convention"), and
* an ``openapi_extra={"x-cli": {...}}`` block with at minimum
  ``group``, ``verb``, ``summary``, and ``mutates`` fields
  (§12 "CLI surface extensions").

Every **mutating** route (``POST`` / ``DELETE`` / ``PATCH`` / ``PUT``,
or ``x-cli.mutates == True``) must additionally carry **exactly one**
of ``x-agent-confirm`` / ``x-agent-forbidden`` / ``x-interactive-only``
(§12 "Rule for mutating routes").

This test parses the generated OpenAPI document and asserts those
invariants for every ``/auth/passkey/`` route the factory mounts — the
workspace-scoped register / revoke tree, the bare-host signup flow,
and the bare-host login flow. If a new passkey route lands without
the annotations it will fail here instead of silently sneaking
through CI until the ``openapi-agent-annotations`` gate (§17) wires
up and retroactively audits the schema.

The negative case (a deliberately under-annotated route) is proven
out-of-process: constructing a fresh ``FastAPI`` with a mutating route
that lacks ``operation_id`` / ``x-cli`` and running the same
assertions demonstrates the guard fires, so a future regression can't
pass silently.

See ``docs/specs/12-rest-api.md`` §"OpenAPI",
§"CLI surface extensions", §"Rule for mutating routes".
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from pydantic import SecretStr

from app.api.v1.auth import passkey as passkey_module
from app.auth._throttle import Throttle
from app.config import Settings

# Verb sets are pulled from §12 — FastAPI never mints HEAD / OPTIONS
# operations from a handler decorator, but we keep them in the
# "read-only" set for completeness against the spec text.
_MUTATING_METHODS: frozenset[str] = frozenset({"post", "put", "patch", "delete"})
_READ_METHODS: frozenset[str] = frozenset({"get", "head", "options"})

# The three mutually-exclusive "agent boundary" extensions, per §12
# "Rule for mutating routes". Exactly one must appear on every
# mutating route.
_AGENT_GATE_KEYS: tuple[str, str, str] = (
    "x-agent-confirm",
    "x-agent-forbidden",
    "x-interactive-only",
)

# Required keys inside ``x-cli`` per §12 "CLI surface extensions".
_REQUIRED_XCLI_KEYS: frozenset[str] = frozenset(
    {"group", "verb", "summary", "mutates"},
)


def _minimal_app() -> FastAPI:
    """Return a fresh :class:`FastAPI` with the three passkey routers.

    **Do NOT replace this with ``app.main.create_app()``.** Calling the
    real factory pulls ``app.logging.setup_logging()`` into
    module-import side effects and installs the JSON + redaction
    handler on the root logger — a process-wide mutation that leaks
    across the test session. The nearest victims are
    ``tests/unit/auth/test_passkey_login.py``'s ``caplog``-based
    assertions, which rely on pytest's default logging config and
    start reporting stale / misshapen records once our JSON handler
    is wired in. Previous attempts to "simplify" this fixture back
    to ``create_app()`` reintroduced exactly that flake.

    A minimal router-only app has the same OpenAPI surface for the
    ``/auth/passkey/*`` paths as the real factory (the prefixes
    mirror :func:`app.api.factory._mount_auth_routers`) and keeps
    root-logger state untouched.

    ``Throttle`` + pinned :class:`Settings` are the only non-default
    inputs the login-router builder needs — its schema (paths,
    operations, openapi_extra) is independent of their values.
    """
    settings = Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("unit-test-passkey-openapi-root-key"),
        bind_host="127.0.0.1",
        bind_port=8000,
        allow_public_bind=False,
        worker="internal",
        smtp_host=None,
        smtp_port=587,
        smtp_from=None,
        smtp_use_tls=False,
        log_level="INFO",
        cors_allow_origins=[],
        profile="prod",
        vite_dev_url="http://127.0.0.1:5173",
    )
    app = FastAPI()
    # Mirror :func:`app.api.factory._mount_auth_routers` — same prefix,
    # same three routers, same build shape. If the factory ever
    # changes the passkey mount prefix or router set the mismatch
    # will surface as missing paths in ``test_every_route_has_*``.
    app.include_router(passkey_module.signup_router, prefix="/api/v1")
    app.include_router(
        passkey_module.build_login_router(throttle=Throttle(), settings=settings),
        prefix="/api/v1",
    )
    app.include_router(passkey_module.router, prefix="/w/{slug}/api/v1")
    return app


@pytest.fixture(scope="module")
def passkey_operations() -> list[tuple[str, str, dict[str, Any]]]:
    """Return ``(path, method, operation_object)`` for every passkey route.

    Module-scoped so we only pay the ``openapi()`` build cost once;
    the returned tuples are read-only views into the schema dict so
    cross-test mutation isn't a hazard.
    """
    schema = _minimal_app().openapi()
    triples: list[tuple[str, str, dict[str, Any]]] = []
    for path, path_item in schema.get("paths", {}).items():
        if "/auth/passkey" not in path:
            continue
        for method, operation in path_item.items():
            # Path-item objects also carry non-operation keys
            # (``parameters``, ``summary``, …) — operations are the
            # HTTP-method-named entries. We keep the check permissive:
            # any dict at an HTTP-method key.
            if method.lower() in _MUTATING_METHODS | _READ_METHODS and isinstance(
                operation, dict
            ):
                triples.append((path, method.lower(), operation))
    # Belt-and-braces: if the minimal app wiring ever drops a passkey
    # mount silently we want the test to scream instead of passing
    # with an empty loop.
    assert triples, (
        "no /auth/passkey routes found in OpenAPI — did the passkey "
        "router set change shape?"
    )
    return triples


class TestPasskeyRoutesCarryOperationId:
    """Every passkey route declares an ``{group}.{verb}`` ``operationId``."""

    def test_every_route_has_operation_id(
        self,
        passkey_operations: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        missing: list[str] = []
        for path, method, operation in passkey_operations:
            if not operation.get("operationId"):
                missing.append(f"{method.upper()} {path}")
        assert not missing, f"passkey routes missing operationId: {missing}"

    def test_operation_ids_follow_dot_convention(
        self,
        passkey_operations: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """§12 "operationId convention": dot-separated, first segment is
        a CLI group name. Passkey routes belong to the ``auth`` group.
        """
        bad: list[str] = []
        for path, method, operation in passkey_operations:
            op_id = operation.get("operationId", "")
            # Expect at least ``{group}.{verb}`` — two segments minimum,
            # first segment ``auth``. Passkey routes uniformly nest
            # under ``auth.passkey.*`` so we also assert that prefix
            # for a tighter guard.
            if "." not in op_id:
                bad.append(f"{method.upper()} {path}: {op_id!r}")
                continue
            if not op_id.startswith("auth.passkey."):
                bad.append(
                    f"{method.upper()} {path}: {op_id!r} (expected auth.passkey.*)",
                )
        assert not bad, f"operationIds violating convention: {bad}"

    def test_operation_ids_are_unique(
        self,
        passkey_operations: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """CI gate: no duplicate operationId across the schema.

        Scoped to the passkey tree here (the broad repo-wide check
        belongs in a factory-level test); a collision within the
        passkey routers is both the most likely copy-paste failure
        and the one this file owns.
        """
        seen: dict[str, str] = {}
        duplicates: list[str] = []
        for path, method, operation in passkey_operations:
            op_id = operation.get("operationId", "")
            key = f"{method.upper()} {path}"
            if op_id in seen:
                duplicates.append(f"{op_id!r}: {seen[op_id]} vs {key}")
            else:
                seen[op_id] = key
        assert not duplicates, f"duplicate operationIds: {duplicates}"


class TestPasskeyRoutesCarryXCli:
    """Every passkey route declares ``openapi_extra={"x-cli": {...}}``."""

    def test_every_route_has_x_cli_block(
        self,
        passkey_operations: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        missing: list[str] = []
        for path, method, operation in passkey_operations:
            x_cli = operation.get("x-cli")
            if not isinstance(x_cli, dict):
                missing.append(f"{method.upper()} {path}")
        assert not missing, f"passkey routes missing x-cli: {missing}"

    def test_x_cli_has_required_fields(
        self,
        passkey_operations: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """§12 "CLI surface extensions" — required fields are
        ``group`` / ``verb`` / ``summary`` / ``mutates``.
        """
        bad: list[str] = []
        for path, method, operation in passkey_operations:
            x_cli = operation.get("x-cli")
            assert isinstance(x_cli, dict), f"x-cli missing on {method} {path}"
            missing_keys = _REQUIRED_XCLI_KEYS - set(x_cli.keys())
            if missing_keys:
                bad.append(
                    f"{method.upper()} {path}: missing {sorted(missing_keys)}",
                )
        assert not bad, f"x-cli blocks missing required keys: {bad}"

    def test_x_cli_group_is_auth(
        self,
        passkey_operations: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """All passkey routes belong to the ``auth`` CLI group."""
        bad: list[str] = []
        for path, method, operation in passkey_operations:
            x_cli = operation.get("x-cli", {})
            if x_cli.get("group") != "auth":
                bad.append(
                    f"{method.upper()} {path}: group={x_cli.get('group')!r}",
                )
        assert not bad, f"passkey routes with wrong CLI group: {bad}"


class TestPasskeyMutatingRoutesHaveAgentGate:
    """§12 "Rule for mutating routes": exactly one of the three agent gates."""

    def test_every_mutating_route_has_exactly_one_gate(
        self,
        passkey_operations: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        bad: list[str] = []
        for path, method, operation in passkey_operations:
            if method not in _MUTATING_METHODS:
                continue
            present = [k for k in _AGENT_GATE_KEYS if operation.get(k) is not None]
            if len(present) != 1:
                bad.append(f"{method.upper()} {path}: gates present={present}")
        assert not bad, (
            "mutating passkey routes must carry exactly one of "
            f"{_AGENT_GATE_KEYS}: {bad}"
        )


class TestPasskeyRevokeIsInteractiveOnly:
    """DELETE /auth/passkey/{credential_id} is explicitly session-only.

    §03 "Additional passkeys" pins credential revocation to a live
    passkey session — PATs and delegated tokens reject with 403
    ``session_only_endpoint`` upstream. The annotation is the
    machine-readable record of that rule; this test freezes it.
    """

    def test_delete_is_interactive_only_and_not_hidden(
        self,
        passkey_operations: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        matches = [
            (path, method, op)
            for path, method, op in passkey_operations
            if method == "delete" and "{credential_id}" in path
        ]
        assert len(matches) == 1, (
            "expected exactly one DELETE /auth/passkey/{credential_id} route"
        )
        _, _, op = matches[0]
        assert op.get("x-interactive-only") is True, (
            "DELETE passkey must carry x-interactive-only: true"
        )
        x_cli = op.get("x-cli", {})
        # Revoke IS CLI-visible — operators must be able to drop a
        # lost device from the command line.
        assert x_cli.get("hidden") is not True, (
            "DELETE passkey must NOT be hidden from the CLI surface"
        )


class TestBrowserCeremoniesAreHidden:
    """WebAuthn ceremonies (register / signup-register / login) are browser-only.

    The CLI generator has no way to drive ``navigator.credentials.*``
    so emitting a ``crewday auth passkey register-start`` verb would
    just hand the user a dead command. ``hidden: true`` is the record
    that tells the generator to skip them.
    """

    @pytest.mark.parametrize(
        "path_fragment",
        [
            "/auth/passkey/register/start",
            "/auth/passkey/register/finish",
            "/auth/passkey/signup/register/start",
            "/auth/passkey/signup/register/finish",
            "/auth/passkey/login/start",
            "/auth/passkey/login/finish",
        ],
    )
    def test_ceremony_is_hidden(
        self,
        passkey_operations: list[tuple[str, str, dict[str, Any]]],
        path_fragment: str,
    ) -> None:
        matches = [
            op for path, _, op in passkey_operations if path.endswith(path_fragment)
        ]
        assert matches, f"no passkey route matched fragment {path_fragment!r}"
        for op in matches:
            x_cli = op.get("x-cli", {})
            assert x_cli.get("hidden") is True, (
                f"{path_fragment} must carry x-cli.hidden: true"
            )


# ---------------------------------------------------------------------------
# Negative control — the guard itself fires on a deliberately underannotated
# route, so the "green" above is not a false positive from a no-op check.
# ---------------------------------------------------------------------------


def _annotation_violations(
    operations: list[tuple[str, str, dict[str, Any]]],
) -> list[str]:
    """Return the aggregate list of violations for ``operations``.

    Shared shape of the three guards above so the negative-control
    test below exercises the exact same predicates without drifting
    from the primary checks.
    """
    problems: list[str] = []
    for path, method, op in operations:
        if not op.get("operationId"):
            problems.append(f"{method.upper()} {path}: missing operationId")
        x_cli = op.get("x-cli")
        if not isinstance(x_cli, dict):
            problems.append(f"{method.upper()} {path}: missing x-cli")
        elif _REQUIRED_XCLI_KEYS - set(x_cli.keys()):
            problems.append(f"{method.upper()} {path}: x-cli missing required keys")
        if method in _MUTATING_METHODS:
            present = [k for k in _AGENT_GATE_KEYS if op.get(k) is not None]
            if len(present) != 1:
                problems.append(f"{method.upper()} {path}: agent gates={present}")
    return problems


class TestGuardFiresOnMissingAnnotations:
    """Deliberate under-annotation must produce the violations we expect.

    Without this, all four test classes above could pass simply
    because the predicates never reject anything (for instance, a
    future refactor that accidentally collapses the checks into a
    no-op). Running the same predicates on a deliberately broken
    FastAPI sub-app proves the signal is real.
    """

    @staticmethod
    def _collect_operations(
        app: FastAPI,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        schema = app.openapi()
        triples: list[tuple[str, str, dict[str, Any]]] = []
        for path, path_item in schema.get("paths", {}).items():
            for method, operation in path_item.items():
                if method.lower() in (_MUTATING_METHODS | _READ_METHODS) and isinstance(
                    operation, dict
                ):
                    triples.append((path, method.lower(), operation))
        return triples

    def test_missing_xcli_and_agent_gate_are_flagged(self) -> None:
        bad_router = APIRouter()

        # No openapi_extra — the classic bug this regression guard
        # is supposed to catch. FastAPI auto-synthesises an
        # operationId from the handler name when none is passed, so
        # the "missing operationId" signal is exercised by the
        # dotted-convention guard on the live schema rather than a
        # raw absence check here.
        @bad_router.post("/deliberately-broken")
        def _handler() -> dict[str, bool]:
            return {"ok": True}

        app = FastAPI()
        app.include_router(bad_router)
        violations = _annotation_violations(self._collect_operations(app))
        assert any("missing x-cli" in v for v in violations), violations
        # Mutating route with no agent gate — second failure mode.
        assert any("agent gates=[]" in v for v in violations), violations

    def test_fully_annotated_route_is_not_flagged(self) -> None:
        """Sanity: a well-formed route produces zero violations.

        Guards against a bug in ``_annotation_violations`` itself
        that would flag every route regardless of shape.
        """
        good_router = APIRouter()

        @good_router.post(
            "/ok",
            operation_id="auth.passkey.smoke_ok",
            openapi_extra={
                "x-cli": {
                    "group": "auth",
                    "verb": "smoke",
                    "summary": "smoke route",
                    "mutates": True,
                },
                "x-interactive-only": True,
            },
        )
        def _handler() -> dict[str, bool]:
            return {"ok": True}

        app = FastAPI()
        app.include_router(good_router)
        assert _annotation_violations(self._collect_operations(app)) == []
