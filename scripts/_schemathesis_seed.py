"""Internal helper for ``scripts/schemathesis_run.sh`` (cd-3j25).

Mints a workspace-scoped API token plaintext on stdout so the runner
can hand a Bearer token to ``schemathesis run``. Runs in the same
process as the rest of the schemathesis sweep tooling, against a
dev-only SQLite database.

The token is minted via the domain service
(:func:`app.auth.tokens.mint`) directly rather than through the
HTTP surface — the HTTP path requires a CSRF round-trip + a session
cookie that we'd otherwise have to thread through curl, which is
brittle. The domain call has the same audit + cap semantics as the
HTTP route; bypassing the wire layer here is fine for a dev-only
seed helper.

Hard-gated on ``CREWDAY_DEV_AUTH=1`` + ``profile=dev`` + a SQLite
URL — same gates as :mod:`scripts.dev_login`. A misconfigured prod
deploy that happens to flip the env var still fails.

Output (one line, no trailing newline):

* ``--output token``   : ``<plaintext>``
* ``--output bearer``  : ``Authorization: Bearer <plaintext>``

Spec refs: ``docs/specs/03-auth-and-tokens.md`` §"API tokens",
``docs/specs/17-testing-quality.md`` §"API contract".
"""

from __future__ import annotations

import os
import sys
from typing import Final, Literal

import click
from sqlalchemy import select
from sqlalchemy.orm import Session as SqlaSession

from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import Workspace
from app.auth.tokens import mint as mint_token
from app.config import get_settings
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import SystemClock
from app.util.ulid import new_ulid
from scripts.dev_login import mint_session

OutputFormat = Literal["token", "bearer", "session", "cookie"]
_SESSION_COOKIE_NAME: Final[str] = "__Host-crewday_session"


_DEV_AUTH_ENV_VAR: Final[str] = "CREWDAY_DEV_AUTH"


def _check_gates() -> None:
    """Refuse to run unless the dev-auth gates are green.

    Mirrors :mod:`scripts.dev_login`'s gate set so a single env switch
    blocks both helpers.
    """
    raw = os.environ.get(_DEV_AUTH_ENV_VAR, "0").lower()
    if raw not in {"1", "yes", "true"}:
        raise SystemExit(
            f"error: {_DEV_AUTH_ENV_VAR} not set to 1/yes/true; refusing to run."
        )
    settings = get_settings()
    if settings.profile != "dev":
        raise SystemExit(
            f"error: CREWDAY_PROFILE={settings.profile!r} — schemathesis seed "
            "requires profile=dev."
        )
    scheme = settings.database_url.split(":", 1)[0].lower()
    if not scheme.startswith("sqlite"):
        raise SystemExit(
            f"error: database_url scheme {scheme!r} is not SQLite; refusing."
        )


@click.command(
    help=(
        "Dev-only: seed a workspace + mint a Bearer token + dev session "
        "for schemathesis."
    )
)
@click.option(
    "--email", default="schemathesis@dev.local", help="Dev-login email address."
)
@click.option(
    "--workspace",
    "workspace_slug",
    default="schemathesis",
    help="Workspace slug (created if missing).",
)
@click.option("--label", default="schemathesis", help="Token audit label.")
@click.option(
    "--output",
    type=click.Choice(["token", "bearer", "session", "cookie"]),
    default="token",
    help="Output format. 'token' prints the API-token plaintext only; "
    "'bearer' prefixes with 'Authorization: Bearer '. 'session' prints the "
    "session cookie value only; 'cookie' prints the full "
    "'__Host-crewday_session=<value>' pair.",
)
def main(email: str, workspace_slug: str, label: str, output: OutputFormat) -> None:
    """CLI entry point — gate, seed, mint, print plaintext."""
    _check_gates()

    # 1. Drive the dev-login flow so user + workspace + role grants
    #    + the 4 system permission groups exist. The session is also
    #    surfaced — the runner uses it for bare-host paths that the
    #    workspace Bearer token can't reach.
    session_result = mint_session(
        email=email,
        workspace_slug=workspace_slug,
        role="owner",
    )

    # 2. Resolve the row ids the token mint needs. The dev-login
    #    helper is idempotent on (email, workspace_slug); we look the
    #    rows up with a fresh UoW so the token mint runs in its own
    #    transaction. Keep the lookups under :func:`tenant_agnostic`
    #    because both queries hit identity / workspace tables that
    #    pre-date any :class:`WorkspaceContext` we'd otherwise
    #    install.
    email_lower = canonicalise_email(email)
    with make_uow() as uow:
        assert isinstance(uow, SqlaSession)
        with tenant_agnostic():
            user = uow.scalars(
                select(User).where(User.email_lower == email_lower)
            ).one()
            workspace = uow.scalars(
                select(Workspace).where(Workspace.slug == workspace_slug)
            ).one()

        # 3. Mint a workspace-scoped token. ``scopes`` is left empty
        #    on purpose — empty-scope workspace tokens are the v1
        #    contract (`docs/specs/03-auth-and-tokens.md` §"Scopes":
        #    "Empty is allowed on v1"); the token still resolves
        #    capabilities through the user's role grants. That gives
        #    the schemathesis fuzzer a token that exercises every
        #    operation a real owner would.
        ctx = WorkspaceContext(
            workspace_id=workspace.id,
            workspace_slug=workspace_slug,
            actor_id=user.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        result = mint_token(
            uow,
            ctx,
            user_id=user.id,
            label=label,
            scopes={},
            expires_at=None,
            kind="scoped",
            now=SystemClock().now(),
        )
        plaintext = result.token

    if output == "token":
        sys.stdout.write(plaintext)
    elif output == "bearer":
        sys.stdout.write(f"Authorization: Bearer {plaintext}")
    elif output == "session":
        sys.stdout.write(session_result.session_issue.cookie_value)
    else:  # output == "cookie"
        sys.stdout.write(
            f"{_SESSION_COOKIE_NAME}={session_result.session_issue.cookie_value}"
        )


if __name__ == "__main__":
    main()
