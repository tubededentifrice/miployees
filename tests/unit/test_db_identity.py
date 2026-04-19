"""Unit tests for :mod:`app.adapters.db.identity.models`.

Pure-Python sanity on the SQLAlchemy mapped classes: construction,
tablenames, indexes, and the ``email_lower`` canonicalisation that
keeps the case-insensitive uniqueness contract portable across
SQLite and Postgres (see module docstring). Integration coverage
(migrations, FK cascade, uniqueness violations on a real DB) lives
in ``tests/integration/test_db_identity.py``.

See ``docs/specs/02-domain-model.md`` §"users" /
§"passkey_credential" / §"session" / §"api_token" and
``docs/specs/03-auth-and-tokens.md`` §"Data model".
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

from sqlalchemy import Index

from app.adapters.db.identity.models import (
    ApiToken,
    PasskeyCredential,
    Session,
    User,
    _user_before_insert,
    _user_before_update,
    canonicalise_email,
)

_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


class TestCanonicaliseEmail:
    """``canonicalise_email`` lowercases + strips whitespace."""

    def test_lowercases(self) -> None:
        assert canonicalise_email("Maria@Example.COM") == "maria@example.com"

    def test_strips_whitespace(self) -> None:
        assert canonicalise_email("  Maria@Example.com  ") == "maria@example.com"

    def test_already_canonical_is_noop(self) -> None:
        assert canonicalise_email("maria@example.com") == "maria@example.com"

    def test_unicode_local_part_roundtrip(self) -> None:
        """Non-ASCII local parts survive the case-fold.

        Accented letters fold deterministically under ``str.lower``;
        we want the fold to be locale-independent (no Turkish ``İ``
        surprises), which CPython guarantees.
        """
        assert canonicalise_email("MARÍA@example.com") == "maría@example.com"


class TestUserModel:
    """The ``User`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        user = User(
            id="01HWA00000000000000000USRA",
            email="Maria@Example.com",
            email_lower="maria@example.com",
            display_name="Maria",
            created_at=_PINNED,
        )
        assert user.id == "01HWA00000000000000000USRA"
        assert user.email == "Maria@Example.com"
        assert user.email_lower == "maria@example.com"
        assert user.display_name == "Maria"
        assert user.created_at == _PINNED
        # Optional columns default to ``None`` at construction time.
        assert user.locale is None
        assert user.timezone is None
        assert user.avatar_blob_hash is None
        assert user.last_login_at is None

    def test_tablename(self) -> None:
        assert User.__tablename__ == "user"

    def test_before_insert_listener_canonicalises(self) -> None:
        """The ``before_insert`` hook rewrites ``email_lower`` from ``email``.

        Called directly (no live DB) to prove the invariant: feed a
        mixed-case value in and the canonical form comes out, even if
        the caller forgot to pre-fill ``email_lower``.
        """
        target = User(
            id="01HWA00000000000000000USRB",
            email="  MiXeD@CASE.com  ",
            email_lower="stale-value-ignored",
            display_name="Mixed",
            created_at=_PINNED,
        )
        _user_before_insert(Mock(), Mock(), target)
        assert target.email_lower == "mixed@case.com"

    def test_before_update_listener_canonicalises(self) -> None:
        """The ``before_update`` hook keeps ``email_lower`` in sync on edit."""
        target = User(
            id="01HWA00000000000000000USRC",
            email="Renamed@Example.com",
            email_lower="old@example.com",
            display_name="Renamed",
            created_at=_PINNED,
        )
        _user_before_update(Mock(), Mock(), target)
        assert target.email_lower == "renamed@example.com"


class TestPasskeyCredentialModel:
    """The ``PasskeyCredential`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        cred = PasskeyCredential(
            id=b"\x01\x02\x03",
            user_id="01HWA00000000000000000USRA",
            public_key=b"\xaa\xbb\xcc",
            sign_count=0,
            backup_eligible=False,
            created_at=_PINNED,
        )
        assert cred.id == b"\x01\x02\x03"
        assert cred.user_id == "01HWA00000000000000000USRA"
        assert cred.public_key == b"\xaa\xbb\xcc"
        assert cred.sign_count == 0
        assert cred.backup_eligible is False
        assert cred.created_at == _PINNED
        assert cred.transports is None
        assert cred.label is None
        assert cred.last_used_at is None

    def test_tablename(self) -> None:
        assert PasskeyCredential.__tablename__ == "passkey_credential"

    def test_user_index_present(self) -> None:
        indexes = [i for i in PasskeyCredential.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_passkey_credential_user" in names
        target = next(i for i in indexes if i.name == "ix_passkey_credential_user")
        assert [c.name for c in target.columns] == ["user_id"]


class TestSessionModel:
    """The ``Session`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        sess = Session(
            id="01HWA00000000000000000SESA",
            user_id="01HWA00000000000000000USRA",
            expires_at=_PINNED,
            last_seen_at=_PINNED,
            created_at=_PINNED,
        )
        assert sess.id == "01HWA00000000000000000SESA"
        assert sess.user_id == "01HWA00000000000000000USRA"
        # ``workspace_id`` is nullable — signed-in users pick a workspace
        # post-login.
        assert sess.workspace_id is None
        assert sess.expires_at == _PINNED
        assert sess.last_seen_at == _PINNED
        assert sess.ua_hash is None
        assert sess.ip_hash is None

    def test_workspace_id_can_be_set(self) -> None:
        sess = Session(
            id="01HWA00000000000000000SESB",
            user_id="01HWA00000000000000000USRA",
            workspace_id="01HWA00000000000000000WSPA",
            expires_at=_PINNED,
            last_seen_at=_PINNED,
            created_at=_PINNED,
        )
        assert sess.workspace_id == "01HWA00000000000000000WSPA"

    def test_tablename(self) -> None:
        assert Session.__tablename__ == "session"

    def test_user_expires_index_present(self) -> None:
        indexes = [i for i in Session.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_session_user_expires" in names
        target = next(i for i in indexes if i.name == "ix_session_user_expires")
        assert [c.name for c in target.columns] == ["user_id", "expires_at"]


class TestApiTokenModel:
    """The ``ApiToken`` mapped class constructs from the v1 slice."""

    def test_minimal_construction(self) -> None:
        tok = ApiToken(
            id="01HWA00000000000000000TOKA",
            user_id="01HWA00000000000000000USRA",
            workspace_id="01HWA00000000000000000WSPA",
            label="kitchen-printer",
            scope_json={"me.tasks": True},
            prefix="mip_abcd",
            hash="a" * 64,
            created_at=_PINNED,
        )
        assert tok.id == "01HWA00000000000000000TOKA"
        assert tok.user_id == "01HWA00000000000000000USRA"
        assert tok.workspace_id == "01HWA00000000000000000WSPA"
        assert tok.label == "kitchen-printer"
        assert tok.scope_json == {"me.tasks": True}
        assert tok.prefix == "mip_abcd"
        assert tok.hash == "a" * 64
        assert tok.expires_at is None
        assert tok.last_used_at is None
        assert tok.revoked_at is None
        assert tok.created_at == _PINNED

    def test_tablename(self) -> None:
        assert ApiToken.__tablename__ == "api_token"

    def test_user_index_present(self) -> None:
        indexes = [i for i in ApiToken.__table_args__ if isinstance(i, Index)]
        names = [i.name for i in indexes]
        assert "ix_api_token_user" in names
        assert "ix_api_token_workspace" in names
