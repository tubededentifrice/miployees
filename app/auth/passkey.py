"""Passkey registration ceremony — domain service.

Two round-trips:

* :func:`register_start` mints a challenge, persists it as a
  :class:`~app.adapters.db.identity.models.WebAuthnChallenge` row
  (10-minute TTL), and returns the options JSON
  (``PublicKeyCredentialCreationOptions``) the browser passes to
  ``navigator.credentials.create()``.
* :func:`register_finish` loads the challenge row by id, verifies the
  authenticator's attestation via
  :func:`app.auth.webauthn.verify_registration`, inserts one
  :class:`~app.adapters.db.identity.models.PasskeyCredential` row and
  one ``audit.passkey.registered`` row in the same transaction, and
  deletes the challenge so the request is single-use.

Two subject families are supported:

* **Authenticated user adding a passkey** — :func:`register_start` /
  :func:`register_finish`. The challenge row carries the ``user_id``;
  a workspace context is present but the write is identity-scoped,
  so the ORM tenant filter is bypassed via
  :func:`app.tenancy.tenant_agnostic` on every query.
* **Signup session enrolling its first passkey** —
  :func:`register_start_signup` / :func:`register_finish_signup`. No
  user row exists yet at ``start``; the caller supplies a
  ``signup_session_id`` we stash on the challenge. ``finish`` accepts
  the freshly-minted ``user_id`` (created in the same transaction by
  the signup service, cd-3i5) and inserts the credential row.

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

**Transaction discipline.** The service never calls
``session.commit()`` — the Unit-of-Work that opened the session owns
the transaction boundary (§01 "Key runtime invariants" #3).
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
from app.auth.webauthn import (
    InvalidRegistrationResponse,
    RelyingParty,
    VerifiedRegistration,
    bytes_to_base64url,
    generate_registration_challenge,
    make_relying_party,
    options_to_dict,
    verify_registration,
)
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "ChallengeAlreadyConsumed",
    "ChallengeExpired",
    "ChallengeNotFound",
    "ChallengeSubjectMismatch",
    "InvalidRegistration",
    "PasskeyCredentialRef",
    "RegistrationOptions",
    "TooManyPasskeys",
    "register_finish",
    "register_finish_signup",
    "register_start",
    "register_start_signup",
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


class ChallengeSubjectMismatch(ValueError):
    """Challenge subject does not match the finish call's subject.

    Raised when a user-bound challenge is redeemed by the signup path
    (or vice versa), or when the asserted ``user_id`` disagrees with
    the one stashed on the challenge row. 400-equivalent — the HTTP
    router should not distinguish this from :class:`InvalidRegistration`
    for privacy, but the type exists so domain tests can pin the
    cross-subject bug.
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
    """
    del ctx  # signature-compat only; no workspace predicate applies here
    now = _now(clock)
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
        now=now,
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
) -> PasskeyCredentialRef:
    """Verify the attestation response + persist the passkey row.

    Writes one ``passkey_credential`` row and one
    ``audit.passkey.registered`` audit row in the caller's open
    transaction, then deletes the consumed challenge so a replay
    raises :class:`ChallengeAlreadyConsumed`.

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
    now = _now(clock)
    rp = rp or make_relying_party()

    row = _load_challenge(session, challenge_id=challenge_id, now=now)
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
        now=now,
    )

    _delete_challenge(session, row=row)
    return credential_ref


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
    now = _now(clock)
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
        now=now,
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
) -> PasskeyCredentialRef:
    """Verify the signup-flow attestation + persist the first passkey.

    No audit row is written here — the signup service (cd-3i5) owns
    the ``user.enrolled`` audit emission once it creates the
    ``users`` / ``role_grant`` / ``permission_group_member`` rows in
    the same transaction. Splitting the writes would either produce
    an orphan audit (our row without the signup scaffold) or a
    missing audit (signup's row with no passkey), both of which
    defeat §03 "Every enrollment … writes to the audit log".
    """
    now = _now(clock)
    rp = rp or make_relying_party()

    row = _load_challenge(session, challenge_id=challenge_id, now=now)
    if row.signup_session_id is None or row.user_id is not None:
        raise ChallengeSubjectMismatch(
            "challenge was minted for an authenticated user; call register_finish"
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
        now=now,
    )

    _delete_challenge(session, row=row)
    return credential_ref
