"""Unit tests for :mod:`app.domain.messaging.push_tokens` (cd-0bnz).

Exercises the service surface against an in-memory SQLite engine
built via ``Base.metadata.create_all()`` — no alembic, no tenant
filter, just the ORM round-trip + the SSRF-gate + the audit
side-effects.

Covers:

* :func:`register` happy path — new row persists, one audit row
  with ``messaging.push.subscribed`` fires.
* :func:`register` idempotent — same ``(user_id, endpoint)`` second
  call returns the same view, no duplicate row, no duplicate audit.
* :func:`register` on an endpoint whose host is not in the allow-list
  → :class:`EndpointNotAllowed`.
* :func:`register` on an ``http://`` endpoint →
  :class:`EndpointSchemeInvalid`.
* :func:`register` on a userinfo-carrying endpoint →
  :class:`EndpointSchemeInvalid`.
* :func:`register` on a non-443 explicit-port endpoint →
  :class:`EndpointSchemeInvalid`.
* :func:`unregister` happy path — row deleted, one audit row with
  ``messaging.push.unsubscribed`` fires.
* :func:`unregister` on a missing row — no-op, no audit row written.
* :func:`get_vapid_public_key` happy path reads from
  ``workspace.settings_json``.
* :func:`get_vapid_public_key` raises :class:`VapidNotConfigured`
  when the setting is absent.
* :func:`list_for_user` self-only — cross-user listing raises
  :class:`PermissionError`.
* Cross-workspace tenancy: a token registered in workspace A is
  invisible to workspace B.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User, canonicalise_email
from app.adapters.db.messaging.models import PushToken
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.domain.messaging.push_tokens import (
    SETTINGS_KEY_VAPID_PUBLIC,
    EndpointNotAllowed,
    EndpointSchemeInvalid,
    VapidNotConfigured,
    get_vapid_public_key,
    list_for_user,
    register,
    unregister,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# A legal (allow-listed) FCM endpoint. Per-test suffix lets the
# idempotency tests exercise the upsert path without colliding.
def _fcm_endpoint(suffix: str = "alpha") -> str:
    return f"https://fcm.googleapis.com/fcm/send/{suffix}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_all_models() -> None:
    """Import every ``app.adapters.db.<context>.models`` so FKs resolve."""
    import importlib
    import pkgutil

    import app.adapters.db as pkg

    for modinfo in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
        if not modinfo.ispkg:
            continue
        try:
            importlib.import_module(f"{modinfo.name}.models")
        except ModuleNotFoundError as exc:
            if exc.name == f"{modinfo.name}.models":
                continue
            raise


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema built from ``Base.metadata``."""
    _load_all_models()
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


def _bootstrap_workspace(
    s: Session,
    *,
    slug: str,
    vapid_public: str | None = "vapid-pub-test-key",
) -> str:
    workspace_id = new_ulid()
    settings_json: dict[str, str] = {}
    if vapid_public is not None:
        settings_json[SETTINGS_KEY_VAPID_PUBLIC] = vapid_public
    s.add(
        Workspace(
            id=workspace_id,
            slug=slug,
            name=f"Workspace {slug}",
            plan="free",
            quota_json={},
            settings_json=settings_json,
            created_at=_PINNED,
        )
    )
    s.flush()
    return workspace_id


def _bootstrap_user(s: Session, *, email: str, display_name: str) -> str:
    user_id = new_ulid()
    s.add(
        User(
            id=user_id,
            email=email,
            email_lower=canonicalise_email(email),
            display_name=display_name,
            created_at=_PINNED,
        )
    )
    s.flush()
    return user_id


def _ctx(*, workspace_id: str, actor_id: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug="ws",
        actor_id=actor_id,
        actor_kind="user",
        actor_grant_role="worker",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _count_audit(s: Session, *, workspace_id: str, action: str) -> int:
    stmt = select(AuditLog).where(
        AuditLog.workspace_id == workspace_id,
        AuditLog.action == action,
    )
    return len(list(s.scalars(stmt).all()))


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


class TestRegister:
    def test_happy_path_persists_and_audits(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="reg-happy")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        clock = FrozenClock(_PINNED)

        view = register(
            session,
            ctx,
            endpoint=_fcm_endpoint("alpha"),
            p256dh="p256dh-test",
            auth="auth-test",
            user_agent="Mozilla/5.0 Test",
            clock=clock,
        )
        session.flush()

        assert view.user_id == user_id
        assert view.workspace_id == ws_id
        assert view.endpoint == _fcm_endpoint("alpha")
        assert view.user_agent == "Mozilla/5.0 Test"
        assert view.created_at == _PINNED

        rows = list(
            session.scalars(
                select(PushToken).where(PushToken.workspace_id == ws_id)
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].id == view.id
        assert rows[0].p256dh == "p256dh-test"
        assert rows[0].auth == "auth-test"

        assert (
            _count_audit(
                session,
                workspace_id=ws_id,
                action="messaging.push.subscribed",
            )
            == 1
        )

    def test_idempotent_second_call_returns_same_row(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="reg-idem")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        clock = FrozenClock(_PINNED)

        first = register(
            session,
            ctx,
            endpoint=_fcm_endpoint("idem"),
            p256dh="p1",
            auth="a1",
            user_agent="UA1",
            clock=clock,
        )
        second = register(
            session,
            ctx,
            endpoint=_fcm_endpoint("idem"),
            # Browser rotated the encryption material — the upsert
            # picks up the new values without writing a second row
            # or a duplicate audit entry.
            p256dh="p2",
            auth="a2",
            user_agent="UA2",
            clock=clock,
        )

        assert first.id == second.id
        rows = list(
            session.scalars(
                select(PushToken).where(PushToken.workspace_id == ws_id)
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].p256dh == "p2"
        assert rows[0].auth == "a2"
        assert rows[0].user_agent == "UA2"
        # One audit row total — the initial subscribe. A refresh is
        # not an interesting audit signal, per the service docstring.
        assert (
            _count_audit(
                session,
                workspace_id=ws_id,
                action="messaging.push.subscribed",
            )
            == 1
        )

    def test_endpoint_host_not_in_allow_list_rejected(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="reg-bad-host")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)

        with pytest.raises(EndpointNotAllowed):
            register(
                session,
                ctx,
                endpoint="https://attacker.example/push/sink",
                p256dh="p",
                auth="a",
                user_agent=None,
            )
        # No row and no audit row — reject landed before any side
        # effect.
        assert not session.scalars(
            select(PushToken).where(PushToken.workspace_id == ws_id)
        ).all()

    def test_http_scheme_rejected(self, session: Session) -> None:
        ws_id = _bootstrap_workspace(session, slug="reg-http")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)

        with pytest.raises(EndpointSchemeInvalid):
            register(
                session,
                ctx,
                endpoint="http://fcm.googleapis.com/fcm/send/xyz",
                p256dh="p",
                auth="a",
                user_agent=None,
            )

    def test_userinfo_rejected(self, session: Session) -> None:
        ws_id = _bootstrap_workspace(session, slug="reg-userinfo")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)

        with pytest.raises(EndpointSchemeInvalid):
            register(
                session,
                ctx,
                endpoint="https://user:pass@fcm.googleapis.com/fcm/send/x",
                p256dh="p",
                auth="a",
                user_agent=None,
            )

    def test_non_443_explicit_port_rejected(self, session: Session) -> None:
        ws_id = _bootstrap_workspace(session, slug="reg-port")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)

        with pytest.raises(EndpointSchemeInvalid):
            register(
                session,
                ctx,
                endpoint="https://fcm.googleapis.com:8443/fcm/send/x",
                p256dh="p",
                auth="a",
                user_agent=None,
            )

    def test_explicit_port_443_accepted(self, session: Session) -> None:
        """An explicit ``:443`` is equivalent to the default and accepted.

        Real browsers strip the default port, but some test harnesses
        and curl callers retain it. Rejecting it would surface a
        spurious 422 against a legitimate URL — defensive but not
        paranoid (see :func:`validate_endpoint` docstring).
        """
        ws_id = _bootstrap_workspace(session, slug="reg-port-443")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        clock = FrozenClock(_PINNED)

        view = register(
            session,
            ctx,
            endpoint="https://fcm.googleapis.com:443/fcm/send/x",
            p256dh="p",
            auth="a",
            user_agent=None,
            clock=clock,
        )
        assert view.endpoint == "https://fcm.googleapis.com:443/fcm/send/x"

    def test_fragment_rejected(self, session: Session) -> None:
        """A ``#fragment`` on the endpoint is rejected.

        The Web Push subscription URL the browser produces never
        carries one, so a fragment signals a caller bug or an
        attempt to slip past the SSRF gate (the eventual HTTP probe
        strips fragments).
        """
        ws_id = _bootstrap_workspace(session, slug="reg-fragment")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)

        with pytest.raises(EndpointSchemeInvalid):
            register(
                session,
                ctx,
                endpoint="https://fcm.googleapis.com/fcm/send/x#frag",
                p256dh="p",
                auth="a",
                user_agent=None,
            )

    def test_query_string_accepted(self, session: Session) -> None:
        """A ``?query`` is allowed — FCM emits ``?auth=...`` on some endpoints."""
        ws_id = _bootstrap_workspace(session, slug="reg-query")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        clock = FrozenClock(_PINNED)

        view = register(
            session,
            ctx,
            endpoint="https://fcm.googleapis.com/fcm/send/x?auth=tok",
            p256dh="p",
            auth="a",
            user_agent=None,
            clock=clock,
        )
        assert view.endpoint == "https://fcm.googleapis.com/fcm/send/x?auth=tok"


# ---------------------------------------------------------------------------
# unregister()
# ---------------------------------------------------------------------------


class TestUnregister:
    def test_happy_path_deletes_and_audits(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(session, slug="unreg-happy")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        clock = FrozenClock(_PINNED)

        register(
            session,
            ctx,
            endpoint=_fcm_endpoint("unreg"),
            p256dh="p",
            auth="a",
            user_agent=None,
            clock=clock,
        )
        session.flush()

        unregister(
            session,
            ctx,
            endpoint=_fcm_endpoint("unreg"),
            clock=clock,
        )
        session.flush()

        rows = list(
            session.scalars(
                select(PushToken).where(PushToken.workspace_id == ws_id)
            ).all()
        )
        assert rows == []
        assert (
            _count_audit(
                session,
                workspace_id=ws_id,
                action="messaging.push.unsubscribed",
            )
            == 1
        )

    def test_missing_row_is_noop(self, session: Session) -> None:
        ws_id = _bootstrap_workspace(session, slug="unreg-miss")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        clock = FrozenClock(_PINNED)

        unregister(
            session,
            ctx,
            endpoint=_fcm_endpoint("ghost"),
            clock=clock,
        )
        session.flush()

        # No audit row — nothing happened.
        assert (
            _count_audit(
                session,
                workspace_id=ws_id,
                action="messaging.push.unsubscribed",
            )
            == 0
        )

    def test_cannot_unsubscribe_other_users_token(self, session: Session) -> None:
        """Bob calling unregister with Alice's endpoint is a no-op.

        The service filters by ``user_id == ctx.actor_id``, so Bob's
        unsubscribe call against an endpoint Alice owns silently
        falls through the no-prior-row branch — Alice's row stays
        in the table, no audit row is written, and Bob does not
        get a misleading "deleted" signal.
        """
        ws_id = _bootstrap_workspace(session, slug="unreg-cross-user")
        alice = _bootstrap_user(session, email="a@example.com", display_name="A")
        bob = _bootstrap_user(session, email="b@example.com", display_name="B")
        session.commit()
        clock = FrozenClock(_PINNED)

        # Alice registers her browser.
        ctx_alice = _ctx(workspace_id=ws_id, actor_id=alice)
        register(
            session,
            ctx_alice,
            endpoint=_fcm_endpoint("alice-device"),
            p256dh="p",
            auth="a",
            user_agent=None,
            clock=clock,
        )
        session.flush()

        # Bob attempts to unsubscribe Alice's endpoint.
        ctx_bob = _ctx(workspace_id=ws_id, actor_id=bob)
        unregister(
            session,
            ctx_bob,
            endpoint=_fcm_endpoint("alice-device"),
            clock=clock,
        )
        session.flush()

        # Alice's row is still there.
        rows = list(
            session.scalars(
                select(PushToken).where(PushToken.workspace_id == ws_id)
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].user_id == alice

        # No unsubscribe audit row — Bob's call hit the no-op branch.
        assert (
            _count_audit(
                session,
                workspace_id=ws_id,
                action="messaging.push.unsubscribed",
            )
            == 0
        )


# ---------------------------------------------------------------------------
# get_vapid_public_key()
# ---------------------------------------------------------------------------


class TestVapidPublicKey:
    def test_reads_from_workspace_settings(
        self,
        session: Session,
    ) -> None:
        ws_id = _bootstrap_workspace(
            session, slug="vapid-ok", vapid_public="base64url-pubkey-xyz"
        )
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)

        key = get_vapid_public_key(session, ctx)
        assert key == "base64url-pubkey-xyz"

    def test_missing_raises(self, session: Session) -> None:
        ws_id = _bootstrap_workspace(session, slug="vapid-missing", vapid_public=None)
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)

        with pytest.raises(VapidNotConfigured):
            get_vapid_public_key(session, ctx)

    def test_empty_string_raises(self, session: Session) -> None:
        """An empty-string value is treated as not configured."""
        ws_id = _bootstrap_workspace(session, slug="vapid-empty", vapid_public="")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)

        with pytest.raises(VapidNotConfigured):
            get_vapid_public_key(session, ctx)


# ---------------------------------------------------------------------------
# list_for_user()
# ---------------------------------------------------------------------------


class TestListForUser:
    def test_self_list_returns_rows(self, session: Session) -> None:
        ws_id = _bootstrap_workspace(session, slug="list-self")
        user_id = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=user_id)
        clock = FrozenClock(_PINNED)

        register(
            session,
            ctx,
            endpoint=_fcm_endpoint("a"),
            p256dh="p1",
            auth="a1",
            user_agent=None,
            clock=clock,
        )
        register(
            session,
            ctx,
            endpoint=_fcm_endpoint("b"),
            p256dh="p2",
            auth="a2",
            user_agent=None,
            clock=clock,
        )
        session.flush()

        views = list_for_user(session, ctx)
        assert len(views) == 2
        endpoints = {v.endpoint for v in views}
        assert endpoints == {_fcm_endpoint("a"), _fcm_endpoint("b")}

    def test_cross_user_list_rejected(self, session: Session) -> None:
        ws_id = _bootstrap_workspace(session, slug="list-cross")
        alice = _bootstrap_user(session, email="a@example.com", display_name="A")
        bob = _bootstrap_user(session, email="b@example.com", display_name="B")
        session.commit()
        ctx = _ctx(workspace_id=ws_id, actor_id=alice)

        with pytest.raises(PermissionError):
            list_for_user(session, ctx, user_id=bob)


# ---------------------------------------------------------------------------
# Cross-workspace tenancy
# ---------------------------------------------------------------------------


class TestTenancyIsolation:
    def test_token_visible_only_in_own_workspace(
        self,
        session: Session,
    ) -> None:
        ws_a = _bootstrap_workspace(session, slug="tenant-a")
        ws_b = _bootstrap_workspace(session, slug="tenant-b")
        user = _bootstrap_user(session, email="u@example.com", display_name="U")
        session.commit()

        clock = FrozenClock(_PINNED)
        ctx_a = _ctx(workspace_id=ws_a, actor_id=user)
        register(
            session,
            ctx_a,
            endpoint=_fcm_endpoint("a-only"),
            p256dh="p",
            auth="a",
            user_agent=None,
            clock=clock,
        )
        session.flush()

        # Same user, peer workspace — list returns nothing.
        ctx_b = _ctx(workspace_id=ws_b, actor_id=user)
        views = list_for_user(session, ctx_b)
        assert views == ()
