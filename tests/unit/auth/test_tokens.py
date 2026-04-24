"""Unit tests for :mod:`app.auth.tokens` and :mod:`app.api.v1.auth.tokens`.

Covers the cd-c91 acceptance surface end-to-end at the domain-service
level plus the thin HTTP router on top:

* Mint — happy path, cap, argon2id shape, prefix derivation, one-
  time plaintext, audit row content, scope dict round-trip.
* Verify — happy path, malformed token, unknown ``key_id``, expired,
  revoked, bad secret, ``last_used_at`` debouncing.
* Revoke — happy path, idempotent double-revoke, cross-workspace
  guard, 404 for unknown id.
* List — returns both active and revoked, never the hash, most-recent
  first.

Runs against an in-memory SQLite engine with :class:`Base.metadata`
schema. argon2id hashing is real (no stub) — the test suite exercises
the exact same hasher the production path uses so we don't skip
surface the real behaviour wraps.

See ``docs/specs/03-auth-and-tokens.md`` §"API tokens" and
``docs/specs/15-security-privacy.md`` §"Token hashing".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import ApiToken
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.auth.tokens import (
    InvalidToken,
    MintedToken,
    TokenExpired,
    TokenKindInvalid,
    TokenRevoked,
    TokenShapeError,
    TooManyPersonalTokens,
    TooManyTokens,
    list_personal_tokens,
    list_tokens,
    mint,
    revoke,
    revoke_personal,
    verify,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.ulid import new_ulid
from tests.factories.identity import bootstrap_user

_PINNED = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalise a SQLite-roundtripped datetime to aware UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


@pytest.fixture
def workspace(db_session: Session) -> Workspace:
    """Seed a workspace row for the token FK target."""
    ws_id = new_ulid()
    ws = Workspace(
        id=ws_id,
        slug="ws-tokens",
        name="Tokens WS",
        plan="free",
        quota_json={},
        created_at=_PINNED,
    )
    with tenant_agnostic():
        db_session.add(ws)
        db_session.flush()
    return ws


@pytest.fixture
def user(db_session: Session) -> object:
    """Seed a user row for the token FK target."""
    return bootstrap_user(db_session, email="tok@example.com", display_name="Tok User")


@pytest.fixture
def ctx(workspace: Workspace, user: object) -> WorkspaceContext:
    """Return a :class:`WorkspaceContext` scoped to the seeded workspace + user."""
    return WorkspaceContext(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        actor_id=user.id,  # type: ignore[attr-defined]
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id=new_ulid(),
    )


# ---------------------------------------------------------------------------
# ``mint``
# ---------------------------------------------------------------------------


class TestMint:
    """``mint`` returns plaintext once, persists the argon2id hash + audit."""

    def test_happy_path_returns_mip_prefixed_token(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result: MintedToken = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="hermes",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        # ``mip_<key_id>_<secret>`` — 4 + 26 + 1 + 52 = 83 chars.
        assert result.token.startswith("mip_")
        _mip, key_id, secret = result.token.split("_", 2)
        assert key_id == result.key_id
        assert len(key_id) == 26  # ULID
        assert len(secret) == 52  # base32(32 bytes) with padding stripped
        assert result.prefix == secret[:8]
        assert result.expires_at == _PINNED + timedelta(days=90)

    def test_row_carries_argon2id_hash(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Stored ``hash`` is the argon2id PHC string, never plaintext."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="hash-check",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            now=_PINNED,
        )
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        # PHC string shape: ``$argon2id$v=19$m=...,t=...,p=...$<salt>$<digest>``
        assert row.hash.startswith("$argon2id$")
        # Plaintext secret must never appear in the stored hash.
        _mip, _key_id, secret = result.token.split("_", 2)
        assert secret not in row.hash

    def test_prefix_matches_first_8_chars_of_secret(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="prefix-check",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        _mip, _key_id, secret = result.token.split("_", 2)
        assert row.prefix == secret[:8]
        assert row.prefix == result.prefix

    def test_scopes_round_trip(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        scopes = {"tasks:read": True, "stays:read": True}
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scopes",
            scopes=scopes,
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.scope_json == scopes

    def test_audit_row_carries_no_plaintext(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """The mint audit must contain prefix + label but NEVER the plaintext."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="audit-check",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "api_token.minted")
        ).all()
        assert len(audits) == 1
        row = audits[0]
        assert row.entity_kind == "api_token"
        assert row.entity_id == result.key_id
        assert isinstance(row.diff, dict)
        assert row.diff["prefix"] == result.prefix
        assert row.diff["label"] == "audit-check"
        assert row.diff["scopes"] == ["tasks:read"]
        # Plaintext token must not appear anywhere in the diff.
        _mip, _key_id, secret = result.token.split("_", 2)
        serialised = repr(row.diff)
        assert secret not in serialised
        assert result.token not in serialised

    def test_too_many_tokens_raises_on_sixth_mint(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """A 6th live token on the same user + workspace raises TooManyTokens."""
        for i in range(5):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"t-{i}",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )
        with pytest.raises(TooManyTokens):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="6th",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )

    def test_expired_tokens_do_not_count_against_cap(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Expired rows don't block a new mint — they're inert."""
        # 5 already-expired tokens.
        for i in range(5):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"exp-{i}",
                scopes={},
                expires_at=_PINNED - timedelta(days=1),
                now=_PINNED - timedelta(days=2),
            )
        # 6th mint against ``now=_PINNED`` — the five earlier rows have
        # ``expires_at`` in the past, so the cap doesn't fire.
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="fresh",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        assert result.key_id

    def test_revoked_tokens_do_not_count_against_cap(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Revoked rows don't block a new mint."""
        # Mint 5 tokens then revoke them all.
        for i in range(5):
            out = mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"rev-{i}",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )
            revoke(db_session, ctx, token_id=out.key_id, now=_PINNED)
        # 6th mint must succeed.
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="fresh",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        assert result.key_id


# ---------------------------------------------------------------------------
# ``verify``
# ---------------------------------------------------------------------------


class TestVerify:
    """``verify`` resolves user + workspace + scopes or raises."""

    def test_happy_path_returns_user_and_scopes(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        scopes = {"tasks:read": True, "stays:read": True}
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="verify",
            scopes=scopes,
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.user_id == ctx.actor_id
        assert verified.workspace_id == ctx.workspace_id
        assert verified.scopes == scopes
        assert verified.key_id == result.key_id

    def test_malformed_token_raises_invalid(self, db_session: Session) -> None:
        for bad in [
            "not-a-token",
            "mip_",
            "mip_only-key-id",
            "mip__no-key-id",
            "mip_key_",
        ]:
            with pytest.raises(InvalidToken):
                verify(db_session, token=bad, now=_PINNED)

    def test_unknown_key_id_raises_invalid(self, db_session: Session) -> None:
        """A well-formed token with a ``key_id`` that doesn't exist → InvalidToken."""
        # 26-char ULID shape but no row.
        fake_key = "01HWA00000000000000000XXXX"
        # 52-char base32-looking secret.
        fake_secret = "A" * 52
        with pytest.raises(InvalidToken):
            verify(
                db_session,
                token=f"mip_{fake_key}_{fake_secret}",
                now=_PINNED,
            )

    def test_expired_token_raises(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="expires",
            scopes={},
            expires_at=_PINNED + timedelta(days=1),
            now=_PINNED,
        )
        with pytest.raises(TokenExpired):
            verify(
                db_session,
                token=result.token,
                now=_PINNED + timedelta(days=2),
            )

    def test_revoked_token_raises(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="revoked",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        revoke(db_session, ctx, token_id=result.key_id, now=_PINNED)
        with pytest.raises(TokenRevoked):
            verify(db_session, token=result.token, now=_PINNED)

    def test_bad_secret_raises_invalid(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """A tampered secret collapses into InvalidToken (not TokenRevoked /
        TokenExpired)."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="tamper",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        # Flip the first char of the secret.
        _mip, key_id, secret = result.token.split("_", 2)
        tampered_secret = ("A" if secret[0] != "A" else "B") + secret[1:]
        tampered = f"mip_{key_id}_{tampered_secret}"
        with pytest.raises(InvalidToken):
            verify(db_session, token=tampered, now=_PINNED)

    def test_last_used_at_not_updated_within_debounce(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Two verifies within 1 min leave ``last_used_at`` on the first write."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="debounce",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        # First verify — ``last_used_at`` is NULL, bump lands.
        t1 = _PINNED + timedelta(hours=1)
        verify(db_session, token=result.token, now=t1)
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.last_used_at is not None
        assert _as_utc(row.last_used_at) == t1

        # Second verify 30s later — within 1min debounce, skip.
        t2 = t1 + timedelta(seconds=30)
        verify(db_session, token=result.token, now=t2)
        db_session.expire(row)
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.last_used_at is not None
        assert _as_utc(row.last_used_at) == t1  # unchanged

    def test_last_used_at_updated_past_debounce(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Two verifies 90s apart both bump ``last_used_at``."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="debounce2",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        t1 = _PINNED + timedelta(hours=1)
        verify(db_session, token=result.token, now=t1)
        t2 = t1 + timedelta(seconds=90)
        verify(db_session, token=result.token, now=t2)
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.last_used_at is not None
        assert _as_utc(row.last_used_at) == t2

    def test_first_use_null_bumps_last_used(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="null-bump",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        row_before = db_session.get(ApiToken, result.key_id)
        assert row_before is not None
        assert row_before.last_used_at is None

        verify(db_session, token=result.token, now=_PINNED)

        row_after = db_session.get(ApiToken, result.key_id)
        assert row_after is not None
        assert row_after.last_used_at is not None


# ---------------------------------------------------------------------------
# ``revoke``
# ---------------------------------------------------------------------------


class TestRevoke:
    """``revoke`` flips ``revoked_at`` and audits."""

    def test_sets_revoked_at_and_audits(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="revoke-me",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        revoke_time = _PINNED + timedelta(hours=2)
        revoke(db_session, ctx, token_id=result.key_id, now=revoke_time)

        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is not None
        assert _as_utc(row.revoked_at) == revoke_time

        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "api_token.revoked")
        ).all()
        assert len(audits) == 1
        audit = audits[0]
        assert audit.entity_kind == "api_token"
        assert audit.entity_id == result.key_id

    def test_double_revoke_is_idempotent_and_audits_noop(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="idem",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        first = _PINNED + timedelta(hours=1)
        second = _PINNED + timedelta(hours=2)
        revoke(db_session, ctx, token_id=result.key_id, now=first)
        revoke(db_session, ctx, token_id=result.key_id, now=second)

        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is not None
        # ``revoked_at`` stays at the first revocation time.
        assert _as_utc(row.revoked_at) == first

        audits = db_session.scalars(
            select(AuditLog).where(AuditLog.action == "api_token.revoked_noop")
        ).all()
        assert len(audits) == 1

    def test_unknown_token_id_raises_invalid(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(InvalidToken):
            revoke(
                db_session,
                ctx,
                token_id="01HWA00000000000000000NOPE",
                now=_PINNED,
            )

    def test_cross_workspace_revoke_rejected(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        workspace: Workspace,
    ) -> None:
        """A ctx on workspace B cannot revoke a token on workspace A."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="cross",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        # Build a second workspace row + ctx.
        other_id = new_ulid()
        with tenant_agnostic():
            db_session.add(
                Workspace(
                    id=other_id,
                    slug="other-ws",
                    name="Other",
                    plan="free",
                    quota_json={},
                    created_at=_PINNED,
                )
            )
            db_session.flush()
        other_ctx = WorkspaceContext(
            workspace_id=other_id,
            workspace_slug="other-ws",
            actor_id=ctx.actor_id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id=new_ulid(),
        )
        with pytest.raises(InvalidToken):
            revoke(db_session, other_ctx, token_id=result.key_id, now=_PINNED)


# ---------------------------------------------------------------------------
# ``list_tokens``
# ---------------------------------------------------------------------------


class TestListTokens:
    """``list_tokens`` projects rows onto :class:`TokenSummary`."""

    def test_returns_summaries_with_prefix_never_hash(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="listed",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        summaries = list_tokens(db_session, ctx)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.key_id == result.key_id
        assert s.label == "listed"
        assert s.prefix == result.prefix
        assert s.scopes == {"tasks:read": True}
        assert s.revoked_at is None
        # :class:`TokenSummary` doesn't expose ``hash`` at all.
        assert not hasattr(s, "hash")

    def test_includes_revoked_rows(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """A revoked row still appears — the /tokens UI needs the history."""
        active = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="active",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        dead = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="dead",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        revoke(db_session, ctx, token_id=dead.key_id, now=_PINNED)

        summaries = list_tokens(db_session, ctx)
        key_ids = {s.key_id for s in summaries}
        assert {active.key_id, dead.key_id} <= key_ids
        dead_summary = next(s for s in summaries if s.key_id == dead.key_id)
        assert dead_summary.revoked_at is not None

    def test_empty_workspace_returns_empty(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        assert list_tokens(db_session, ctx) == []


# ---------------------------------------------------------------------------
# cd-i1qe — delegated tokens
# ---------------------------------------------------------------------------


class TestMintDelegated:
    """Delegated mint path — workspace-pinned, scope-less, inherits user grants."""

    def test_happy_path_returns_delegated_kind_and_fk(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result: MintedToken = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="chat-agent",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        assert result.kind == "delegated"
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.kind == "delegated"
        assert row.delegate_for_user_id == ctx.actor_id
        assert row.subject_user_id is None
        assert row.workspace_id == ctx.workspace_id
        assert row.scope_json == {}

    def test_delegated_token_verifies_with_delegate_fk(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """``verify`` returns kind + delegate_for_user_id on the happy path."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="chat",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.kind == "delegated"
        assert verified.delegate_for_user_id == ctx.actor_id
        assert verified.subject_user_id is None
        assert verified.workspace_id == ctx.workspace_id
        # Spec: delegated tokens have empty scopes — authority
        # resolves against the delegating user's grants at request
        # time, not against the token itself.
        assert verified.scopes == {}

    def test_delegated_mint_with_scopes_raises_shape_error(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="bad",
                scopes={"tasks:read": True},
                expires_at=_PINNED + timedelta(days=30),
                kind="delegated",
                delegate_for_user_id=ctx.actor_id,
                now=_PINNED,
            )

    def test_delegated_mint_without_delegate_fk_raises_shape_error(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="bad",
                scopes={},
                expires_at=_PINNED + timedelta(days=30),
                kind="delegated",
                delegate_for_user_id=None,
                now=_PINNED,
            )

    def test_delegated_counts_against_workspace_cap(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Mixed 4 scoped + 1 delegated → 6th mint (either kind) 422s."""
        for i in range(4):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"sc-{i}",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )
        mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="delegate",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        with pytest.raises(TooManyTokens):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="6th",
                scopes={},
                expires_at=_PINNED + timedelta(days=30),
                kind="delegated",
                delegate_for_user_id=ctx.actor_id,
                now=_PINNED,
            )

    def test_audit_row_carries_kind_and_delegate_fk(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="audit-del",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        audits = db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "api_token.minted")
            .where(AuditLog.entity_id == result.key_id)
        ).all()
        assert len(audits) == 1
        row = audits[0]
        assert isinstance(row.diff, dict)
        assert row.diff["kind"] == "delegated"
        assert row.diff["delegate_for_user_id"] == ctx.actor_id


# ---------------------------------------------------------------------------
# cd-i1qe — personal access tokens (PATs)
# ---------------------------------------------------------------------------


class TestMintPersonal:
    """PAT mint path — identity-scoped, ``me:*`` scopes, workspace NULL."""

    def test_happy_path_returns_personal_kind_and_workspace_null(
        self, db_session: Session, user: object
    ) -> None:
        """PATs carry workspace_id=NULL, subject_user_id populated."""
        user_id = user.id  # type: ignore[attr-defined]
        result: MintedToken = mint(
            db_session,
            None,
            user_id=user_id,
            label="kitchen-printer",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        assert result.kind == "personal"
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.kind == "personal"
        assert row.workspace_id is None
        assert row.subject_user_id == user_id
        assert row.delegate_for_user_id is None

    def test_personal_token_verifies_with_null_workspace(
        self, db_session: Session, user: object
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.bookings:read": True, "me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        verified = verify(db_session, token=result.token, now=_PINNED)
        assert verified.kind == "personal"
        assert verified.workspace_id is None
        assert verified.subject_user_id == user_id
        assert verified.delegate_for_user_id is None
        assert verified.scopes == {
            "me.bookings:read": True,
            "me.tasks:read": True,
        }

    def test_personal_mint_with_workspace_ctx_raises_shape_error(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="bad",
                scopes={"me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=ctx.actor_id,
                now=_PINNED,
            )

    def test_personal_mint_with_workspace_scope_raises_shape_error(
        self, db_session: Session, user: object
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                None,
                user_id=user_id,
                label="bad",
                scopes={"tasks:read": True},  # workspace scope!
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )

    def test_personal_mint_without_scopes_raises_shape_error(
        self, db_session: Session, user: object
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                None,
                user_id=user_id,
                label="bad",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )

    def test_sixth_personal_token_raises_too_many_personal(
        self, db_session: Session, user: object
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        for i in range(5):
            mint(
                db_session,
                None,
                user_id=user_id,
                label=f"pat-{i}",
                scopes={"me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )
        with pytest.raises(TooManyPersonalTokens):
            mint(
                db_session,
                None,
                user_id=user_id,
                label="6th",
                scopes={"me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )

    def test_personal_does_not_count_against_workspace_cap(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """5 PATs + 5 workspace tokens all coexist — separate caps."""
        user_id = user.id  # type: ignore[attr-defined]
        for i in range(5):
            mint(
                db_session,
                None,
                user_id=user_id,
                label=f"pat-{i}",
                scopes={"me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                kind="personal",
                subject_user_id=user_id,
                now=_PINNED,
            )
        # 5 workspace tokens still fit.
        for i in range(5):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label=f"ws-{i}",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )
        # 6th workspace token now 422s, but PAT cap is independent.
        with pytest.raises(TooManyTokens):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="over",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )


# ---------------------------------------------------------------------------
# cd-i1qe — list / revoke seam narrowing
# ---------------------------------------------------------------------------


class TestListPersonal:
    """``list_personal_tokens`` returns only PATs for the subject."""

    def test_includes_only_personal_tokens(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        # 1 PAT + 1 scoped + 1 delegated for the same user.
        pat = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scoped",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="delegated",
            scopes={},
            expires_at=_PINNED + timedelta(days=30),
            kind="delegated",
            delegate_for_user_id=ctx.actor_id,
            now=_PINNED,
        )
        summaries = list_personal_tokens(db_session, subject_user_id=user_id)
        key_ids = {s.key_id for s in summaries}
        assert key_ids == {pat.key_id}
        assert summaries[0].kind == "personal"

    def test_workspace_list_excludes_personal_tokens(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """``list_tokens`` (workspace view) never surfaces PATs."""
        user_id = user.id  # type: ignore[attr-defined]
        # 1 PAT + 1 scoped — only the scoped row should appear.
        mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        scoped = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scoped",
            scopes={"tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        summaries = list_tokens(db_session, ctx)
        key_ids = {s.key_id for s in summaries}
        assert key_ids == {scoped.key_id}


class TestRevokePersonal:
    """``revoke_personal`` is subject-scoped and refuses workspace tokens."""

    def test_revokes_own_pat(self, db_session: Session, user: object) -> None:
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        revoke_personal(
            db_session,
            token_id=result.key_id,
            subject_user_id=user_id,
            now=_PINNED + timedelta(hours=1),
        )
        row = db_session.get(ApiToken, result.key_id)
        assert row is not None
        assert row.revoked_at is not None
        # Re-verify now fails with TokenRevoked.
        with pytest.raises(TokenRevoked):
            verify(db_session, token=result.token, now=_PINNED + timedelta(hours=2))

    def test_revoke_another_users_pat_raises_invalid(
        self, db_session: Session, user: object
    ) -> None:
        """Subject B cannot revoke subject A's PAT."""
        user_a_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_a_id,
            label="a-pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_a_id,
            now=_PINNED,
        )
        # Pass a different user id — the row shouldn't match.
        other_id = new_ulid()
        with pytest.raises(InvalidToken):
            revoke_personal(
                db_session,
                token_id=result.key_id,
                subject_user_id=other_id,
                now=_PINNED,
            )

    def test_revoke_personal_on_workspace_token_raises_invalid(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """``revoke_personal`` refuses a workspace-scoped token id."""
        result = mint(
            db_session,
            ctx,
            user_id=ctx.actor_id,
            label="scoped",
            scopes={},
            expires_at=_PINNED + timedelta(days=90),
            now=_PINNED,
        )
        with pytest.raises(InvalidToken):
            revoke_personal(
                db_session,
                token_id=result.key_id,
                subject_user_id=ctx.actor_id,
                now=_PINNED,
            )

    def test_workspace_revoke_on_personal_token_raises_invalid(
        self,
        db_session: Session,
        ctx: WorkspaceContext,
        user: object,
    ) -> None:
        """``revoke`` (workspace) refuses to touch a PAT row."""
        user_id = user.id  # type: ignore[attr-defined]
        result = mint(
            db_session,
            None,
            user_id=user_id,
            label="pat",
            scopes={"me.tasks:read": True},
            expires_at=_PINNED + timedelta(days=90),
            kind="personal",
            subject_user_id=user_id,
            now=_PINNED,
        )
        with pytest.raises(InvalidToken):
            revoke(db_session, ctx, token_id=result.key_id, now=_PINNED)


class TestKindValidation:
    """Domain vocabulary guards."""

    def test_unknown_kind_raises_token_kind_invalid(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        with pytest.raises(TokenKindInvalid):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="bad",
                scopes={},
                expires_at=_PINNED + timedelta(days=90),
                kind="scopped",  # type: ignore[arg-type]
                now=_PINNED,
            )

    def test_scoped_mint_with_me_scope_raises_shape_error(
        self, db_session: Session, ctx: WorkspaceContext
    ) -> None:
        """Mixing a me:* scope into a scoped token is refused."""
        with pytest.raises(TokenShapeError):
            mint(
                db_session,
                ctx,
                user_id=ctx.actor_id,
                label="mix",
                scopes={"tasks:read": True, "me.tasks:read": True},
                expires_at=_PINNED + timedelta(days=90),
                now=_PINNED,
            )
