"""Magic-link domain service — mint, send, consume.

Two public entry points:

* :func:`request_link` — rate-limit check, token mint, nonce-row
  insert, email send. Always returns ``None`` to uphold the
  enumeration guard: the caller cannot distinguish whether the email
  existed, only whether the rate-limit tripped.
* :func:`consume_link` — unseal + row-check + single-use flip. Returns
  a :class:`MagicLinkOutcome` on success; raises a typed error
  otherwise.

Token format (§03 "Magic link format"):

```
{
  "purpose": "signup_verify | recover_passkey | email_change_confirm |
              email_change_revert | grant_invite",
  "subject_id": "<ULID>",
  "jti":        "<ULID>",
  "exp":        <unix_timestamp>,
}
```

Signed with an HKDF subkey derived from ``settings.root_key`` via
:func:`app.auth.keys.derive_subkey` and the fixed purpose label
``"magic-link"``. The subkey doesn't rotate with the link's
``purpose`` field — we want a single signing key across every purpose
so ``purpose`` stays inside the signed payload where it belongs, not
smuggled into the key schedule.

**TTL ceilings (§03).** Signup verification gets 15 minutes; every
other purpose gets 10. The caller may request a shorter TTL (future
"link this device now" flow), but never a longer one — the server
caps at the spec.

**Single-use under concurrency.** The consume step runs one
conditional ``UPDATE``:

```
UPDATE magic_link_nonce
   SET consumed_at = :now
 WHERE jti = :jti AND consumed_at IS NULL
```

SQLite serialises the writing transaction; Postgres takes a
row-level lock (``FOR UPDATE`` happens implicitly for the filtered
``UPDATE`` under READ COMMITTED). Exactly one concurrent consumer
sees ``rowcount == 1``; the loser sees ``0`` and the service raises
:class:`AlreadyConsumed`.

**PII minimisation (§15).** No plaintext email or IP is ever logged,
persisted, or passed to audit. We store SHA-256 hashes salted with a
per-deployment HKDF subkey; audit rows carry the same hashes. The
plaintext email is handed to the :class:`Mailer` port and never
retained past that call.

**Rate limits (§15).** Enforced by :mod:`app.auth._throttle`. A
single :class:`Throttle` instance is shared per-process and passed
into both entry points so tests can use a fresh one per case.
cd-7huk will absorb this into the deployment-wide throttle; until
then the in-memory instance is the canonical store.

**Audit.** Every ``request`` writes ``audit.magic_link.sent``; every
``consume`` writes ``audit.magic_link.consumed`` on success or
``audit.magic_link.rejected`` on failure. Both use a synthetic
tenant-agnostic :class:`WorkspaceContext` because the magic-link
flow runs at the bare host (no slug, no tenant — see
:func:`_agnostic_audit_ctx`).

The rejected row must land even when the caller's primary UoW rolls
back — :func:`consume_link` itself can't write it, because the typed
domain exception rolls back the caller's UoW (and with it any audit
row the service had queued on the same session). The router opens
a **fresh** UoW via :func:`app.adapters.db.session.make_uow` and
calls :func:`write_rejected_audit` there, committing the rejected
audit independently of whatever state the primary UoW left behind.
Forensic value: pre-signup magic-link abuse (signature forgeries,
cross-purpose replays, brute-force consumes) has no other trail —
the nonce row either never existed or stays pending under rollback.

**Outbox ordering for ``request`` (cd-9i7z).** :func:`request_link`
mints the token and queues the nonce + ``audit.magic_link.sent``
rows on the caller's session, then returns a :class:`PendingMagicLink`
whose :meth:`PendingMagicLink.deliver` runs the SMTP send. The
caller (today, the magic-link HTTP router) is responsible for
committing the UoW *before* invoking :meth:`deliver` — that
ordering is the outbox boundary the bug fix relies on.

The original failure mode this closes: the SMTP send used to run
*inside* :func:`request_link`, so it fired before the caller's UoW
commit at HTTP-handler exit. A commit-time failure (schema drift on
``audit_log`` was the cd-t2jz repro, but FK violations or any
sibling-write rollback work the same way) then rolled back the
nonce *after* the mailer had already shipped a working token —
fail-open, not the §15 enumeration-guard intent. With the deferred
:meth:`deliver` the nonce is durable on disk before the mailer is
touched: an SMTP failure is then a no-leak event (caught by the
§15 swallow inside :meth:`deliver`), and a commit failure
short-circuits the send entirely (the caller never reaches
:meth:`deliver`).

We do not use a *separate* UoW for the nonce write — SQLite
serialises writers via a database-wide lock, so opening a sibling
session from inside the caller's open write transaction would
deadlock against itself (the sibling's INSERT would wait for the
caller's lock; the caller is mid-call to :func:`request_link`). The
deferred-send shape sidesteps that: the caller's UoW commits via
its existing path (the FastAPI ``db_session`` dep at handler exit),
and only *then* does the router fire :meth:`deliver`.

**Outbox ordering for non-router callers (cd-9slq).** The other
flows that mint magic links — :func:`app.auth.signup.start_signup`,
:func:`app.auth.recovery.request_recovery`,
:func:`app.domain.identity.email_change.request_change` /
:func:`~app.domain.identity.email_change.verify_change`,
:func:`app.domain.identity.membership.invite`, and the manager-
mediated reissue in :mod:`app.api.v1.users` — return a
:class:`PendingDispatch` (the outbox queue for one or more
deferred sends) instead of calling :meth:`PendingMagicLink.deliver`
themselves. Each calling HTTP router runs the domain call inside an
explicit ``with make_uow() as session:`` block (replacing the
shared ``db_session`` FastAPI dep) and invokes
:meth:`PendingDispatch.deliver` only after the ``with`` exits
cleanly — a commit failure short-circuits every queued send. The
single shared invariant: nobody fires SMTP for a magic-link token
before the UoW that holds the matching nonce row has committed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeTimedSerializer,
)
from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from app.adapters.db.identity.models import (
    MagicLinkNonce,
    User,
    canonicalise_email,
)
from app.adapters.mail.ports import MailDeliveryError, Mailer
from app.audit import write_audit
from app.auth._hashing import hash_with_pepper
from app.auth._throttle import ConsumeLockout, RateLimited, Throttle
from app.auth.keys import derive_subkey
from app.config import Settings, get_settings
from app.mail.templates import magic_link as magic_link_template
from app.mail.templates import render as render_template
from app.tenancy import WorkspaceContext, tenant_agnostic
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "AlreadyConsumed",
    "ConsumeLockout",
    "InvalidToken",
    "MagicLinkOutcome",
    "MagicLinkPurpose",
    "PendingDispatch",
    "PendingMagicLink",
    "PurposeMismatch",
    "RateLimited",
    "Throttle",
    "TokenExpired",
    "consume_link",
    "inspect_token_jti",
    "peek_link",
    "reason_for_exception",
    "request_link",
    "write_rejected_audit",
]


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — spec-pinned
# ---------------------------------------------------------------------------


MagicLinkPurpose = Literal[
    "signup_verify",
    "recover_passkey",
    "email_change_confirm",
    "email_change_revert",
    "grant_invite",
]


_VALID_PURPOSES: Final[frozenset[str]] = frozenset(
    {
        "signup_verify",
        "recover_passkey",
        "email_change_confirm",
        "email_change_revert",
        "grant_invite",
    }
)

# Per-purpose TTL ceiling (§03). Requests for a shorter TTL are
# respected; longer requests silently cap at the value below. The
# ``grant_invite`` ceiling is 24 hours per §03 "Additional users
# (invite → click-to-accept)" — invitees often discover the email
# the morning after, and a tighter cap would force managers to
# re-send constantly. The ``email_change_revert`` ceiling is 72
# hours per §03 "Self-service email change" "Revert window" — the
# old-mailbox notice carries a revert link with that TTL so the
# rightful owner has the weekend to spot a hijack and roll back.
_TTL_BY_PURPOSE: Final[dict[str, timedelta]] = {
    "signup_verify": timedelta(minutes=15),
    "recover_passkey": timedelta(minutes=10),
    "email_change_confirm": timedelta(minutes=15),
    "email_change_revert": timedelta(hours=72),
    "grant_invite": timedelta(hours=24),
}

# itsdangerous salt. Changing the value invalidates every link in
# flight and is a breaking change; treat it like a schema migration.
# Purpose is in the payload, not in the salt, so one signing key
# covers every purpose (and a stolen token can't be re-signed under
# a different purpose).
_SERIALIZER_SALT: Final[str] = "magic-link-v1"

# HKDF purpose used to derive the signing subkey. A future rotation
# bumps this to ``"magic-link-v2"`` and keeps the v1 signer around
# during the grace window so in-flight tokens keep verifying.
_HKDF_PURPOSE: Final[str] = "magic-link"

# Synthetic tenant for audit emission. The tenant-agnostic flows
# (signup, recovery) have no :class:`WorkspaceContext` to borrow, but
# :func:`app.audit.write_audit` requires one. We build a sentinel
# value matching the actor-free system voice — the audit reader
# recognises the zero-ULID workspace as "pre-tenant identity event"
# and displays it accordingly (cd-dir, future). Until that reader
# lands, the audit row still pins actor / ip-hash / email-hash
# through ``diff``, which is the forensic data operators need.
_AGNOSTIC_WORKSPACE_ID: Final[str] = "00000000000000000000000000"
_AGNOSTIC_ACTOR_ID: Final[str] = "00000000000000000000000000"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MagicLinkOutcome:
    """Result of :func:`consume_link` on success.

    ``purpose`` + ``subject_id`` are the fields the calling flow
    (signup / recover / email-change / invite-accept) needs to
    continue: they identify *why* the link was minted and *against
    whom*. ``email_hash`` and ``ip_hash`` are exposed so the caller
    can log or audit without re-deriving the hashes.
    """

    purpose: str
    subject_id: str
    email_hash: str
    ip_hash: str


@dataclass(eq=False, slots=True)
class PendingMagicLink:
    """Result of :func:`request_link`: signed URL + deferred SMTP send.

    The :func:`request_link` call writes the nonce + audit rows on
    the caller's session but does **not** commit and does **not**
    contact the mailer. The caller's outer UoW is responsible for
    committing those rows; once that commit succeeds, the caller
    invokes :meth:`deliver` to fire the SMTP send. This separation
    is the cd-9i7z outbox boundary: the email never goes out until
    the matching nonce row is durable on disk.

    Fields:

    * ``url`` — the signed acceptance URL
      (``{base_url}/auth/magic/{token}``). Always populated when
      :func:`request_link` returns a :class:`PendingMagicLink`
      (the ``None`` short-circuit covers the enumeration-guard miss
      branch and never produces a pending object).
    * ``_send_callback`` — internal closure that performs the SMTP
      send. ``None`` when :func:`request_link` was called with
      ``send_email=False`` (the invite flow uses this — it grabs the
      URL and renders its own template). Callers should treat this
      field as opaque and go through :meth:`deliver`.

    Why a callback instead of returning the raw mailer + body? The
    rendering (subject + body, TTL minute math, purpose-label lookup)
    must use the same inputs the in-line send used to use, and we
    don't want to scatter that knowledge across every router. The
    closure captures all of it at mint time so the router only has
    to call one method after commit.

    **PII safety on repr (§15).** The default dataclass ``__repr__``
    would render the full ``url`` field, and the URL contains the
    signed magic-link token. Any traceback that captures a local
    ``pending`` variable, any defensive ``print(pending)``, or any
    ``logger.info("...", extra={"obj": pending})`` would then leak the
    token to logs — the same forensic surface §15 explicitly forbids
    for plaintext email and IP. We override :meth:`__repr__` to mask
    the token, and disable ``eq=True`` so the default
    ``__eq__`` / ``__hash__`` don't pull the token into hash keys
    (per-instance identity is what the callers actually need anyway).

    **Single-fire on :meth:`deliver` (idempotency).** :meth:`deliver`
    clears the callback after a successful invocation so a buggy
    retry path can't fire the SMTP send twice — the second call is
    a silent no-op. Without the guard, a defensive
    ``try: pending.deliver() … finally: pending.deliver()`` shape
    in a future caller would result in two emails to the user. Use
    ``slots=True`` + ``eq=False`` to keep mutation cheap (no
    ``object.__setattr__`` dance) without paying for full equality
    semantics we don't use.
    """

    url: str
    _send_callback: Callable[[], None] | None = field(default=None)

    def __repr__(self) -> str:
        """Mask the token so a stray log line can't leak it.

        URL layout is ``{base}/auth/magic/{token}``. We render the
        base path verbatim and the token as ``<redacted>`` so debug
        output stays useful (you can still see which deployment +
        purpose minted it) without spilling the secret. Mirrors the
        §15 redaction posture used for ``ip`` / ``email`` strings on
        sibling structs.
        """
        if "/auth/magic/" in self.url:
            base, _, _token = self.url.rpartition("/")
            url_repr = f"{base}/<redacted>"
        else:  # pragma: no cover - defensive; mint always uses /auth/magic/
            url_repr = "<redacted>"
        callback_repr = "<set>" if self._send_callback is not None else None
        return f"PendingMagicLink(url={url_repr!r}, _send_callback={callback_repr})"

    def deliver(self) -> None:
        """Fire the deferred SMTP send. Idempotent.

        MUST be called *after* the caller's UoW commits. If commit
        failed, the caller never reaches here and no email is sent
        — that's the cd-9i7z fail-closed invariant.

        Idempotent across repeat invocations: the callback is cleared
        after a successful send so a buggy retry path can't fire the
        SMTP send twice. A :class:`MailDeliveryError` (caught below)
        also clears the callback — the relay outage is recorded in
        the audit trail by the prior :func:`request_link` write, and
        a sibling caller-driven retry would just be a second futile
        SMTP attempt without a fresh nonce. The route to deliver
        again is to call :func:`request_link` afresh, which mints a
        new nonce + audit row.

        :class:`MailDeliveryError` is swallowed and logged at
        WARNING here (§15 enumeration guard — a mailer outage must
        not turn into an observable 5xx that leaks hit vs miss on
        the recovery / email-change paths). The nonce + audit are
        already committed by the caller, so the link is redeemable
        once SMTP recovers and forensic data is preserved.
        """
        callback = self._send_callback
        if callback is None:
            return
        # Clear before the send so the no-double-fire guard holds even
        # if the callback raises a non-MailDeliveryError exception
        # that propagates past us — the caller's retry loop would
        # otherwise re-enter ``deliver`` with the closure still wired.
        self._send_callback = None
        try:
            callback()
        except MailDeliveryError:
            _log.warning(
                "magic-link mail send failed; swallowing per §15 enumeration guard",
                exc_info=True,
            )


@dataclass(eq=False, slots=True)
class PendingDispatch:
    """Outbox collector for one or more deferred SMTP sends (cd-9slq).

    The non-router callers of :func:`request_link` (signup, recovery,
    invite, email-change, manager-mediated reissue) typically need to
    send more than one email per request — the magic-link send plus
    a flow-specific template (recovery notice, invite copy, revert
    link). They also stack on top of the magic-link service's own
    :class:`PendingMagicLink`. This dataclass is the seam those
    callers return to their HTTP router so the router can sequence
    ``with UoW: domain_call() → commit → dispatch.deliver()``,
    matching the cd-9i7z fix at the magic-link router.

    The router calls :meth:`deliver` once after the UoW commits; this
    fires every queued send in registration order. Each individual
    send is wrapped so a :class:`MailDeliveryError` on one entry is
    logged and swallowed (§15 enumeration guard) without aborting the
    rest — a recovery flow must still ship the unknown-email notice
    even when the magic-link mailer happens to fail at the same
    moment, and an invite must still ship its flavoured template even
    if the magic-link side hiccupped.

    **Append semantics.** :meth:`add_callback` and :meth:`add_pending`
    extend the queue. The order is preserved so callers that need a
    specific sequencing (magic-link first, sibling notice second)
    just register in that order. :meth:`add_pending` stores the
    :class:`PendingMagicLink` whole — :meth:`deliver` invokes its
    own :meth:`PendingMagicLink.deliver` so its idempotency / repr /
    swallow guarantees apply uniformly.

    **Single-fire on :meth:`deliver`.** Each entry is consumed at most
    once: the dispatch clears its queue after iterating, so a buggy
    retry path that calls ``dispatch.deliver()`` twice doesn't ship
    duplicate emails. A second call is a silent no-op.
    """

    _entries: list[Callable[[], None]] = field(default_factory=list)

    def add_callback(self, callback: Callable[[], None]) -> None:
        """Register one parameter-free deferred send.

        Use this for sibling templates the domain caller renders by
        hand (recovery notice, invite copy, revert link). Wrap any
        captured state at the call site so this dispatch sees a
        zero-arg closure.
        """
        self._entries.append(callback)

    def add_pending(self, pending: PendingMagicLink | None) -> None:
        """Register a :class:`PendingMagicLink` for post-commit delivery.

        ``None`` is a no-op so callers can pipe the
        :func:`request_link` return value directly without a guard
        (the enumeration-guard short-circuit returns ``None``).
        :meth:`deliver` invokes the pending's own
        :meth:`PendingMagicLink.deliver` so the idempotency + token-
        redacting repr + MailDeliveryError swallow on that class apply.
        """
        if pending is None:
            return
        self._entries.append(pending.deliver)

    def deliver(self) -> None:
        """Fire every queued send. Idempotent across repeat calls.

        MUST be called *after* the caller's UoW commits. If commit
        failed, the caller never reaches here and no email leaves the
        host — that's the §15 invariant: no working magic-link token
        in a user's inbox without a matching nonce + audit_log row
        durable on disk.

        Each entry's :class:`MailDeliveryError` is logged and swallowed
        so one relay miss does not abort sibling sends queued behind
        it. Other exception types propagate — a programming bug in a
        deferred closure should fail loud, not silently drop the
        rest of the dispatch.
        """
        # Snapshot + clear up front so a re-entrant ``deliver()`` call
        # (e.g. inside a swallowed entry's logging path) sees an empty
        # queue and returns immediately. Mirrors the
        # :class:`PendingMagicLink` single-fire shape.
        entries = self._entries
        self._entries = []
        for entry in entries:
            try:
                entry()
            except MailDeliveryError:
                _log.warning(
                    "deferred mail send failed; swallowing per §15 enumeration guard",
                    exc_info=True,
                )

    def __repr__(self) -> str:
        """Render the dispatch without leaking captured tokens.

        The closures we collect typically capture signed magic-link
        tokens by reference; the default dataclass ``__repr__`` would
        render the closure's ``__qualname__`` + the captured frame,
        which is enough to leak tokens via stray log lines or
        tracebacks. We collapse to a count to keep debug output
        useful without that surface (§15 redaction posture).
        """
        return f"PendingDispatch(entries={len(self._entries)})"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InvalidToken(ValueError):
    """Token failed signature verification or is structurally malformed.

    400-equivalent. Covers :class:`itsdangerous.BadSignature`,
    tampering, unknown claims, and unexpected types — the caller
    sees one error symbol regardless so the HTTP body doesn't leak
    which part of the payload was wrong.
    """


class TokenExpired(ValueError):
    """Token's ``exp`` claim is in the past, or the nonce-row TTL lapsed.

    410-equivalent — the caller's link is no longer redeemable; they
    must request a fresh one.
    """


class PurposeMismatch(ValueError):
    """Token's ``purpose`` differs from what the caller asked to redeem.

    400-equivalent. The signup endpoint must never accept a
    recovery token (and vice versa), even if every other field
    lines up — the defence is independent of the nonce row existing.
    """


class AlreadyConsumed(ValueError):
    """Nonce row is already flipped (``consumed_at`` is not NULL).

    409-equivalent. Also raised when a concurrent consumer wins the
    race — the conditional ``UPDATE`` returns ``rowcount == 0`` and
    we map that to this type.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now(clock: Clock | None) -> datetime:
    """Return an aware UTC ``datetime`` from ``clock`` or a fresh system clock."""
    return (clock if clock is not None else SystemClock()).now()


def _subkey(settings: Settings | None) -> bytes:
    """Return the HKDF subkey used for signing + hashing.

    One key covers both the itsdangerous signature (via the
    serializer) and the IP / email hash-pepper — separating them
    would require two :attr:`Settings.root_key` subkeys without any
    concrete security gain: both are server-owned secrets and a
    compromise of one would, in practice, happen with the other.
    """
    s = settings if settings is not None else get_settings()
    return derive_subkey(s.root_key, purpose=_HKDF_PURPOSE)


def _serializer(settings: Settings | None) -> URLSafeTimedSerializer:
    """Build a fresh :class:`URLSafeTimedSerializer` bound to the HKDF subkey.

    We don't cache the instance: the serializer is cheap to construct
    (one HMAC init) and caching couples the returned object to a
    ``Settings`` instance in a way that complicates test rewiring.
    A :class:`SecretStr` wrapper on the key would move the caching
    burden to the caller; plain bytes keep it symmetric with the
    other HMAC surfaces.
    """
    key = _subkey(settings)
    # itsdangerous accepts bytes for ``secret_key`` directly (via its
    # typing-union of ``str | bytes``); passing the raw HKDF subkey
    # avoids a lossy encode roundtrip.
    return URLSafeTimedSerializer(secret_key=key, salt=_SERIALIZER_SALT)


# Local re-export of the shared helper — see :mod:`app.auth._hashing`
# (cd-3dc7). The private ``_hash_with_pepper`` alias stays so sibling
# modules that already import it (router refusal-audit path in
# :mod:`app.api.v1.auth.signup`, for instance) keep working without a
# drive-by import shuffle.
_hash_with_pepper = hash_with_pepper


def _ip_hash(ip: str, pepper: bytes) -> str:
    return _hash_with_pepper(ip, pepper)


def _email_hash(email: str, pepper: bytes) -> str:
    return _hash_with_pepper(canonicalise_email(email), pepper)


def _agnostic_audit_ctx() -> WorkspaceContext:
    """Return a sentinel :class:`WorkspaceContext` for bare-host events.

    Mirrors the pattern used by :func:`app.auth.passkey.register_finish_signup`'s
    deferred audit emission: the signup flow emits its own audit
    rows with a :class:`WorkspaceContext` borrowed from the freshly-
    minted workspace. Magic-link *request* and *consume* both run
    strictly before a workspace exists (or outside the workspace
    scope for recovery), so no real ctx is available — we synthesise
    one whose workspace_id / actor_id are the zero-ULID (26 zeros)
    and whose ``actor_kind`` is ``"system"``. The audit reader
    recognises this sentinel and renders the row as a pre-tenant
    identity event.
    """
    return WorkspaceContext(
        workspace_id=_AGNOSTIC_WORKSPACE_ID,
        workspace_slug="",  # no slug exists at this layer
        actor_id=_AGNOSTIC_ACTOR_ID,
        actor_kind="system",
        actor_grant_role="manager",  # unused for system actors
        actor_was_owner_member=False,
        audit_correlation_id=new_ulid(),
    )


def _lookup_user_by_email(session: Session, *, email: str) -> User | None:
    """Return the :class:`User` row matching canonicalised ``email``, if any.

    Runs under :func:`tenant_agnostic` because ``user`` is
    identity-scoped (see :mod:`app.adapters.db.identity`) and the
    ORM tenant filter has nothing to apply. Returning ``None`` for
    missing rows is part of the enumeration guard — the caller
    always returns 202, regardless.
    """
    email_lower = canonicalise_email(email)
    # justification: user is identity-scoped; no tenant predicate applies.
    with tenant_agnostic():
        return session.scalar(select(User).where(User.email_lower == email_lower))


def _resolve_subject_id(
    session: Session,
    *,
    email: str,
    purpose: str,
) -> str | None:
    """Return the subject ULID to bake into the token, per ``purpose``.

    For ``signup_verify`` and ``grant_invite`` we mint a fresh ULID on
    the fly — the signup session / invite row doesn't exist yet at
    this point in the flow, and the signup service (cd-3i5) / invite
    accept service will persist under the same id when it lands.

    For ``recover_passkey`` / ``email_change_confirm`` /
    ``email_change_revert`` we resolve the existing :class:`User` row
    by email; if no row exists we return ``None`` and the caller
    short-circuits silently (still sending a 202 response to preserve
    the enumeration guard). Email-change callers (cd-601a) typically
    hand a pre-resolved ``subject_id`` to :func:`request_link` so this
    branch is bypassed entirely.
    """
    if purpose in ("signup_verify", "grant_invite"):
        # Fresh subject id; the downstream flow owns the row creation.
        return new_ulid()
    user = _lookup_user_by_email(session, email=email)
    return user.id if user is not None else None


def _sign(
    serializer: URLSafeTimedSerializer,
    *,
    purpose: str,
    subject_id: str,
    jti: str,
    exp: int,
) -> str:
    """Pack + sign the magic-link payload into a URL-safe token."""
    payload = {
        "purpose": purpose,
        "subject_id": subject_id,
        "jti": jti,
        "exp": exp,
    }
    return serializer.dumps(payload)


def _unseal(
    serializer: URLSafeTimedSerializer, *, token: str, now: datetime
) -> dict[str, object]:
    """Verify the signature + payload shape, return the claims dict.

    itsdangerous' ``max_age`` is **not** used: we do the ``exp``
    check ourselves so the error semantics (``TokenExpired`` vs
    ``InvalidToken``) are ours to control and the server-side
    ``exp`` cap applies uniformly to every purpose.
    """
    try:
        data = serializer.loads(token)
    except SignatureExpired as exc:
        # ``SignatureExpired`` only fires if the caller set ``max_age``
        # on :meth:`loads` — we don't, so this branch is defensive.
        raise TokenExpired("token expired per signature timestamp") from exc
    except BadSignature as exc:
        raise InvalidToken("token signature is invalid") from exc
    if not isinstance(data, dict):
        raise InvalidToken("token payload is not an object")
    for key in ("purpose", "subject_id", "jti", "exp"):
        if key not in data:
            raise InvalidToken(f"token payload missing {key!r}")
    if not isinstance(data.get("exp"), int):
        raise InvalidToken("token payload 'exp' is not an integer")
    exp_at = datetime.fromtimestamp(int(data["exp"]), tz=UTC)
    if exp_at <= now:
        raise TokenExpired("token expired per payload exp")
    return data


def _claim_nonce(session: Session, *, jti: str, now: datetime) -> MagicLinkNonce:
    """Flip the matching nonce row from pending → consumed under a conditional
    ``UPDATE``.

    Raises :class:`AlreadyConsumed` when ``rowcount == 0`` — either
    the row was never inserted (caller never went through the
    ``request`` step) or a parallel consumer won the race.
    """
    # justification: magic_link_nonce is identity-scoped; no tenant
    # predicate applies.
    with tenant_agnostic():
        stmt = (
            update(MagicLinkNonce)
            .where(
                MagicLinkNonce.jti == jti,
                MagicLinkNonce.consumed_at.is_(None),
            )
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )
        # :class:`Result` is the generic public type; the concrete
        # object returned by an UPDATE is a :class:`CursorResult` whose
        # ``rowcount`` attribute is the row-count integer we need to
        # detect the race. The ``isinstance`` narrow is purely a
        # mypy-strict gate — every real DB-API driver returns a
        # CursorResult for an UPDATE.
        result = session.execute(stmt)
        if not isinstance(result, CursorResult):  # pragma: no cover - defensive
            raise RuntimeError(
                f"expected CursorResult from UPDATE; got {type(result).__name__}"
            )
        if result.rowcount != 1:
            raise AlreadyConsumed(f"nonce {jti!r} already consumed or unknown")
        row = session.get(MagicLinkNonce, jti)
    if row is None:  # pragma: no cover - defensive; UPDATE touched one row
        raise AlreadyConsumed(f"nonce {jti!r} update succeeded but row vanished")
    return row


def _check_row_expiry(row: MagicLinkNonce, *, now: datetime) -> None:
    """Raise :class:`TokenExpired` if the persisted TTL lapsed.

    Checked after the conditional update so the expiry message wins
    over AlreadyConsumed on a stale, unconsumed row. Normalises
    tzinfo for the SQLite roundtrip (see the same comment in
    :mod:`app.auth.passkey`).
    """
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        raise TokenExpired(f"nonce {row.jti!r} expired at {expires_at.isoformat()}")


# ---------------------------------------------------------------------------
# Public surface — request
# ---------------------------------------------------------------------------


def request_link(
    session: Session,
    *,
    email: str,
    purpose: MagicLinkPurpose,
    ip: str,
    mailer: Mailer | None,
    base_url: str,
    now: datetime | None = None,
    ttl: timedelta | None = None,
    throttle: Throttle,
    settings: Settings | None = None,
    clock: Clock | None = None,
    subject_id: str | None = None,
    send_email: bool = True,
) -> PendingMagicLink | None:
    """Mint one magic-link and queue its writes; return a deferred-send object.

    Steps:

    1. Validate ``purpose``; canonicalise the email.
    2. Rate-limit on (ip, email_hash) — :class:`RateLimited` propagates
       up so the HTTP router can return 429.
    3. Resolve the subject id (new ULID for signup/invite, existing
       user id for recover/email-change). No subject → return ``None``
       silently; the 202 response is identical whether or not the
       email existed. Callers that already own a subject row (e.g. the
       signup service pre-inserts a :class:`SignupAttempt` row) pass
       ``subject_id=`` directly so the nonce lines up with the
       caller's row without a subsequent rewrite.
    4. Server-cap the TTL at the per-purpose ceiling, mint jti + token.
    5. Insert the pending :class:`MagicLinkNonce` row and write the
       ``audit.magic_link.sent`` row on the caller's ``session``.
       The caller's UoW owns the commit (this function never calls
       ``session.commit()`` itself).
    6. Return a :class:`PendingMagicLink` whose
       :meth:`PendingMagicLink.deliver` performs the SMTP send. The
       caller MUST commit its UoW before calling :meth:`deliver` —
       that ordering is the cd-9i7z outbox boundary that prevents
       a working token from reaching the user inbox without a
       matching nonce on disk. When ``send_email`` is ``False`` the
       returned :class:`PendingMagicLink` carries a no-op
       :meth:`deliver` and the caller is responsible for sending its
       own template (the invite + manager-reissue flows do this).

    Returns a :class:`PendingMagicLink` on success, or ``None`` if
    the enumeration guard short-circuited (no user row matched the
    email on a recovery / email-change request).

    .. note::

       Old in-line-send signature (returned ``str | None``) is gone.
       Existing callers are migrated to invoke
       :meth:`PendingMagicLink.deliver` from their HTTP-router layer,
       AFTER the caller's UoW has committed. The router-level
       commit-then-deliver shape is what closes the cd-t2jz
       fail-open: if the commit raises (schema drift on
       ``audit_log`` was the original repro), the router never
       reaches :meth:`deliver` and no email leaves the host.
    """
    if purpose not in _VALID_PURPOSES:
        # Router body validation should have caught this, but the
        # domain gate means a programmatic caller (CLI, worker) can't
        # sneak through with a typo.
        raise InvalidToken(f"unknown magic-link purpose: {purpose!r}")

    resolved_now = now if now is not None else _now(clock)
    pepper = _subkey(settings)
    email_hash = _email_hash(email, pepper)
    ip_hash = _ip_hash(ip, pepper)

    # Rate-limit BEFORE we touch the DB or the mailer — this is both
    # cheaper (no IO) and the right order to stop a burst from
    # hammering the mail relay.
    throttle.check_request(ip=ip, email_hash=email_hash, now=resolved_now)

    if subject_id is None:
        subject_id = _resolve_subject_id(session, email=email, purpose=purpose)
    if subject_id is None:
        # No user row for a recovery / email-change request — silently
        # return without inserting a nonce or scheduling a send.
        # Enumeration guard: the caller sees the same 202 either way.
        return None

    # Cap the TTL at the per-purpose ceiling. Callers passing a larger
    # window get silently clamped; passing a shorter window is fine.
    max_ttl = _TTL_BY_PURPOSE[purpose]
    effective_ttl = min(ttl, max_ttl) if ttl is not None else max_ttl
    expires_at = resolved_now + effective_ttl

    jti = new_ulid(clock=clock)
    token = _sign(
        _serializer(settings),
        purpose=purpose,
        subject_id=subject_id,
        jti=jti,
        exp=int(expires_at.timestamp()),
    )
    url = f"{base_url.rstrip('/')}/auth/magic/{token}"

    # Insert the pending nonce row + the audit row on the caller's
    # session. The caller's UoW owns the commit; this function never
    # calls ``session.commit()``. The cd-9i7z outbox shape lives
    # *outside* this function: the caller commits, then calls
    # ``PendingMagicLink.deliver()`` to fire the SMTP send.
    # justification: magic_link_nonce is identity-scoped.
    with tenant_agnostic():
        session.add(
            MagicLinkNonce(
                jti=jti,
                purpose=purpose,
                subject_id=subject_id,
                consumed_at=None,
                expires_at=expires_at,
                created_ip_hash=ip_hash,
                created_email_hash=email_hash,
                created_at=resolved_now,
            )
        )
        session.flush()
    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="magic_link",
        entity_id=jti,
        action="magic_link.sent",
        diff={
            "purpose": purpose,
            "email_hash": email_hash,
            "ip_hash": ip_hash,
            "ttl_seconds": int(effective_ttl.total_seconds()),
        },
        clock=clock,
    )

    if not send_email:
        # Caller renders + sends its own template (invite,
        # manager-reissue, …). No deferred callback to wire.
        return PendingMagicLink(url=url)

    if mailer is None:
        raise ValueError("send_email=True requires a non-None mailer")
    # Capture every input :func:`_send_link_email` needs at mint
    # time so the deferred send is a parameter-free closure the
    # caller can invoke after commit. The ``MailDeliveryError``
    # swallow lives inside :meth:`PendingMagicLink.deliver` so the
    # call site stays a single ``pending.deliver()`` line.
    captured_mailer = mailer
    captured_email = email
    captured_base_url = base_url
    captured_token = token
    captured_purpose = purpose
    captured_ttl = effective_ttl

    def _deferred_send() -> None:
        _send_link_email(
            mailer=captured_mailer,
            to_email=captured_email,
            base_url=captured_base_url,
            token=captured_token,
            purpose=captured_purpose,
            ttl=captured_ttl,
        )

    return PendingMagicLink(url=url, _send_callback=_deferred_send)


def _send_link_email(
    *,
    mailer: Mailer,
    to_email: str,
    base_url: str,
    token: str,
    purpose: str,
    ttl: timedelta,
) -> None:
    """Render the template and hand the message to the mailer port.

    ``base_url`` is typed-in by the deployment operator (e.g.
    ``https://crew.day``) and **not** a user-supplied value. We strip
    a trailing slash defensively so the joined URL ends up with
    exactly one slash before ``auth/``.
    """
    url = f"{base_url.rstrip('/')}/auth/magic/{token}"
    ttl_minutes = max(1, int(ttl.total_seconds() // 60))
    label = magic_link_template.purpose_label(purpose)
    subject = render_template(magic_link_template.SUBJECT, purpose_label=label)
    body_text = render_template(
        magic_link_template.BODY_TEXT,
        purpose_label=label,
        url=url,
        ttl_minutes=str(ttl_minutes),
    )
    mailer.send(to=[to_email], subject=subject, body_text=body_text)


# ---------------------------------------------------------------------------
# Public surface — peek (read-only preview)
# ---------------------------------------------------------------------------


def peek_link(
    session: Session,
    *,
    token: str,
    expected_purpose: MagicLinkPurpose,
    ip: str,
    now: datetime | None = None,
    throttle: Throttle,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> MagicLinkOutcome:
    """Validate ``token`` without burning the nonce — read-only preview.

    Mirrors :func:`consume_link`'s validation surface (signature,
    payload shape, ``exp`` check, persisted-TTL check, purpose match,
    already-consumed check) but **never** flips the nonce row's
    ``consumed_at``. Used by introspect-style endpoints that want to
    render an Accept card before the user clicks Accept — the actual
    consume happens on the subsequent POST.

    Same throttle bucket as :func:`consume_link` — both call
    :meth:`Throttle.check_consume_allowed` as a pre-flight gate, so
    a locked-out IP cannot peek either. Failure-recording stays the
    router's job (matches the consume path's wiring); peek + accept
    both ride the same 3-fails / 60s → 10-minute IP lockout because
    they share the throttle instance.

    Raises:

    * :class:`ConsumeLockout` — IP locked out (pre-flight, no DB).
    * :class:`InvalidToken` — signature failed, payload malformed.
    * :class:`PurposeMismatch` — token purpose != ``expected_purpose``.
    * :class:`TokenExpired` — ``exp`` claim or persisted TTL lapsed.
    * :class:`AlreadyConsumed` — the nonce row's ``consumed_at`` is
      already set (i.e. the token was already redeemed). Distinct
      from the consume race because peek itself never flips the row;
      this branch only fires if a sibling :func:`consume_link` call
      previously won.

    Returns the same :class:`MagicLinkOutcome` shape as
    :func:`consume_link` so a caller that wants to inspect the
    subject_id without committing can stay symmetric. Does **not**
    write an audit row — peek is read-only and audit is the
    consume's job.
    """
    if expected_purpose not in _VALID_PURPOSES:
        raise InvalidToken(f"unknown magic-link purpose: {expected_purpose!r}")

    resolved_now = now if now is not None else _now(clock)

    # Same pre-flight lockout as consume — a locked-out IP cannot
    # introspect either (otherwise the introspect path becomes a
    # token-validity oracle for an attacker the lockout was supposed
    # to silence).
    throttle.check_consume_allowed(ip=ip, now=resolved_now)

    payload = _unseal(_serializer(settings), token=token, now=resolved_now)
    payload_purpose = payload["purpose"]
    if not isinstance(payload_purpose, str):
        raise InvalidToken("token payload 'purpose' is not a string")
    if payload_purpose != expected_purpose:
        raise PurposeMismatch(
            f"token purpose {payload_purpose!r} != expected {expected_purpose!r}"
        )
    jti = payload["jti"]
    subject_id = payload["subject_id"]
    if not isinstance(jti, str) or not isinstance(subject_id, str):
        raise InvalidToken("token payload 'jti' / 'subject_id' must be strings")

    # justification: magic_link_nonce is identity-scoped.
    with tenant_agnostic():
        row = session.get(MagicLinkNonce, jti)
    if row is None:
        # No nonce row for this jti — either a forged token (signature
        # check passed, but no row was ever inserted) or a sweeper
        # purged it. We collapse onto :class:`AlreadyConsumed` so the
        # router can map both "spent" and "never existed" to the same
        # 404 ``invite_not_found`` without a per-branch leak.
        raise AlreadyConsumed(f"nonce {jti!r} unknown or already swept")

    _check_row_expiry(row, now=resolved_now)

    if row.consumed_at is not None:
        raise AlreadyConsumed(f"nonce {jti!r} already consumed")

    if row.purpose != payload_purpose:
        # Token and row disagree — same defensive branch as consume.
        raise InvalidToken("nonce / token purpose disagree")
    if row.subject_id != subject_id:
        raise InvalidToken("nonce / token subject disagree")

    return MagicLinkOutcome(
        purpose=row.purpose,
        subject_id=row.subject_id,
        email_hash=row.created_email_hash,
        ip_hash=row.created_ip_hash,
    )


# ---------------------------------------------------------------------------
# Public surface — inspect (signature-verified peek at the jti)
# ---------------------------------------------------------------------------


def inspect_token_jti(token: str, *, settings: Settings | None = None) -> str:
    """Return the ``jti`` claim of a signature-verified ``token``.

    Cheap, read-only helper used by callers that have just minted a
    token via :func:`request_link` and need to bind their own ledger
    row to the magic-link nonce id (cd-601a's
    :func:`app.domain.identity.email_change.request_change` /
    :func:`verify_change` are the first users). Verifies the
    signature using the same serializer as :func:`peek_link` /
    :func:`consume_link` so a caller cannot accidentally trust a
    payload that was tampered with in transit, but does **not**
    touch the nonce row, expiry gates, or audit log — those are
    :func:`peek_link` / :func:`consume_link`'s job.

    Raises :class:`InvalidToken` on any signature / shape failure.
    """
    try:
        data = _serializer(settings).loads(token)
    except BadSignature as exc:
        raise InvalidToken("token signature is invalid") from exc
    if not isinstance(data, dict):
        raise InvalidToken("token payload is not an object")
    raw_jti = data.get("jti")
    if not isinstance(raw_jti, str):
        raise InvalidToken("token payload jti is not a string")
    return raw_jti


# ---------------------------------------------------------------------------
# Public surface — consume
# ---------------------------------------------------------------------------


def consume_link(
    session: Session,
    *,
    token: str,
    expected_purpose: MagicLinkPurpose,
    ip: str,
    now: datetime | None = None,
    throttle: Throttle,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> MagicLinkOutcome:
    """Unseal, race-check-flip, audit. Return the outcome.

    Raises:

    * :class:`ConsumeLockout` — the caller's IP is currently locked
      out. The router maps to ``429 consume_locked_out``. This check
      happens first so a locked-out IP never touches the nonce row.
    * :class:`InvalidToken` — signature failed, payload malformed,
      unknown purpose. Maps to 400.
    * :class:`PurposeMismatch` — token purpose != ``expected_purpose``.
      Maps to 400.
    * :class:`TokenExpired` — ``exp`` claim in the past, or the
      nonce row's ``expires_at`` lapsed. Maps to 410.
    * :class:`AlreadyConsumed` — nonce was already redeemed, or a
      parallel consumer won the race. Maps to 409.

    On any of the above (except :class:`ConsumeLockout` itself), the
    router calls :meth:`Throttle.record_consume_failure` so the IP
    gradually trips the 3-fails lockout. On success we reset the
    counter — a legitimate redemption should wipe the slate for the
    next one.
    """
    if expected_purpose not in _VALID_PURPOSES:
        raise InvalidToken(f"unknown magic-link purpose: {expected_purpose!r}")

    resolved_now = now if now is not None else _now(clock)

    # Pre-flight lockout check — raises ConsumeLockout which the router
    # maps to 429 without touching the DB.
    throttle.check_consume_allowed(ip=ip, now=resolved_now)

    payload = _unseal(_serializer(settings), token=token, now=resolved_now)
    payload_purpose = payload["purpose"]
    if not isinstance(payload_purpose, str):
        raise InvalidToken("token payload 'purpose' is not a string")
    if payload_purpose != expected_purpose:
        raise PurposeMismatch(
            f"token purpose {payload_purpose!r} != expected {expected_purpose!r}"
        )
    jti = payload["jti"]
    subject_id = payload["subject_id"]
    if not isinstance(jti, str) or not isinstance(subject_id, str):
        raise InvalidToken("token payload 'jti' / 'subject_id' must be strings")

    row = _claim_nonce(session, jti=jti, now=resolved_now)
    _check_row_expiry(row, now=resolved_now)

    if row.purpose != payload_purpose:
        # Token and row disagree — shouldn't happen unless somebody
        # tampered with the row out-of-band. Map to InvalidToken for
        # privacy (don't tell the caller their row exists with a
        # different purpose).
        raise InvalidToken("nonce / token purpose disagree")
    if row.subject_id != subject_id:
        raise InvalidToken("nonce / token subject disagree")

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="magic_link",
        entity_id=jti,
        action="magic_link.consumed",
        diff={
            "purpose": row.purpose,
            "email_hash": row.created_email_hash,
            "ip_hash_at_request": row.created_ip_hash,
            "ip_hash_at_consume": _ip_hash(ip, _subkey(settings)),
        },
        clock=clock,
    )

    return MagicLinkOutcome(
        purpose=row.purpose,
        subject_id=row.subject_id,
        email_hash=row.created_email_hash,
        ip_hash=row.created_ip_hash,
    )


# ---------------------------------------------------------------------------
# Public surface — rejected audit (pre-signup abuse trail)
# ---------------------------------------------------------------------------


# Sentinel used when the token failed signature verification (pre-parse
# failure): we can't derive a ``jti`` from it, so the audit row lands
# with a fixed entity_id that reader tooling can filter on. Any real
# jti is a ULID (26 chars) so the sentinel can't collide.
_UNKNOWN_JTI: Final[str] = "unknown"


# Symbol-level reasons the router may attach to a rejected-audit row.
# Keeping the vocabulary in one place (instead of formatting strings
# at each call site) means the audit reader can index by reason without
# reverse-engineering message text.
_REASON_BY_EXC: Final[dict[type[Exception], str]] = {
    InvalidToken: "invalid_token",
    PurposeMismatch: "purpose_mismatch",
    TokenExpired: "expired",
    AlreadyConsumed: "already_consumed",
    RateLimited: "rate_limited",
    ConsumeLockout: "consume_locked_out",
}


def reason_for_exception(exc: Exception) -> str:
    """Return the rejected-audit ``reason`` symbol for ``exc``.

    Unmapped exception types return ``"unknown"`` so the caller never
    blocks on an audit row just because a future error type slipped
    through without a symbol of its own.
    """
    for exc_type, symbol in _REASON_BY_EXC.items():
        if isinstance(exc, exc_type):
            return symbol
    return "unknown"


def _best_effort_unseal(
    token: str, *, settings: Settings | None
) -> tuple[str | None, str | None]:
    """Best-effort extract ``(jti, purpose)`` from ``token``; never raise.

    Used by :func:`write_rejected_audit` so the rejected row can carry
    forensic fields when the token parsed far enough to reveal them.
    Unlike :func:`_unseal`, this function suppresses every failure
    path — we do not want the audit-writer to throw while the caller
    is already handling another exception.
    """
    try:
        data = _serializer(settings).loads(token)
    except BadSignature:
        return (None, None)
    if not isinstance(data, dict):
        return (None, None)
    raw_jti = data.get("jti")
    raw_purpose = data.get("purpose")
    jti = raw_jti if isinstance(raw_jti, str) else None
    purpose = raw_purpose if isinstance(raw_purpose, str) else None
    return (jti, purpose)


def write_rejected_audit(
    session: Session,
    *,
    token: str | None,
    expected_purpose: str,
    ip: str,
    reason: str,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> None:
    """Best-effort write of an ``audit.magic_link.rejected`` row.

    Called from the HTTP router inside a **fresh** UoW after a consume
    raised a typed domain error: the primary UoW has rolled back any
    rows the service queued, so the rejected trail must land on its
    own transaction (see module docstring).

    Contents of the ``diff`` payload (PII minimisation §15):

    * ``reason`` — the caller-supplied symbol (e.g. ``"invalid_token"``,
      ``"already_consumed"``, ``"consume_locked_out"``). Kept symbolic
      instead of the exception's ``args[0]`` so downstream readers can
      aggregate by reason without regex.
    * ``ip_hash`` — SHA-256 hashed with the deployment's HKDF pepper;
      the plaintext IP is never persisted.
    * ``expected_purpose`` — the ``purpose`` string the caller asked
      us to redeem (from the request body). Always known.
    * ``token_purpose`` — extracted from the token payload when the
      signature verified; absent otherwise. A mismatch with
      ``expected_purpose`` is the forensic signature of a cross-purpose
      replay attempt.
    * ``email_hash`` — looked up from the nonce row when the token
      parsed far enough to reveal a ``jti``. Absent when signature
      verification failed or the nonce row has already been deleted.

    Plaintext email, plaintext IP, and the raw token are **never**
    included. The ``token`` argument is optional so tests can simulate
    a pre-parse failure path where the body never reached the handler
    at all.
    """
    pepper = _subkey(settings)
    diff: dict[str, Any] = {
        "reason": reason,
        "ip_hash": _ip_hash(ip, pepper),
        "expected_purpose": expected_purpose,
    }

    jti: str | None = None
    if token is not None:
        jti, token_purpose = _best_effort_unseal(token, settings=settings)
        if token_purpose is not None:
            diff["token_purpose"] = token_purpose

    if jti is not None:
        # The nonce row may not exist (orphaned token / TTL row swept)
        # — a miss leaves ``email_hash`` out of the diff rather than
        # carrying a bogus value. ``tenant_agnostic`` mirrors the
        # service's own lookup pattern.
        with tenant_agnostic():
            row = session.get(MagicLinkNonce, jti)
        if row is not None:
            diff["email_hash"] = row.created_email_hash

    write_audit(
        session,
        _agnostic_audit_ctx(),
        entity_kind="magic_link",
        entity_id=jti if jti is not None else _UNKNOWN_JTI,
        action="magic_link.rejected",
        diff=diff,
        clock=clock,
    )
