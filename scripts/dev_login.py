#!/usr/bin/env python3
"""Dev-only force-login — mint a session cookie without passkey.

Hard-gated on ``CREWDAY_DEV_AUTH=1`` + ``CREWDAY_PROFILE=dev`` + a
SQLite database URL. Writes a real :class:`Session` row via
:func:`app.auth.session.issue` and prints the
``__Host-crewday_session=<value>`` cookie on stdout so local agents
and Playwright tests can round-trip authenticated requests against
the production app running at ``http://127.0.0.1:8100/`` without
walking through the passkey ceremony.

**Never use in production.** The script refuses to run unless every
gate is green; a misconfigured prod box setting ``CREWDAY_DEV_AUTH=1``
still fails the profile + DB-URL checks.

**How to run.** Inside the dev stack (recommended — no host-side
Python deps; the compose file already sets the two env vars). Invoke
via the ``-m`` flag so Python treats the repo root as the import
anchor — ``python scripts/dev_login.py`` puts ``scripts/`` (not
``/app``) on ``sys.path`` and the ``from app.…`` imports miss::

    docker compose -f mocks/docker-compose.yml exec app-api \\
        python -m scripts.dev_login --email me@dev.local --workspace smoke

Host-side (requires ``uv sync`` / ``pip install -e .`` so sqlalchemy
+ click resolve)::

    CREWDAY_DEV_AUTH=1 python -m scripts.dev_login \\
        --email me@example.com --workspace myhome

    # Shell wrapper: ``./scripts/dev-login.sh me@example.com myhome``.

Outputs (exact one line; no trailing junk):

* ``cookie`` (default): ``__Host-crewday_session=<value>``
* ``json``:             ``{"name": "__Host-crewday_session", "value": "<value>"}``
* ``curl``:             ``-b '__Host-crewday_session=<value>'``
* ``header``:           ``Cookie: __Host-crewday_session=<value>``

See ``docs/specs/03-auth-and-tokens.md`` §"Sessions" and
:mod:`app.auth.session` for the row lifecycle. The Beads task
``cd-w1ia`` carries the motivation.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from typing import Final, Literal

import click
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlaSession

from app.adapters.db.authz.bootstrap import (
    seed_owners_system_group,
    seed_system_permission_groups,
)
from app.adapters.db.authz.models import (
    PermissionGroup,
    PermissionGroupMember,
    RoleGrant,
)
from app.adapters.db.identity.models import Session as SessionRow
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.audit import write_audit
from app.auth.session import SessionIssue, issue
from app.auth.signup import provision_workspace_and_owner_seat
from app.config import get_settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock
from app.util.ulid import new_ulid

__all__ = ["MintResult", "main", "mint_session"]


# Env-var gate. Read via :func:`os.environ` on purpose (see task
# cd-w1ia "Don't do"): this is a script-only lever, not a public
# surface — adding a typed :class:`Settings` field would imply it's
# part of the deployment contract.
_DEV_AUTH_ENV_VAR: Final[str] = "CREWDAY_DEV_AUTH"

# Role → ``has_owner_grant`` boolean fed into :func:`session.issue`.
# Owners (`manager` surface grant on workspace) + managers receive
# the shorter owner TTL; workers / clients / guests get the longer
# non-owner TTL. See :mod:`app.auth.session` §"Lifetime".
_OWNER_ROLES: Final[frozenset[str]] = frozenset({"owner", "manager"})

# Canonical cookie name for the production app (see
# :mod:`app.auth.session_cookie`). Hard-coded here so the output
# shapes stay stable regardless of which cookie variant the domain
# service decided to mint today; the domain service always returns
# the opaque value, never the name.
_COOKIE_NAME: Final[str] = "__Host-crewday_session"

Role = Literal["owner", "manager", "worker"]
OutputFormat = Literal["cookie", "json", "curl", "header"]


class _GateError(RuntimeError):
    """Refused — one of the hard gates failed.

    Carries the failing-gate name so the CLI can print a focused
    error. Kept private: any SystemExit from the script funnels
    through :func:`_fail_gate` so stderr formatting stays uniform.
    """


# ---------------------------------------------------------------------------
# Mint result
# ---------------------------------------------------------------------------


class MintResult:
    """Return value of :func:`mint_session` for callers / tests.

    ``session_issue`` carries the cookie value + expiry; the two
    ``*_created`` flags tell the caller (and the audit row) whether
    this call provisioned a fresh user / workspace or reused existing
    rows — dev-login is idempotent on identity + tenancy but always
    mints a fresh session, matching the task's acceptance criteria.
    """

    __slots__ = ("session_issue", "user_created", "workspace_created")

    def __init__(
        self,
        *,
        session_issue: SessionIssue,
        user_created: bool,
        workspace_created: bool,
    ) -> None:
        self.session_issue = session_issue
        self.user_created = user_created
        self.workspace_created = workspace_created


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------


def _check_gates() -> None:
    """Refuse to run unless every dev-auth gate is green.

    Three gates, all of which must pass:

    1. ``CREWDAY_DEV_AUTH`` env var is one of ``1`` / ``yes`` / ``true``
       (case-insensitive). A deploy with the flag unset or ``0`` is
       rejected.
    2. ``settings.profile == "dev"``. A prod-profile deploy that
       someone forgot to scrub the env var on still fails here.
    3. The database URL starts with ``sqlite`` (ignoring
       ``sqlite+aiosqlite`` / ``sqlite+pysqlite`` variants). Defensive
       gate against accidentally minting a session against a Postgres
       cluster — the script has no business touching a real DB.

    Raises :class:`_GateError` with a descriptive message naming the
    failing gate. The CLI wraps that into ``SystemExit(1)`` with a
    hint.
    """
    raw = os.environ.get(_DEV_AUTH_ENV_VAR, "0").lower()
    if raw not in {"1", "yes", "true"}:
        raise _GateError(
            f"{_DEV_AUTH_ENV_VAR} is not set to 1/yes/true "
            f"(got {raw!r}); dev-login is hard-gated off. "
            f"Hint: set {_DEV_AUTH_ENV_VAR}=1 in your .env."
        )

    settings = get_settings()
    if settings.profile != "dev":
        raise _GateError(
            f"CREWDAY_PROFILE={settings.profile!r} — dev-login requires "
            "profile=dev; refusing to run."
        )

    # ``database_url`` may carry a driver suffix (``sqlite+aiosqlite``,
    # ``sqlite+pysqlite``). Any of those starts with ``sqlite`` — the
    # split-on-colon-first-segment prefix captures them all. A
    # Postgres URL (``postgresql://``, ``postgresql+psycopg://``) does
    # not, so the guard fires as intended.
    scheme = settings.database_url.split(":", 1)[0].lower()
    if not scheme.startswith("sqlite"):
        raise _GateError(
            f"database_url scheme {scheme!r} is not SQLite; dev-login "
            "refuses to mint sessions against a non-SQLite DB."
        )


def _fail_gate(exc: _GateError) -> int:
    """Format a gate failure onto stderr and return the exit code."""
    print(f"error: dev-login refused to run: {exc}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Row helpers — idempotent lookups
# ---------------------------------------------------------------------------


def _find_user(session: SqlaSession, email_lower: str) -> User | None:
    """Return the existing :class:`User` for ``email_lower`` or ``None``."""
    # justification: ``user`` is identity-scoped (not workspace-scoped)
    # — tenant filter has nothing to apply here.
    with tenant_agnostic():
        return session.scalars(
            select(User).where(User.email_lower == email_lower)
        ).one_or_none()


def _find_workspace(session: SqlaSession, slug: str) -> Workspace | None:
    """Return the existing :class:`Workspace` for ``slug`` or ``None``."""
    # justification: listing workspaces runs before a WorkspaceContext
    # exists (dev-login has no ctx to borrow); the table is workspace-
    # scoped so the ORM tenant filter would otherwise reject the
    # SELECT.
    with tenant_agnostic():
        return session.scalars(
            select(Workspace).where(Workspace.slug == slug)
        ).one_or_none()


def _ensure_user_workspace(
    session: SqlaSession, *, user_id: str, workspace_id: str, now: datetime
) -> None:
    """Insert a :class:`UserWorkspace` row if one is not already present."""
    with tenant_agnostic():
        existing = session.scalars(
            select(UserWorkspace)
            .where(UserWorkspace.user_id == user_id)
            .where(UserWorkspace.workspace_id == workspace_id)
        ).one_or_none()
        if existing is not None:
            return
        session.add(
            UserWorkspace(
                user_id=user_id,
                workspace_id=workspace_id,
                source="workspace_grant",
                added_at=now,
            )
        )
        session.flush()


def _ensure_role_grant(
    session: SqlaSession,
    *,
    user_id: str,
    workspace_id: str,
    role: Role,
    now: datetime,
) -> None:
    """Insert a :class:`RoleGrant` for ``(user, workspace, role)`` if missing.

    The ``owner`` script-role collapses onto the schema ``manager``
    surface grant (v1 enum drops the legacy ``owner`` value; the
    governance bit lives on the ``owners`` permission group). Workers
    and managers map 1:1.
    """
    grant_role = "manager" if role in _OWNER_ROLES else role
    with tenant_agnostic():
        existing = session.scalars(
            select(RoleGrant)
            .where(RoleGrant.user_id == user_id)
            .where(RoleGrant.workspace_id == workspace_id)
            .where(RoleGrant.grant_role == grant_role)
            .where(RoleGrant.scope_property_id.is_(None))
        ).one_or_none()
        if existing is not None:
            return
        session.add(
            RoleGrant(
                id=new_ulid(),
                workspace_id=workspace_id,
                user_id=user_id,
                grant_role=grant_role,
                scope_property_id=None,
                created_at=now,
                created_by_user_id=None,
            )
        )
        session.flush()


def _ensure_owners_membership(
    session: SqlaSession,
    *,
    user_id: str,
    workspace_id: str,
    now: datetime,
) -> None:
    """Place ``user_id`` in the workspace's ``owners`` group, if not already.

    No-op when the workspace has no ``owners`` group (should never
    happen — :func:`provision_workspace_and_owner_seat` seeds it
    unconditionally — but defensive). Used when the user-exists /
    workspace-exists path needs to grant governance authority without
    re-running the seed.
    """
    with tenant_agnostic():
        owners_group = session.scalars(
            select(PermissionGroup)
            .where(PermissionGroup.workspace_id == workspace_id)
            .where(PermissionGroup.slug == "owners")
        ).one_or_none()
        if owners_group is None:
            return
        existing = session.scalars(
            select(PermissionGroupMember)
            .where(PermissionGroupMember.group_id == owners_group.id)
            .where(PermissionGroupMember.user_id == user_id)
        ).one_or_none()
        if existing is not None:
            return
        session.add(
            PermissionGroupMember(
                group_id=owners_group.id,
                user_id=user_id,
                workspace_id=workspace_id,
                added_at=now,
                added_by_user_id=None,
            )
        )
        session.flush()


def _resolve_or_create_user(
    session: SqlaSession,
    *,
    existing: User | None,
    email_lower: str,
    display_name: str,
    timezone: str,
    now: datetime,
) -> str:
    """Return the user id, inserting a fresh :class:`User` if needed."""
    if existing is not None:
        return existing.id
    user_id = new_ulid()
    # justification: ``user`` is identity-scoped; the ORM tenant
    # filter ignores it.
    with tenant_agnostic():
        session.add(
            User(
                id=user_id,
                email=email_lower,
                email_lower=email_lower,
                display_name=display_name,
                timezone=timezone,
                created_at=now,
            )
        )
        session.flush()
    return user_id


def _resolve_or_create_workspace(
    session: SqlaSession,
    *,
    existing: Workspace | None,
    slug: str,
    owner_user_id: str,
    now: datetime,
) -> str:
    """Return the workspace id, seeding a fresh one + groups if missing.

    When the workspace is missing we can't reuse
    :func:`provision_workspace_and_owner_seat` (it also inserts a
    :class:`User` row), so we replay the workspace-only half: insert
    the :class:`Workspace` row and call the same two seed helpers that
    the shared provisioning path uses, which take care of the
    ``owners`` group + sole member + ``manager`` role grant + the
    three empty non-owners groups + the
    ``workspace.owners_bootstrapped`` audit row.
    """
    if existing is not None:
        return existing.id
    workspace_id = new_ulid()
    # justification: seeding the tenancy anchor before any ctx exists;
    # the ORM tenant filter has nothing to apply.
    with tenant_agnostic():
        session.add(
            Workspace(
                id=workspace_id,
                slug=slug,
                name=slug,
                plan="free",
                quota_json={},
                created_at=now,
            )
        )
        session.flush()
        seed_ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=slug,
            actor_id=owner_user_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        seed_owners_system_group(
            session,
            seed_ctx,
            workspace_id=workspace_id,
            owner_user_id=owner_user_id,
        )
        seed_system_permission_groups(
            session,
            workspace_id=workspace_id,
        )
    return workspace_id


# ---------------------------------------------------------------------------
# Session mint — the importable entry point
# ---------------------------------------------------------------------------


def mint_session(
    *,
    email: str,
    workspace_slug: str,
    display_name: str | None = None,
    timezone: str = "UTC",
    role: Role = "owner",
    ua: str = "",
    ip: str = "127.0.0.1",
    accept_language: str = "",
) -> MintResult:
    """Return a fresh :class:`SessionIssue` for ``(email, workspace_slug)``.

    Creates the :class:`User` / :class:`Workspace` / :class:`RoleGrant`
    / owner-membership rows if missing, then mints a session via
    :func:`app.auth.session.issue`. Idempotent on user + workspace
    (the rows are reused when they already exist); a fresh session row
    is always minted so repeat calls produce distinct cookies.

    Writes one ``audit.dev.force_login`` row inside the UoW with
    structured context — the action must be visible in the audit trail
    so an operator can spot every dev-auth mint. The row is attributed
    to the freshly-resolved :class:`WorkspaceContext` when the
    workspace exists; the first call (which creates the workspace)
    also emits the usual ``workspace.owners_bootstrapped`` row via
    :func:`provision_workspace_and_owner_seat`.

    **No gate check here** — the importable function exists to be
    called from tests (where gate enforcement is covered with
    :func:`click.testing.CliRunner`). The CLI entry point in
    :func:`main` enforces the gates before calling through.

    Transaction model: one :func:`app.adapters.db.session.make_uow`
    scope around everything. A failure anywhere rolls the whole mint
    back — no partial workspace, no dangling session, no audit row.
    """
    email_lower = canonicalise_email(email)
    resolved_display_name = (
        display_name
        if display_name is not None
        else (email_lower.split("@", 1)[0] or "dev-user")
    )

    with make_uow() as uow_session:
        # ``make_uow`` returns the :class:`~app.adapters.db.ports.DbSession`
        # protocol; the concrete instance is always a
        # :class:`sqlalchemy.orm.Session` (see
        # :class:`app.adapters.db.session.UnitOfWorkImpl`). The assertion
        # narrows the type for the helpers below and matches the pattern
        # used across :mod:`app.api.v1.auth.*`.
        assert isinstance(uow_session, SqlaSession)
        session = uow_session
        now = SystemClock().now()

        existing_user = _find_user(session, email_lower)
        existing_workspace = _find_workspace(session, workspace_slug)
        user_created = existing_user is None
        workspace_created = existing_workspace is None

        if existing_workspace is None and existing_user is None:
            # Both fresh — one call to the shared provisioning helper
            # (workspace + user + user_workspace + four groups +
            # owners seat + ``workspace.owners_bootstrapped`` audit).
            user_id = new_ulid()
            workspace_id = new_ulid()
            provision_workspace_and_owner_seat(
                session,
                workspace_id=workspace_id,
                user_id=user_id,
                slug=workspace_slug,
                email_lower=email_lower,
                display_name=resolved_display_name,
                timezone=timezone,
                now=now,
            )
        else:
            user_id = _resolve_or_create_user(
                session,
                existing=existing_user,
                email_lower=email_lower,
                display_name=resolved_display_name,
                timezone=timezone,
                now=now,
            )
            workspace_id = _resolve_or_create_workspace(
                session,
                existing=existing_workspace,
                slug=workspace_slug,
                owner_user_id=user_id,
                now=now,
            )

        # Idempotent ensure-* passes: harmless no-ops when the
        # greenfield helper already laid the rows down, row-inserts
        # otherwise. Keeping the final shape identical for every
        # branch means the audit row is always accurate.
        _ensure_user_workspace(
            session, user_id=user_id, workspace_id=workspace_id, now=now
        )
        _ensure_role_grant(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            role=role,
            now=now,
        )
        if role in _OWNER_ROLES:
            _ensure_owners_membership(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
                now=now,
            )

        has_owner_grant = role in _OWNER_ROLES
        # Forward the resolved :class:`Settings` to :func:`issue` so
        # the session's HKDF pepper is derived from the same root key
        # the rest of the deployment uses. ``issue`` falls back to
        # :func:`get_settings` when ``settings=None``, but routing
        # explicitly here keeps the test-injection seam honest — a
        # test that monkeypatches ``dev_login.get_settings`` gets a
        # single source of truth instead of a split brain between the
        # CLI wrapper and the domain service.
        settings = get_settings()
        session_issue = issue(
            session,
            user_id=user_id,
            workspace_id=workspace_id,
            has_owner_grant=has_owner_grant,
            ua=ua,
            ip=ip,
            accept_language=accept_language,
            now=now,
            settings=settings,
        )
        # Wipe the fingerprint so the session validates against any
        # caller's ``User-Agent`` / ``Accept-Language`` pair — curl,
        # Playwright, and ad-hoc httpx all differ, and pinning the
        # row to a single UA would brittle the dev flow the script
        # exists to enable. :func:`app.auth.session.validate` skips
        # the fingerprint gate when ``row.fingerprint_hash is None``
        # (see its docstring — "pre-hardening rows"), so nulling it
        # downgrades to the idle + absolute caps, which is the right
        # level of guarantee for a dev-only cookie.
        fingerprint_row = session.get(SessionRow, session_issue.session_id)
        assert fingerprint_row is not None  # just inserted
        fingerprint_row.fingerprint_hash = None
        with tenant_agnostic():
            session.flush()

        # Audit row — attributed to the resolved workspace so the
        # forensic trail joins on ``workspace_id``. ``actor_kind`` is
        # ``system`` because the invoker is a script, not the end
        # user whose session we just minted; the user id is in the
        # diff for grep-ability.
        audit_ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
            actor_id="00000000000000000000000000",
            actor_kind="system",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            audit_correlation_id=new_ulid(),
        )
        write_audit(
            session,
            audit_ctx,
            entity_kind="session",
            entity_id=session_issue.session_id,
            action="dev.force_login",
            diff={
                "email": email_lower,
                "workspace_slug": workspace_slug,
                "role": role,
                "user_id": user_id,
                "workspace_id": workspace_id,
                "user_created": user_created,
                "workspace_created": workspace_created,
                "has_owner_grant": has_owner_grant,
            },
        )

        return MintResult(
            session_issue=session_issue,
            user_created=user_created,
            workspace_created=workspace_created,
        )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_output(cookie_value: str, output: OutputFormat) -> str:
    """Return the stdout string for ``output``. Never trailing newline."""
    if output == "cookie":
        return f"{_COOKIE_NAME}={cookie_value}"
    if output == "json":
        return json.dumps({"name": _COOKIE_NAME, "value": cookie_value})
    if output == "curl":
        # Single-quoted so the shell preserves the cookie verbatim;
        # the cookie value is base64url (no single quotes) so the
        # escape is safe without shell-quoting gymnastics.
        return f"-b '{_COOKIE_NAME}={cookie_value}'"
    if output == "header":
        return f"Cookie: {_COOKIE_NAME}={cookie_value}"
    # mypy sees Literal exhaustion; defensive branch guards against a
    # future enum extension that forgets to update this switch.
    raise ValueError(f"unsupported output format: {output!r}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


@click.command(
    help=(
        "Dev-only: mint a session cookie for (email, workspace) without "
        "passkey. Hard-gated on CREWDAY_DEV_AUTH=1 + profile=dev + "
        "sqlite."
    )
)
@click.option("--email", required=True, help="User email address.")
@click.option(
    "--workspace",
    "workspace_slug",
    required=True,
    help="Workspace slug (created if missing).",
)
@click.option(
    "--display-name",
    default=None,
    help="User display name (defaults to the email local-part).",
)
@click.option(
    "--timezone",
    default="UTC",
    help="User timezone IANA name (default: UTC).",
)
@click.option(
    "--role",
    type=click.Choice(["owner", "manager", "worker"]),
    default="owner",
    help="Role grant on the workspace (default: owner).",
)
@click.option(
    "--output",
    type=click.Choice(["cookie", "json", "curl", "header"]),
    default="cookie",
    help="Output format (default: cookie).",
)
@click.option(
    "--ua",
    default="",
    help=(
        "User-Agent seed for the session row (stored only as a hash). "
        "Unused by default — the session's fingerprint gate is wiped "
        "so any caller UA validates."
    ),
)
@click.option(
    "--ip",
    default="127.0.0.1",
    help="IP hash seed for the session row.",
)
@click.option(
    "--accept-language",
    default="",
    help=(
        "Accept-Language seed (stored only as part of the fingerprint "
        "hash, which this script wipes — see ``--ua``)."
    ),
)
def main(
    email: str,
    workspace_slug: str,
    display_name: str | None,
    timezone: str,
    role: Role,
    output: OutputFormat,
    ua: str,
    ip: str,
    accept_language: str,
) -> None:
    """CLI entry point — runs the gates, mints, prints the cookie.

    click handles argument parsing + ``--help``; we own exit-code
    semantics. A gate failure exits 1 with a stderr message naming
    the failing gate; any other exception bubbles up to click (which
    exits 1 with its usage-style traceback). A successful mint exits
    0 with exactly one line on stdout.
    """
    try:
        _check_gates()
    except _GateError as exc:
        raise SystemExit(_fail_gate(exc)) from exc

    result = mint_session(
        email=email,
        workspace_slug=workspace_slug,
        display_name=display_name,
        timezone=timezone,
        role=role,
        ua=ua,
        ip=ip,
        accept_language=accept_language,
    )
    # Interactive paste-guard. When stdout is a TTY the cookie is about
    # to land on a terminal the user will probably copy wholesale —
    # drop a one-line banner on stderr so a screenshot / chat paste is
    # at least self-warned that the value is a live bearer token. On
    # pipe / redirect we stay silent so ``cookie=$(dev-login.sh ...)``
    # capture stays a single clean line.
    if sys.stdout.isatty():
        print(
            "# Dev-only session cookie — do not commit or log this value.",
            file=sys.stderr,
        )
    click.echo(_format_output(result.session_issue.cookie_value, output))


if __name__ == "__main__":
    main()
