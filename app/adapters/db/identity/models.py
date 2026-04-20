"""User / PasskeyCredential / Session / ApiToken SQLAlchemy models.

v1 slice — sufficient for magic-link + passkey + session + token
flows (cd-4zz, cd-8m4, cd-c91, cd-cyq). The richer §02 / §03
surface (``full_legal_name``, ``phone_e164``, ``emergency_contact``,
``agent_approval_mode``, ``delegate_for_user_id`` / ``subject_user_id``
/ ``kind`` on ``api_token``, observability fields, rotation-pair
hashes, etc.) lands via follow-ups without breaking this migration's
public write contract.

**Email uniqueness — case-insensitive.** SQLite cannot express
``LOWER(email) UNIQUE`` portably; PG has ``citext`` but that drags a
dialect-specific type through the model layer. The cleanest portable
solution is to keep ``email`` as the display value (whatever case the
user typed at enrolment) and carry a second ``email_lower`` column
with the unique constraint — filled on every insert / update by the
SQLAlchemy event listeners below. Lookups for magic links and login
all go through ``email_lower``; the UI reads ``email`` when it needs
to echo the address back to the user.

See ``docs/specs/02-domain-model.md`` §"users" / §"passkey_credential"
/ §"session" / §"api_token" and ``docs/specs/03-auth-and-tokens.md``
§"Data model".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = [
    "ApiToken",
    "PasskeyCredential",
    "Session",
    "User",
    "WebAuthnChallenge",
    "canonicalise_email",
]


def canonicalise_email(email: str) -> str:
    """Return the canonical case-folded lookup form of ``email``.

    Cases an incoming address to lower and strips surrounding
    whitespace. Deliberately simple — no local-part normalisation
    (gmail ``+`` tags, dots) because the spec treats addresses
    verbatim below the case fold. Exposed as a module-level helper
    so the auth domain layer (magic-link enrolment, passkey sign-in)
    can reuse the exact same rule without re-deriving it.
    """
    return email.strip().lower()


class User(Base):
    """Globally unique identity row.

    One row per human, regardless of how many workspaces they belong
    to. ``email`` is the display value; ``email_lower`` carries the
    unique-index lookup form so the case-insensitive contract holds
    on both SQLite and Postgres without dialect-specific types.
    """

    __tablename__ = "user"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    # Canonical lookup form — see module docstring and
    # ``canonicalise_email``. Written by the event listeners below.
    email_lower: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    locale: Mapped[str | None] = mapped_column(String, nullable=True)
    timezone: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_blob_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PasskeyCredential(Base):
    """WebAuthn credential bound to a :class:`User`.

    The credential id is raw bytes per the WebAuthn spec; storing it
    as ``LargeBinary`` keeps the round-trip lossless (base64url is a
    display concern, not a storage one). ``public_key`` is the
    credential's COSE public key in its raw byte form — the
    verification library wants bytes, not text. ``sign_count`` is
    the monotonic counter the RP enforces to detect cloned
    authenticators.
    """

    __tablename__ = "passkey_credential"

    id: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Comma-separated transport hints as returned by the authenticator
    # (``"usb,nfc,ble,internal"``). Kept as text so future transports
    # don't force a schema change.
    transports: Mapped[str | None] = mapped_column(String, nullable=True)
    backup_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_passkey_credential_user", "user_id"),)


class Session(Base):
    """Web session row — one per logged-in browser.

    ``workspace_id`` is **nullable** because users pick a workspace
    *after* login: the sign-in ceremony mints a session with
    ``workspace_id = NULL`` and the workspace-picker request swaps
    it in. See ``docs/specs/03-auth-and-tokens.md`` §"Sessions".
    ``ua_hash`` / ``ip_hash`` are the hashed device fingerprints the
    security page shows — never the raw values (§15 PII minimisation).
    """

    __tablename__ = "session"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ua_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (Index("ix_session_user_expires", "user_id", "expires_at"),)


class ApiToken(Base):
    """Long-lived programmatic credential.

    The opaque token value is never stored — only ``hash`` (sha256)
    for verification and ``prefix`` (first 8 chars) for listings.
    ``scope_json`` is the scope set the domain layer consults at
    request time. ``workspace_id`` is required in the v1 slice;
    delegated and personal-access-token variants that relax this
    land alongside cd-c91.
    """

    __tablename__ = "api_token"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    # ``scope_json`` is a flat mapping of scope-name → enabled-flag
    # (or a nested policy blob). The outer ``Any`` is scoped to
    # SQLAlchemy's JSON column type — callers writing a typed
    # payload should use a TypedDict locally and coerce into this
    # column.
    scope_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    prefix: Mapped[str] = mapped_column(String, nullable=False)
    hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_api_token_user", "user_id"),
        Index("ix_api_token_workspace", "workspace_id"),
    )


class WebAuthnChallenge(Base):
    """Short-lived registration challenge, persisted for the finish step.

    A WebAuthn ceremony is two round-trips: ``/register/start`` mints
    the options (carrying a fresh random ``challenge``) and stashes
    them here; ``/register/finish`` loads the row by ``id`` and hands
    ``challenge`` to py_webauthn so it can cross-check the value the
    authenticator echoed in ``clientDataJSON``. The row is
    single-use: :func:`app.auth.passkey.register_finish` deletes it
    on success, so a replay of the same ``finish`` request fails the
    load and the router surfaces 409.

    **Why a column, not an in-memory cache.** The spec pins a 10-min
    TTL (§03 "WebAuthn specifics" implicitly — the browser timeout is
    60s, the server gives itself a comfortable grace window) and
    requires the challenge to survive a process restart between the
    two round-trips. An in-memory dict would lose every in-flight
    ceremony on deploy; SQLite / Postgres give us durability + an
    ``expires_at`` a sweeper can reap.

    **Subject discriminator.** A challenge is always bound to one of
    two subjects:

    * an authenticated user adding another passkey (``user_id`` set,
      ``signup_session_id`` null);
    * a pending signup session with no user row yet
      (``signup_session_id`` set, ``user_id`` null).

    Exactly one of the two FKs MUST carry a value — enforced by the
    ``ck_webauthn_challenge_subject`` CHECK constraint. The finish
    handler reads whichever is set and passes it to the matching
    service entry point.

    **Why not workspace-scoped.** Registration happens at the bare
    host (no ``/w/<slug>/`` prefix) during signup, and at the owning
    user's identity scope during "add another passkey". Neither is
    workspace-scoped; we follow the surrounding identity tables and
    stay out of :mod:`app.tenancy.registry`. The domain layer owns
    the subject-based access check.
    """

    __tablename__ = "webauthn_challenge"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=True,
    )
    # ``signup_session_id`` is a soft-typed pointer to the future
    # ``signup_session`` row (cd-3i5). The row doesn't exist yet, so
    # we store the id as a bare string without a foreign key until
    # that table lands. When the signup flow lands it adds the FK
    # in its own migration.
    signup_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    challenge: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # ``exclude_credentials`` is the base64url-encoded list of
    # credential ids the browser's authenticator MUST refuse to
    # re-register against (the per-user uniqueness gate, §03
    # "Additional passkeys"). We snapshot it on start so finish can
    # re-verify against the same set — a sibling client adding a
    # passkey mid-ceremony won't retroactively widen the gate.
    exclude_credentials: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND signup_session_id IS NULL) OR "
            "(user_id IS NULL AND signup_session_id IS NOT NULL)",
            name="ck_webauthn_challenge_subject",
        ),
        Index("ix_webauthn_challenge_expires", "expires_at"),
        Index("ix_webauthn_challenge_user", "user_id"),
        Index("ix_webauthn_challenge_signup", "signup_session_id"),
    )


@event.listens_for(User, "before_insert")
def _user_before_insert(_mapper: object, _conn: object, target: User) -> None:
    """Keep ``email_lower`` in sync with ``email`` on insert.

    Enforces the case-insensitive uniqueness contract without relying
    on a dialect-specific functional index (see module docstring).

    **Caveat**: this is a Python-layer ORM event. Direct Core-level or
    raw-SQL ``UPDATE "user" SET email=...`` paths bypass it and will
    leave ``email_lower`` stale — the domain layer that rewrites an
    email must go through the ORM or call :func:`canonicalise_email`
    itself.
    """
    target.email_lower = canonicalise_email(target.email)


@event.listens_for(User, "before_update")
def _user_before_update(_mapper: object, _conn: object, target: User) -> None:
    """Keep ``email_lower`` in sync with ``email`` on update.

    Same ORM-layer caveat as :func:`_user_before_insert` — raw SQL
    bypasses it.
    """
    target.email_lower = canonicalise_email(target.email)
