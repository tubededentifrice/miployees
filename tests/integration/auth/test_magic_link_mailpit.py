"""End-to-end magic-link round-trip against the dev stack with Mailpit.

This test exercises the *whole* magic-link bootstrap path that
:mod:`app.api.v1.auth.magic` mounts under the v1 bare-host prefix
(``/api/v1`` per :func:`app.api.factory.create_app`):

1. ``POST /api/v1/auth/magic/request`` mints a token, persists a
   nonce row, and hands the rendered email to the SMTP relay.
2. The dev compose stack pipes that send to the in-stack Mailpit
   sink (``CREWDAY_SMTP_HOST=mailpit`` / ``_PORT=1025`` —
   ``mocks/docker-compose.yml``).
3. We poll Mailpit's HTTP API for the delivered envelope (default
   shared port: ``127.0.0.1:8026``, see the bind-conflict comment in
   the compose file).
4. We extract the magic-link URL from the plain-text body, peel off
   the ``<token>`` path segment, and ``POST
   /api/v1/auth/magic/consume`` with that token + the same purpose.
   Success returns :class:`app.api.v1.auth.magic.MagicConsumeResponse`
   (200 with a :class:`MagicLinkOutcome`-shaped JSON body).

This is the only test that catches a regression *across* the four
moving parts at once — token mint, email render, SMTP transport,
nonce consume — so a change to any of them surfaces here even when
each unit test still passes. See ``docs/specs/03-auth-and-tokens.md``
§"Magic link format" and ``docs/specs/10-messaging-notifications.md``
§"Transport".

**Skip behaviour.** The dev stack is the canonical place to smoke-test
email; we do *not* spin up a per-test container. Both
``127.0.0.1:8026`` (Mailpit) and ``127.0.0.1:8100`` (the dev stack's
app-api proxied through the Vite dev server) must be reachable, else
the test skips with a clear reason — CI hosts without the stack up
get a clean skip rather than a noisy failure.

**Note on the consume response.** The HTTP layer returns 200 with a
:class:`MagicConsumeResponse` JSON body; it does **not** mint a
session cookie. Session issuance happens on a downstream flow
(signup-verify finalisation, recovery-passkey enrolment, etc.), each
of which owns its own router — ``/api/v1/auth/magic/consume`` is the
single-use redemption seam, not the session minter. cd-o62m's
acceptance criteria asks the assertion to track "whatever the route
actually emits"; a 200 + outcome body is what it does.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.integration.mail import (
    fetch_message_detail,
    is_reachable,
    purge_inbox,
    wait_for_message,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Dev-stack endpoints
# ---------------------------------------------------------------------------


# Mailpit's host port — set to ``8026`` because the dev compose file
# remaps the container's ``8025`` to host ``8026`` to avoid conflicting
# with another project's mailpit on the shared dev box (see
# ``mocks/docker-compose.yml`` "127.0.0.1:8026:8025"). Override via
# ``CREWDAY_TEST_MAILPIT_URL`` when running against a non-dev sink
# (testcontainers-style throwaway, port-forwarded prod-shadow, …).
_DEFAULT_MAILPIT_URL = "http://127.0.0.1:8026"
# App-api is only reachable from the host through the Vite dev proxy
# at ``127.0.0.1:8100`` — Vite forwards ``/auth/*`` and ``/api/*`` to
# the in-network ``app-api:8000`` container. Override via
# ``CREWDAY_TEST_APP_URL`` when the operator has the API published
# directly (e.g. ``-p 127.0.0.1:8000:8000``).
_DEFAULT_APP_URL = "http://127.0.0.1:8100"


def _mailpit_url() -> str:
    return os.environ.get("CREWDAY_TEST_MAILPIT_URL", _DEFAULT_MAILPIT_URL)


def _app_url() -> str:
    return os.environ.get("CREWDAY_TEST_APP_URL", _DEFAULT_APP_URL)


def _app_reachable(app_url: str, *, timeout: float = 2.0) -> bool:
    """Return ``True`` when ``GET {app_url}/healthz`` answers 2xx.

    The app-api factory mounts ``/healthz`` unconditionally — see
    :mod:`app.api.factory`. A 2xx there is the cheapest "is the app
    actually serving?" probe we have, and it doesn't need auth.
    """
    try:
        with urllib.request.urlopen(f"{app_url}/healthz", timeout=timeout) as resp:
            status = int(resp.status)
            resp.read()
    except urllib.error.URLError, ConnectionError, OSError:
        return False
    return 200 <= status < 300


def _readyz_failures(app_url: str, *, timeout: float = 2.0) -> list[str] | None:
    """Return a list of failing ``/readyz`` checks, or ``None`` when ready.

    The dev-stack ``app-api`` container runs ``alembic upgrade head`` in
    its entrypoint, but a long-lived container can drift behind the
    repo's migration head whenever a new revision lands and the
    container hasn't been restarted (``docker compose restart app-api``
    re-runs the upgrade). When that happens, ``/healthz`` still answers
    200 (the ASGI server is up) but every write that touches a column
    added by the missing migration fails at commit time — silently
    rolling back the magic-link nonce row, so a subsequent ``consume``
    sees ``rowcount == 0`` and maps onto ``409 already_consumed``.

    That's exactly the failure mode cd-t2jz reproduced before this
    helper existed: the round-trip looked like the consume side was
    broken, but the real cause was schema drift on the request side.
    Probing ``/readyz`` lets the fixture distinguish "app down" from
    "app up but migrations behind / worker stalled / root key missing"
    and surface a clear remediation hint, instead of a confusing 409.

    Returns ``None`` when readyz returns 200; on a 503 returns the
    ``checks[].check`` symbols (e.g. ``["migrations"]``); on any
    network / parse failure returns a one-element fallback list so the
    fixture still skips with a coherent reason.
    """
    try:
        with urllib.request.urlopen(f"{app_url}/readyz", timeout=timeout) as resp:
            status = int(resp.status)
            payload_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            status = exc.code
            payload_bytes = exc.read()
        finally:
            exc.close()
    except urllib.error.URLError, ConnectionError, OSError:
        return ["unreachable"]

    if 200 <= status < 300:
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
    except ValueError, UnicodeDecodeError:
        return [f"http_{status}"]
    if not isinstance(payload, dict):
        return [f"http_{status}"]
    checks = payload.get("checks", [])
    if not isinstance(checks, list):
        return [f"http_{status}"]
    failures = [
        check.get("check", "unknown")
        for check in checks
        if isinstance(check, dict) and check.get("ok") is False
    ]
    return failures or [f"http_{status}"]


@pytest.fixture(scope="module")
def stack_endpoints() -> Iterator[tuple[str, str]]:
    """Yield ``(app_url, mailpit_url)`` after sanity-checking both.

    Skips the whole module when either endpoint is unreachable, so a
    CI run on a host without the dev compose stack up just records
    a skip — not a failure. Module-scoped because we only need one
    reachability probe per test session; the per-test inbox purge
    (function-scoped fixture below) handles isolation.

    Beyond raw reachability, we also gate on ``/readyz`` so a dev-stack
    container running stale migrations (the cd-t2jz failure mode —
    ``audit_log`` missing the ``scope_kind`` column added by a
    revision newer than the one the running image migrated to) skips
    with a precise remediation hint instead of failing later with a
    misleading ``409 already_consumed`` from ``consume``.
    """
    app_url = _app_url()
    mailpit_url = _mailpit_url()
    if not _app_reachable(app_url):
        pytest.skip(
            f"app-api not reachable at {app_url} — start the dev stack via "
            "`docker compose -f mocks/docker-compose.yml up -d`"
        )
    failing_checks = _readyz_failures(app_url)
    if failing_checks is not None:
        pytest.skip(
            f"app-api at {app_url} is not ready (failing: {failing_checks}); "
            "if 'migrations' is listed, restart the dev stack — "
            "`docker compose -f mocks/docker-compose.yml restart app-api` — "
            "to pick up new revisions"
        )
    if not is_reachable(mailpit_url):
        pytest.skip(
            f"Mailpit not reachable at {mailpit_url} — start the dev stack via "
            "`docker compose -f mocks/docker-compose.yml up -d`"
        )
    yield app_url, mailpit_url


@pytest.fixture
def clean_inbox(stack_endpoints: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Purge Mailpit before the test so assertions don't see stale mail.

    The dev-stack Mailpit persists between runs (a named tmpfile inside
    the container — see ``/api/v1/info`` ``Database`` field), so an
    earlier test, manual signup attempt, or another agent's Playwright
    run can leave envelopes in the inbox. Purging at fixture entry
    gives every test a known-empty starting state without coupling
    cases to each other.
    """
    _, mailpit_url = stack_endpoints
    purge_inbox(mailpit_url)
    yield stack_endpoints


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post_json(
    url: str, body: dict[str, Any], *, timeout: float = 10.0
) -> tuple[int, dict[str, Any]]:
    """POST ``body`` as JSON and return ``(status, parsed_json)``.

    Built on :mod:`urllib.request` to stay dep-free — pulling
    ``httpx`` into an integration test only for one POST adds a
    transitive concern that doesn't pay for itself. Errors raised by
    the app (4xx / 5xx) come back as :class:`urllib.error.HTTPError`,
    which exposes ``.status`` and ``.read()`` so we still get the
    parsed body for assertions.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload_bytes = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        try:
            payload_bytes = exc.read()
            status = exc.code
        finally:
            exc.close()
    payload = json.loads(payload_bytes.decode("utf-8")) if payload_bytes else {}
    if not isinstance(payload, dict):
        raise AssertionError(f"POST {url} returned non-object JSON: {payload!r}")
    return status, payload


def _extract_magic_url(body_text: str) -> str:
    """Return the first ``…/auth/magic/<token>`` URL in a plain-text body.

    The magic-link template (``app/mail/templates/magic_link.py``)
    drops the URL on its own line; we walk the body, strip whitespace,
    and pick the first line that *both* starts with ``http`` and
    contains ``/auth/magic/``. Belt-and-braces against a future
    template tweak that adds an unrelated link to the body.
    """
    for raw_line in body_text.splitlines():
        line = raw_line.strip()
        if line.startswith("http") and "/auth/magic/" in line:
            return line
    raise AssertionError(f"no /auth/magic/ URL found in body:\n{body_text!r}")


def _token_from_url(magic_url: str) -> str:
    """Return the ``<token>`` segment of ``…/auth/magic/<token>``.

    The path is exactly ``/auth/magic/<token>`` — see the URL builder
    in :func:`app.auth.magic_link.request_link` and the template's
    ``{url}`` placeholder. Anything else is a programming error.
    """
    parsed = urlparse(magic_url)
    parts = parsed.path.split("/")
    # /auth/magic/<token> → ["", "auth", "magic", "<token>"]
    if len(parts) != 4 or parts[1] != "auth" or parts[2] != "magic" or not parts[3]:
        raise AssertionError(
            f"unexpected magic-link path shape {parsed.path!r}; "
            "expected '/auth/magic/<token>'"
        )
    return parts[3]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


_PURPOSE = "signup_verify"


def _unique_email() -> str:
    """Return a fresh ``magic-link-mailpit-<uuid4>@dev.local`` address.

    The dev-stack throttle (5 hits / 60 s per email, per
    :class:`app.auth._throttle.Throttle`) is process-scoped and shared
    across tests because the running app-api is a single process; if
    every test reused the same address they'd start tripping 429 once
    a second-pass run lands within the 60s window. A per-test UUIDv4
    sidesteps the per-email bucket entirely without disabling the
    rate-limit code path under test — the *per-IP* bucket still
    fires (all three tests share one source IP) but a 429 there is
    environmental and we skip cleanly via :func:`_request_or_skip`
    rather than fail. Rate-limit regressions have their own coverage
    in ``tests/integration/auth/test_abuse_guards.py`` —
    re-asserting them here would just turn a back-to-back rerun of
    the suite into a flake.
    """
    return f"magic-link-mailpit-{uuid.uuid4()}@dev.local"


# The magic router sits under the v1 bare-host prefix — see
# :func:`app.api.factory.create_app` (``bare_prefix = "/api/v1"``)
# and :func:`app.api.v1.auth.magic.build_magic_router` (the router's
# own prefix is ``/auth/magic``). The email body still embeds an
# unprefixed ``/auth/magic/<token>`` URL because that's the
# user-facing front-door path the SPA is expected to map to its own
# consume call (the SPA, not the user's browser, calls the JSON
# consume endpoint). A regression that flips the SPA path matters,
# but the *back-end* contract is the prefixed POST endpoints below.
_MAGIC_REQUEST_PATH = "/api/v1/auth/magic/request"
_MAGIC_CONSUME_PATH = "/api/v1/auth/magic/consume"


def _request_or_skip(app_url: str, email: str) -> None:
    """POST a magic-link request; ``pytest.skip`` if the per-IP bucket trips.

    The dev-stack throttle is shared across the whole compose process,
    so a back-to-back rerun of this module within the 60-s window can
    legitimately hit 429 on the per-IP bucket (5 hits/min) even with
    fresh per-test emails. That's environmental — not a regression in
    the magic-link flow this module is designed to catch — so we skip
    cleanly with a clear "rerun in 60s" reason rather than fail. The
    per-IP rate-limit code path has its own dedicated coverage in
    ``test_abuse_guards.py``; re-asserting it from here would only
    turn a useful round-trip test into a flake.
    """
    status, body = _post_json(
        f"{app_url}{_MAGIC_REQUEST_PATH}",
        {"email": email, "purpose": _PURPOSE},
    )
    if status == 429:
        pytest.skip(
            "magic-link per-IP rate limit tripped on the dev stack "
            f"(body={body!r}); rerun in ~60s once the bucket drains"
        )
    assert status == 202, f"unexpected bootstrap status; body={body!r}"
    assert body == {"status": "accepted"}


def test_magic_link_round_trip(clean_inbox: tuple[str, str]) -> None:
    """bootstrap → poll Mailpit → consume → assert outcome.

    The whole round-trip in one assertion graph:

    1. ``POST /api/v1/auth/magic/request`` returns 202
       ``{"status": "accepted"}`` (per
       :class:`MagicRequestAcceptedResponse`).
    2. Mailpit receives one envelope addressed to the test email
       within :data:`tests.integration.mail.DEFAULT_DEADLINE_S`.
    3. The plain-text body carries a ``…/auth/magic/<token>`` URL.
    4. ``POST /api/v1/auth/magic/consume`` with that token returns
       200 and a :class:`MagicLinkOutcome`-shaped JSON body whose
       ``purpose`` matches the request.
    5. Replaying the consume returns 409 ``already_consumed`` —
       the single-use guarantee, end-to-end.
    """
    app_url, mailpit_url = clean_inbox
    email = _unique_email()

    # --- bootstrap ----------------------------------------------------------
    _request_or_skip(app_url, email)

    # --- poll Mailpit -------------------------------------------------------
    envelope = wait_for_message(mailpit_url, to=email)
    assert envelope["From"]["Address"]  # truthy — relay actually sent
    internal_id = envelope["ID"]
    assert isinstance(internal_id, str) and internal_id

    detail = fetch_message_detail(mailpit_url, internal_id)
    text_body = detail.get("Text")
    assert isinstance(text_body, str) and text_body, (
        f"Mailpit returned empty/missing text body: {detail!r}"
    )

    magic_url = _extract_magic_url(text_body)
    token = _token_from_url(magic_url)
    # The signed itsdangerous token is well over 32 chars — see
    # cd-o62m's acceptance criteria; the floor catches a regression
    # to a placeholder / truncated value without pinning the exact
    # length (which depends on the signer's salt + payload).
    assert len(token) >= 32, (
        f"magic-link token too short ({len(token)} chars); url={magic_url!r}"
    )

    # --- consume ------------------------------------------------------------
    consume_url = f"{app_url}{_MAGIC_CONSUME_PATH}"
    status, outcome = _post_json(
        consume_url,
        {"token": token, "purpose": _PURPOSE},
    )
    assert status == 200, f"unexpected consume status; body={outcome!r}"
    assert outcome["purpose"] == _PURPOSE
    # subject_id is a ULID minted server-side for ``signup_verify``;
    # we don't pin the value, only the shape (non-empty string).
    assert isinstance(outcome.get("subject_id"), str) and outcome["subject_id"]
    assert isinstance(outcome.get("email_hash"), str) and outcome["email_hash"]
    assert isinstance(outcome.get("ip_hash"), str) and outcome["ip_hash"]

    # --- replay → 409 already_consumed --------------------------------------
    # The router's HTTPException carries ``detail={"error":
    # "already_consumed"}``, but :mod:`app.api.errors` flattens that
    # into the RFC 7807 envelope at the top level — the wire body is
    # ``{"type":..., "status": 409, "error": "already_consumed", ...}``,
    # not nested under ``detail``. Asserting on the flat key catches
    # both the typed-error mapping and the envelope shape.
    status, replay = _post_json(
        consume_url,
        {"token": token, "purpose": _PURPOSE},
    )
    assert status == 409, f"replay should be already_consumed; body={replay!r}"
    assert replay.get("error") == "already_consumed", (
        f"unexpected replay envelope: {replay!r}"
    )


def test_subject_matches_template(clean_inbox: tuple[str, str]) -> None:
    """The delivered ``Subject`` is exactly what the template renders.

    Pinning on the literal string ``"crew.day — verify your email and
    finish signing up"`` (purpose: ``signup_verify``) catches:

    * A template edit that drops the leading ``"crew.day — "`` brand.
    * A ``purpose_label`` map regression mapping ``signup_verify`` to
      a different phrase.
    * An SMTP-side header rewrite mangling the em-dash on the wire.

    The test deliberately re-asserts the literal rather than re-rendering
    the template via :mod:`app.mail.templates.magic_link` — the goal is
    to catch a divergence between *what the template renders* and
    *what arrives in the inbox*, not to tautologically compare the
    template to itself.
    """
    app_url, mailpit_url = clean_inbox
    email = _unique_email()

    _request_or_skip(app_url, email)

    envelope = wait_for_message(mailpit_url, to=email)
    assert envelope["Subject"] == ("crew.day — verify your email and finish signing up")


def test_text_body_contains_link(clean_inbox: tuple[str, str]) -> None:
    """Plain-text body has a ``/auth/magic/consume``-redeemable URL.

    cd-o62m specifies the assertion as "plain text body has
    /auth/magic/consume URL with 32+ char token". The actual *email*
    URL embeds the token as a path segment (``/auth/magic/<token>``);
    that token is what the consume endpoint takes. We assert both:

    * The email body carries the ``/auth/magic/<token>`` URL the
      template documents (catches a template edit that swaps to a
      query-string layout, or drops the URL entirely).
    * The token is at least 32 chars — long enough to survive a
      reasonable signer + payload cap without pinning an exact length
      (which depends on the itsdangerous salt + payload).

    The pure-text shape is what matters: many MUAs will only render
    the plain part, and a regression to "HTML-only with the URL" would
    silently break those clients without any unit test catching it.
    """
    app_url, mailpit_url = clean_inbox
    email = _unique_email()

    _request_or_skip(app_url, email)

    envelope = wait_for_message(mailpit_url, to=email)
    detail = fetch_message_detail(mailpit_url, envelope["ID"])
    text_body = detail.get("Text")
    assert isinstance(text_body, str) and text_body

    magic_url = _extract_magic_url(text_body)
    token = _token_from_url(magic_url)
    assert len(token) >= 32, (
        f"magic-link token too short ({len(token)} chars); url={magic_url!r}"
    )
