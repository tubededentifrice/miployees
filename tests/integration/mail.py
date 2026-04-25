"""Mailpit polling helpers for integration tests.

A handful of integration tests sit on top of a real Mailpit sink and
need to assert on the delivered envelope: ``test_mail_smtp.py`` for the
:class:`SMTPMailer` adapter, ``auth/test_magic_link_mailpit.py`` for the
end-to-end magic-link round-trip, and the upcoming signup / recovery /
quote round-trips queued behind cd-m1ls / cd-3ld1 / cd-yff4.

Without a shared helper the polling loop, header endpoint, and detail
endpoint get copy-pasted four times — which is exactly what cd-o62m's
acceptance criteria forbids ("do not copy-paste the polling loop four
times"). This module owns the contract; the callers stay short.

All public helpers take an ``api_url`` (e.g. ``http://127.0.0.1:8026``)
so the same code drives both an in-stack Mailpit (the dev compose stack
publishes to ``127.0.0.1:8026``) and a per-test :mod:`testcontainers`
Mailpit (random host port).

See ``docs/specs/10-messaging-notifications.md`` §"Transport" and
``docs/specs/17-testing-quality.md`` §"Integration".
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Final

__all__ = [
    "DEFAULT_DEADLINE_S",
    "MailpitMessage",
    "fetch_headers",
    "fetch_message_detail",
    "fetch_messages",
    "is_reachable",
    "purge_inbox",
    "wait_for_http",
    "wait_for_message",
]


DEFAULT_DEADLINE_S: Final[float] = 10.0


# Re-exporting Mailpit's ``messages`` array element shape under a name
# the call sites can read. The actual JSON has no fixed schema we
# control, so :class:`dict[str, Any]` is the honest type — callers
# narrow on the specific keys they touch (``MessageID``, ``Subject``,
# ``ID``) just like the existing test_mail_smtp.py does.
MailpitMessage = dict[str, Any]


def fetch_messages(api_url: str, *, timeout: float = 5.0) -> list[MailpitMessage]:
    """Return the ``messages`` array from Mailpit's ``/api/v1/messages``.

    Mailpit's JSON shape — ``{"total": N, "messages": [...]}`` — is
    documented and stable; the runtime ``isinstance`` guards keep mypy
    honest without pretending we know more than we do about
    third-party JSON.
    """
    with urllib.request.urlopen(f"{api_url}/api/v1/messages", timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Mailpit returned non-object payload: {payload!r}")
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        raise AssertionError(f"Mailpit 'messages' is not a list: {messages!r}")
    return [item for item in messages if isinstance(item, dict)]


def fetch_headers(
    api_url: str, internal_id: str, *, timeout: float = 5.0
) -> dict[str, list[str]]:
    """Return Mailpit's per-message header map (``/api/v1/message/{id}/headers``).

    Mailpit normalises header keys to ``Canonical-Case`` and yields
    each header's values as a list of strings — exactly the shape the
    caller wants for ``Reply-To`` / ``X-…`` assertions.
    """
    with urllib.request.urlopen(
        f"{api_url}/api/v1/message/{internal_id}/headers", timeout=timeout
    ) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Mailpit headers payload is not a dict: {payload!r}")
    out: dict[str, list[str]] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, list):
            continue
        str_values = [v for v in value if isinstance(v, str)]
        out[key] = str_values
    return out


def fetch_message_detail(
    api_url: str, internal_id: str, *, timeout: float = 5.0
) -> dict[str, Any]:
    """Return Mailpit's full message detail (``/api/v1/message/{id}``).

    Includes ``Text`` and ``HTML`` rendered bodies — the caller asserts
    on body content (e.g. magic-link URL inside the plain-text body).
    """
    with urllib.request.urlopen(
        f"{api_url}/api/v1/message/{internal_id}", timeout=timeout
    ) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Mailpit detail payload is not a dict: {payload!r}")
    return payload


def wait_for_message(
    api_url: str,
    *,
    message_id: str | None = None,
    to: str | None = None,
    deadline_s: float = DEFAULT_DEADLINE_S,
    poll_interval_s: float = 0.2,
) -> MailpitMessage:
    """Poll Mailpit until a matching envelope arrives, then return it.

    Exactly one of ``message_id`` or ``to`` must be supplied:

    * ``message_id`` — match the top-level ``MessageID`` field
      (angle-brackets-stripped). This is the strongest match: the
      caller minted the ID themselves (e.g. via ``SMTPMailer.send``'s
      return value) and wants to assert against *this* send,
      independent of inbox ordering.
    * ``to`` — match the recipient's ``Address`` on the first ``To``
      record. Right shape when the caller drove a flow that mints the
      Message-ID server-side and only the recipient is known up
      front (the magic-link bootstrap path).

    Raises :class:`AssertionError` if no envelope matches within
    ``deadline_s``. The default 10 s deadline matches what the
    existing :mod:`test_mail_smtp` test used; adjust on a flow with a
    deliberately slower mailer.
    """
    if (message_id is None) == (to is None):
        raise ValueError("wait_for_message requires exactly one of message_id= or to=")
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        items = fetch_messages(api_url)
        for item in items:
            if message_id is not None and item.get("MessageID") == message_id:
                return item
            if to is not None and _matches_recipient(item, to):
                return item
        time.sleep(poll_interval_s)
    selector = f"MessageID={message_id!r}" if message_id is not None else f"to={to!r}"
    raise AssertionError(
        f"Mailpit at {api_url} never received a message matching {selector} "
        f"within {deadline_s}s"
    )


def _matches_recipient(item: MailpitMessage, address: str) -> bool:
    """Return ``True`` when ``item['To']`` contains ``address``.

    Mailpit stores ``To`` as a list of ``{"Name", "Address"}`` records;
    we only match on ``Address`` (lower-cased) so a caller passing
    ``"Alice@Example.com"`` still matches the canonical form Mailpit
    stores. ``EmailAddress``-style mailbox parsing is overkill here —
    the helper's contract is "did this address receive an email", and
    case-insensitive equality covers it.
    """
    to_records = item.get("To")
    if not isinstance(to_records, list):
        return False
    target = address.casefold()
    for record in to_records:
        if not isinstance(record, dict):
            continue
        addr = record.get("Address")
        if isinstance(addr, str) and addr.casefold() == target:
            return True
    return False


def purge_inbox(api_url: str, *, timeout: float = 5.0) -> None:
    """Delete every message stored in Mailpit (``DELETE /api/v1/messages``).

    Test isolation knob: the dev-stack Mailpit is shared across
    sessions, so a test that asserts ``count == 1`` after sending one
    message will fail if a previous run left envelopes behind. Call
    this at the start of a test (or in a fixture) to start from a
    clean inbox.

    Mailpit returns ``200 OK`` with an empty body on success. On the
    rare path where the call fails (Mailpit down, transient HTTP error)
    we re-raise — the caller wanted a clean inbox and didn't get one,
    silencing the failure would let the test assert against stale
    state.
    """
    req = urllib.request.Request(
        f"{api_url}/api/v1/messages",
        method="DELETE",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Drain the body so the connection returns to the pool cleanly.
        resp.read()


def is_reachable(api_url: str, *, timeout: float = 2.0) -> bool:
    """Return ``True`` when Mailpit's ``/livez`` answers 2xx.

    Used by integration tests to skip cleanly when the dev compose
    stack isn't up. Catches the union of network-layer failures
    (no listener, DNS, refused) so the caller writes a one-liner skip
    guard instead of a five-line ``except`` ladder.
    """
    try:
        with urllib.request.urlopen(f"{api_url}/livez", timeout=timeout) as resp:
            status = int(resp.status)
    except (urllib.error.URLError, ConnectionError, OSError):
        return False
    return 200 <= status < 300


def wait_for_http(api_url: str, *, deadline_s: float = 15.0) -> None:
    """Poll Mailpit's ``/livez`` until it returns 2xx or we time out.

    Right shape for the testcontainers fixture: the container is up
    well before Mailpit finishes binding its listeners, and an
    ``urlopen`` against a not-yet-open port throws
    :class:`ConnectionRefusedError`. For "is the dev stack up?" use
    :func:`is_reachable` instead — that one returns a bool rather than
    raising.
    """
    end = time.monotonic() + deadline_s
    last_exc: BaseException | None = None
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(f"{api_url}/livez", timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
        time.sleep(0.2)
    raise RuntimeError(
        f"Mailpit HTTP API at {api_url} never came up in {deadline_s}s "
        f"(last error: {last_exc!r})"
    )
