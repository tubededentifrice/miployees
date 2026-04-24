"""Integration tests for :mod:`app.adapters.db.identity` against a real DB.

Covers the post-migration schema shape (tables, unique
``email_lower``, FK cascades), the referential-integrity contract
on the user-scoped tables (``passkey_credential``, ``session``,
``api_token`` all CASCADE on ``user``; ``session`` and ``api_token``
additionally CASCADE on ``workspace``), the case-insensitive email
uniqueness enforced via ``email_lower``, and the binary round-trip
on ``PasskeyCredential.public_key``.

The sibling ``tests/unit/test_db_identity.py`` covers pure-Python
model construction without the migration harness.

See ``docs/specs/02-domain-model.md`` §"users" /
§"passkey_credential" / §"session" / §"api_token" and
``docs/specs/03-auth-and-tokens.md`` §"Data model".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from app.adapters.db.identity.models import (
    ApiToken,
    PasskeyCredential,
    Session,
    User,
)
from app.adapters.db.workspace.models import Workspace
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_ctx() -> Iterator[None]:
    """Every test starts with no active :class:`WorkspaceContext`.

    Identity tables are not workspace-scoped, but other tests in the
    suite may leave a ctx set through ``set_current`` — clear it so
    each test observes a fresh baseline.
    """
    token = set_current(None)
    try:
        yield
    finally:
        reset_current(token)


class TestMigrationShape:
    """The migration lands the four identity tables with their indexes."""

    def test_user_table_exists(self, engine: Engine) -> None:
        assert "user" in inspect(engine).get_table_names()

    def test_passkey_credential_table_exists(self, engine: Engine) -> None:
        assert "passkey_credential" in inspect(engine).get_table_names()

    def test_session_table_exists(self, engine: Engine) -> None:
        assert "session" in inspect(engine).get_table_names()

    def test_api_token_table_exists(self, engine: Engine) -> None:
        assert "api_token" in inspect(engine).get_table_names()

    def test_user_columns_match_v1_slice(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("user")}
        expected = {
            "id",
            "email",
            "email_lower",
            "display_name",
            "locale",
            "timezone",
            "avatar_blob_hash",
            "created_at",
            "last_login_at",
        }
        assert set(cols) == expected
        # NOT NULL surface: everything except the optional profile + last-login
        # fields.
        nullable = {"locale", "timezone", "avatar_blob_hash", "last_login_at"}
        for name in expected - nullable:
            assert cols[name]["nullable"] is False, f"{name} must be NOT NULL"
        for name in nullable:
            assert cols[name]["nullable"] is True, f"{name} must be nullable"
        pk = inspect(engine).get_pk_constraint("user")
        assert pk["constrained_columns"] == ["id"]

    def test_user_email_lower_unique(self, engine: Engine) -> None:
        unique_cols: list[list[str]] = [
            uc["column_names"] for uc in inspect(engine).get_unique_constraints("user")
        ]
        unique_idx_cols: list[list[str]] = [
            ix["column_names"]
            for ix in inspect(engine).get_indexes("user")
            if ix.get("unique")
        ]
        assert ["email_lower"] in unique_cols + unique_idx_cols

    def test_passkey_credential_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("passkey_credential")}
        expected = {
            "id",
            "user_id",
            "public_key",
            "sign_count",
            "transports",
            "backup_eligible",
            "label",
            "created_at",
            "last_used_at",
        }
        assert set(cols) == expected

    def test_passkey_credential_user_index(self, engine: Engine) -> None:
        indexes = {
            ix["name"]: ix for ix in inspect(engine).get_indexes("passkey_credential")
        }
        assert "ix_passkey_credential_user" in indexes
        assert indexes["ix_passkey_credential_user"]["column_names"] == ["user_id"]

    def test_session_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("session")}
        expected = {
            "id",
            "user_id",
            "workspace_id",
            "expires_at",
            "absolute_expires_at",
            "last_seen_at",
            "ua_hash",
            "ip_hash",
            "fingerprint_hash",
            "invalidated_at",
            "invalidation_cause",
            "created_at",
        }
        assert set(cols) == expected
        # Signed-in users pick a workspace post-login.
        assert cols["workspace_id"]["nullable"] is True

    def test_session_user_expires_index(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("session")}
        assert "ix_session_user_expires" in indexes
        assert indexes["ix_session_user_expires"]["column_names"] == [
            "user_id",
            "expires_at",
        ]

    def test_api_token_columns(self, engine: Engine) -> None:
        cols = {c["name"]: c for c in inspect(engine).get_columns("api_token")}
        # cd-i1qe added ``kind`` / ``delegate_for_user_id`` /
        # ``subject_user_id``; see §03 "API tokens" for the shape
        # invariant the three columns encode.
        expected = {
            "id",
            "user_id",
            "workspace_id",
            "kind",
            "delegate_for_user_id",
            "subject_user_id",
            "label",
            "scope_json",
            "prefix",
            "hash",
            "expires_at",
            "last_used_at",
            "revoked_at",
            "created_at",
        }
        assert set(cols) == expected
        # ``workspace_id`` widened to nullable so PAT rows can land.
        assert cols["workspace_id"]["nullable"] is True
        assert cols["kind"]["nullable"] is False
        assert cols["delegate_for_user_id"]["nullable"] is True
        assert cols["subject_user_id"]["nullable"] is True

    def test_api_token_hash_unique(self, engine: Engine) -> None:
        unique_cols: list[list[str]] = [
            uc["column_names"]
            for uc in inspect(engine).get_unique_constraints("api_token")
        ]
        unique_idx_cols: list[list[str]] = [
            ix["column_names"]
            for ix in inspect(engine).get_indexes("api_token")
            if ix.get("unique")
        ]
        assert ["hash"] in unique_cols + unique_idx_cols

    def test_api_token_indexes(self, engine: Engine) -> None:
        indexes = {ix["name"]: ix for ix in inspect(engine).get_indexes("api_token")}
        assert "ix_api_token_user" in indexes
        assert indexes["ix_api_token_user"]["column_names"] == ["user_id"]
        assert "ix_api_token_workspace" in indexes
        assert indexes["ix_api_token_workspace"]["column_names"] == ["workspace_id"]

    def test_user_cascade_foreign_keys(self, engine: Engine) -> None:
        """Every user-scoped table CASCADE-deletes on the *owning* user FK.

        ``api_token`` carries three FKs to ``user`` since cd-i1qe:

        * ``user_id`` — the creator; CASCADE on delete (archiving the
          user hard-deletes every token they ever minted).
        * ``delegate_for_user_id`` — populated on delegated rows only;
          SET NULL on delete (the row's audit trail survives the
          delegating user's deletion, the service layer already
          returns 401 for a delegated token whose user is gone).
        * ``subject_user_id`` — populated on PATs only; SET NULL on
          delete for the same forensic reason.

        The invariant here is: every user-scoped table has exactly
        one *creator* FK that CASCADEs. Additional SET-NULL FKs that
        exist for audit preservation are allowed.
        """
        for table in ("passkey_credential", "session", "api_token"):
            cascade_fks = [
                fk
                for fk in inspect(engine).get_foreign_keys(table)
                if fk["referred_table"] == "user"
                and fk["options"].get("ondelete") == "CASCADE"
            ]
            assert len(cascade_fks) == 1, f"{table} missing its creator CASCADE user FK"
            assert cascade_fks[0]["constrained_columns"] == ["user_id"]

    def test_workspace_cascade_foreign_keys(self, engine: Engine) -> None:
        """Session and api_token CASCADE on ``workspace`` too."""
        for table in ("session", "api_token"):
            fks = [
                fk
                for fk in inspect(engine).get_foreign_keys(table)
                if fk["referred_table"] == "workspace"
            ]
            assert len(fks) == 1, f"{table} missing workspace FK"
            assert fks[0]["options"].get("ondelete") == "CASCADE"


class TestUserInsertAndRead:
    """Insert a user, commit, read back — ``email_lower`` is canonical."""

    def test_email_lower_written_by_listener(self, db_session: SaSession) -> None:
        """The ``before_insert`` hook rewrites ``email_lower`` even if the
        caller pre-fills it incorrectly.
        """
        user = User(
            id="01HWA00000000000000000USRR",
            email="Maria@Example.COM",
            email_lower="WRONG",  # listener overwrites this.
            display_name="Maria",
            created_at=_PINNED,
        )
        db_session.add(user)
        db_session.commit()
        db_session.expire_all()

        loaded = db_session.scalars(
            select(User).where(User.id == "01HWA00000000000000000USRR")
        ).one()
        assert loaded.email == "Maria@Example.COM"
        assert loaded.email_lower == "maria@example.com"


class TestUniqueEmailCaseInsensitive:
    """``email_lower`` rejects two users whose emails differ only in case."""

    def test_duplicate_case_variant_raises(self, db_session: SaSession) -> None:
        db_session.add(
            User(
                id="01HWA00000000000000000DUP1",
                email="dupe@example.com",
                email_lower="dupe@example.com",
                display_name="Dupe 1",
                created_at=_PINNED,
            )
        )
        db_session.flush()

        db_session.add(
            User(
                id="01HWA00000000000000000DUP2",
                email="DUPE@example.com",
                # Leave the pre-flush value stale on purpose; the listener
                # canonicalises to ``dupe@example.com`` and the unique
                # constraint fires.
                email_lower="DUPE@example.com",
                display_name="Dupe 2",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


class TestPasskeyBinaryRoundtrip:
    """``PasskeyCredential`` stores raw bytes for id + public_key."""

    def test_bytes_survive_roundtrip(self, db_session: SaSession) -> None:
        user = bootstrap_user(
            db_session,
            email="passkey@example.com",
            display_name="Passkey",
            clock=FrozenClock(_PINNED),
        )
        raw_id = bytes(range(32))
        raw_pk = bytes(range(32, 96))
        cred = PasskeyCredential(
            id=raw_id,
            user_id=user.id,
            public_key=raw_pk,
            sign_count=0,
            backup_eligible=False,
            created_at=_PINNED,
        )
        db_session.add(cred)
        db_session.commit()
        db_session.expire_all()

        loaded = db_session.scalars(
            select(PasskeyCredential).where(PasskeyCredential.id == raw_id)
        ).one()
        assert loaded.id == raw_id
        assert loaded.public_key == raw_pk
        # Default survives the roundtrip — SA defaults fire only when the
        # column is left unset in the INSERT. Set explicitly here to
        # mirror the happy-path insertion; a separate test covers the
        # bare default below.
        assert loaded.sign_count == 0
        assert loaded.backup_eligible is False

    def test_sign_count_default_is_zero(self, db_session: SaSession) -> None:
        """Leaving ``sign_count`` unset gives 0 via the column default."""
        user = bootstrap_user(
            db_session,
            email="signcount@example.com",
            display_name="SignCount",
            clock=FrozenClock(_PINNED),
        )
        cred = PasskeyCredential(
            id=b"\xde\xad\xbe\xef",
            user_id=user.id,
            public_key=b"\x00" * 32,
            backup_eligible=False,
            created_at=_PINNED,
        )
        db_session.add(cred)
        db_session.flush()
        db_session.expire_all()

        loaded = db_session.scalars(
            select(PasskeyCredential).where(PasskeyCredential.id == b"\xde\xad\xbe\xef")
        ).one()
        assert loaded.sign_count == 0


class TestSessionNullableWorkspace:
    """``Session.workspace_id`` accepts NULL for pre-pick sessions."""

    def test_session_without_workspace_id(self, db_session: SaSession) -> None:
        user = bootstrap_user(
            db_session,
            email="sess@example.com",
            display_name="Sess",
            clock=FrozenClock(_PINNED),
        )
        sess = Session(
            id="01HWA00000000000000000SESX",
            user_id=user.id,
            workspace_id=None,
            expires_at=_PINNED,
            last_seen_at=_PINNED,
            created_at=_PINNED,
        )
        db_session.add(sess)
        db_session.commit()
        db_session.expire_all()

        loaded = db_session.scalars(
            select(Session).where(Session.id == "01HWA00000000000000000SESX")
        ).one()
        assert loaded.workspace_id is None


class TestApiTokenHashUnique:
    """``api_token.hash`` rejects a duplicate."""

    def test_duplicate_hash_raises(self, db_session: SaSession) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="tokuser@example.com",
            display_name="TokUser",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="tok-ws",
            name="TokWS",
            owner_user_id=user.id,
            clock=clock,
        )
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000TOK1",
                user_id=user.id,
                workspace_id=ws.id,
                label="first",
                scope_json={},
                prefix="mip_aaaa",
                hash="c" * 64,
                created_at=_PINNED,
            )
        )
        db_session.flush()

        db_session.add(
            ApiToken(
                id="01HWA00000000000000000TOK2",
                user_id=user.id,
                workspace_id=ws.id,
                label="second",
                scope_json={},
                prefix="mip_bbbb",
                hash="c" * 64,
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


class TestApiTokenKindShapeConstraint:
    """cd-i1qe ``ck_api_token_kind_shape`` blocks malformed rows at the DB layer.

    The service layer (:mod:`app.auth.tokens`) enforces the same
    invariant, but the DB CHECK is belt-and-braces: a future caller
    that bypasses the service seam (raw SQL, bulk insert) still can't
    write a shape-violating row. Every branch of the XOR is exercised
    here so a future edit to the constraint catches a drift even if
    the service test didn't move.
    """

    def _make_prereqs(self, db_session: SaSession) -> tuple[str, str]:
        """Seed a user + workspace; return ``(user_id, workspace_id)``."""
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="shape@example.com",
            display_name="Shape",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="shape-ws",
            name="ShapeWS",
            owner_user_id=user.id,
            clock=clock,
        )
        return user.id, ws.id

    def test_scoped_without_workspace_id_is_rejected(
        self, db_session: SaSession
    ) -> None:
        user_id, _ws_id = self._make_prereqs(db_session)
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000SHP1",
                user_id=user_id,
                workspace_id=None,  # scoped MUST carry a workspace
                kind="scoped",
                label="bad",
                scope_json={},
                prefix="pre",
                hash="h1",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_delegated_without_delegate_fk_is_rejected(
        self, db_session: SaSession
    ) -> None:
        user_id, ws_id = self._make_prereqs(db_session)
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000SHP2",
                user_id=user_id,
                workspace_id=ws_id,
                kind="delegated",
                delegate_for_user_id=None,  # delegated MUST carry it
                label="bad",
                scope_json={},
                prefix="pre",
                hash="h2",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_delegated_without_workspace_is_rejected(
        self, db_session: SaSession
    ) -> None:
        user_id, _ws_id = self._make_prereqs(db_session)
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000SHP3",
                user_id=user_id,
                workspace_id=None,  # delegated MUST carry a workspace
                kind="delegated",
                delegate_for_user_id=user_id,
                label="bad",
                scope_json={},
                prefix="pre",
                hash="h3",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_personal_with_workspace_is_rejected(self, db_session: SaSession) -> None:
        user_id, ws_id = self._make_prereqs(db_session)
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000SHP4",
                user_id=user_id,
                workspace_id=ws_id,  # personal MUST NOT carry a workspace
                kind="personal",
                subject_user_id=user_id,
                label="bad",
                scope_json={"me.tasks:read": True},
                prefix="pre",
                hash="h4",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_personal_with_delegate_fk_is_rejected(self, db_session: SaSession) -> None:
        user_id, _ws_id = self._make_prereqs(db_session)
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000SHP5",
                user_id=user_id,
                workspace_id=None,
                kind="personal",
                subject_user_id=user_id,
                delegate_for_user_id=user_id,  # mutually exclusive with subject
                label="bad",
                scope_json={"me.tasks:read": True},
                prefix="pre",
                hash="h5",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_unknown_kind_is_rejected(self, db_session: SaSession) -> None:
        """``ck_api_token_kind`` allowlist refuses a typo'd kind."""
        user_id, ws_id = self._make_prereqs(db_session)
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000SHP6",
                user_id=user_id,
                workspace_id=ws_id,
                kind="scopped",  # typo
                label="bad",
                scope_json={},
                prefix="pre",
                hash="h6",
                created_at=_PINNED,
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_valid_shapes_land(self, db_session: SaSession) -> None:
        """Happy path: one row per kind survives the CHECK."""
        user_id, ws_id = self._make_prereqs(db_session)
        # scoped
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000SHP7",
                user_id=user_id,
                workspace_id=ws_id,
                kind="scoped",
                label="ok-sc",
                scope_json={"tasks:read": True},
                prefix="ps",
                hash="ok1",
                created_at=_PINNED,
            )
        )
        db_session.flush()
        # delegated
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000SHP8",
                user_id=user_id,
                workspace_id=ws_id,
                kind="delegated",
                delegate_for_user_id=user_id,
                label="ok-dl",
                scope_json={},
                prefix="pd",
                hash="ok2",
                created_at=_PINNED,
            )
        )
        db_session.flush()
        # personal
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000SHP9",
                user_id=user_id,
                workspace_id=None,
                kind="personal",
                subject_user_id=user_id,
                label="ok-pat",
                scope_json={"me.tasks:read": True},
                prefix="pp",
                hash="ok3",
                created_at=_PINNED,
            )
        )
        db_session.flush()


class TestCascadeOnUserDelete:
    """Deleting a user sweeps their passkey / session / api_token rows."""

    def test_delete_user_cascades(self, db_session: SaSession) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="cascade@example.com",
            display_name="Cascade",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="cascade-ws",
            name="CascadeWS",
            owner_user_id=user.id,
            clock=clock,
        )
        db_session.add(
            PasskeyCredential(
                id=b"\xca\x5c\xad\xe0",
                user_id=user.id,
                public_key=b"\x00" * 32,
                sign_count=0,
                backup_eligible=False,
                created_at=_PINNED,
            )
        )
        db_session.add(
            Session(
                id="01HWA00000000000000000SESC",
                user_id=user.id,
                workspace_id=None,
                expires_at=_PINNED,
                last_seen_at=_PINNED,
                created_at=_PINNED,
            )
        )
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000TOKC",
                user_id=user.id,
                workspace_id=ws.id,
                label="cascade-tok",
                scope_json={},
                prefix="mip_cccc",
                hash="d" * 64,
                created_at=_PINNED,
            )
        )
        db_session.flush()

        # Sanity: rows exist.
        assert db_session.scalars(
            select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
        ).all()
        assert db_session.scalars(
            select(Session).where(Session.user_id == user.id)
        ).all()
        assert db_session.scalars(
            select(ApiToken).where(ApiToken.user_id == user.id)
        ).all()

        db_session.delete(user)
        db_session.flush()

        # Every user-scoped row gone.
        assert (
            db_session.scalars(
                select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)
            ).all()
            == []
        )
        assert (
            db_session.scalars(select(Session).where(Session.user_id == user.id)).all()
            == []
        )
        assert (
            db_session.scalars(
                select(ApiToken).where(ApiToken.user_id == user.id)
            ).all()
            == []
        )


class TestCascadeOnWorkspaceDelete:
    """Deleting a workspace sweeps its ``session`` + ``api_token`` rows."""

    def test_workspace_delete_cascades_to_tokens_and_sessions(
        self, db_session: SaSession
    ) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="wscascade@example.com",
            display_name="WsCascade",
            clock=clock,
        )
        ws = bootstrap_workspace(
            db_session,
            slug="ws-cascade",
            name="WsCascade",
            owner_user_id=user.id,
            clock=clock,
        )
        db_session.add(
            Session(
                id="01HWA00000000000000000SESD",
                user_id=user.id,
                workspace_id=ws.id,
                expires_at=_PINNED,
                last_seen_at=_PINNED,
                created_at=_PINNED,
            )
        )
        db_session.add(
            ApiToken(
                id="01HWA00000000000000000TOKD",
                user_id=user.id,
                workspace_id=ws.id,
                label="ws-tok",
                scope_json={},
                prefix="mip_dddd",
                hash="e" * 64,
                created_at=_PINNED,
            )
        )
        db_session.flush()

        loaded_ws = db_session.get(Workspace, ws.id)
        assert loaded_ws is not None
        db_session.delete(loaded_ws)
        db_session.flush()

        assert (
            db_session.scalars(
                select(Session).where(Session.workspace_id == ws.id)
            ).all()
            == []
        )
        assert (
            db_session.scalars(
                select(ApiToken).where(ApiToken.workspace_id == ws.id)
            ).all()
            == []
        )


class TestBootstrapUserHelper:
    """The ``bootstrap_user`` seed helper canonicalises email."""

    def test_seeds_user_with_canonical_email(self, db_session: SaSession) -> None:
        clock = FrozenClock(_PINNED)
        user = bootstrap_user(
            db_session,
            email="BootStrap@Example.com",
            display_name="Bootstrap",
            clock=clock,
        )
        assert user.email == "BootStrap@Example.com"
        assert user.email_lower == "bootstrap@example.com"
        # SQLite's ``DateTime(timezone=True)`` loses tzinfo on reload;
        # mirror the workspace test's wall-clock comparison.
        assert user.created_at.replace(tzinfo=None) == _PINNED.replace(tzinfo=None)
