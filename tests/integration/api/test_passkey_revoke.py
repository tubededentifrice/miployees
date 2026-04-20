"""Integration test for ``DELETE /auth/passkey/{credential_id}`` (cd-hiko).

End-to-end against the real :mod:`app.api.v1.auth.passkey` router with a
live DB session (integration harness engine — SQLite by default,
Postgres when ``CREWDAY_TEST_DB=postgres``). The test:

1. seeds a user with two passkey credentials and a real session row
   (via :func:`app.auth.session.issue`),
2. calls DELETE on one credential id,
3. asserts the HTTP 204 response,
4. asserts the DB state: one credential row dropped, the other
   intact, and the session row invalidated with
   ``invalidation_cause = "passkey_revoked"``.

The other router-shape cases (wrong owner, last credential,
malformed id, auth gate) live in
``tests/unit/auth/test_passkey_router.py`` — the unit path uses the
same FastAPI seam and adds no coverage when run through the slower
integration harness.

See cd-hiko + ``docs/specs/15-security-privacy.md`` §"Shared-origin
XSS containment".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.identity.models import PasskeyCredential
from app.adapters.db.identity.models import Session as SessionRow
from app.api.deps import current_workspace_context
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth.passkey import router
from app.auth import session as session_module
from app.auth.session import SessionInvalid
from app.auth.session import issue as session_issue
from app.auth.session import validate as session_validate
from app.auth.webauthn import bytes_to_base64url
from app.config import Settings
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def pinned_settings() -> Settings:
    """Settings pinned for deterministic session peppering + TTLs."""
    return Settings.model_construct(
        database_url="sqlite:///:memory:",  # not used; see db_session fixture
        root_key=SecretStr("integration-test-passkey-revoke-root-key"),
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def seeded(
    db_session: Session,
) -> tuple[WorkspaceContext, str]:
    """Seed one user + workspace inside the test's savepoint.

    Returns ``(ctx, user_id)`` — the ctx's ``actor_id`` matches the
    bootstrapped user so the router's ownership check passes.
    """
    from app.adapters.db.workspace.models import Workspace

    ws = Workspace(
        id=new_ulid(),
        slug="revoke-int",
        name="Revoke Integration",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    db_session.add(ws)
    db_session.flush()
    user = bootstrap_user(
        db_session,
        email="revoke-int@example.com",
        display_name="Revoke Integration",
        clock=FrozenClock(_PINNED),
    )
    db_session.flush()
    ctx = WorkspaceContext(
        workspace_id=ws.id,
        workspace_slug=ws.slug,
        actor_id=user.id,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000RVKI",
    )
    return ctx, user.id


def _build_app(
    db_session: Session,
    ctx: WorkspaceContext,
) -> FastAPI:
    """Wire the real passkey router with a dep override that hands back
    the integration harness's transaction-scoped session.

    Mirrors the override shape in
    :mod:`tests.unit.auth.test_passkey_router` but routes the dep back
    to the harness's savepoint-bound :class:`Session` so every write
    lands under the same rollback.
    """
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def _override_ctx() -> WorkspaceContext:
        return ctx

    def _override_db() -> Iterator[Session]:
        # Yield the harness session directly — it's bound to a savepoint
        # that the conftest rolls back at teardown, so committing here
        # is safe and the test asserts against the same session.
        yield db_session

    app.dependency_overrides[current_workspace_context] = _override_ctx
    app.dependency_overrides[db_session_dep] = _override_db
    return app


class TestDeletePasskeyIntegration:
    """End-to-end DELETE: real router + real DB session (savepoint)."""

    def test_delete_drops_row_and_invalidates_sessions(
        self,
        db_session: Session,
        seeded: tuple[WorkspaceContext, str],
        pinned_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pin the session module's settings so the integration pepper
        # matches the one the seeded session carries.
        monkeypatch.setattr(
            session_module,
            "get_settings",
            lambda: pinned_settings,
            raising=False,
        )

        ctx, user_id = seeded

        # Seed two credentials (so last-credential gate doesn't fire)
        # and one live session row for the user.
        target_cid = b"\xaa" * 32
        other_cid = b"\xbb" * 32
        for cid in (target_cid, other_cid):
            db_session.add(
                PasskeyCredential(
                    id=cid,
                    user_id=user_id,
                    public_key=b"\x88" * 64,
                    sign_count=0,
                    transports="internal",
                    backup_eligible=False,
                    label=None,
                    created_at=_PINNED,
                )
            )
        db_session.flush()

        issued = session_issue(
            db_session,
            user_id=user_id,
            has_owner_grant=True,
            ua="integration-ua",
            ip="127.0.0.1",
            now=_PINNED,
            settings=pinned_settings,
        )
        db_session.flush()

        app = _build_app(db_session, ctx)
        client = TestClient(app)

        resp = client.delete(
            f"/api/v1/auth/passkey/{bytes_to_base64url(target_cid)}",
        )
        assert resp.status_code == 204, resp.text
        assert resp.content == b""

        # Flush FastAPI's request-scoped writes back into the harness
        # session's view — the override yielded the same session, so
        # ``get`` below reads the router's writes directly.
        db_session.expire_all()

        # Row dropped; sibling intact.
        assert db_session.get(PasskeyCredential, target_cid) is None
        assert db_session.get(PasskeyCredential, other_cid) is not None

        # Session row flipped with the right cause (non-destructive).
        row = db_session.get(SessionRow, issued.session_id)
        assert row is not None
        assert row.invalidated_at is not None
        assert row.invalidation_cause == "passkey_revoked"

        # Replay the seeded cookie through the real validator — a
        # future regression that lets ``validate`` still accept an
        # invalidated row would pass the DB-state assertion above but
        # fail here. This is the authoritative "cookie no longer
        # works" check without standing up the middleware stack.
        with pytest.raises(SessionInvalid):
            session_validate(
                db_session,
                cookie_value=issued.cookie_value,
                now=_PINNED,
                settings=pinned_settings,
            )

        # Audit trail: passkey.revoked before session.invalidated.
        actions = list(
            db_session.scalars(
                select(AuditLog.action)
                .where(AuditLog.action.in_(["passkey.revoked", "session.invalidated"]))
                .order_by(AuditLog.id)
            ).all()
        )
        assert actions == ["passkey.revoked", "session.invalidated"], (
            f"audit order {actions!r}"
        )
