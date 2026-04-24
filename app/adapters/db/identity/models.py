"""User / PasskeyCredential / Session / ApiToken SQLAlchemy models.

v1 slice — sufficient for magic-link + passkey + session + token
flows (cd-4zz, cd-8m4, cd-c91, cd-cyq, cd-i1qe). The richer §02 / §03
surface (``full_legal_name``, ``phone_e164``, ``emergency_contact``,
``agent_approval_mode``, observability fields, rotation-pair hashes,
etc.) lands via follow-ups without breaking this migration's public
write contract. cd-i1qe added the ``kind`` / ``delegate_for_user_id``
/ ``subject_user_id`` columns on ``api_token`` so the §03
"Delegated" / "Personal access" surfaces can mint alongside the
cd-c91 ``scoped`` rows.

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
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.adapters.db.base import Base

__all__ = [
    "ApiToken",
    "Invite",
    "MagicLinkNonce",
    "PasskeyCredential",
    "Session",
    "SignupAttempt",
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

    **Hardening fields (cd-geqp, §15 "Cookies" / "Passkey specifics"):**

    * ``absolute_expires_at`` — the 90-day hard cutoff. Even a session
      that keeps sliding-refreshing stops being honoured past this
      instant; it exists so a stolen cookie cannot stay alive forever
      just by bouncing a long-lived tab off the server. Nullable for
      pre-hardening rows backfilled by the migration.
    * ``fingerprint_hash`` — SHA-256 of ``User-Agent + "\n" +
      Accept-Language`` under an HKDF-peppered key. A mismatch on
      :func:`app.auth.session.validate` forces re-auth (audit +
      :class:`SessionInvalid`). Nullable for pre-hardening rows.
    * ``invalidated_at`` / ``invalidation_cause`` — set when the
      session is invalidated mid-flight (passkey registered, recovery
      consumed, sign-count rollback detected) without deleting the
      row, so the forensic trail survives. ``None`` on a live session;
      non-null on an invalidated one. The row is still deleted on an
      **explicit** :func:`app.auth.session.revoke` (user-driven sign
      out) — invalidation and revocation are distinct.
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
    # Hard 90-day cap — see class docstring. Nullable so the initial
    # migration can land without a blanket backfill from the config's
    # cap (reading Settings from Alembic is awkward); new rows ALWAYS
    # carry a value, enforced by :func:`app.auth.session.issue`.
    absolute_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ua_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # SHA-256(ua + "\n" + accept_language + hkdf_subkey). Nullable so
    # the migration lands without backfilling (a backfill would need
    # the raw UA + Accept-Language which we never stored). Pre-
    # hardening rows validate without the fingerprint gate.
    fingerprint_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Free-text slug (``"passkey_registered"``, ``"recovery_consumed"``,
    # ``"clone_detected"``, ``"fingerprint_mismatch"``). NULL when the
    # row is live.
    invalidation_cause: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (Index("ix_session_user_expires", "user_id", "expires_at"),)


class ApiToken(Base):
    """Long-lived programmatic credential.

    The opaque token value is never stored — only ``hash`` (sha256)
    for verification and ``prefix`` (first 8 chars) for listings.
    ``scope_json`` is the scope set the domain layer consults at
    request time.

    **Three kinds** (§03 "API tokens"), discriminated by :attr:`kind`:

    * ``scoped`` — the classic workspace-pinned, scope-limited token
      a manager mints for an external agent (cd-c91 default).
      :attr:`workspace_id` set, :attr:`delegate_for_user_id` NULL,
      :attr:`subject_user_id` NULL.
    * ``delegated`` — inherits the creator's full authority for as
      long as their account is active; used by embedded agents (§11).
      :attr:`workspace_id` set, :attr:`delegate_for_user_id` = the
      creating user's id, :attr:`subject_user_id` NULL.
    * ``personal`` — personal access token minted by a user for
      themselves, limited to the ``me:*`` scope family (§03
      "Personal access tokens"). :attr:`workspace_id` NULL,
      :attr:`subject_user_id` = the creating user's id,
      :attr:`delegate_for_user_id` NULL.

    The kind x id-columns invariant is enforced both by
    :func:`app.auth.tokens.mint` and by the ``ck_api_token_kind_shape``
    CHECK constraint the cd-i1qe migration lands — a row that
    violates the shape fails at INSERT time regardless of which
    codepath wrote it.
    """

    __tablename__ = "api_token"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Nullable since cd-i1qe so personal access tokens (which have no
    # workspace pin) can land. The CHECK constraint reasserts the
    # kind-specific invariant: ``scoped`` / ``delegated`` MUST carry a
    # workspace_id; ``personal`` MUST NOT.
    workspace_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Three-kind discriminator. The :data:`TOKEN_KINDS` literal below
    # is the domain vocabulary; the column carries the raw string so
    # the CHECK constraint + mypy narrowing share a single source.
    kind: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="scoped",
        server_default="scoped",
    )
    # Populated only when :attr:`kind` == ``'delegated'``. ``SET NULL``
    # on delete keeps audit-trail joins intact after a user hard-delete
    # (the service layer already returns 401 for a delegated token
    # whose user is archived / gone).
    delegate_for_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Populated only when :attr:`kind` == ``'personal'``. Mutually
    # exclusive with :attr:`delegate_for_user_id` per §03 "Personal
    # access tokens" — the CHECK constraint enforces the XOR.
    subject_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    label: Mapped[str] = mapped_column(String, nullable=False)
    # ``scope_json`` is a flat mapping of scope-name → enabled-flag
    # (or a nested policy blob). The outer ``Any`` is scoped to
    # SQLAlchemy's JSON column type — callers writing a typed
    # payload should use a TypedDict locally and coerce into this
    # column. Delegated tokens carry an empty mapping per §03
    # (authority resolves against the delegating user's grants, not
    # the token's scopes); personal tokens carry ``me:*`` keys only.
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
        CheckConstraint(
            "kind IN ('scoped', 'delegated', 'personal')",
            name="ck_api_token_kind",
        ),
        CheckConstraint(
            "("
            "(kind = 'scoped' AND delegate_for_user_id IS NULL "
            "AND subject_user_id IS NULL AND workspace_id IS NOT NULL)"
            " OR "
            "(kind = 'delegated' AND delegate_for_user_id IS NOT NULL "
            "AND subject_user_id IS NULL AND workspace_id IS NOT NULL)"
            " OR "
            "(kind = 'personal' AND subject_user_id IS NOT NULL "
            "AND delegate_for_user_id IS NULL AND workspace_id IS NULL)"
            ")",
            name="ck_api_token_kind_shape",
        ),
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


class MagicLinkNonce(Base):
    """Single-use token ledger for ``POST /auth/magic/consume``.

    Every magic-link emission inserts one row in the *pending* state
    (``consumed_at IS NULL``); the matching consume flips
    ``consumed_at`` via a conditional ``UPDATE`` that only touches
    still-pending rows. The ``WHERE consumed_at IS NULL`` predicate on
    that update is how the single-use guarantee is enforced on every
    backend: SQLite serialises the write transaction, Postgres takes
    a row-level lock, and in either case exactly one of two racing
    consumers sees ``rowcount = 1`` while the loser sees ``0`` and is
    rejected with ``409 already_consumed``.

    **PII minimisation (§15).** We never store the plaintext email
    or IP — only SHA-256 hashes salted with the deployment's root key
    (see :func:`app.auth.keys.derive_subkey`). The hashes are enough
    to correlate related requests (rate-limit enforcement, abuse
    trail) without turning the table into a PII sink.

    **Tenant-agnostic.** Like :class:`User`, :class:`WebAuthnChallenge`,
    this row has no ``workspace_id``: magic-link purposes span
    ``signup_verify`` (pre-workspace), ``recover_passkey``,
    ``email_change_confirm``, and ``grant_invite``, and the first
    two exist before any :class:`~app.tenancy.WorkspaceContext` has
    been resolved. The domain layer (:mod:`app.auth.magic_link`)
    wraps every read/write in :func:`app.tenancy.tenant_agnostic`.

    **``subject_id`` is soft-typed.** For ``recover_passkey`` and
    ``email_change_confirm`` it's a ``user.id`` ULID; for
    ``signup_verify`` it points at a future ``signup_session`` row
    (cd-3i5) that doesn't exist yet; for ``grant_invite`` it points at
    an ``invite.id``. We store the bare string and let the consuming
    service interpret it per its ``purpose`` — adding the FKs would
    force the column to be nullable-per-FK and the row to exist
    pre-commit in one particular subject space.
    """

    __tablename__ = "magic_link_nonce"

    jti: Mapped[str] = mapped_column(String, primary_key=True)
    purpose: Mapped[str] = mapped_column(String, nullable=False)
    subject_id: Mapped[str] = mapped_column(String, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # SHA-256 of ``ip + hkdf_subkey``; 64 hex chars.
    created_ip_hash: Mapped[str] = mapped_column(String, nullable=False)
    # SHA-256 of ``canonicalise_email(email) + hkdf_subkey``; 64 hex chars.
    created_email_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_magic_link_nonce_expires", "expires_at"),
        Index("ix_magic_link_nonce_email_hash", "created_email_hash"),
        Index("ix_magic_link_nonce_ip_hash", "created_ip_hash"),
    )


class SignupAttempt(Base):
    """Self-serve signup session ledger — one row per ``/signup/start`` request.

    The row is born *verified=false, completed=false* at
    :func:`app.auth.signup.start_signup`, flips to *verified=true* when
    the magic link is consumed
    (:func:`app.auth.signup.consume_verify`), and finally *completed=
    true, workspace_id=<ws>* when the passkey ceremony lands the new
    workspace + user + role-grant row
    (:func:`app.auth.signup.complete_signup`).

    **PII minimisation (§15).** We store ``email_lower`` so the signup
    service can hand it to the workspace / user inserts verbatim, plus
    ``email_hash`` (the same SHA-256 + HKDF-pepper shape that magic
    link nonces carry) so abuse-tracking joins stay PII-free. The
    plaintext email is never logged, never audited — only the hash
    flows into audit diffs. ``ip_hash`` mirrors the magic-link shape.

    **Tenant-agnostic.** No ``workspace_id`` column — the whole point
    of this row is to precede the workspace's existence. The domain
    service reads/writes under :func:`app.tenancy.tenant_agnostic` the
    same way the magic-link and identity tables do.

    **Unique on `(email_lower, desired_slug)`.** A re-hit of
    ``/signup/start`` with the same email + slug inside the 15-minute
    TTL is treated as a duplicate — the caller may get a fresh magic
    link for the same attempt row, but the row itself does not
    duplicate. Different slugs for the same email each get their own
    row; different emails aiming at the same slug each get their own
    row too, and the slug-taken / homoglyph guards fire at start time
    to stop a downstream conflict on ``workspace.slug``.
    """

    __tablename__ = "signup_attempt"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email_lower: Mapped[str] = mapped_column(String, nullable=False)
    # SHA-256 of ``canonicalise_email(email) + hkdf_subkey``; 64 hex chars.
    email_hash: Mapped[str] = mapped_column(String, nullable=False)
    desired_slug: Mapped[str] = mapped_column(String, nullable=False)
    # SHA-256 of ``ip + hkdf_subkey``; 64 hex chars.
    ip_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set on successful completion. Soft FK — the workspace table is
    # workspace-scoped and this row is tenant-agnostic, so carrying a
    # hard FK would force the write to cross the tenancy seam on
    # insert. Left as a plain string until a later cleanup consolidates
    # the FK discipline across identity-layer tables.
    workspace_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "email_lower",
            "desired_slug",
            name="uq_signup_attempt_email_slug",
        ),
        Index("ix_signup_attempt_expires", "expires_at"),
        Index("ix_signup_attempt_email_hash", "email_hash"),
    )


class Invite(Base):
    """Pending "come join this workspace" row — primary entity of the
    click-to-accept flow.

    Created by :func:`app.domain.identity.membership.invite` when a
    manager / owner invites someone; its lifecycle mirrors the spec's
    §03 "Additional users (invite → click-to-accept)" two-surface
    flow:

    * born ``state = 'pending'`` with ``workspace_id`` pinned, the
      invitee's email + display name, and the requested
      ``grants_json`` + ``group_memberships_json`` payloads waiting
      to activate;
    * consumed (``state = 'accepted'`` + ``accepted_at``) once the
      invitee's click-to-accept ceremony lands every row in one
      transaction — for a new user that is the post-passkey
      ``complete_invite`` callback; for an existing user it is the
      second ``POST /invite/{id}/confirm`` press;
    * pruned (``state = 'expired'``) by a future nightly
      ``signup_gc`` worker once ``expires_at`` lapses.

    **PII minimisation (§15).** We store the invitee's email in the
    clear on this row (``pending_email``) because the accept flow
    needs to seed the :class:`User` row's ``email`` on first click —
    the value has to round-trip from the DB intact to hand to the
    UX. The canonical ``email_lower`` mirrors the :class:`User` row
    pattern so idempotent re-invites collapse onto a single row.
    Audit diffs never carry the plaintext — only :attr:`email_hash`
    (same SHA-256 + HKDF pepper shape as every other identity row).

    **``grants_json``** is the serialised spec payload:

    .. code-block:: json

        [
          {
            "scope_kind": "workspace",
            "scope_id": "01HWA...",
            "grant_role": "worker",
            "scope_property_id": null
          },
          ...
        ]

    Only ``workspace`` scope and the four v1 grant roles (``manager
    | worker | client | guest``) are validated on insert; the
    ``binding_org_id`` / ``organization`` scope variants are
    deferred to a follow-up (the ``organization`` table doesn't
    land in Phase 1).

    **``group_memberships_json``** is a list of group ids the
    invitee lands in on accept. The domain service validates each
    id against :class:`PermissionGroup` before activation — no
    cross-workspace membership is possible.

    **Soft FK on ``user_id``.** Populated on accept by the domain
    service (tracks the :class:`User` row inserted at accept time
    for a brand-new invitee, or the pre-existing row for a known
    email). No hard FK because the user row may not exist at
    insert time.
    """

    __tablename__ = "invite"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workspace.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Set at creation time iff the email already matched an existing
    # :class:`User` row; left NULL for a fresh invitee and filled on
    # accept. Soft reference (no FK) so a future user hard-delete
    # doesn't cascade the audit-forensic row.
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pending_email: Mapped[str] = mapped_column(String, nullable=False)
    pending_email_lower: Mapped[str] = mapped_column(String, nullable=False)
    # SHA-256 of ``canonicalise_email(email) + hkdf_subkey``. Carried
    # in audit diffs instead of the plaintext.
    email_hash: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    # One of ``pending | accepted | expired | revoked``.
    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    # Payload shapes documented above. JSON so the v2 schema can add
    # fields without a migration — callers validate at the domain
    # boundary.
    grants_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    group_memberships_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    invited_by_user_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'accepted', 'expired', 'revoked')",
            name="ck_invite_state",
        ),
        # One live invite per ``(workspace, email_lower)`` — a second
        # call for the same pair reuses / refreshes the existing row
        # rather than stacking duplicates. The domain service is the
        # enforcement point; the unique index is the safety net.
        UniqueConstraint(
            "workspace_id",
            "pending_email_lower",
            "state",
            name="uq_invite_workspace_email_state",
        ),
        Index("ix_invite_workspace", "workspace_id"),
        Index("ix_invite_email_lower", "pending_email_lower"),
        Index("ix_invite_expires", "expires_at"),
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
