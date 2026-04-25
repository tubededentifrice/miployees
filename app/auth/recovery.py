"""Self-service lost-device recovery domain service.

Three public entry points wire the spec's §03 "Self-service lost-
device recovery" flow:

1. :func:`request_recovery` — rate-limit gate, user lookup, audit
   ``recovery.requested`` (distinguishing hit vs miss), mail the
   magic link for a matched user / mail the no-account notice for
   an unmatched one. Always returns ``None`` — the caller sees an
   identical 202 on both branches (§15 enumeration guard).
2. :func:`verify_recovery` — consume the magic link with
   ``expected_purpose='recover_passkey'``, mint a transient
   **recovery session** that permits ONLY the passkey-finish step,
   audit ``recovery.verified``.
3. :func:`complete_recovery` — **one transaction**: revoke every
   ``passkey_credential`` row for the user, revoke every non-
   recovery ``session`` row for the user, register the new passkey,
   consume the recovery session, audit ``recovery.completed``.
   Atomicity is load-bearing — a partial revoke would leave the
   user either still authenticated with a stolen device or locked
   out without a working credential.

**Recovery session storage.** Recovery sessions live in an in-
memory process-wide dict with a 15-minute TTL. This is the same
"temporary home" shape the magic-link :class:`Throttle` uses until
cd-7huk absorbs both into the shared deployment-wide state store:
adding a ``kind`` column to the :class:`Session` table would need a
migration + backfill that does not belong inside this slice, and
persisting to :class:`Session` with ``workspace_id=NULL`` would
re-purpose the existing auth-session shape in ways downstream code
(session-cleanup, security page) would have to branch on. A
single-process dict is the right level of abstraction for v1 —
crew.day runs one worker per deployment (§01 "One worker pool per
process") and a worker restart invalidates every in-flight recovery,
which is the safe fail-closed default. cd-7huk absorbs this alongside
the throttle.

**Enumeration-timing hardening.** The ``request`` entry point is
designed so the two branches pay (nearly) the same CPU:

* both hash the email + IP under the HKDF pepper;
* both advance the recover-start throttle (so a hostile scanner
  can't spin "is this email known?" any faster than "is this email
  flagged?");
* the hit branch signs the magic-link token (HMAC-SHA256 via
  ``magic_link.request_link``); the miss branch burns the **same**
  HMAC cost on throwaway bytes inside
  :func:`_burn_cpu_for_miss_branch`, so the sign cost is not a
  timing channel;
* both write ``audit.recovery.requested`` — with ``hit=True`` when
  a user row was found, ``hit=False`` otherwise — so an operator
  can tell "nothing happened" from "everything went fine" without
  the caller seeing the diff;
* both hand a message to the :class:`Mailer` port. The template
  differs (``recovery_new_link`` vs ``recovery_unknown``) so a
  legitimate owner who mistyped their address gets a useful
  signal, but the cadence on the wire matches.

The **one residual gap** is the DB INSERT the hit branch performs
on the magic-link nonce row — roughly a sub-millisecond on SQLite
in-memory, indistinguishable under real network jitter. Balancing
that with a throwaway DB row would be worse (garbage data, extra
index churn) than accepting the documented residual. This matches
the spec §15 "constant-time responses" requirement while preserving
the enumeration guard: nothing the caller can practically observe
(status, latency on a real network, mail cadence) tells them
whether their email was known to the deployment.

**Audit.** Every entry point emits one row under an agnostic
:class:`WorkspaceContext` (recovery runs strictly before a workspace
context is resolved — the user may belong to any number of
workspaces):

* ``audit.recovery.requested`` with ``hit``, ``email_hash``,
  ``ip_hash``.
* ``audit.recovery.disabled_by_workspace`` with ``email_hash``,
  ``ip_hash``, ``reason`` (§03 "Workspace kill-switch"; written
  on a fresh UoW because the primary UoW has no domain state to
  commit in this branch).
* ``audit.recovery.verified`` with ``email_hash``,
  ``ip_hash_at_verify``.
* ``audit.recovery.completed`` with ``user_id``,
  ``revoked_credential_count``, ``revoked_session_count``,
  ``new_credential_id``.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service lost-device
recovery", §"Recovery paths" and ``docs/specs/15-security-privacy.md``
§"Self-service lost-device & email-change abuse mitigations".
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as SqlaSession

from app.adapters.db.authz.models import RoleGrant
from app.adapters.db.identity.models import (
    PasskeyCredential,
    User,
    canonicalise_email,
)
from app.adapters.db.session import make_uow
from app.adapters.db.workspace.models import Workspace
from app.adapters.mail.ports import MailDeliveryError, Mailer
from app.audit import write_audit
from app.auth import magic_link, passkey
from app.auth import session as session_module
from app.auth._hashing import hash_with_pepper
from app.auth._throttle import RecoveryRateLimited, Throttle
from app.auth.keys import derive_subkey
from app.config import Settings, get_settings
from app.mail.templates import recovery_new_link as recovery_new_link_template
from app.mail.templates import recovery_unknown as recovery_unknown_template
from app.mail.templates import render as render_template
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

_log = logging.getLogger(__name__)

__all__ = [
    "CompletedRecovery",
    "RecoveryRateLimited",
    "RecoverySession",
    "RecoverySessionExpired",
    "RecoverySessionNotFound",
    "complete_recovery",
    "is_self_service_recovery_disabled",
    "prune_expired_recovery_sessions",
    "request_recovery",
    "verify_recovery",
]


# Spec §03 "Self-service lost-device recovery": the magic link TTL is
# 15 minutes. We pin the recovery-session TTL to the same value so a
# user who verifies the link at minute 14 gets a full 15-minute window
# to complete the passkey ceremony (they might need to pair a hardware
# key, find the device, etc.). Magic-link's own TTL cap for
# ``recover_passkey`` is 10 minutes today (§03 table); if the spec's
# 15-min target shifts, the cap here and in :mod:`app.auth.magic_link`
# flex together.
_RECOVERY_SESSION_TTL: Final[timedelta] = timedelta(minutes=15)

# HKDF purpose for the email / IP hash pepper. Reuses the magic-link
# subkey — the recovery audit row hashes the same email with the same
# pepper as the magic-link nonce row, so abuse correlation joins
# without a re-derivation.
_HKDF_PURPOSE: Final[str] = "magic-link"


# Canonical setting key for the workspace kill-switch (§02 "Settings
# cascade" catalog + §03 "Workspace kill-switch"). Lives on
# ``workspace.settings_json`` — cd-n6p is the task that lands owner-
# facing writes, but the recovery gate is the first reader and reads
# the key straight out of the JSON blob so the resolver can stay
# simple until the richer cd-n6p surface lands.
_SELF_SERVICE_RECOVERY_KEY: Final[str] = "auth.self_service_recovery_enabled"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RecoverySessionNotFound(LookupError):
    """No recovery session matches the id supplied to :func:`complete_recovery`.

    404-equivalent. The router surfaces this as
    ``404 recovery_session_not_found``. Distinguishing "session never
    existed" from "session expired" in the HTTP body would leak a
    forensic bit an attacker could use to time the replay window; the
    router folds both into the same 404.
    """


class RecoverySessionExpired(LookupError):
    """Recovery session's TTL elapsed before :func:`complete_recovery`.

    Declared as a distinct type so tests can pin the spec mapping —
    the router collapses it with :class:`RecoverySessionNotFound` onto
    a single 404 for privacy.
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoverySession:
    """Payload returned by :func:`verify_recovery`.

    ``recovery_session_id`` is the opaque handle the caller passes
    back to :func:`complete_recovery`; it indexes the in-memory
    :data:`_RECOVERY_SESSIONS` store. The ``email_hash`` / ``ip_hash``
    fields let the caller audit / log without re-deriving the hashes.
    """

    user_id: str
    recovery_session_id: str
    email_hash: str
    ip_hash: str


@dataclass(frozen=True, slots=True)
class CompletedRecovery:
    """Payload returned by :func:`complete_recovery`.

    ``revoked_credential_count`` / ``revoked_session_count`` surface
    the destructive blast radius so the caller (SPA or CLI) can
    render a "we revoked N passkeys + signed out M sessions"
    confirmation without a second round-trip.
    """

    user_id: str
    new_credential_id: str
    revoked_credential_count: int
    revoked_session_count: int


# ---------------------------------------------------------------------------
# Recovery session store — process-local, 15-min TTL
# ---------------------------------------------------------------------------


@dataclass
class _RecoverySessionRow:
    """In-memory record for one verified-but-not-yet-completed recovery."""

    recovery_session_id: str
    user_id: str
    email_hash: str
    ip_hash: str
    created_at: datetime
    expires_at: datetime


# Module-level state — one dict per process, guarded by a lock. Matches
# the :class:`Throttle` shape (cd-4zz) on purpose: cd-7huk absorbs both
# into the deployment-wide state store in one swap. A dedicated class
# (``_RecoverySessionStore``) would be marginally tidier but would
# double the surface area tests have to reason about; the module-level
# primitives mirror the throttle's own layout.
_RECOVERY_SESSIONS: dict[str, _RecoverySessionRow] = {}
_RECOVERY_SESSIONS_LOCK: threading.Lock = threading.Lock()


def _store_recovery_session(row: _RecoverySessionRow) -> None:
    """Insert ``row`` under its id, replacing any prior entry at that key.

    ULIDs are astronomically unlikely to collide, but if the caller
    explicitly passes a duplicate id (e.g. a test pinning one for
    determinism), the later write wins rather than raising — matches
    dict semantics and keeps the helper total.
    """
    with _RECOVERY_SESSIONS_LOCK:
        _RECOVERY_SESSIONS[row.recovery_session_id] = row


def _load_recovery_session(
    recovery_session_id: str, *, now: datetime
) -> _RecoverySessionRow:
    """Return the live recovery session for ``id`` or raise.

    Missing row → :class:`RecoverySessionNotFound`.
    Row present but ``expires_at`` elapsed → :class:`RecoverySessionExpired`
    (the row is also evicted so a retry sees "not found" cleanly).
    """
    with _RECOVERY_SESSIONS_LOCK:
        row = _RECOVERY_SESSIONS.get(recovery_session_id)
        if row is None:
            raise RecoverySessionNotFound(recovery_session_id)
        if _aware_utc(row.expires_at) <= now:
            # Evict in passing so the caller's second attempt sees
            # "not found" rather than "expired" — both map to 404 at
            # the router anyway, and an eviction keeps the dict
            # bounded without a separate sweeper.
            del _RECOVERY_SESSIONS[recovery_session_id]
            raise RecoverySessionExpired(recovery_session_id)
    return row


def _consume_recovery_session(recovery_session_id: str) -> None:
    """Remove the recovery session from the store.

    Called on the success path of :func:`complete_recovery` so a
    replay raises :class:`RecoverySessionNotFound`. Tolerates a
    missing key (idempotent drop) — the concurrent ceremony that
    consumed it already flipped every downstream row, and raising
    here would be a shadow error during the happy path.
    """
    with _RECOVERY_SESSIONS_LOCK:
        _RECOVERY_SESSIONS.pop(recovery_session_id, None)


def prune_expired_recovery_sessions(*, now: datetime) -> int:
    """Evict every expired recovery session; return the count dropped.

    Exposed as a module-level helper so a future scheduler hook can
    keep the dict bounded without walking every key on each lookup.
    Called opportunistically from tests; cd-7huk will replace this
    with a TTL-expiring Redis / shared-state wiring.
    """
    dropped = 0
    with _RECOVERY_SESSIONS_LOCK:
        expired_keys = [
            key
            for key, row in _RECOVERY_SESSIONS.items()
            if _aware_utc(row.expires_at) <= now
        ]
        for key in expired_keys:
            del _RECOVERY_SESSIONS[key]
            dropped += 1
    return dropped


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or a fresh system clock."""
    return (clock if clock is not None else SystemClock()).now()


def _pepper(settings: Settings | None) -> bytes:
    """Return the HKDF pepper used for email / IP hashes."""
    s = settings if settings is not None else get_settings()
    return derive_subkey(s.root_key, purpose=_HKDF_PURPOSE)


def _aware_utc(value: datetime) -> datetime:
    """Normalise naive ``datetime`` values to aware UTC.

    SQLite's ``DateTime(timezone=True)`` drops tzinfo on round-trip;
    the in-memory recovery-session rows carry aware values by
    construction, but the helper also normalises inputs from the
    magic-link service which may round-trip through the DB. Mirrors
    :func:`app.auth.signup._aware_utc`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _agnostic_audit_ctx() -> WorkspaceContext:
    """Sentinel :class:`WorkspaceContext` for pre-tenant audit rows.

    Mirrors :func:`app.auth.magic_link._agnostic_audit_ctx` and
    :func:`app.auth.signup._agnostic_audit_ctx`: recovery runs
    outside every workspace (the user may hold grants in any number
    of workspaces and we don't pick one for them at recovery time).
    The audit reader recognises the zero-ULID workspace as a pre-
    tenant identity event.
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


def _lookup_user_by_email(session: SqlaSession, *, email_lower: str) -> User | None:
    """Return the :class:`User` row matching ``email_lower`` or ``None``."""
    # justification: user is identity-scoped (see app/adapters/db/identity).
    with tenant_agnostic():
        return session.scalar(select(User).where(User.email_lower == email_lower))


def is_self_service_recovery_disabled(session: SqlaSession, *, user_id: str) -> bool:
    """Return ``True`` if **any** workspace holding a grant for ``user_id``
    has the self-service recovery kill-switch flipped.

    Spec §03 "Workspace kill-switch": the flag is evaluated *most-
    restrictive-wins* across every workspace the user holds a non-
    archived grant in. A single workspace with
    ``auth.self_service_recovery_enabled = false`` disables self-
    service recovery for the user deployment-wide — managers still
    re-issue manually, but the ``/recover/passkey/request`` flow
    mails nothing and audits
    ``audit.recovery.disabled_by_workspace``.

    **Non-archived grants in v1.** The v1 ``role_grant`` schema has no
    ``revoked_at`` column — revocation is a hard DELETE today (see
    :mod:`app.domain.identity.role_grants` module docstring), so every
    extant row is an active grant by construction. ``users.archived_at``
    likewise hasn't landed. When those columns arrive (cd-x1xh lands
    the role_grant soft-retire shape) the WHERE-clause below extends
    with ``RoleGrant.revoked_at IS NULL`` / a ``users.archived_at IS
    NULL`` pre-gate; until then "non-archived" == "row exists". The
    helper's public contract ("return True iff **any** workspace
    disables self-service recovery for the user") stays unchanged.

    **Default semantics.** Absence of the key — a workspace that
    never wrote a value for the flag — is treated as ``True`` (the
    catalog default in §02 "Settings cascade"). The check only
    disables recovery when a workspace has **explicitly** stored
    ``False`` under the canonical key. Non-bool values are ignored:
    operators write to this column through the admin UI which
    validates the payload, but a corrupt row is better fail-open
    (recovery still works) than a locked-out user. Fail-open matches
    §03's guidance that the kill-switch is a deliberate operator
    action, not a mis-parse.

    Runs under :func:`tenant_agnostic` because ``role_grant`` is
    workspace-scoped and recovery executes outside any
    :class:`~app.tenancy.WorkspaceContext` (the user may hold grants
    in arbitrarily many workspaces and we don't pick one at recovery
    time). One SELECT joins ``role_grant`` → ``workspace`` by
    ``workspace_id`` so a single round-trip returns the full set of
    candidate ``settings_json`` payloads; the Python-side loop stops
    at the first ``False`` for the common case where one workspace
    flags the kill-switch while most do not.
    """
    # justification: ``role_grant`` is workspace-scoped and recovery
    # runs outside any tenant context (see module docstring on
    # :func:`_agnostic_audit_ctx`). ``workspace`` is tenant-agnostic
    # by registration — the filter ignores it — but the
    # ``tenant_agnostic`` guard is required for the RoleGrant side of
    # the join.
    stmt = (
        select(Workspace.settings_json)
        .join(RoleGrant, RoleGrant.workspace_id == Workspace.id)
        .where(RoleGrant.user_id == user_id)
    )
    with tenant_agnostic():
        payloads = session.scalars(stmt).all()
    for payload in payloads:
        # ``payload`` is the flat dotted-key map the cascade reads
        # (§02 "Schema"). A defensive ``isinstance`` catches a
        # corrupt JSON row (e.g. someone wrote a list) without
        # tripping ``KeyError``; the non-dict path falls through to
        # fail-open, matching the rationale in the docstring.
        if not isinstance(payload, dict):
            continue
        value = payload.get(_SELF_SERVICE_RECOVERY_KEY)
        # Strict ``is False`` rather than ``not value`` — we want to
        # disable only on the explicit operator choice, not on every
        # falsy-adjacent value (``0``, ``""``, ``None``, missing
        # key) the JSON could land with.
        if value is False:
            return True
    return False


def _send_unknown_email(
    *,
    mailer: Mailer,
    to_email: str,
) -> None:
    """Send the no-account notice template.

    Pays the same mailer render + send cost as the hit branch so the
    wire cadence stays identical across the two branches.
    """
    subject = render_template(recovery_unknown_template.SUBJECT)
    body_text = render_template(recovery_unknown_template.BODY_TEXT)
    mailer.send(to=[to_email], subject=subject, body_text=body_text)


def _audit_recovery_disabled_by_workspace(
    *,
    user_id: str,
    email_hash: str,
    ip_hash: str,
    clock: Clock | None = None,
) -> None:
    """Write one ``audit.recovery.disabled_by_workspace`` row on a fresh UoW.

    Spec §03 "Self-service lost-device recovery" step 5 + §"Workspace
    kill-switch": when the user's grants land in a workspace whose
    ``auth.self_service_recovery_enabled`` flag is ``false``, the
    recovery service neither mints nor mails anything. The caller's
    primary UoW therefore has no domain row to commit; using it only
    for the audit would work but mirrors poorly with the other
    "refusal without domain state" paths (the router's
    :func:`_audit_recovery_refusal`). A fresh UoW pins the audit row
    in its own transaction — any rollback later in the caller's
    request handler (e.g. middleware tripping an HTTP error) cannot
    silently swallow the forensic trail of "operator has disabled
    this user's self-service recovery".

    Failures of the audit UoW are logged and swallowed: the caller is
    about to return 202 per §15's enumeration guard, and a shadowing
    500 here would leak the disabled state on the wire. The catch is
    deliberately broad (``Exception``) so a transient DB / config
    hiccup still logs-and-drops; ``BaseException`` propagates so
    operator aborts aren't swallowed.
    """
    diff = {
        "email_hash": email_hash,
        "ip_hash": ip_hash,
        "reason": "workspace_kill_switch",
    }
    try:
        with make_uow() as uow_session:
            assert isinstance(uow_session, SqlaSession)
            write_audit(
                uow_session,
                _agnostic_audit_ctx(),
                entity_kind="user",
                entity_id=user_id,
                action="recovery.disabled_by_workspace",
                diff=diff,
                clock=clock,
            )
    except Exception:
        _log.exception("recovery disabled-by-workspace audit write failed on fresh UoW")


def _burn_cpu_for_miss_branch(pepper: bytes) -> None:
    """Cover most of the hit-branch HMAC cost on the miss branch.

    The hit branch pays an HMAC-SHA256 sign on the magic-link token
    inside :func:`magic_link.request_link`. Without a matching HMAC on
    the miss branch, a local timing adversary could measure a few
    hundred microseconds of gap and learn whether the submitted email
    is known — which is exactly what the §15 "constant-time responses"
    requirement forbids.

    We compute (and discard) one HMAC over 48 throwaway random bytes
    using the same pepper the hit branch signs with. This balances the
    CPU-side of the asymmetry; the residual gap is the one DB INSERT
    the hit branch performs (the magic-link nonce row). Balancing the
    DB round-trip would require writing a throwaway row, which is
    worse than the residual timing leak (now a few hundred microseconds
    on SQLite in-memory, indistinguishable under real network jitter).
    The residual gap is documented in :func:`request_recovery` and
    accepted as the pragmatic tradeoff.
    """
    # :func:`hmac.new` expects ``bytes`` for both key and msg. We use
    # 48 bytes to match the token size :func:`magic_link.request_link`
    # signs (see ``_TOKEN_ENTROPY_BYTES`` there). ``hmac.digest`` is
    # the fast single-shot form — no stateful object allocation.
    throwaway = secrets.token_bytes(48)
    hmac.digest(pepper, throwaway, "sha256")


def _send_recovery_link(
    *,
    mailer: Mailer,
    to_email: str,
    display_name: str,
    base_url: str,
    token: str,
    ttl: timedelta,
) -> None:
    """Render the recovery template and hand the message to the mailer port.

    ``base_url`` is an operator-configured URL (e.g.
    ``https://crew.day``) with a trailing slash stripped defensively.
    The URL path pins ``/recover/enroll`` to match the SPA route
    spec §03 "Redemption" documents.
    """
    url = f"{base_url.rstrip('/')}/recover/enroll?token={token}"
    ttl_minutes = max(1, int(ttl.total_seconds() // 60))
    subject = render_template(recovery_new_link_template.SUBJECT)
    body_text = render_template(
        recovery_new_link_template.BODY_TEXT,
        display_name=display_name,
        url=url,
        ttl_minutes=str(ttl_minutes),
    )
    mailer.send(to=[to_email], subject=subject, body_text=body_text)


def _revoke_passkeys(session: SqlaSession, *, user_id: str) -> int:
    """Delete every :class:`PasskeyCredential` row for ``user_id``.

    Returns the number of rows deleted so the caller can surface
    ``revoked_credential_count`` in the completion payload. Wrapped
    under :func:`tenant_agnostic` because ``passkey_credential`` is
    identity-scoped (no workspace_id).
    """
    # justification: passkey_credential is identity-scoped.
    with tenant_agnostic():
        count = (
            session.scalar(
                select(func.count())
                .select_from(PasskeyCredential)
                .where(PasskeyCredential.user_id == user_id)
            )
            or 0
        )
        session.execute(
            delete(PasskeyCredential)
            .where(PasskeyCredential.user_id == user_id)
            .execution_options(synchronize_session=False)
        )
        session.flush()
    return int(count)


def _invalidate_sessions(
    session: SqlaSession,
    *,
    user_id: str,
    now: datetime,
    clock: Clock | None,
) -> int:
    """Invalidate every active session row for ``user_id``.

    Recovery has no "current session" to preserve — the caller is
    completing the ceremony on a device that did not have a passkey
    (by construction), so no prior session row belongs to them.
    Uses :func:`app.auth.session.invalidate_for_user` (cause
    ``"recovery_consumed"``) rather than the destructive
    :func:`revoke_all_for_user` so the forensic rows survive: a
    recovery is one of the rare surgical events where post-hoc
    operators want "every session the attacker rode in on" preserved.
    Returns the count for the completion payload.
    """
    return session_module.invalidate_for_user(
        session,
        user_id=user_id,
        cause="recovery_consumed",
        now=now,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Public surface — request
# ---------------------------------------------------------------------------


def request_recovery(
    session: SqlaSession,
    *,
    email: str,
    ip: str,
    mailer: Mailer,
    base_url: str,
    throttle: Throttle,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> None:
    """Kick off a self-service recovery request.

    Spec §03 "Self-service lost-device recovery" + §15 abuse
    mitigations. Steps, in order:

    1. Canonicalise the email; derive ``email_hash`` + ``ip_hash``
       from the HKDF pepper.
    2. Rate-limit via :meth:`Throttle.check_recover_start` — hashes
       only, no plaintext. Raises :class:`RecoveryRateLimited` on
       any over-cap bucket.
    3. Look up the user by canonical email.
    4. **Kill-switch gate** (§03 "Workspace kill-switch"): for a
       matched user, consult
       :func:`is_self_service_recovery_disabled`. If any workspace
       the user holds a non-archived grant in has
       ``auth.self_service_recovery_enabled = false``, skip every
       downstream step and write one
       ``audit.recovery.disabled_by_workspace`` row on a fresh UoW.
       Wire contract (202) is unchanged.
    5. **Hit branch** (user exists + not kill-switched): hand off to
       :func:`magic_link.request_link` with purpose
       ``recover_passkey``. The magic-link service mints the token,
       inserts the nonce row, and sends the mail via the
       :data:`recovery_new_link_template` template — which we pass
       as the caller-owned ``mailer_template`` parameter. ``subject_id``
       is pinned to the user's id so the verify step binds directly to
       the row without trusting the browser.
    6. **Miss branch** (no user): send the ``recovery_unknown``
       template to the typed address so a legitimate owner who
       typo'd sees "we couldn't find you" instead of a silent
       no-op.
    7. In the hit + miss branches (but NOT the kill-switch branch),
       write one ``audit.recovery.requested`` row with ``hit``
       discriminator — the spec AC requires audit to distinguish
       hit vs miss even though the caller's response is identical.
       The kill-switch branch has a dedicated audit action so
       operators see the refusal distinctly from a normal "no
       recovery happened yet" row.

    Returns ``None`` in every success path. The caller's UoW owns
    the transaction; this function does not commit.

    Note: the magic-link service's own ``recovery_new_link`` template
    selection lives inside :func:`_send_recovery_link`, not in
    :func:`magic_link.request_link`. We cannot use the generic magic-
    link template (it carries the wrong wording for recovery's
    destructive side-effects), so the recovery service handles the
    mint-then-send by hand rather than asking ``magic_link`` to
    proxy the send. This keeps the magic-link module's template
    dispatch single-purpose.
    """
    resolved_now = now if now is not None else _now(clock)
    pepper = _pepper(settings)
    email_lower = canonicalise_email(email)
    email_hash = hash_with_pepper(email_lower, pepper)
    ip_hash = hash_with_pepper(ip, pepper)

    # Rate-limit BEFORE we touch the DB or mailer — this is cheaper
    # and the right order to stop a burst from hammering the mail
    # relay. Propagates to the router as 429.
    throttle.check_recover_start(
        ip_hash=ip_hash, email_hash=email_hash, now=resolved_now
    )

    user = _lookup_user_by_email(session, email_lower=email_lower)
    hit = user is not None

    # Workspace kill-switch (§03 "Workspace kill-switch"): if any of
    # the user's non-archived grants lands in a workspace with
    # ``auth.self_service_recovery_enabled = false``, refuse the
    # flow. Caller still sees the same 202 as every other branch
    # (the enumeration guard also covers "known user, disabled by
    # workspace"); the forensic trail lives in
    # ``audit.recovery.disabled_by_workspace`` written on a FRESH
    # UoW. Primary UoW is not used for the audit because this branch
    # writes nothing else — committing the caller's UoW with just an
    # audit row and no nonce / session state would work, but the
    # fresh UoW mirrors the rate-limit refusal shape in the HTTP
    # router (``_audit_recovery_refusal``) and keeps "we took no
    # action" visible in a dedicated transaction even when the
    # caller's UoW later rolls back for an unrelated reason.
    if user is not None and is_self_service_recovery_disabled(session, user_id=user.id):
        _audit_recovery_disabled_by_workspace(
            user_id=user.id,
            email_hash=email_hash,
            ip_hash=ip_hash,
        )
        return

    # Enumeration guard (§15 "constant-time responses"): both branches
    # must produce an identical observable response regardless of
    # mailer outcome. A :class:`MailDeliveryError` here (SMTP down, DNS
    # fail, relay refusal) must not short-circuit the audit row or
    # surface as a 5xx — the caller always sees 202. The row minted by
    # the hit branch inside :func:`magic_link.request_link` still
    # commits so the link is usable once SMTP recovers (an operator
    # can re-render from the row or the user can re-request); the
    # miss branch has nothing to commit. Log loudly so operators notice
    # a relay outage even though the wire stays silent.
    if user is not None:
        # Hit branch — mint the magic link + send the recovery template.
        # The mint + nonce-insert reuses :func:`magic_link.request_link`;
        # the send is handled by the recovery module so we can use the
        # recovery-specific template (which calls out the destructive
        # side-effect). See :func:`_mint_and_send_recovery_link` for
        # the capturing-mailer seam.
        try:
            _mint_and_send_recovery_link(
                session,
                user=user,
                ip=ip,
                mailer=mailer,
                base_url=base_url,
                throttle=throttle,
                now=resolved_now,
                settings=settings,
                clock=clock,
            )
        except MailDeliveryError:
            _log.warning(
                "recovery mail send failed (hit branch); swallowing "
                "per §15 enumeration guard",
                exc_info=True,
            )
    else:
        # Miss branch — burn matching CPU so the HMAC-sign cost the hit
        # branch pays inside ``magic_link.request_link`` doesn't show
        # up as a timing channel, then send the no-account notice to
        # keep the mailer-send cost identical between the two branches.
        # The residual gap is the one DB INSERT the hit branch performs
        # on the nonce row (documented in :func:`_burn_cpu_for_miss_branch`
        # — writing a throwaway row to balance it is worse than the
        # sub-millisecond residual leak).
        _burn_cpu_for_miss_branch(pepper)
        try:
            _send_unknown_email(mailer=mailer, to_email=email)
        except MailDeliveryError:
            _log.warning(
                "recovery mail send failed (miss branch); swallowing "
                "per §15 enumeration guard",
                exc_info=True,
            )

    # Audit lands in the caller's UoW — committing or rolling back
    # with the rest of the request's state. The ``hit`` discriminator
    # is the forensic bit that tells operators whether the inbound
    # email matched a user without recording the plaintext address.
    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="user",
        entity_id=user.id if user is not None else "00000000000000000000000000",
        action="recovery.requested",
        diff={
            "hit": hit,
            "email_hash": email_hash,
            "ip_hash": ip_hash,
        },
        clock=clock,
    )


@dataclass
class _CapturingMailer:
    """No-op :class:`Mailer` double used to intercept the magic-link send.

    :func:`magic_link.request_link` mints the token, inserts the
    nonce row, then calls :meth:`Mailer.send` with a rendered
    generic magic-link body. We want the mint + nonce insert — they
    carry the single source of truth for token layout and rate-
    limiting — but we want to replace the send with the recovery-
    specific template. Feeding the magic-link service a capturing
    mailer (rather than adding a template-selection parameter on
    its public API) is the minimum-surface seam: the magic-link
    module stays single-purpose, and the token-layout knowledge
    stays in one file.

    The magic-link body carries the full magic URL
    (``{base_url}/auth/magic/{token}``) on its own line; we pull
    the trailing segment to recover the token and pass it to our
    own :func:`_send_recovery_link` below.
    """

    base_url: str
    captured_token: str | None = None

    def send(
        self,
        *,
        to: Sequence[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
        headers: Mapping[str, str] | None = None,
        reply_to: str | None = None,
    ) -> str:
        del to, subject, body_html, headers, reply_to
        prefix = self.base_url.rstrip("/")
        for line in body_text.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                # Magic-link URL layout: ``{base}/auth/magic/{token}``.
                self.captured_token = stripped.rsplit("/", 1)[-1]
                return "captured"
        # No URL found — magic-link's template always carries one;
        # treat this as a programming error. Better to fail loudly than
        # silently ship a no-link recovery mail.
        raise RuntimeError("recovery capture: magic-link body did not carry a URL")


def _mint_and_send_recovery_link(
    session: SqlaSession,
    *,
    user: User,
    ip: str,
    mailer: Mailer,
    base_url: str,
    throttle: Throttle,
    now: datetime,
    settings: Settings | None,
    clock: Clock | None,
) -> None:
    """Mint the recovery magic link + send the recovery-specific template.

    Reuses :func:`magic_link.request_link` for the token mint +
    nonce insert + ``audit.magic_link.sent`` audit row (one source
    of truth for token layout + single-use enforcement). Swaps the
    magic-link template for the recovery-specific template by
    capturing the generic send through :class:`_CapturingMailer` and
    re-sending with :func:`_send_recovery_link`.

    The magic-link service also advances its own rate-limit
    (:meth:`Throttle.check_request`); a trip there raises
    :class:`~app.auth._throttle.RateLimited`, which we let
    propagate unchanged. The recover-start limit bounds abuse of
    the recovery flow; magic-link's limit bounds abuse of the
    underlying mail-send primitive — both applying is correct
    layering (signup already does the same).
    """
    capture = _CapturingMailer(base_url=base_url)
    # cd-9i7z follow-up: we deliver synchronously here so the
    # capturing mailer fires and yields ``captured_token`` we then
    # re-frame with the recovery template. Lifting the deferred send
    # past the recovery flow's caller-side commit is tracked
    # separately; the bug fix on this branch is bounded to the
    # magic-link HTTP router that drove cd-t2jz, where the SMTP send
    # now waits for the UoW commit.
    pending = magic_link.request_link(
        session,
        email=user.email,
        purpose="recover_passkey",
        ip=ip,
        mailer=capture,
        base_url=base_url,
        now=now,
        ttl=_RECOVERY_SESSION_TTL,
        throttle=throttle,
        settings=settings,
        clock=clock,
        subject_id=user.id,
    )
    if pending is not None:
        pending.deliver()

    if capture.captured_token is None:
        # Defensive — :func:`_CapturingMailer.send` raises on an
        # empty capture, so this branch is unreachable in practice.
        # Kept as a belt-and-braces guard against a future refactor
        # that changes the mailer's send semantics.
        raise RuntimeError("recovery: magic-link service produced no token")

    # Magic-link's per-purpose TTL ceiling wins if it's tighter than
    # the recovery session TTL — the user sees the magic-link TTL in
    # the rendered "valid for N minutes" line because that's the one
    # that bounds clicking the link.
    effective_ttl = min(
        _RECOVERY_SESSION_TTL,
        magic_link._TTL_BY_PURPOSE["recover_passkey"],
    )

    _send_recovery_link(
        mailer=mailer,
        to_email=user.email,
        display_name=user.display_name,
        base_url=base_url,
        token=capture.captured_token,
        ttl=effective_ttl,
    )


# ---------------------------------------------------------------------------
# Public surface — verify
# ---------------------------------------------------------------------------


def verify_recovery(
    session: SqlaSession,
    *,
    token: str,
    ip: str,
    throttle: Throttle,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> RecoverySession:
    """Consume the recovery magic link; mint a recovery session.

    Delegates the unseal + nonce flip to
    :func:`magic_link.consume_link` with
    ``expected_purpose='recover_passkey'``. The magic-link service
    raises its own typed errors (:class:`~app.auth.magic_link.InvalidToken`
    / :class:`~app.auth.magic_link.TokenExpired` /
    :class:`~app.auth.magic_link.AlreadyConsumed` /
    :class:`~app.auth.magic_link.PurposeMismatch`); the HTTP router
    maps those to their existing symbols — we do not re-wrap.

    On success, inserts a :class:`_RecoverySessionRow` into the
    process-local store and returns a :class:`RecoverySession`
    handle. The handle permits **only** the passkey-finish step;
    it is not a web session and cannot be used to authenticate
    any other API route. That guarantee is structural: the store
    is a separate dict from
    :class:`~app.adapters.db.identity.models.Session`, and no other
    code path reads it.

    Writes one ``audit.recovery.verified`` row under the caller's
    UoW.
    """
    resolved_now = now if now is not None else _now(clock)

    outcome = magic_link.consume_link(
        session,
        token=token,
        expected_purpose="recover_passkey",
        ip=ip,
        now=resolved_now,
        throttle=throttle,
        settings=settings,
        clock=clock,
    )

    # The magic-link nonce's ``subject_id`` is the user.id we pinned
    # at request time. The user may have been deleted between request
    # and verify — surface that as :class:`RecoverySessionNotFound`
    # (same shape as a stale recovery session) so we don't leak the
    # deletion through a different error code.
    # justification: user is identity-scoped.
    with tenant_agnostic():
        user = session.get(User, outcome.subject_id)
    if user is None:
        raise RecoverySessionNotFound(outcome.subject_id)

    recovery_session_id = new_ulid(clock=clock)
    pepper = _pepper(settings)
    # Re-derive the consume-side IP hash so the row's ``ip_hash`` is
    # the device that clicked the link (not the device that requested
    # it). Useful forensic data for operators.
    ip_hash_at_verify = hash_with_pepper(ip, pepper)
    row = _RecoverySessionRow(
        recovery_session_id=recovery_session_id,
        user_id=user.id,
        email_hash=outcome.email_hash,
        ip_hash=ip_hash_at_verify,
        created_at=resolved_now,
        expires_at=resolved_now + _RECOVERY_SESSION_TTL,
    )
    _store_recovery_session(row)

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="user",
        entity_id=user.id,
        action="recovery.verified",
        diff={
            "email_hash": outcome.email_hash,
            "ip_hash_at_verify": ip_hash_at_verify,
        },
        clock=clock,
    )

    return RecoverySession(
        user_id=user.id,
        recovery_session_id=recovery_session_id,
        email_hash=outcome.email_hash,
        ip_hash=ip_hash_at_verify,
    )


# ---------------------------------------------------------------------------
# Public surface — complete
# ---------------------------------------------------------------------------


def complete_recovery(
    session: SqlaSession,
    *,
    recovery_session_id: str,
    challenge_id: str,
    credential: dict[str, Any],
    ip: str,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> CompletedRecovery:
    """Revoke old passkeys + sessions, register the new passkey; one tx.

    Spec §03 "Redemption" step 3 + §"Re-enrollment side-effects".
    Called from the HTTP router inside a fresh UoW; every write
    below lands or rolls back together.

    Steps (one transaction):

    1. Load the recovery session from the in-memory store; 404 if
       missing / expired.
    2. Revoke **every** :class:`PasskeyCredential` row for the
       user. Not "every except the one being registered" — recovery
       is the "start from scratch" door.
    3. Invalidate **every** :class:`~app.adapters.db.identity.models.Session`
       row for the user with cause ``"recovery_consumed"``. No
       "current session" exists (the recovery session is a separate
       dict, not a session row). Invalidation (cd-geqp) is
       non-destructive — the rows stay in the table with
       ``invalidated_at`` / ``invalidation_cause`` set so the
       forensic trail survives.
    4. Register the new passkey via
       :func:`passkey.register_finish`. The service inserts one
       credential row + audit, deletes the one-shot WebAuthn
       challenge.
    5. Evict the recovery session so a replay 404s.
    6. Write ``audit.recovery.completed``.

    Atomicity is load-bearing. If :func:`passkey.register_finish`
    raises (bad attestation, expired challenge, subject mismatch),
    the caller's UoW rolls back and the passkey / session revocations
    roll back with it — the user's pre-recovery state is restored
    intact rather than stranded mid-surgery.

    The recovery-session store is module-local; consumption
    (:func:`_consume_recovery_session`) happens **after** the DB
    writes flush but **before** the caller's UoW commits. A
    rollback *inside* this function (e.g. ``passkey.register_finish``
    raising before eviction) therefore leaves the recovery session
    redeemable again — tested by
    :class:`tests.unit.auth.test_recovery.TestCompleteRecoveryAtomicity`.
    A commit failure *after* this function returns (e.g. the caller's
    UoW tripping a unique constraint on an outer write) leaves the
    dict row evicted but the DB rolled back, so the next call 404s.
    Accepted as the pragmatic shape: the caller restarts the flow
    with a fresh magic link. cd-7huk tracks absorbing the store into
    the shared state layer, at which point wiring a SQLAlchemy
    ``after_commit`` hook becomes natural.
    """
    resolved_now = now if now is not None else _now(clock)

    row = _load_recovery_session(recovery_session_id, now=resolved_now)

    # Revoke every existing credential FIRST so a future spec change
    # that limits the total passkey count (cap + 1?) doesn't cause
    # the register_finish call below to trip :class:`TooManyPasskeys`.
    revoked_credential_count = _revoke_passkeys(session, user_id=row.user_id)
    # Sessions get **invalidated** (non-destructive) rather than
    # hard-deleted — cd-geqp / §15 wants the forensic trail preserved
    # on a security-sensitive event like "recovery redeemed". The
    # user is about to get a fresh session via the new passkey; any
    # prior session is refused by :func:`app.auth.session.validate`.
    revoked_session_count = _invalidate_sessions(
        session,
        user_id=row.user_id,
        now=resolved_now,
        clock=clock,
    )

    # Build an agnostic ctx for the passkey-register audit row — the
    # user may belong to any number of workspaces and we do not pick
    # one at recovery time. Matches :func:`_agnostic_audit_ctx`
    # semantics.
    ctx = _agnostic_audit_ctx()
    # Forward ``resolved_now`` so the callee's challenge-TTL comparison
    # uses the same instant this caller resolved (same class of time-
    # drift bug fixed in :func:`app.auth.signup.complete_signup`).
    credential_ref = passkey.register_finish(
        ctx,
        session,
        user_id=row.user_id,
        challenge_id=challenge_id,
        credential=credential,
        clock=clock,
        now=resolved_now,
    )

    # Audit FIRST under the caller's UoW, THEN evict the store.
    # Evicting before the audit row is queued would open a window
    # where a rollback after the evict leaves the recovery session
    # permanently lost — a partially-recoverable state we don't
    # want.
    pepper = _pepper(settings)
    ip_hash_at_completion = hash_with_pepper(ip, pepper)
    write_audit(
        session,
        ctx,
        entity_kind="user",
        entity_id=row.user_id,
        action="recovery.completed",
        diff={
            "email_hash": row.email_hash,
            "ip_hash_at_completion": ip_hash_at_completion,
            "revoked_credential_count": revoked_credential_count,
            "revoked_session_count": revoked_session_count,
            "new_credential_id": credential_ref.credential_id_b64url,
        },
        clock=clock,
    )

    _consume_recovery_session(recovery_session_id)

    return CompletedRecovery(
        user_id=row.user_id,
        new_credential_id=credential_ref.credential_id_b64url,
        revoked_credential_count=revoked_credential_count,
        revoked_session_count=revoked_session_count,
    )
