"""Integration tests for the shared abuse-mitigation gates.

Two acceptance criteria from cd-7huk land here:

* **Passkey login: 11th begin attempt in a minute returns 429.**
  Exercises the :func:`app.abuse.throttle.throttle` decorator wired
  to ``/auth/passkey/login/start`` via
  :func:`app.api.v1.auth.passkey.build_login_router`. Spec §15
  "Rate limiting and abuse controls": *"10/min per IP for login
  begin"*.

* **Signup with a disposable domain returns ``422 disposable_email``.**
  Regression test against the new bundled list location
  (``app/abuse/data/disposable_domains.txt``) — confirms the path
  migration did not break the signup abuse guard. Spec §15 maps
  this to a 4xx ``disposable_email`` envelope; the app uses 422
  (per the existing ``SignupStartBody`` convention); we pin the
  current behaviour so a path-migration regression is caught.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.identity.models import WebAuthnChallenge
from app.api.deps import db_session as db_session_dep
from app.api.v1.auth.passkey import build_login_router
from app.auth import passkey as passkey_module
from app.auth import signup_abuse
from app.auth._throttle import Throttle
from app.auth.webauthn import RelyingParty
from app.config import Settings

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings.model_construct(
        database_url="sqlite:///:memory:",
        root_key=SecretStr("integration-abuse-guards-root-key"),
        public_url="http://localhost:8000",
        session_owner_ttl_days=7,
        session_user_ttl_days=30,
    )


@pytest.fixture
def rp() -> RelyingParty:
    return RelyingParty(
        rp_id="localhost",
        rp_name="crew.day",
        origin="http://localhost:8000",
        allowed_origins=("http://localhost:8000",),
    )


@pytest.fixture
def throttle() -> Throttle:
    return Throttle()


@pytest.fixture
def login_client(
    engine: Engine,
    settings: Settings,
    throttle: Throttle,
    rp: RelyingParty,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` mounted on the login router only.

    We build the router with the default ``begin_shield=None`` so the
    throttle store is fresh for this test case — the decorator will
    instantiate its own per-router :class:`ShieldStore`.

    Each HTTP request opens its own :class:`Session` bound to ``engine``
    and commits on clean exit / rolls back on exception — matching
    the production UoW. We don't need refusal-path audit tables here
    because the throttle-before-handler path never reaches the audit
    write seam.
    """
    monkeypatch.setattr(
        passkey_module,
        "make_relying_party",
        lambda settings=None: rp,
    )
    monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

    router = build_login_router(throttle=throttle, settings=settings)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

    def _session() -> Iterator[Session]:
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[db_session_dep] = _session

    with TestClient(app) as c:
        yield c

    # Session-scoped ``engine`` is shared across every integration
    # test, so challenge rows committed by ``/login/start`` above
    # would otherwise leak into sibling tests (e.g.
    # :mod:`tests.integration.auth.test_passkey_login_pg` asserts
    # zero-challenge-rows post-flow). Drop the challenges we created
    # — no other table is touched by the throttle-before-handler
    # path.
    with factory() as s:
        for challenge in s.scalars(select(WebAuthnChallenge)).all():
            s.delete(challenge)
        s.commit()


# ---------------------------------------------------------------------------
# Acceptance criterion: passkey login 10/min/IP
# ---------------------------------------------------------------------------


class TestPasskeyLoginBeginRateLimit:
    """Spec §15: 10/min per IP for login begin; 11th returns 429."""

    def test_eleventh_login_begin_in_a_minute_returns_429(
        self, login_client: TestClient
    ) -> None:
        # Ten calls in rapid succession must all succeed.
        for i in range(10):
            resp = login_client.post("/api/v1/auth/passkey/login/start")
            assert resp.status_code == 200, (
                f"call #{i + 1} unexpectedly failed: {resp.status_code} {resp.text}"
            )
            body = resp.json()
            assert "challenge_id" in body
            assert "options" in body

        # Eleventh call trips the rate limiter.
        over = login_client.post("/api/v1/auth/passkey/login/start")
        assert over.status_code == 429, over.text
        assert over.json()["detail"] == {"error": "rate_limited"}

    def test_separate_clients_have_independent_budgets(
        self,
        engine: Engine,
        settings: Settings,
        throttle: Throttle,
        rp: RelyingParty,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FastAPI's TestClient binds a fixed client host (``testclient``);
        two clients off the *same* router therefore share the per-IP
        bucket by design — proving per-IP isolation end-to-end needs
        two routers (each with its own shield) or a live request with
        distinct ``X-Forwarded-For`` plumbing, neither of which belongs
        in cd-7huk's minimal AC coverage.

        What we can prove here without over-scoping: two **independently
        built** routers (each with its own :class:`ShieldStore` because
        that is the default shape) do not share state, so a capped IP
        on router A doesn't poison router B. Mirrors the production
        shape of one shield-per-process when the binary restarts.
        """
        monkeypatch.setattr(
            passkey_module,
            "make_relying_party",
            lambda settings=None: rp,
        )
        monkeypatch.setattr("app.auth.session.get_settings", lambda: settings)

        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

        def _session() -> Iterator[Session]:
            s = factory()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()

        # Router A — fill its budget.
        app_a = FastAPI()
        app_a.include_router(
            build_login_router(throttle=throttle, settings=settings), prefix="/api/v1"
        )
        app_a.dependency_overrides[db_session_dep] = _session

        # Router B — separate shield, separate bucket.
        app_b = FastAPI()
        app_b.include_router(
            build_login_router(throttle=throttle, settings=settings), prefix="/api/v1"
        )
        app_b.dependency_overrides[db_session_dep] = _session

        try:
            with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
                # A: burn through 10 + trip to 429.
                for _ in range(10):
                    assert (
                        client_a.post("/api/v1/auth/passkey/login/start").status_code
                        == 200
                    )
                assert (
                    client_a.post("/api/v1/auth/passkey/login/start").status_code == 429
                )
                # B: still fully fresh despite A being capped.
                assert (
                    client_b.post("/api/v1/auth/passkey/login/start").status_code == 200
                )
        finally:
            # Clean up challenge rows we committed so sibling
            # integration tests see the empty tables they expect.
            with factory() as s:
                for challenge in s.scalars(select(WebAuthnChallenge)).all():
                    s.delete(challenge)
                s.commit()


# ---------------------------------------------------------------------------
# Acceptance criterion: disposable-domain block (new bundled path)
# ---------------------------------------------------------------------------


class TestDisposableDomainBlocklist:
    """Spec §15: the bundled list blocks known throwaway providers.

    Pins behaviour **against the new file path**
    (``app/abuse/data/disposable_domains.txt``) — if the path migration
    or pin format drift silently broke the loader, the list would
    load as empty and every throwaway address would slip through. The
    integration test here consults :func:`signup_abuse.is_disposable`
    directly; a full end-to-end signup test of the same assertion
    lives in :mod:`tests.integration.auth.test_signup_abuse_wired`.
    """

    def test_known_disposable_domain_is_blocked(self) -> None:
        # ``mailinator.com`` is on the bundled curated seed (see
        # app/abuse/data/disposable_domains.txt). Force a reload so
        # the module picks up the new path in case a sibling test
        # cached the previous one.
        signup_abuse.reload_disposable_domains()
        assert signup_abuse.is_disposable("anyone@mailinator.com") is True

    def test_non_disposable_domain_passes(self) -> None:
        signup_abuse.reload_disposable_domains()
        assert signup_abuse.is_disposable("owner@example.com") is False

    def test_reload_picks_up_the_new_bundled_path(self) -> None:
        """``reload_disposable_domains()`` must return a non-zero count
        after re-reading the bundled file at the new location — i.e.
        the path migration did not accidentally zero-out the loader."""
        count = signup_abuse.reload_disposable_domains()
        assert count > 0, (
            "bundled disposable-domains list loaded as empty — "
            "the app/abuse/data/ path migration may be broken"
        )
