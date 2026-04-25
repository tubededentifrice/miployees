"""Self-service email-change domain service.

Three public entry points wire the spec's §03 "Self-service email
change" flow (cd-601a):

1. :func:`request_change` — passkey-session caller asks to swap their
   email. The service:

   * canonicalises + validates the new address;
   * rejects with :class:`EmailInUse` (case-insensitive) when another
     :class:`User` row already holds it;
   * rejects with :class:`RecentReenrollment` when the caller's
     newest :class:`PasskeyCredential` was registered less than 15
     minutes ago (§15 "Self-service lost-device & email-change abuse
     mitigations" — bounds the post-recovery hijack window);
   * mints an ``email_change_confirm`` magic link to the **new**
     address (15-min TTL, single-use);
   * persists an :class:`EmailChangePending` row with both plaintext
     addresses so the verify step can swap and the revert step
     (later) can restore;
   * emails an informational notice to the **old** address (no
     link — the old-address recipient cannot abort the swap from
     this notice, only from inaction or a manager-mediated reset);
   * writes ``audit.email.change_requested`` with hashed addresses +
     hashed IP only.

2. :func:`verify_change` — caller redeems the new-address magic link
   from a passkey session belonging to the same user. The service:

   * consumes the ``email_change_confirm`` token;
   * re-checks email uniqueness and swaps ``users.email`` atomically
     (the SQLAlchemy ``before_update`` listener keeps
     ``email_lower`` in sync);
   * mints an ``email_change_revert`` magic link to the **old**
     address (72-hour TTL) and stamps it onto the same
     :class:`EmailChangePending` row;
   * emails the new address (informational confirmation) and the
     old address (revert link);
   * writes ``audit.email.change_verified``.

3. :func:`revert_change` — caller redeems the old-address revert
   token (no session required — the token alone is the auth, per
   spec "the revert link is the only flow that consumes a magic
   link against the old address after the swap — it is not an
   authentication primitive"). The service:

   * consumes the ``email_change_revert`` token;
   * restores ``users.email`` to the snapshot stored on the
     :class:`EmailChangePending` row;
   * stamps ``reverted_at`` so the row terminates;
   * writes ``audit.email.change_reverted``.

**Atomicity.** Each entry point runs in the caller's
:class:`~sqlalchemy.orm.Session` UoW. The caller commits or rolls
back; the service never calls ``session.commit()``. The magic-link
single-use guarantee (cd-4zz) plus a row-level lookup of the
:class:`EmailChangePending` row keyed by ``request_jti`` /
``revert_jti`` provides the atomicity contract — a concurrent verify
or revert resolves through the conditional ``UPDATE`` on the nonce
row.

**PII minimisation (§15).** The service handles plaintext addresses
only at three seams: the inbound request body, the
:class:`EmailChangePending` row, and the outbound :class:`Mailer`
send. Audit diffs carry the SHA-256 + HKDF-pepper hashed forms only,
matching the magic-link / recovery / signup pattern.

**Mailer outage tolerance.** A :class:`MailDeliveryError` on the
notice-to-old send is swallowed-and-logged, mirroring the
enumeration-guard pattern in :mod:`app.auth.recovery` — the
domain-level row + audit still commit so an operator can re-render
or the user can retry. The new-address magic-link send goes through
:func:`app.auth.magic_link.request_link` which already handles its
own outage.

See ``docs/specs/03-auth-and-tokens.md`` §"Self-service email change",
``docs/specs/12-rest-api.md`` §"Auth", and
``docs/specs/15-security-privacy.md`` §"Self-service lost-device &
email-change abuse mitigations".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import (
    EmailChangePending,
    PasskeyCredential,
    User,
    canonicalise_email,
)
from app.adapters.mail.ports import MailDeliveryError, Mailer
from app.audit import write_audit
from app.auth import magic_link
from app.auth._hashing import hash_with_pepper
from app.auth._throttle import Throttle
from app.auth.keys import derive_subkey
from app.auth.magic_link import (
    AlreadyConsumed,
    InvalidToken,
    PendingDispatch,
    PurposeMismatch,
    TokenExpired,
)
from app.config import Settings, get_settings
from app.mail.templates import email_change_confirmed, email_change_notice
from app.mail.templates import email_change_revert as revert_template
from app.mail.templates import render as render_template
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "EmailChangeOutcome",
    "EmailInUse",
    "EmailRevertOutcome",
    "EmailVerifyOutcome",
    "InvalidEmail",
    "PendingNotFound",
    "RecentReenrollment",
    "SessionUserMismatch",
    "request_change",
    "revert_change",
    "verify_change",
]


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — spec-pinned
# ---------------------------------------------------------------------------


# §03 "Self-service email change" step 3 / §15 "Self-service lost-
# device & email-change abuse mitigations" "Recent re-enrollment
# cool-off". A passkey registered less than this window ago refuses
# the change_request — bounds the attacker window after a hijacked
# recovery magic link.
_REENROLLMENT_COOLOFF: Final[timedelta] = timedelta(minutes=15)


# Same HKDF subkey label as :mod:`app.auth.magic_link` so the audit
# rows this service writes hash IPs and emails under the same pepper
# as the sibling magic-link rows. A separate label would make abuse
# correlation joins double-hash without payoff.
_HKDF_PURPOSE: Final[str] = "magic-link"


# Synthetic tenant for audit emission — the email-change flow is
# bare-host (identity-scoped) and runs without any workspace context.
# Mirrors the sentinel used by :mod:`app.auth.magic_link`,
# :mod:`app.auth.recovery`, and :mod:`app.auth.session`.
_AGNOSTIC_WORKSPACE_ID: Final[str] = "00000000000000000000000000"
_AGNOSTIC_ACTOR_ID: Final[str] = "00000000000000000000000000"


# ---------------------------------------------------------------------------
# Value objects — outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmailChangeOutcome:
    """Result of :func:`request_change` on success."""

    pending_id: str
    request_jti: str
    new_email_hash: str
    old_email_hash: str


@dataclass(frozen=True, slots=True)
class EmailVerifyOutcome:
    """Result of :func:`verify_change` on success."""

    pending_id: str
    user_id: str
    revert_jti: str
    old_email_hash: str
    new_email_hash: str


@dataclass(frozen=True, slots=True)
class EmailRevertOutcome:
    """Result of :func:`revert_change` on success."""

    pending_id: str
    user_id: str
    old_email_hash: str
    new_email_hash: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidEmail(ValueError):
    """The submitted ``new_email`` failed syntactic validation.

    422-equivalent. Covers empty / whitespace-only / missing-``@`` /
    overlong addresses. Distinct from :class:`EmailInUse` so the
    router can map both to spec error codes.
    """


class EmailInUse(ValueError):
    """The submitted ``new_email`` already belongs to another :class:`User`.

    409-equivalent. Comparison is case-insensitive — we use
    :func:`canonicalise_email` for the lookup, matching the magic-
    link / signup convention.
    """


class RecentReenrollment(ValueError):
    """The caller's newest passkey is younger than the cool-off window.

    409-equivalent. Triggered when the user enrolled (or
    re-enrolled) a passkey within the past 15 minutes — bounds the
    post-recovery-hijack pivot window per §15.
    """


class PendingNotFound(ValueError):
    """No live :class:`EmailChangePending` row matches the consumed nonce.

    410-equivalent for the verify path; the magic-link service
    already burnt the jti by the time we reach this branch, so we
    cannot tell the caller "your row was swept" apart from "your
    token was tampered with" — both collapse here. The router maps
    this to ``410 expired``.
    """


class SessionUserMismatch(ValueError):
    """Verify came in from a session belonging to a different user.

    403-equivalent. Spec §03 step 2: "Requires an active passkey
    session for the same ``user_id``". An attacker who phished the
    new-address magic link cannot complete the swap from their own
    session.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or a system clock."""
    return (clock if clock is not None else SystemClock()).now()


def _pepper(settings: Settings | None) -> bytes:
    """Return the HKDF subkey used to pepper IP / email hashes for audit."""
    s = settings if settings is not None else get_settings()
    return derive_subkey(s.root_key, purpose=_HKDF_PURPOSE)


def _email_hash(email: str, pepper: bytes) -> str:
    return hash_with_pepper(canonicalise_email(email), pepper)


def _ip_hash(ip: str, pepper: bytes) -> str:
    return hash_with_pepper(ip, pepper)


def _agnostic_audit_ctx() -> WorkspaceContext:
    """Return the bare-host :class:`WorkspaceContext` used for audit emission."""
    return WorkspaceContext(
        workspace_id=_AGNOSTIC_WORKSPACE_ID,
        workspace_slug="",
        actor_id=_AGNOSTIC_ACTOR_ID,
        actor_kind="system",
        actor_grant_role="manager",  # unused for system actors
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _validate_email_syntax(raw: str) -> str:
    """Return the canonicalised ``new_email`` or raise :class:`InvalidEmail`.

    Cheap structural check only — the magic-link service rejects
    syntactically-fine but unreachable addresses at SMTP send time.
    """
    candidate = raw.strip()
    if len(candidate) < 3 or len(candidate) > 320:
        raise InvalidEmail(f"email length out of bounds: {len(candidate)}")
    if "@" not in candidate or candidate.startswith("@") or candidate.endswith("@"):
        raise InvalidEmail("email must contain a non-empty local + domain")
    # Reject obvious whitespace inside the address — :class:`Mailer`
    # implementations may silently drop them and the §03 "user-typed
    # at enrolment" promise stops holding.
    if any(ch.isspace() for ch in candidate):
        raise InvalidEmail("email cannot contain whitespace")
    return candidate


def _mask_email(email: str) -> str:
    """Return an obfuscated form of ``email`` for use in notice copy.

    Collapses the local part to one character + ``***`` (e.g.
    ``a***@example.com``) — enough signal for the rightful owner to
    correlate without exposing the full new address mid-attack.
    Mirrors the §03 "Owner-initiated worker passkey reset" convention.
    """
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if len(local) == 0:
        return f"***@{domain}"
    return f"{local[0]}***@{domain}"


def _ip_prefix(ip: str) -> str:
    """Return the §15 PII-minimised prefix of ``ip`` for the notice copy.

    IPv4 truncates to ``/24`` (drops the last octet); IPv6 truncates
    to ``/64`` (keeps the first 4 hextets). Anything that does not
    parse cleanly returns ``"unknown"`` — the notice still ships,
    just without geographic provenance, which is preferable to
    refusing to send.
    """
    if not ip:
        return "unknown"
    if "." in ip and ":" not in ip:
        # IPv4 — keep first three octets.
        parts = ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
        return "unknown"
    if ":" in ip:
        # IPv6 — keep first four hextets. Don't parse formally; a
        # best-effort split is good enough for a notice copy.
        hextets = ip.split(":")
        if len(hextets) >= 4:
            return ":".join(hextets[:4]) + "::/64"
        return "unknown"
    return "unknown"


def _user_has_recent_passkey(session: Session, *, user_id: str, now: datetime) -> bool:
    """Return ``True`` iff ``user_id`` has a passkey younger than the cool-off.

    The cool-off looks at the most-recently-created credential — the
    bound the spec wants is "last 15 minutes", not "any credential
    recently". A user who has held a passkey for years and added a
    new device an hour ago is not in the cool-off; the spec's intent
    is "an attacker who just hijacked the account via a compromised
    recovery link cannot pivot to a new mailbox". A re-enrolment
    within the past 15 minutes is exactly the signature of that
    flow.
    """
    cutoff = now - _REENROLLMENT_COOLOFF
    with tenant_agnostic():
        stmt = (
            select(PasskeyCredential.created_at)
            .where(PasskeyCredential.user_id == user_id)
            .order_by(PasskeyCredential.created_at.desc())
            .limit(1)
        )
        latest = session.scalar(stmt)
    if latest is None:
        return False
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=now.tzinfo)
    return latest > cutoff


def _email_taken_by_other(
    session: Session, *, new_email_lower: str, current_user_id: str
) -> bool:
    """Return ``True`` iff another :class:`User` already holds ``new_email_lower``."""
    with tenant_agnostic():
        existing = session.scalars(
            select(User).where(User.email_lower == new_email_lower)
        ).first()
    if existing is None:
        return False
    return existing.id != current_user_id


def _send_notice_to_old(
    *,
    mailer: Mailer,
    old_email: str,
    display_name: str,
    masked_new_email: str,
    ip_prefix: str,
    ttl_minutes: int,
) -> None:
    """Render + send the informational notice to the old address.

    Swallows :class:`MailDeliveryError` — the audit row + pending
    row commit either way so the swap is still completable when SMTP
    recovers. A 5xx surfacing here would shadow the typed domain
    error vocabulary the router relies on.
    """
    subject = render_template(email_change_notice.SUBJECT)
    body = render_template(
        email_change_notice.BODY_TEXT,
        display_name=display_name,
        masked_new_email=masked_new_email,
        ip_prefix=ip_prefix,
        ttl_minutes=str(ttl_minutes),
    )
    try:
        mailer.send(to=[old_email], subject=subject, body_text=body)
    except MailDeliveryError:
        _log.warning(
            "email-change notice send failed; swallowing per enumeration guard",
            exc_info=True,
        )


def _send_confirmation_to_new(
    *,
    mailer: Mailer,
    new_email: str,
    display_name: str,
    masked_old_email: str,
) -> None:
    """Render + send the post-swap confirmation to the new address.

    Same swallow-and-log policy as the notice send.
    """
    subject = render_template(email_change_confirmed.SUBJECT)
    body = render_template(
        email_change_confirmed.BODY_TEXT,
        display_name=display_name,
        masked_old_email=masked_old_email,
    )
    try:
        mailer.send(to=[new_email], subject=subject, body_text=body)
    except MailDeliveryError:
        _log.warning(
            "email-change confirmation send failed; swallowing",
            exc_info=True,
        )


def _send_revert_link_to_old(
    *,
    mailer: Mailer,
    old_email: str,
    display_name: str,
    masked_new_email: str,
    base_url: str,
    token: str,
    ttl: timedelta,
) -> None:
    """Render + send the 72-hour revert link to the old address.

    The URL lands on the spec's ``POST /auth/email/revert`` endpoint;
    we surface the bare token in the URL so a static landing page
    can post the body or a click-through can redeem directly.
    """
    url = f"{base_url.rstrip('/')}/auth/email/revert?token={token}"
    ttl_hours = max(1, int(ttl.total_seconds() // 3600))
    subject = render_template(revert_template.SUBJECT)
    body = render_template(
        revert_template.BODY_TEXT,
        display_name=display_name,
        masked_new_email=masked_new_email,
        url=url,
        ttl_hours=str(ttl_hours),
    )
    try:
        mailer.send(to=[old_email], subject=subject, body_text=body)
    except MailDeliveryError:
        _log.warning(
            "email-change revert-link send failed; swallowing",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Public surface — request_change
# ---------------------------------------------------------------------------


def request_change(
    session: Session,
    *,
    user: User,
    new_email: str,
    ip: str,
    mailer: Mailer,
    base_url: str,
    throttle: Throttle,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
    dispatch: PendingDispatch | None = None,
) -> EmailChangeOutcome:
    """Mint the magic link for ``new_email``; persist + notify; audit.

    Steps (matches §03):

    1. Validate + canonicalise ``new_email``.
    2. Reject :class:`EmailInUse` if another user holds it.
    3. Reject :class:`RecentReenrollment` if the caller's newest
       passkey is too young.
    4. Mint the ``email_change_confirm`` magic link via
       :func:`app.auth.magic_link.request_link` (the throttle, nonce
       insert, and SMTP send all happen there). The mailed body
       carries the canonical ``/auth/magic/<token>`` URL — landing
       on the SPA's ``/me/email/verify`` page is the SPA's job; the
       domain layer does not need to invent a parallel URL surface.
    5. Persist the :class:`EmailChangePending` row with both
       plaintext addresses so verify + revert can act later.
    6. Send the informational notice to the old address.
    7. Audit ``email.change_requested`` with hashed addresses + IP.

    Raises :class:`InvalidEmail`, :class:`EmailInUse`,
    :class:`RecentReenrollment`. Magic-link rate-limit /
    consume-lockout errors propagate verbatim from
    :func:`request_link` (already typed).

    The caller's UoW owns the transaction; this function never
    calls ``session.commit()``.

    **Outbox ordering (cd-9slq).** When ``dispatch`` is supplied
    every SMTP send (the magic-link template to the new address +
    the informational notice to the old one) is queued onto it for
    post-commit delivery, mirroring the cd-9i7z pattern. The
    calling HTTP router runs this function inside ``with make_uow()
    as session:`` and invokes :meth:`PendingDispatch.deliver` only
    after the ``with`` exits, so a commit failure short-circuits
    every queued send. When ``dispatch`` is ``None`` the function
    falls back to the legacy synchronous behaviour for tests /
    direct callers that own the commit boundary themselves;
    production wiring always supplies a :class:`PendingDispatch`.
    """
    resolved_now = now if now is not None else _now(clock)
    pepper = _pepper(settings)

    new_email_clean = _validate_email_syntax(new_email)
    new_email_lower = canonicalise_email(new_email_clean)

    if _email_taken_by_other(
        session, new_email_lower=new_email_lower, current_user_id=user.id
    ):
        raise EmailInUse(f"email {new_email_lower!r} already held by another user")

    if _user_has_recent_passkey(session, user_id=user.id, now=resolved_now):
        raise RecentReenrollment(
            f"user {user.id!r} re-enrolled a passkey inside the cool-off window"
        )

    # Mint the magic link. The magic-link service already handles
    # the rate-limit, nonce insert, and audit row — passing
    # ``subject_id=user.id`` short-circuits its own user-lookup
    # branch (which would otherwise key off ``new_email`` that has
    # no matching ``User`` row). The SMTP send is deferred via the
    # returned :class:`PendingMagicLink` and queued onto
    # ``dispatch`` below for post-commit delivery (cd-9slq).
    confirm_link = magic_link.request_link(
        session,
        email=new_email_clean,
        purpose="email_change_confirm",
        ip=ip,
        mailer=mailer,
        base_url=base_url,
        now=resolved_now,
        throttle=throttle,
        settings=settings,
        clock=clock,
        subject_id=user.id,
    )
    # ``request_link`` returns ``None`` only on the silent-miss
    # branch (no user row resolved by email). We bypassed that
    # branch via ``subject_id=user.id``, so a ``None`` here would be
    # a programming error. Asserting this rather than mapping it to
    # a typed error: the caller cannot recover, only report.
    if confirm_link is None:  # pragma: no cover - defensive
        raise RuntimeError("request_link returned None despite explicit subject_id")
    confirm_url = confirm_link.url

    # Recover the jti from the URL — the magic-link service does not
    # surface the freshly-minted nonce id directly. The URL layout
    # is ``{base}/auth/magic/<token>`` so the trailing path segment
    # is the signed token; we re-unseal it locally to extract the
    # jti for our own row. This mirrors the recovery service's
    # ``_CapturingMailer`` pattern (where the token is recovered
    # from a captured mail body) but is even cheaper because we
    # already own the URL.
    token = confirm_url.rsplit("/", 1)[-1]
    request_jti = magic_link.inspect_token_jti(token, settings=settings)

    pending_id = new_ulid(clock=clock)
    pending = EmailChangePending(
        id=pending_id,
        user_id=user.id,
        request_jti=request_jti,
        revert_jti=None,
        previous_email=user.email,
        previous_email_lower=user.email_lower,
        new_email=new_email_clean,
        new_email_lower=new_email_lower,
        created_at=resolved_now,
        verified_at=None,
        revert_expires_at=None,
        reverted_at=None,
    )
    with tenant_agnostic():
        session.add(pending)
        session.flush()

    # Capture the inputs the notice send needs at mint time so the
    # deferred entry is parameter-free.
    captured_notice_mailer = mailer
    captured_old_email = user.email
    captured_display_name = user.display_name
    captured_masked_new_email = _mask_email(new_email_clean)
    captured_ip_prefix = _ip_prefix(ip)

    def _deferred_notice_send() -> None:
        _send_notice_to_old(
            mailer=captured_notice_mailer,
            old_email=captured_old_email,
            display_name=captured_display_name,
            masked_new_email=captured_masked_new_email,
            ip_prefix=captured_ip_prefix,
            ttl_minutes=15,
        )

    if dispatch is not None:
        # Production path — the calling router commits then drains
        # the dispatch. Both the magic-link send and the old-address
        # notice fire only after the pending row + audit are durable.
        dispatch.add_pending(confirm_link)
        dispatch.add_callback(_deferred_notice_send)
    else:
        # Legacy fallback for tests / direct callers that own their
        # own commit boundary. Mailer outage policy (swallow + log)
        # lives in :func:`_send_notice_to_old` itself.
        confirm_link.deliver()
        _deferred_notice_send()

    new_hash = _email_hash(new_email_clean, pepper)
    old_hash = _email_hash(user.email, pepper)

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="user",
        entity_id=user.id,
        action="email.change_requested",
        diff={
            "user_id": user.id,
            "old_email_hash": old_hash,
            "new_email_hash": new_hash,
            "ip_hash": _ip_hash(ip, pepper),
            "request_jti": request_jti,
            "pending_id": pending_id,
        },
        clock=clock,
    )

    return EmailChangeOutcome(
        pending_id=pending_id,
        request_jti=request_jti,
        new_email_hash=new_hash,
        old_email_hash=old_hash,
    )


# ---------------------------------------------------------------------------
# Public surface — verify_change
# ---------------------------------------------------------------------------


def verify_change(
    session: Session,
    *,
    token: str,
    session_user_id: str,
    ip: str,
    mailer: Mailer,
    base_url: str,
    throttle: Throttle,
    now: datetime | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
    dispatch: PendingDispatch | None = None,
) -> EmailVerifyOutcome:
    """Consume the new-address magic link; swap ``users.email``; mint revert.

    Steps (matches §03 "Confirmation"):

    1. **Peek** the ``email_change_confirm`` magic link — validate
       signature, expiry, purpose, single-use availability without
       burning the nonce. This lets the session check fire before we
       commit to consuming, so an attacker who phished the link but
       holds a session bound to a different user cannot DoS the swap
       by burning the token from their own session.
    2. Look up the :class:`EmailChangePending` row by ``request_jti``.
    3. Reject :class:`SessionUserMismatch` if the caller's session
       user differs from the pending row's user_id (and from the
       nonce's subject_id — both must agree).
    4. **Now** consume the magic link — the session matched, so we
       have the right caller to burn the nonce against. The
       magic-link service writes its own ``audit.magic_link.consumed``
       row.
    5. Re-check uniqueness (a sibling change_request flow may have
       claimed the address mid-window) and swap ``users.email``.
       The ``before_update`` ORM listener keeps ``email_lower`` in
       sync.
    6. Mint the ``email_change_revert`` magic link (72-hour TTL)
       to the **old** address; stamp ``revert_jti`` +
       ``revert_expires_at`` + ``verified_at`` on the pending row.
    7. Send the confirmation to the new address; the revert link
       to the old address.
    8. Audit ``email.change_verified``.

    Raises :class:`InvalidToken`, :class:`PurposeMismatch`,
    :class:`TokenExpired`, :class:`AlreadyConsumed`,
    :class:`SessionUserMismatch`, :class:`EmailInUse`,
    :class:`PendingNotFound`.

    **Outbox ordering (cd-9slq).** When ``dispatch`` is supplied the
    confirmation-to-new and revert-link-to-old SMTP sends are queued
    onto it for post-commit delivery. The calling HTTP router runs
    this function inside ``with make_uow() as session:`` and invokes
    :meth:`PendingDispatch.deliver` only after the ``with`` exits,
    so a commit failure short-circuits both sends — no working
    revert token reaches the old mailbox without the matching
    revert nonce + verified-pending row durable on disk. When
    ``dispatch`` is ``None`` the function falls back to the legacy
    synchronous send for tests / direct callers that own the commit
    boundary themselves.
    """
    resolved_now = now if now is not None else _now(clock)
    pepper = _pepper(settings)

    # Peek the link first (signature + expiry + purpose + single-use
    # availability) without burning the nonce. The session check has
    # to fire before we burn — otherwise an attacker holding a session
    # for a different user can DoS the legit user's swap by submitting
    # the phished confirm link from their own session, burning the
    # nonce, and forcing the legit user to start over.
    peek_outcome = magic_link.peek_link(
        session,
        token=token,
        expected_purpose="email_change_confirm",
        ip=ip,
        now=resolved_now,
        throttle=throttle,
        settings=settings,
        clock=clock,
    )

    request_jti = magic_link.inspect_token_jti(token, settings=settings)

    with tenant_agnostic():
        pending = session.scalars(
            select(EmailChangePending).where(
                EmailChangePending.request_jti == request_jti
            )
        ).first()
    if pending is None or pending.verified_at is not None:
        # Either the pending row was hard-deleted out from under us
        # (a sweeper, an admin clean-up) or a sibling verify already
        # flipped ``verified_at``. Both collapse to 410-equivalent.
        raise PendingNotFound(f"no pending email change for jti {request_jti!r}")

    if peek_outcome.subject_id != pending.user_id:  # pragma: no cover - defensive
        # The nonce row's subject_id must match the pending user.
        # A divergence means somebody hand-edited one of the two
        # rows; refuse loudly rather than swap email under a
        # mis-bound subject.
        raise InvalidToken("nonce subject_id does not match pending row user_id")

    if pending.user_id != session_user_id:
        raise SessionUserMismatch(
            f"verify session user {session_user_id!r} != "
            f"pending user {pending.user_id!r}"
        )

    # Session matched — now burn the nonce. The conditional UPDATE
    # in :func:`consume_link` is the single-use chokepoint; if a
    # concurrent same-user verify won the race between peek and
    # consume, we'll surface :class:`AlreadyConsumed` and the caller
    # gets a 409 (which is honest — the swap landed once already).
    magic_link.consume_link(
        session,
        token=token,
        expected_purpose="email_change_confirm",
        ip=ip,
        now=resolved_now,
        throttle=throttle,
        settings=settings,
        clock=clock,
    )

    # Re-check uniqueness inside the same UoW as the swap. A sibling
    # user who just claimed the address would otherwise win silently.
    if _email_taken_by_other(
        session,
        new_email_lower=pending.new_email_lower,
        current_user_id=pending.user_id,
    ):
        raise EmailInUse(
            f"email {pending.new_email_lower!r} already held by another user"
        )

    with tenant_agnostic():
        user = session.get(User, pending.user_id)
    if user is None:
        # The user row vanished between request and verify — the
        # CASCADE on ``email_change_pending.user_id`` should have
        # taken this row with it. Defensive branch.
        raise PendingNotFound(f"user {pending.user_id!r} no longer exists")

    # Snapshot the old email for the revert mail (the listener
    # rewrites ``email_lower`` after assignment, but ``previous_email``
    # is the source-of-truth for the revert path so we trust the row).
    old_email = pending.previous_email
    new_email = pending.new_email

    # Atomic swap. The :func:`_user_before_update` ORM listener
    # rewrites ``email_lower`` from ``email`` so we don't have to
    # do it manually.
    user.email = new_email

    # Mint the revert magic link (72-hour TTL). Same throttle bucket
    # as the confirm flow — abuse via repeated revert attempts
    # against a hijacked old mailbox cannot bypass the lockout.
    revert_link = magic_link.request_link(
        session,
        email=old_email,
        purpose="email_change_revert",
        ip=ip,
        # The magic-link service requires a non-None mailer when
        # ``send_email=True`` is the default; we want to send our
        # own template (the generic magic-link copy talks about
        # "completing your action", which is wrong for a revert
        # notice). Pass ``send_email=False`` and a placeholder
        # mailer; we render + send ourselves below via
        # :func:`_send_revert_link_to_old`.
        mailer=None,
        base_url=base_url,
        now=resolved_now,
        throttle=throttle,
        settings=settings,
        clock=clock,
        subject_id=user.id,
        send_email=False,
    )
    if revert_link is None:  # pragma: no cover - defensive
        raise RuntimeError("revert request_link returned None")
    # ``send_email=False`` so ``deliver()`` is a no-op — kept
    # explicit to mirror the deferred-send protocol the cd-9i7z
    # outbox fix introduced; future revert-template work can lift
    # the actual send through the same seam.
    revert_link.deliver()
    revert_url = revert_link.url
    revert_token = revert_url.rsplit("/", 1)[-1]
    revert_jti = magic_link.inspect_token_jti(revert_token, settings=settings)
    revert_expires_at = resolved_now + timedelta(hours=72)

    pending.revert_jti = revert_jti
    pending.revert_expires_at = revert_expires_at
    pending.verified_at = resolved_now
    with tenant_agnostic():
        session.flush()

    # Capture every input the deferred sends need at mint time so
    # the dispatch entries are parameter-free closures. Each send
    # is independent: a relay miss on the confirmation MUST NOT
    # abort the revert-link send (and vice versa), matching the
    # prior swallow-and-log behaviour now lifted post-commit.
    captured_verify_mailer = mailer
    captured_new_email = new_email
    captured_old_email = old_email
    captured_display_name = user.display_name
    captured_masked_old_email = _mask_email(old_email)
    captured_masked_new_email = _mask_email(new_email)
    captured_base_url = base_url
    captured_revert_token = revert_token

    def _deferred_confirmation_send() -> None:
        _send_confirmation_to_new(
            mailer=captured_verify_mailer,
            new_email=captured_new_email,
            display_name=captured_display_name,
            masked_old_email=captured_masked_old_email,
        )

    def _deferred_revert_send() -> None:
        _send_revert_link_to_old(
            mailer=captured_verify_mailer,
            old_email=captured_old_email,
            display_name=captured_display_name,
            masked_new_email=captured_masked_new_email,
            base_url=captured_base_url,
            token=captured_revert_token,
            ttl=timedelta(hours=72),
        )

    if dispatch is not None:
        # Production path — calling router commits then drains the
        # dispatch. Both sends fire only after the swap +
        # ``email_change_pending`` row (with stamped revert_jti) +
        # audit are durable on disk (cd-9slq).
        dispatch.add_callback(_deferred_confirmation_send)
        dispatch.add_callback(_deferred_revert_send)
    else:
        # Legacy fallback for tests / direct callers that own their
        # own commit boundary.
        _deferred_confirmation_send()
        _deferred_revert_send()

    old_hash = _email_hash(old_email, pepper)
    new_hash = _email_hash(new_email, pepper)

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="user",
        entity_id=user.id,
        action="email.change_verified",
        diff={
            "user_id": user.id,
            "old_email_hash": old_hash,
            "new_email_hash": new_hash,
            "ip_hash": _ip_hash(ip, pepper),
            "request_jti": request_jti,
            "revert_jti": revert_jti,
            "pending_id": pending.id,
        },
        clock=clock,
    )

    return EmailVerifyOutcome(
        pending_id=pending.id,
        user_id=user.id,
        revert_jti=revert_jti,
        old_email_hash=old_hash,
        new_email_hash=new_hash,
    )


# ---------------------------------------------------------------------------
# Public surface — revert_change
# ---------------------------------------------------------------------------


def revert_change(
    session: Session,
    *,
    token: str,
    ip: str,
    now: datetime | None = None,
    throttle: Throttle,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> EmailRevertOutcome:
    """Consume the old-address revert link; restore ``users.email``.

    Steps (matches §03 "Revert window"):

    1. Consume the ``email_change_revert`` magic link.
    2. Look up the :class:`EmailChangePending` row by ``revert_jti``.
    3. Restore ``users.email`` to the snapshot
       (``pending.previous_email``).
    4. Stamp ``reverted_at`` so the row terminates.
    5. Audit ``email.change_reverted``.

    No session is required — the spec pins this as a non-auth
    primitive consumed against the **old** address by virtue of
    the token's mailbox-controlled delivery.

    Raises :class:`InvalidToken`, :class:`PurposeMismatch`,
    :class:`TokenExpired`, :class:`AlreadyConsumed`,
    :class:`PendingNotFound`.
    """
    resolved_now = now if now is not None else _now(clock)
    pepper = _pepper(settings)

    outcome = magic_link.consume_link(
        session,
        token=token,
        expected_purpose="email_change_revert",
        ip=ip,
        now=resolved_now,
        throttle=throttle,
        settings=settings,
        clock=clock,
    )

    revert_jti = magic_link.inspect_token_jti(token, settings=settings)

    with tenant_agnostic():
        pending = session.scalars(
            select(EmailChangePending).where(
                EmailChangePending.revert_jti == revert_jti
            )
        ).first()
    if (
        pending is None
        or pending.reverted_at is not None
        or pending.verified_at is None
    ):
        # Three flavours of "no live revert": row already reverted,
        # row never advanced past request (verified_at IS NULL means
        # the revert_jti shouldn't have been minted), row swept.
        # Collapse to a single error symbol — same privacy posture
        # as the magic-link rejected branch.
        raise PendingNotFound(f"no live revert pending for jti {revert_jti!r}")

    if outcome.subject_id != pending.user_id:  # pragma: no cover - defensive
        raise InvalidToken("nonce subject_id does not match pending row user_id")

    with tenant_agnostic():
        user = session.get(User, pending.user_id)
    if user is None:
        raise PendingNotFound(f"user {pending.user_id!r} no longer exists")

    # Capture the address we are leaving (the post-swap value) for
    # the audit row — we lose it the moment we assign the swap-back.
    new_email = user.email
    old_email = pending.previous_email
    user.email = old_email

    pending.reverted_at = resolved_now
    with tenant_agnostic():
        session.flush()

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="user",
        entity_id=user.id,
        action="email.change_reverted",
        diff={
            "user_id": user.id,
            "old_email_hash": _email_hash(old_email, pepper),
            "new_email_hash": _email_hash(new_email, pepper),
            "ip_hash": _ip_hash(ip, pepper),
            "revert_jti": revert_jti,
            "pending_id": pending.id,
        },
        clock=clock,
    )

    return EmailRevertOutcome(
        pending_id=pending.id,
        user_id=user.id,
        old_email_hash=_email_hash(old_email, pepper),
        new_email_hash=_email_hash(new_email, pepper),
    )


# Re-export the magic-link errors the router needs to import
# alongside the email-change ones, so the wiring layer doesn't have
# to dual-import.
__all__ += [
    "AlreadyConsumed",
    "InvalidToken",
    "PurposeMismatch",
    "TokenExpired",
]
