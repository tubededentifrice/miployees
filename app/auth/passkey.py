"""Passkey registration + login ceremonies — domain service.

Two surfaces, one module:

* **Registration.** :func:`register_start` / :func:`register_finish`
  (plus signup + recovery variants) mint a challenge, verify the
  authenticator's attestation via
  :func:`app.auth.webauthn.verify_registration`, and persist one
  :class:`~app.adapters.db.identity.models.PasskeyCredential` row.
* **Login.** :func:`login_start` / :func:`login_finish` mint an
  assertion challenge, verify the browser's signature via
  :func:`app.auth.webauthn.verify_authentication`, detect
  cloned authenticators via the FIDO2 sign-count rollback check
  (§15 "Passkey specifics"), and issue a session cookie via
  :func:`app.auth.session.issue`. Conditional-UI login (§03
  "Login") means the browser picks the credential, so no user
  context is needed at ``start`` — the server discovers the
  authenticating user on ``finish``.

Three challenge subject families are supported:

* **Authenticated user adding a passkey** — :func:`register_start` /
  :func:`register_finish`. The challenge row carries the ``user_id``.
* **Signup session enrolling its first passkey** —
  :func:`register_start_signup` / :func:`register_finish_signup`. The
  challenge row carries a ``signup_session_id``.
* **Login** — :func:`login_start` / :func:`login_finish`. The
  challenge row carries the sentinel value
  :data:`_LOGIN_SUBJECT_SENTINEL` in ``signup_session_id`` so the
  existing ``webauthn_challenge`` CHECK constraint (exactly one of
  ``user_id`` / ``signup_session_id`` non-null) is satisfied without
  a schema change. See the constant's docstring for why we chose the
  sentinel path over a new column.

**Caps (§03 "Additional passkeys").** A user may hold up to 5
passkeys; the 6th registration raises :class:`TooManyPasskeys` (422
``too_many_passkeys``). This count is computed inside the same
transaction as the insert so two concurrent finish calls cannot both
pass the gate and land a 6th row.

**Replay protection.** The challenge row is deleted on a successful
``finish``. A second call with the same ``challenge_id`` returns
:class:`ChallengeAlreadyConsumed` (409). Mismatched challenge, origin,
or rp_id values raise :class:`InvalidRegistration` (400) — we rewrap
py_webauthn's `InvalidRegistrationResponse` so the HTTP layer doesn't
need to know about the upstream type.

**Login error shape.** :func:`login_finish` collapses unknown
credential, bad signature, and challenge-subject mismatch into a
single :class:`InvalidLoginAttempt` so the HTTP layer can return
one opaque 401 envelope. Clone detection raises the separate
:class:`CloneDetected` so audit + metrics can distinguish the two,
but the HTTP response body never reveals which fired. Throttle
lockout (3 fails / 10-min per §15) raises
:class:`~app.auth._throttle.PasskeyLoginLockout`.

**Transaction discipline.** The service never calls
``session.commit()`` — the Unit-of-Work that opened the session owns
the transaction boundary (§01 "Key runtime invariants" #3). The
clone-detected + login-rejected audit rows are written by the HTTP
router on **fresh** UoWs so they land even though the primary UoW
rolls back on the raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import (
    PasskeyCredential,
    User,
    WebAuthnChallenge,
)
from app.audit import write_audit
from app.auth import session as session_module
from app.auth._hashing import hash_with_pepper
from app.auth._throttle import Throttle
from app.auth.webauthn import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
    RelyingParty,
    VerifiedAuthentication,
    VerifiedRegistration,
    base64url_to_bytes,
    bytes_to_base64url,
    generate_authentication_challenge,
    generate_registration_challenge,
    make_relying_party,
    options_to_dict,
    verify_authentication,
    verify_registration,
)
from app.authz.owners import is_owner_on_any_workspace
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AuthenticationOptions",
    "ChallengeAlreadyConsumed",
    "ChallengeExpired",
    "ChallengeNotFound",
    "ChallengeSubjectMismatch",
    "CloneDetected",
    "InvalidLoginAttempt",
    "InvalidRegistration",
    "LastPasskeyCredential",
    "LoginResult",
    "PasskeyCredentialRef",
    "PasskeyNotFound",
    "RegistrationOptions",
    "TooManyPasskeys",
    "login_finish",
    "login_start",
    "register_finish",
    "register_finish_signup",
    "register_start",
    "register_start_recovery",
    "register_start_signup",
    "revoke_passkey",
]


# Spec §03 "Additional passkeys" caps a user at 5 passkeys; the 6th
# attempt raises :class:`TooManyPasskeys`. The count is computed
# inside the finish transaction so concurrent enrolments cannot both
# land a 6th row — the SELECT-then-INSERT pair runs under the caller's
# UoW, and the per-user cap is small enough that the extra round-trip
# is imperceptible.
_MAX_PASSKEYS_PER_USER: Final[int] = 5

# 10-minute challenge TTL. The browser ceremony times out at 60s (see
# ``app.auth.webauthn.policy().timeout_ms``); the server gives itself a
# generous window because a user may walk away from the page, pick up
# another device, and come back. Anything longer makes the
# ``webauthn_challenge`` table a long-tail of dead rows; anything
# shorter tightens the race window on slow authenticators.
_CHALLENGE_TTL: Final[timedelta] = timedelta(minutes=10)

# Subject discriminator sentinel for login challenges. The
# ``webauthn_challenge`` row's CHECK constraint requires exactly one
# of ``user_id`` / ``signup_session_id`` to be non-null. Login
# challenges bind to **no** user up-front (conditional UI means the
# browser picks the credential and the server discovers the user on
# finish), so neither of the existing subject families fits. Rather
# than churn the schema with a new ``purpose`` column (and its
# migration), we reuse the ``signup_session_id`` column with a fixed
# sentinel value and guard against collisions with real signup
# session ids — those are ULIDs (26 chars, Crockford base32), which
# cannot produce the ``__login__`` string. The finish handler
# rejects any challenge whose ``signup_session_id`` isn't this
# sentinel when called through the login path, and the register
# handlers reject it symmetrically on their side.
_LOGIN_SUBJECT_SENTINEL: Final[str] = "__login__"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistrationOptions:
    """Output of :func:`register_start` / :func:`register_start_signup`.

    * ``challenge_id`` is the opaque handle the browser echoes back
      with the assertion so ``finish`` can load the persisted
      challenge bytes.
    * ``options`` is the parsed ``PublicKeyCredentialCreationOptions``
      dict — parsed, not raw JSON, so the HTTP layer can splice in
      extensions without re-parsing.
    """

    challenge_id: str
    options: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AuthenticationOptions:
    """Output of :func:`login_start`.

    Mirrors :class:`RegistrationOptions` on the shape side: an opaque
    challenge handle the browser echoes back with the assertion, and
    the parsed ``PublicKeyCredentialRequestOptions`` dict the SPA
    passes to ``navigator.credentials.get()``.

    Unlike registration, ``options`` carries an empty
    ``allowCredentials`` list — conditional UI + discoverable
    credentials let the browser surface any passkey the user has
    enrolled on the device, and the server discovers the matching
    credential row on finish. That shape is also the privacy-
    preserving one: we never leak the set of credentials the
    attacker's guessed email has.
    """

    challenge_id: str
    options: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Output of :func:`login_finish`.

    Carries the :class:`~app.auth.session.SessionIssue` the router
    stamps into the ``Set-Cookie`` header, the authenticating
    ``user_id`` for the response body, and the credential id
    (base64url) for audit correlation at the HTTP layer — the domain
    service already wrote the ``audit.passkey.assertion_ok`` row in
    the same transaction, but the router needs the id echoable
    without a second DB read.
    """

    session_issue: session_module.SessionIssue
    user_id: str
    credential_id_b64url: str


@dataclass(frozen=True, slots=True)
class PasskeyCredentialRef:
    """Immutable projection of a persisted ``passkey_credential`` row.

    Matches the §03 "Privacy" whitelist verbatim. The credential id is
    surfaced as base64url text for the HTTP layer; the raw bytes live
    on the ORM row.
    """

    credential_id_b64url: str
    user_id: str
    sign_count: int
    transports: str | None
    backup_eligible: bool
    aaguid: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TooManyPasskeys(ValueError):
    """User already has :data:`_MAX_PASSKEYS_PER_USER` registered passkeys.

    422-equivalent — the HTTP layer maps this to
    ``error = "too_many_passkeys"``.
    """


class InvalidRegistration(ValueError):
    """Registration payload failed cryptographic verification.

    400-equivalent. Wraps py_webauthn's
    :class:`InvalidRegistrationResponse` so the HTTP router doesn't
    reach past the seam in :mod:`app.auth.webauthn`.
    """


class ChallengeNotFound(LookupError):
    """No ``webauthn_challenge`` row with that id.

    Indistinguishable from a replay whose row was already deleted; the
    HTTP layer maps both to 409 so the API doesn't leak whether a
    challenge ever existed.
    """


class ChallengeExpired(ValueError):
    """Challenge TTL elapsed before ``finish`` was called.

    400-equivalent. The client must call ``/start`` again.
    """


class ChallengeAlreadyConsumed(LookupError):
    """Reserved alias of :class:`ChallengeNotFound` for clarity at call sites.

    The row is deleted atomically with the credential insert, so a
    replay observes the same "row missing" shape as a genuine unknown
    id. We surface the two as separate types so tests can assert the
    specific HTTP 409 mapping intended by the spec (AC #5).
    """


class InvalidLoginAttempt(ValueError):
    """Passkey assertion failed to verify — any cause.

    The HTTP router maps this to **401 invalid_credential**, with the
    same error symbol as :class:`CloneDetected` so an attacker can't
    tell "unknown credential" from "bad signature" from
    "clone detected" based on the response shape. The domain service
    deliberately collapses several underlying causes (unknown
    credential id, py_webauthn rejection, challenge subject
    mismatch at the login path) into this one type — the audit row
    carries the fine-grained ``reason`` for operators, the HTTP
    response stays opaque.
    """


class CloneDetected(ValueError):
    """Assertion's ``new_sign_count`` did not exceed the stored counter.

    Per §15 "Passkey specifics" — FIDO2's sign-count exists exactly to
    detect cloned authenticators, and ignoring a rollback is the
    worst-of-both-worlds posture. The domain service refuses the
    login; the HTTP router handles every downstream effect on its
    own fresh UoWs so the operator trail lands even though the
    primary UoW rolls back on the raise:

    * ``audit.passkey.cloned_detected`` — the detection event
    * ``session.invalidated`` with cause ``clone_detected`` — every
      session for the credential's owner
    * ``audit.passkey.auto_revoked`` — the revocation event (cd-cx19)
    * the ``passkey_credential`` row is hard-deleted (no
      ``deleted_at`` column; the forensic trail lives in audit_log)

    ``credential_id_b64``, ``old_sign_count``, ``new_sign_count`` are
    stashed on the exception so the router's fresh-UoW audit writes
    have the payload without re-reading the DB.

    Surface-level mapping at the HTTP layer: **401 invalid_credential**,
    identical to :class:`InvalidLoginAttempt`, so the response shape
    reveals nothing. A subsequent login against the revoked credential
    misses the credential lookup and raises :class:`InvalidLoginAttempt`
    rather than re-hitting this type — there is no row left to
    compare sign-counts against.
    """

    def __init__(
        self,
        message: str,
        *,
        credential_id_b64: str,
        old_sign_count: int,
        new_sign_count: int,
    ) -> None:
        super().__init__(message)
        self.credential_id_b64 = credential_id_b64
        self.old_sign_count = old_sign_count
        self.new_sign_count = new_sign_count


class ChallengeSubjectMismatch(ValueError):
    """Challenge subject does not match the finish call's subject.

    Raised when a user-bound challenge is redeemed by the signup path
    (or vice versa), or when the asserted ``user_id`` disagrees with
    the one stashed on the challenge row. 400-equivalent — the HTTP
    router should not distinguish this from :class:`InvalidRegistration`
    for privacy, but the type exists so domain tests can pin the
    cross-subject bug.
    """


class PasskeyNotFound(LookupError):
    """Target credential either does not exist or is owned by another user.

    404-equivalent. The HTTP router maps this to 404 regardless of the
    underlying cause — leaking "exists but belongs to someone else" vs.
    "no such credential" would turn the credential-id space into an
    enumeration oracle. The domain service collapses both shapes into
    this single type on purpose.
    """


class LastPasskeyCredential(ValueError):
    """Revoking this credential would leave the user with zero passkeys.

    422-equivalent. The HTTP router maps this to ``last_credential`` so
    the SPA can surface a clear "enrol another passkey or use recovery"
    message rather than silently locking the user out. §03
    "Self-service lost-device recovery" is the recovery door; a
    dedicated step-up-then-revoke seam that would let the user revoke
    anyway (and step right back into the recovery flow) is a
    follow-up — tracked separately.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or a fresh system clock."""
    return (clock if clock is not None else SystemClock()).now()


def _count_passkeys(session: Session, *, user_id: str) -> int:
    """Return the number of passkeys already registered for ``user_id``.

    Wrapped in :func:`tenant_agnostic` because ``passkey_credential`` is
    identity-scoped and carries no ``workspace_id`` column — a live
    :class:`~app.tenancy.WorkspaceContext` would otherwise make the
    ORM tenant filter try to inject a predicate that doesn't exist.
    """
    # justification: passkey_credential is identity-scoped, not
    # workspace-scoped — the ORM tenant filter has nothing to apply.
    with tenant_agnostic():
        stmt = (
            select(func.count())
            .select_from(PasskeyCredential)
            .where(PasskeyCredential.user_id == user_id)
        )
        return session.scalar(stmt) or 0


def _existing_credential_ids(session: Session, *, user_id: str) -> list[bytes]:
    """Return the raw credential-id bytes for every existing passkey.

    Used as ``excludeCredentials`` so the authenticator refuses a
    duplicate on a device already enrolled under this user.
    """
    # justification: passkey_credential is identity-scoped; reuse of
    # the tenant-agnostic gate mirrors :func:`_count_passkeys`.
    with tenant_agnostic():
        rows = session.scalars(
            select(PasskeyCredential.id).where(PasskeyCredential.user_id == user_id)
        ).all()
    return list(rows)


def _load_challenge(
    session: Session, *, challenge_id: str, now: datetime
) -> WebAuthnChallenge:
    """Load + validate the challenge row; raise on missing or expired.

    :class:`ChallengeNotFound` and :class:`ChallengeExpired` are
    distinct so tests can verify the spec mapping (AC #5 is replay →
    409; a genuine expiry is 400 per §03). The HTTP router collapses
    both `NotFound` flavours to 409 to avoid leaking whether a
    challenge ever existed.
    """
    # justification: webauthn_challenge is identity-scoped; no tenant
    # predicate applies.
    with tenant_agnostic():
        row = session.get(WebAuthnChallenge, challenge_id)
    if row is None:
        raise ChallengeNotFound(challenge_id)
    # SQLite's ``DateTime(timezone=True)`` drops tzinfo on roundtrip
    # (the column stores ISO text, no offset). Postgres preserves the
    # offset. Normalising both sides to aware UTC here keeps the
    # TTL comparison correct on every backend without a dialect
    # branch.
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise ChallengeExpired(challenge_id)
    return row


def _load_user(session: Session, *, user_id: str) -> User:
    """Load the authenticating user or raise ``LookupError``."""
    # justification: user is identity-scoped (tenant-agnostic by
    # design, see app/adapters/db/identity/__init__.py docstring).
    with tenant_agnostic():
        user = session.get(User, user_id)
    if user is None:
        raise LookupError(f"user {user_id!r} not found")
    return user


def _insert_challenge(
    session: Session,
    *,
    challenge_id: str,
    user_id: str | None,
    signup_session_id: str | None,
    challenge: bytes,
    existing_credential_ids: list[bytes],
    now: datetime,
) -> None:
    """Insert one :class:`WebAuthnChallenge` row, ready for ``finish``."""
    exclude_b64: list[str] = [
        bytes_to_base64url(cid) for cid in existing_credential_ids
    ]
    row = WebAuthnChallenge(
        id=challenge_id,
        user_id=user_id,
        signup_session_id=signup_session_id,
        challenge=challenge,
        exclude_credentials=exclude_b64,
        created_at=now,
        expires_at=now + _CHALLENGE_TTL,
    )
    # justification: webauthn_challenge is identity-scoped, no tenant
    # predicate applies; the ORM flush below would otherwise trip on
    # the missing workspace_id column.
    with tenant_agnostic():
        session.add(row)
        session.flush()


def _verify_or_raise(
    *,
    rp: RelyingParty,
    credential: dict[str, Any],
    expected_challenge: bytes,
) -> VerifiedRegistration:
    """Verify the attestation response, rewrapping py_webauthn errors.

    The wrap preserves the cause chain so operators can still see the
    underlying py_webauthn message in logs, while the HTTP layer only
    needs to know about :class:`InvalidRegistration`.
    """
    try:
        return verify_registration(
            rp=rp,
            credential=credential,
            expected_challenge=expected_challenge,
        )
    except InvalidRegistrationResponse as exc:
        raise InvalidRegistration(str(exc)) from exc


def _extract_transports(credential: dict[str, Any]) -> str | None:
    """Pull the transport hints out of the client's attestation payload.

    The WebAuthn Level 3 spec puts ``transports`` inside
    ``response.transports`` (a list of strings); authenticators that
    don't advertise any return an empty list or omit the field
    entirely. Store as a comma-separated string so future transport
    families don't force a schema change
    (``passkey_credential.transports`` is a ``String`` column).
    """
    response = credential.get("response")
    if not isinstance(response, dict):
        return None
    raw = response.get("transports")
    if not isinstance(raw, list) or not raw:
        return None
    # Only accept string transports; a malformed client that sent a
    # non-string entry gets filtered out rather than crashing the
    # ceremony.
    clean = [t for t in raw if isinstance(t, str) and t]
    if not clean:
        return None
    return ",".join(clean)


def _insert_passkey_and_audit(
    session: Session,
    *,
    workspace_ctx: WorkspaceContext | None,
    user_id: str,
    verified: VerifiedRegistration,
    credential: dict[str, Any],
    clock: Clock | None,
    now: datetime,
) -> PasskeyCredentialRef:
    """Persist the verified credential + matching audit row.

    ``workspace_ctx`` is ``None`` on the signup path (no workspace
    exists yet) — in that case the audit row is skipped. The caller
    (the signup service) owns the ``user.enrolled`` audit, not us
    (see §03 "Self-serve signup" step 3 / cd-3i5 handoff).

    ``credential`` is the raw browser payload — we pull transport
    hints off ``response.transports`` because py_webauthn's
    :class:`VerifiedRegistration` does not surface them (the library
    verifies the attestation; transports are informational metadata).
    """
    transports = _extract_transports(credential)

    credential_row = PasskeyCredential(
        id=verified.credential_id,
        user_id=user_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=transports,
        backup_eligible=bool(verified.credential_backed_up),
        label=None,
        created_at=now,
        last_used_at=None,
    )
    # justification: passkey_credential is identity-scoped; writing
    # under a live WorkspaceContext would otherwise force the ORM
    # filter to inject a predicate the table doesn't carry.
    with tenant_agnostic():
        session.add(credential_row)
        session.flush()

    credential_id_b64 = bytes_to_base64url(verified.credential_id)

    if workspace_ctx is not None:
        # AAGUID is an authenticator-model identifier (§03 "Privacy"
        # whitelists it) — safe to include in the audit diff.
        write_audit(
            session,
            workspace_ctx,
            entity_kind="passkey_credential",
            entity_id=credential_id_b64,
            action="passkey.registered",
            diff={
                "user_id": user_id,
                "aaguid": verified.aaguid,
                "transports": transports,
                "backup_eligible": bool(verified.credential_backed_up),
            },
            clock=clock,
        )

    return PasskeyCredentialRef(
        credential_id_b64url=credential_id_b64,
        user_id=user_id,
        sign_count=verified.sign_count,
        transports=transports,
        backup_eligible=bool(verified.credential_backed_up),
        aaguid=verified.aaguid,
        created_at=now,
    )


def _delete_challenge(session: Session, *, row: WebAuthnChallenge) -> None:
    """Delete the consumed challenge row.

    Runs under :func:`tenant_agnostic` for the same reason every
    identity-table mutation does in this module.
    """
    # justification: webauthn_challenge is identity-scoped; the delete
    # must bypass the ORM tenant filter.
    with tenant_agnostic():
        session.delete(row)
        session.flush()


# ---------------------------------------------------------------------------
# Public surface — authenticated-user flow
# ---------------------------------------------------------------------------


def register_start(
    ctx: WorkspaceContext,
    session: Session,
    *,
    user_id: str,
    clock: Clock | None = None,
    rp: RelyingParty | None = None,
    now: datetime | None = None,
) -> RegistrationOptions:
    """Mint a registration challenge for an authenticated user.

    ``ctx`` is accepted so the call signature matches every other
    domain service (audit for "passkey added" emits on finish, not
    start), but the challenge row itself is identity-scoped and
    carries no workspace id.

    Raises :class:`TooManyPasskeys` if the user already holds
    :data:`_MAX_PASSKEYS_PER_USER` passkeys — surfacing the cap
    before the browser ceremony means the user doesn't waste a tap
    on their authenticator.

    ``now`` lets callers who already resolved the wall-clock time
    forward it through unchanged; otherwise it falls back to
    :func:`_now` against ``clock``. The explicit parameter fixes
    the "caller pinned ``now`` but not ``clock``" class of bug —
    see :func:`app.auth.signup.complete_signup`.
    """
    del ctx  # signature-compat only; no workspace predicate applies here
    resolved_now = now if now is not None else _now(clock)
    rp = rp or make_relying_party()

    existing = _existing_credential_ids(session, user_id=user_id)
    if len(existing) >= _MAX_PASSKEYS_PER_USER:
        raise TooManyPasskeys(
            f"user {user_id!r} already has {len(existing)} passkeys "
            f"(max {_MAX_PASSKEYS_PER_USER})"
        )

    user = _load_user(session, user_id=user_id)

    options_json, challenge_bytes = generate_registration_challenge(
        rp=rp,
        # WebAuthn's ``user.id`` is opaque bytes; the RFC binds it to
        # "the user handle" but leaves the encoding to the RP. Our
        # ULID is 26 chars of ASCII, so UTF-8 bytes round-trip through
        # ``clientDataJSON`` without padding surprises.
        user_id=user.id.encode("utf-8"),
        user_name=user.email,
        user_display_name=user.display_name,
        existing_credential_ids=existing,
    )

    challenge_id = new_ulid(clock=clock)
    _insert_challenge(
        session,
        challenge_id=challenge_id,
        user_id=user_id,
        signup_session_id=None,
        challenge=challenge_bytes,
        existing_credential_ids=existing,
        now=resolved_now,
    )

    return RegistrationOptions(
        challenge_id=challenge_id,
        options=options_to_dict(options_json),
    )


def register_finish(
    ctx: WorkspaceContext,
    session: Session,
    *,
    user_id: str,
    challenge_id: str,
    credential: dict[str, Any],
    clock: Clock | None = None,
    rp: RelyingParty | None = None,
    now: datetime | None = None,
) -> PasskeyCredentialRef:
    """Verify the attestation response + persist the passkey row.

    Writes one ``passkey_credential`` row and one
    ``audit.passkey.registered`` audit row in the caller's open
    transaction, then deletes the consumed challenge so a replay
    raises :class:`ChallengeAlreadyConsumed`.

    ``now`` lets callers forward an already-resolved wall-clock time
    so the challenge-TTL comparison and the credential row's
    ``created_at`` stamp stay consistent with upstream — see the
    module docstring for the "pinned now vs real clock" bug.

    Raises:

    * :class:`ChallengeNotFound` — unknown or already-consumed id.
    * :class:`ChallengeExpired` — TTL elapsed.
    * :class:`ChallengeSubjectMismatch` — challenge belongs to the
      signup flow, or to a different user.
    * :class:`InvalidRegistration` — mismatched challenge bytes,
      ``origin``, ``rp_id``, or a broken attestation.
    * :class:`TooManyPasskeys` — concurrent enrolment raced us to the
      5-passkey cap.
    """
    resolved_now = now if now is not None else _now(clock)
    rp = rp or make_relying_party()

    row = _load_challenge(session, challenge_id=challenge_id, now=resolved_now)
    if row.user_id is None or row.signup_session_id is not None:
        # Signup-path challenge smuggled into the authenticated flow.
        raise ChallengeSubjectMismatch(
            "challenge was minted for the signup flow; call register_finish_signup"
        )
    if row.user_id != user_id:
        raise ChallengeSubjectMismatch(
            f"challenge is for user {row.user_id!r}, not {user_id!r}"
        )

    verified = _verify_or_raise(
        rp=rp,
        credential=credential,
        expected_challenge=row.challenge,
    )

    # Recheck the cap inside the finish transaction so two concurrent
    # ceremonies cannot both land a 6th row. A UNIQUE index on
    # ``(user_id, credential_id)`` would be defensive, but the cap is
    # a workspace-policy value and a partial index would duplicate
    # policy in schema.
    if _count_passkeys(session, user_id=user_id) >= _MAX_PASSKEYS_PER_USER:
        raise TooManyPasskeys(
            f"user {user_id!r} already has {_MAX_PASSKEYS_PER_USER} passkeys"
        )

    credential_ref = _insert_passkey_and_audit(
        session,
        workspace_ctx=ctx,
        user_id=user_id,
        verified=verified,
        credential=credential,
        clock=clock,
        now=resolved_now,
    )

    _delete_challenge(session, row=row)

    # §15 "Passkey specifics" / cd-geqp: adding a passkey is a
    # credential-population change. Invalidate every **other** active
    # session for this user so a lurking stolen cookie can't outlive
    # the user's security-relevant action. The caller's own session
    # is unknown at this seam (the router passes ``ctx.actor_id`` but
    # not the session PK), so we invalidate all of them — the user's
    # browser rides on CSRF + an imminent re-auth prompt, and
    # re-signing after enrolling a new passkey is the expected UX.
    session_module.invalidate_for_user(
        session,
        user_id=user_id,
        cause="passkey_registered",
        now=resolved_now,
        clock=clock,
    )

    return credential_ref


def revoke_passkey(
    ctx: WorkspaceContext,
    session: Session,
    *,
    user_id: str,
    credential_id: bytes,
    clock: Clock | None = None,
    now: datetime | None = None,
) -> str:
    """Delete one passkey credential + invalidate the owner's sessions.

    Spec §15 "Shared-origin XSS containment" / cd-geqp: revoking a
    passkey is a credential-population change — a stolen session cookie
    must not outlive the user's "remove this device" action. The
    ``audit.passkey.revoked`` row lands **before**
    ``audit.session.invalidated`` so the forensic trail reads in
    cause-then-effect order.

    The caller's UoW owns the transaction boundary — everything below
    runs under the same :class:`UnitOfWorkImpl` and either all lands
    (happy path) or all rolls back (any raise).

    Ownership is enforced: ``user_id`` is the authenticated actor;
    a credential owned by a different user is indistinguishable from
    a non-existent credential and both collapse to
    :class:`PasskeyNotFound` (the HTTP layer maps that to 404).
    Admin-revocation-on-behalf-of-another-user is not a v1 surface —
    an owner who needs to revoke a worker's passkey uses the existing
    ``POST /api/v1/users/{id}/reset_passkey`` door (§03
    "Owner-initiated worker passkey reset") which rides on the same
    re-enrolment pipeline.

    Refuses to revoke the user's **last** passkey: deleting it would
    leave them with no way to log in short of the recovery flow. We
    surface :class:`LastPasskeyCredential` so the SPA can guide the
    user to enrol another credential first or use the recovery door
    intentionally.

    Returns the base64url-encoded credential id so the HTTP layer can
    surface it in the response / log without re-encoding.

    Raises:

    * :class:`PasskeyNotFound` — credential id is unknown, or the
      credential exists but does not belong to ``user_id``.
    * :class:`LastPasskeyCredential` — this is the user's only passkey.

    Audit writes (in order, both under the caller's UoW):

    1. ``audit.passkey.revoked`` — entity_kind ``passkey_credential``,
       entity_id the base64url credential id. Diff carries the user
       id and the credential's public metadata (AAGUID whitelisted by
       §03 "Privacy").
    2. ``audit.session.invalidated`` with cause ``"passkey_revoked"``
       — emitted by :func:`app.auth.session.invalidate_for_user`.
    """
    resolved_now = now if now is not None else _now(clock)

    # justification: passkey_credential is identity-scoped; no tenant
    # predicate applies. Same gate pattern register_finish uses.
    with tenant_agnostic():
        row = session.get(PasskeyCredential, credential_id)

    # Collapse "unknown credential" and "wrong owner" into the same
    # type so the HTTP layer cannot leak the credential-id space as
    # an enumeration oracle.
    if row is None or row.user_id != user_id:
        raise PasskeyNotFound(bytes_to_base64url(credential_id))

    # Last-credential gate — leaving the user with zero passkeys would
    # force them through the recovery flow to regain access. Refuse so
    # the caller surfaces an actionable error rather than a silent
    # lockout.
    if _count_passkeys(session, user_id=user_id) <= 1:
        raise LastPasskeyCredential(
            f"user {user_id!r} has one remaining passkey; "
            "enrol another or use recovery before revoking"
        )

    credential_id_b64 = bytes_to_base64url(credential_id)

    # Audit BEFORE the destructive write so a flush failure on the
    # audit insert doesn't leave a revoked credential without a paper
    # trail. Both rows live under the same UoW — rollback takes them
    # together.
    write_audit(
        session,
        ctx,
        entity_kind="passkey_credential",
        entity_id=credential_id_b64,
        action="passkey.revoked",
        diff={
            "user_id": user_id,
            "transports": row.transports,
            "backup_eligible": row.backup_eligible,
            "label": row.label,
        },
        clock=clock,
    )

    # justification: passkey_credential is identity-scoped.
    with tenant_agnostic():
        session.delete(row)
        session.flush()

    # §15 "Shared-origin XSS containment" / cd-geqp: a revocation is a
    # credential-population change. Invalidate every active session for
    # this user. The caller's own session is invalidated with the rest
    # — a user revoking their only-other-device's passkey needs to
    # re-auth to confirm intent, and the SPA surfaces that as a clean
    # re-login. Forensic rows survive (invalidation is non-destructive,
    # see :func:`app.auth.session.invalidate_for_user`).
    session_module.invalidate_for_user(
        session,
        user_id=user_id,
        cause="passkey_revoked",
        now=resolved_now,
        clock=clock,
    )

    return credential_id_b64


# ---------------------------------------------------------------------------
# Public surface — recovery flow (existing user, start-from-scratch semantics)
# ---------------------------------------------------------------------------


def register_start_recovery(
    session: Session,
    *,
    user_id: str,
    clock: Clock | None = None,
    rp: RelyingParty | None = None,
    now: datetime | None = None,
) -> RegistrationOptions:
    """Mint a registration challenge during self-service recovery.

    Mirrors :func:`register_start` but:

    * Accepts no :class:`WorkspaceContext` — recovery runs outside
      every workspace (the user may hold grants in any number of
      them; recovery picks none).
    * Skips the :data:`_MAX_PASSKEYS_PER_USER` cap check. Recovery's
      semantics are "start fresh": by the time
      :func:`app.auth.recovery.complete_recovery` lands the new
      credential, every prior passkey is gone. Enforcing the cap
      at start would make a user with 5 existing passkeys
      permanently stuck — they can't log in (lost device) and
      can't recover.
    * Omits the ``excludeCredentials`` list. The existing
      credentials are about to be revoked, so asking the browser
      to refuse a duplicate against one of them would force the
      user onto a specific device (the one that isn't already
      enrolled). Letting the browser choose any authenticator —
      including one that happens to match an old credential id —
      is the correct shape for "register a fresh passkey, any
      device is fine".

    The returned challenge is still single-use, still bound to the
    user via ``user_id``, and still 10-minute TTL. The
    :func:`register_finish` call that consumes it (via
    :func:`app.auth.recovery.complete_recovery`) re-checks the
    user's identity against the challenge row and fails closed if
    they disagree.
    """
    resolved_now = now if now is not None else _now(clock)
    rp = rp or make_relying_party()

    user = _load_user(session, user_id=user_id)

    options_json, challenge_bytes = generate_registration_challenge(
        rp=rp,
        # Same opaque-bytes encoding as :func:`register_start`.
        user_id=user.id.encode("utf-8"),
        user_name=user.email,
        user_display_name=user.display_name,
        # Deliberate empty: existing credentials are about to be
        # revoked by :func:`complete_recovery`, so the browser must
        # NOT refuse a duplicate against them.
        existing_credential_ids=(),
    )

    challenge_id = new_ulid(clock=clock)
    _insert_challenge(
        session,
        challenge_id=challenge_id,
        user_id=user_id,
        signup_session_id=None,
        challenge=challenge_bytes,
        # No excludeCredentials stashed either — matches the
        # empty list we passed to ``generate_registration_challenge``.
        existing_credential_ids=[],
        now=resolved_now,
    )

    return RegistrationOptions(
        challenge_id=challenge_id,
        options=options_to_dict(options_json),
    )


# ---------------------------------------------------------------------------
# Public surface — signup flow (no user, no workspace)
# ---------------------------------------------------------------------------


def register_start_signup(
    session: Session,
    *,
    signup_session_id: str,
    email: str,
    display_name: str,
    user_handle: bytes | None = None,
    clock: Clock | None = None,
    rp: RelyingParty | None = None,
    now: datetime | None = None,
) -> RegistrationOptions:
    """Mint a registration challenge during bare-host signup.

    No user row exists yet — the signup service creates it in the
    same transaction as :func:`register_finish_signup`. We stash the
    challenge against ``signup_session_id`` so the pair round-trips
    the browser without leaking identity to the login-by-discoverable-
    credential path (§03 "Self-serve signup" step 3).

    ``user_handle`` is the opaque bytes bound into the WebAuthn user
    entity; if unset we generate one from a fresh ULID so the
    authenticator has something to bind the resident key to. The
    signup service SHOULD supply the freshly-minted ``user.id`` once
    it reserves one, for symmetry with :func:`register_start`.
    """
    resolved_now = now if now is not None else _now(clock)
    rp = rp or make_relying_party()

    if user_handle is None:
        user_handle = new_ulid(clock=clock).encode("utf-8")

    options_json, challenge_bytes = generate_registration_challenge(
        rp=rp,
        user_id=user_handle,
        user_name=email,
        user_display_name=display_name,
        existing_credential_ids=(),
    )

    challenge_id = new_ulid(clock=clock)
    _insert_challenge(
        session,
        challenge_id=challenge_id,
        user_id=None,
        signup_session_id=signup_session_id,
        challenge=challenge_bytes,
        existing_credential_ids=[],
        now=resolved_now,
    )

    return RegistrationOptions(
        challenge_id=challenge_id,
        options=options_to_dict(options_json),
    )


def register_finish_signup(
    session: Session,
    *,
    signup_session_id: str,
    user_id: str,
    challenge_id: str,
    credential: dict[str, Any],
    clock: Clock | None = None,
    rp: RelyingParty | None = None,
    now: datetime | None = None,
) -> PasskeyCredentialRef:
    """Verify the signup-flow attestation + persist the first passkey.

    No audit row is written here — the signup service (cd-3i5) owns
    the ``user.enrolled`` audit emission once it creates the
    ``users`` / ``role_grant`` / ``permission_group_member`` rows in
    the same transaction. Splitting the writes would either produce
    an orphan audit (our row without the signup scaffold) or a
    missing audit (signup's row with no passkey), both of which
    defeat §03 "Every enrollment … writes to the audit log".

    ``now`` lets :func:`app.auth.signup.complete_signup` forward its
    already-resolved wall-clock time so the challenge-TTL comparison
    here agrees with the caller's view — otherwise a test that pins
    ``now`` without also freezing ``clock`` trips the 10-minute TTL
    against the real system clock.
    """
    resolved_now = now if now is not None else _now(clock)
    rp = rp or make_relying_party()

    row = _load_challenge(session, challenge_id=challenge_id, now=resolved_now)
    if row.signup_session_id is None or row.user_id is not None:
        raise ChallengeSubjectMismatch(
            "challenge was minted for an authenticated user; call register_finish"
        )
    if row.signup_session_id == _LOGIN_SUBJECT_SENTINEL:
        # Login challenge smuggled into the signup finish path. Reject
        # with the same "wrong subject" shape — the HTTP router
        # collapses this to the privacy-preserving envelope.
        raise ChallengeSubjectMismatch(
            "challenge was minted for login; call login_finish"
        )
    if row.signup_session_id != signup_session_id:
        raise ChallengeSubjectMismatch(
            f"challenge is for signup session {row.signup_session_id!r}, "
            f"not {signup_session_id!r}"
        )

    verified = _verify_or_raise(
        rp=rp,
        credential=credential,
        expected_challenge=row.challenge,
    )

    credential_ref = _insert_passkey_and_audit(
        session,
        workspace_ctx=None,
        user_id=user_id,
        verified=verified,
        credential=credential,
        clock=clock,
        now=resolved_now,
    )

    _delete_challenge(session, row=row)
    return credential_ref


# ---------------------------------------------------------------------------
# Login helpers (internal)
# ---------------------------------------------------------------------------


def _load_passkey_by_credential_id(
    session: Session, *, credential_id: bytes
) -> PasskeyCredential | None:
    """Return the ``passkey_credential`` row for ``credential_id``, or None.

    Identity-scoped — the table has no ``workspace_id`` column, so
    the ORM tenant filter is bypassed via :func:`tenant_agnostic`.
    A miss returns ``None`` (not a raise) so the caller can collapse
    "unknown credential" and "bad signature" into a single
    :class:`InvalidLoginAttempt`.
    """
    # justification: passkey_credential is identity-scoped.
    with tenant_agnostic():
        return session.get(PasskeyCredential, credential_id)


def _decode_credential_id(credential: dict[str, Any]) -> bytes:
    """Extract the raw credential id bytes from a browser assertion payload.

    WebAuthn payloads carry the credential id as base64url in the
    top-level ``id`` field. A malformed (non-string or unparsable)
    ``id`` is translated into :class:`InvalidLoginAttempt` — the
    caller already collapses unknown-credential and bad-signature
    into the same envelope, so a wrong-shape body gets the same
    treatment rather than bubbling a py_webauthn internal error.
    """
    raw_id = credential.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        raise InvalidLoginAttempt("credential payload missing 'id'")
    try:
        return base64url_to_bytes(raw_id)
    except (ValueError, TypeError) as exc:
        # py_webauthn raises a concrete :class:`ValueError` /
        # :class:`TypeError` on a malformed base64url. We rewrap
        # into the login vocabulary so the HTTP layer stays opaque;
        # the cause chain preserves the upstream message for log
        # readers.
        raise InvalidLoginAttempt(f"credential id is not base64url: {exc}") from exc


def _tenant_agnostic_login_audit_ctx() -> WorkspaceContext:
    """Return a tenant-agnostic :class:`WorkspaceContext` for login audit.

    Mirrors the shape used by :mod:`app.auth.session` — login runs
    before any workspace is picked, so the audit row carries zero-ULID
    placeholders in the workspace / actor fields and the real
    details live in the ``diff`` payload.
    """
    return WorkspaceContext(
        workspace_id="00000000000000000000000000",
        workspace_slug="",
        actor_id="00000000000000000000000000",
        actor_kind="system",
        actor_grant_role="manager",
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


# ---------------------------------------------------------------------------
# Public surface — login (discoverable credential / conditional UI)
# ---------------------------------------------------------------------------


def login_start(
    session: Session,
    *,
    clock: Clock | None = None,
    rp: RelyingParty | None = None,
    now: datetime | None = None,
) -> AuthenticationOptions:
    """Mint an authentication challenge for the login page.

    Conditional UI + discoverable credentials (§03 "Login") mean the
    browser picks the credential and the server discovers the user
    on ``finish`` — no user context is needed at ``start``, and the
    returned options carry an empty ``allowCredentials`` list so the
    authenticator surfaces every passkey the user has enrolled on
    this device.

    The challenge row carries :data:`_LOGIN_SUBJECT_SENTINEL` in
    ``signup_session_id`` so it satisfies the ``webauthn_challenge``
    CHECK constraint (exactly one of ``user_id`` / ``signup_session_id``
    non-null) while being distinguishable from a real signup session.

    Caller's UoW owns the transaction boundary; this function never
    commits.
    """
    resolved_now = now if now is not None else _now(clock)
    rp = rp or make_relying_party()

    options_json, challenge_bytes = generate_authentication_challenge(
        rp=rp,
        # Conditional UI: empty allow-list so the browser's
        # authenticator surfaces every enrolled passkey.
        allow_credential_ids=(),
    )

    challenge_id = new_ulid(clock=clock)
    _insert_challenge(
        session,
        challenge_id=challenge_id,
        user_id=None,
        signup_session_id=_LOGIN_SUBJECT_SENTINEL,
        challenge=challenge_bytes,
        existing_credential_ids=[],
        now=resolved_now,
    )

    return AuthenticationOptions(
        challenge_id=challenge_id,
        options=options_to_dict(options_json),
    )


def login_finish(
    session: Session,
    *,
    challenge_id: str,
    credential: dict[str, Any],
    ip: str,
    ua: str,
    ip_hash_pepper: bytes,
    throttle: Throttle,
    accept_language: str = "",
    clock: Clock | None = None,
    rp: RelyingParty | None = None,
    now: datetime | None = None,
) -> LoginResult:
    """Verify the browser's assertion, issue a session, return the cookie.

    The flow:

    1. **Lockout check.** Hash the credential id + IP with
       ``ip_hash_pepper`` and ask the throttle whether either bucket
       is locked out (3 fails / 10-min per §15). A lockout raises
       :class:`PasskeyLoginLockout` before any DB read.
    2. **Challenge load.** Load the ``webauthn_challenge`` row;
       reject if it isn't the login sentinel subject. Expiry is
       surfaced as :class:`ChallengeExpired`, which the HTTP router
       maps to the same 401 envelope as an invalid credential.
    3. **Credential load.** Resolve ``credential["id"]`` to a
       ``passkey_credential`` row. A miss collapses to
       :class:`InvalidLoginAttempt` — the caller can't tell unknown
       credential from bad signature.
    4. **Verify assertion.** Hand off to
       :func:`app.auth.webauthn.verify_authentication`. Any rejection
       is rewrapped as :class:`InvalidLoginAttempt`.
    5. **Clone detection.** If the returned counter did not exceed
       the stored counter AND the stored counter is non-zero,
       raise :class:`CloneDetected` (§15 "Passkey specifics"). A
       stored counter of zero means the authenticator doesn't
       implement the counter, which is legal per WebAuthn — we skip
       the check in that case.
    6. **Persist.** Update ``sign_count`` + ``last_used_at`` on the
       credential row, delete the challenge (single-use), write an
       ``audit.passkey.assertion_ok`` row, and issue a session via
       :func:`app.auth.session.issue`. The session's
       ``has_owner_grant`` flag is resolved via
       :func:`app.authz.owners.is_owner_on_any_workspace` so the
       7-day-vs-30-day TTL matches the spec.

    The throttle's failure-recording is the caller's (router's)
    responsibility — the domain service only consults the lockout
    gate. Keeping the write side at the router means a test harness
    can observe individual failures without the success-path being
    coupled to the router's except handler, and the caller's
    transaction rolls back cleanly on a raise without leaving a
    throttle bucket half-advanced.
    """
    resolved_now = now if now is not None else _now(clock)
    rp = rp or make_relying_party()

    ip_hash = hash_with_pepper(ip, ip_hash_pepper)
    credential_id = _decode_credential_id(credential)
    credential_id_b64 = bytes_to_base64url(credential_id)
    credential_id_hash = hash_with_pepper(credential_id_b64, ip_hash_pepper)

    # Lockout gate first — a locked-out bucket short-circuits before
    # any DB read. The throttle raises :class:`PasskeyLoginLockout`
    # which the HTTP router maps to 429 rate_limited.
    throttle.check_passkey_login_allowed(
        credential_id_hash=credential_id_hash,
        ip_hash=ip_hash,
        now=resolved_now,
    )

    # Challenge load — :class:`ChallengeNotFound` / :class:`ChallengeExpired`
    # propagate directly; the HTTP router collapses them into the
    # same 401 envelope as :class:`InvalidLoginAttempt`.
    row = _load_challenge(session, challenge_id=challenge_id, now=resolved_now)
    if row.user_id is not None or row.signup_session_id != _LOGIN_SUBJECT_SENTINEL:
        # Non-login challenge smuggled into the login finish path.
        raise ChallengeSubjectMismatch(
            "challenge was not minted for login; call register_finish"
        )

    # Credential load — a miss collapses to :class:`InvalidLoginAttempt`
    # so the HTTP shape cannot fingerprint "unknown credential" vs
    # "bad signature".
    pk_row = _load_passkey_by_credential_id(session, credential_id=credential_id)
    if pk_row is None:
        raise InvalidLoginAttempt(
            f"no passkey credential for id (b64={credential_id_b64})"
        )

    try:
        verified: VerifiedAuthentication = verify_authentication(
            rp=rp,
            credential=credential,
            expected_challenge=row.challenge,
            credential_public_key=pk_row.public_key,
            credential_current_sign_count=pk_row.sign_count,
        )
    except InvalidAuthenticationResponse as exc:
        raise InvalidLoginAttempt(str(exc)) from exc

    old_sign_count = pk_row.sign_count
    # Clone detection — §15 "Passkey specifics". A stored counter of
    # zero means the authenticator doesn't implement the counter
    # (legal per WebAuthn); skip the check in that case. Otherwise
    # the new counter MUST strictly exceed the stored one. The
    # ``cloned_detected`` audit is written by the HTTP router on a
    # fresh UoW — the caller's UoW rolls back on this raise, so an
    # in-UoW audit would be lost. The exception carries the payload
    # the router needs to emit the audit row without re-reading the
    # DB.
    if old_sign_count > 0 and verified.new_sign_count <= old_sign_count:
        raise CloneDetected(
            f"sign_count rollback detected: stored={old_sign_count} "
            f"returned={verified.new_sign_count}",
            credential_id_b64=credential_id_b64,
            old_sign_count=old_sign_count,
            new_sign_count=verified.new_sign_count,
        )

    # Persist — update counter, bump last_used_at, delete the single-
    # use challenge row.
    # justification: passkey_credential is identity-scoped.
    with tenant_agnostic():
        pk_row.sign_count = verified.new_sign_count
        pk_row.last_used_at = resolved_now
        session.flush()

    _delete_challenge(session, row=row)

    # Issue session — TTL depends on ``has_owner_grant``. The owner
    # lookup is a single SELECT against the permission_group_member
    # table, tenant-agnostic because login runs before a workspace
    # is picked.
    has_owner_grant = is_owner_on_any_workspace(session, user_id=pk_row.user_id)
    session_issue = session_module.issue(
        session,
        user_id=pk_row.user_id,
        has_owner_grant=has_owner_grant,
        ua=ua,
        ip=ip,
        accept_language=accept_language,
        now=resolved_now,
        clock=clock,
    )

    # Audit the successful assertion. Hash-only — the credential id
    # is public per WebAuthn, but the IP stays hashed to match the
    # rest of the identity-layer audit shape (§15 PII minimisation).
    write_audit(
        session,
        _tenant_agnostic_login_audit_ctx(),
        entity_kind="passkey_credential",
        entity_id=credential_id_b64,
        action="passkey.assertion_ok",
        diff={
            "user_id": pk_row.user_id,
            "cred_id_b64": credential_id_b64,
            "ip_hash": ip_hash,
            "has_owner_grant": has_owner_grant,
            "old_sign_count": old_sign_count,
            "new_sign_count": verified.new_sign_count,
        },
        clock=clock,
    )

    return LoginResult(
        session_issue=session_issue,
        user_id=pk_row.user_id,
        credential_id_b64url=credential_id_b64,
    )
