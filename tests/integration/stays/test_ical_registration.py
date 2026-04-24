"""Integration coverage for iCal feed registration.

Spins up a real ``http.server.ThreadingHTTPServer`` on ``127.0.0.1``
and drives :func:`app.domain.stays.ical_service.register_feed`
against it end-to-end:

1. **SSRF guard still blocks loopback** with the production
   default (``allow_private_addresses=False``). The service raises
   :class:`IcalUrlInvalid` with ``ical_url_private_address`` before
   a single byte leaves the process.
2. **With ``allow_private_addresses=True``** (the test-only escape
   hatch, never set in production) and a plain-HTTP fetcher, the
   end-to-end path lands a row in the DB, encrypts the URL via the
   real :class:`Aes256GcmEnvelope`, and flips ``enabled=True`` when
   the fake server returns a VCALENDAR body.

We do **not** stand up a trusted TLS listener because minting a
cert the stdlib SSL context trusts requires a CA dance
disproportionate to what the SSRF guard needs to prove. Instead
the "HTTPS required" rule is enforced at the scheme gate (which
we test in `tests/unit/adapters/ical/test_validator.py`); the
integration test swaps in an HTTP-only fetcher so we can talk to
the real local server without TLS, while still exercising the
service's validate → encrypt → persist → audit pipeline.

See ``docs/specs/17-testing-quality.md`` §"Integration" and
``docs/specs/04-properties-and-stays.md`` §"iCal feed".
"""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from urllib.parse import SplitResult

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.adapters.db.stays.models import IcalFeed
from app.adapters.ical.providers import HostProviderDetector
from app.adapters.ical.validator import (
    Fetcher,
    FetchResponse,
    HttpxIcalValidator,
    IcalValidatorConfig,
)
from app.adapters.storage.envelope import Aes256GcmEnvelope
from app.domain.stays.ical_service import (
    IcalFeedCreate,
    IcalUrlInvalid,
    get_plaintext_url,
    register_feed,
)
from app.tenancy.context import WorkspaceContext
from app.util.clock import FrozenClock
from app.util.ulid import new_ulid

_PINNED = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake ICS server — real HTTP, no TLS
# ---------------------------------------------------------------------------


_ICS_BODY = (
    b"BEGIN:VCALENDAR\r\n"
    b"VERSION:2.0\r\n"
    b"PRODID:-//integration//EN\r\n"
    b"BEGIN:VEVENT\r\n"
    b"UID:it-1\r\n"
    b"DTSTART:20260424T120000Z\r\n"
    b"DTEND:20260425T120000Z\r\n"
    b"SUMMARY:Integration\r\n"
    b"END:VEVENT\r\n"
    b"END:VCALENDAR\r\n"
)


class _IcsHandler(BaseHTTPRequestHandler):
    """Serve a canned VCALENDAR body on ``GET``; anything else is 404."""

    payload: ClassVar[bytes] = _ICS_BODY
    content_type: ClassVar[str] = "text/calendar"

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format: str, *args: object) -> None:
        """Silence the default stderr spam from BaseHTTPRequestHandler."""


@pytest.fixture(name="ics_server")
def fixture_ics_server() -> Iterator[ThreadingHTTPServer]:
    """Yield a local ICS server bound to ``127.0.0.1`` on an ephemeral port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _IcsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# HTTP-only fetcher — for the "allow private addresses" integration path
# ---------------------------------------------------------------------------


@dataclass
class _HttpOnlyFetcher(Fetcher):
    """Test-only :class:`Fetcher` that speaks plain HTTP.

    Connects via :class:`http.client.HTTPConnection` (no TLS). Used
    in tandem with ``allow_private_addresses=True`` so the
    integration path can talk to a loopback-bound fake server. The
    scheme + SSRF guards still run at the validator level before
    the fetcher is invoked.
    """

    def fetch(
        self,
        parsed: SplitResult,
        resolved_ip: str,
        *,
        deadline: float,
        max_body_bytes: int,
    ) -> FetchResponse:
        port = parsed.port if parsed.port is not None else 80
        conn = http.client.HTTPConnection(
            host=resolved_ip,
            port=port,
            timeout=max(0.1, deadline),
        )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            conn.request("GET", path, headers={"Host": parsed.hostname or ""})
            response = conn.getresponse()
            body = response.read(max_body_bytes + 1)
            if len(body) > max_body_bytes:
                from app.adapters.ical.ports import IcalValidationError

                raise IcalValidationError(
                    "ical_url_oversize",
                    f"response body exceeded cap of {max_body_bytes} bytes",
                )
            headers: tuple[tuple[str, str], ...] = tuple(response.getheaders())
            return FetchResponse(status=response.status, headers=headers, body=body)
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Bootstrap helpers (minimal — follow the same shape as the unit test)
# ---------------------------------------------------------------------------


_ACTOR = "01HWA00000000000000000USR1"


def _ctx(workspace_id: str, slug: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        actor_id=_ACTOR,
        actor_kind="user",
        actor_grant_role="manager",
        actor_was_owner_member=True,
        audit_correlation_id="01HWA00000000000000000CRL1",
    )


def _bootstrap(db_session: Session) -> tuple[str, str, WorkspaceContext]:
    from app.adapters.db.places.models import Property
    from app.adapters.db.workspace.models import Workspace

    ws_id = new_ulid()
    db_session.add(
        Workspace(
            id=ws_id,
            slug="it-ical",
            name="It ICal",
            plan="free",
            quota_json={},
            settings_json={},
            created_at=_PINNED,
        )
    )
    prop_id = new_ulid()
    db_session.add(
        Property(
            id=prop_id,
            name="Villa Sud",
            kind="str",
            address="12 Chemin des Oliviers",
            address_json={"country": "FR"},
            country="FR",
            locale=None,
            default_currency=None,
            timezone="Europe/Paris",
            lat=None,
            lon=None,
            client_org_id=None,
            owner_user_id=None,
            tags_json=[],
            welcome_defaults_json={},
            property_notes_md="",
            created_at=_PINNED,
            updated_at=_PINNED,
            deleted_at=None,
        )
    )
    db_session.flush()
    return ws_id, prop_id, _ctx(ws_id, "it-ical")


@pytest.fixture
def envelope_real() -> Aes256GcmEnvelope:
    """Real :class:`Aes256GcmEnvelope` with a deterministic test key."""
    return Aes256GcmEnvelope(SecretStr("x" * 32))


@pytest.fixture
def frozen_clock() -> FrozenClock:
    return FrozenClock(_PINNED)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegistrationEndToEnd:
    """End-to-end registration flow against the local fake ICS server."""

    def test_loopback_rejected_in_production_default(
        self,
        db_session: Session,
        ics_server: ThreadingHTTPServer,
        envelope_real: Aes256GcmEnvelope,
        frozen_clock: FrozenClock,
    ) -> None:
        """Production default rejects ``127.0.0.1`` with ``ical_url_private_address``.

        We point the validator at the loopback server with the
        production-default :class:`IcalValidatorConfig`; the SSRF
        guard fires on the ``127.0.0.1`` resolution and no row lands.
        """
        _ws, prop_id, ctx = _bootstrap(db_session)
        port = ics_server.server_port
        validator = HttpxIcalValidator(IcalValidatorConfig(fetcher=_HttpOnlyFetcher()))
        with pytest.raises(IcalUrlInvalid) as exc_info:
            register_feed(
                db_session,
                ctx,
                body=IcalFeedCreate(
                    property_id=prop_id,
                    # Scheme is still https://, so we don't trip the
                    # scheme gate; the SSRF gate fires on the 127.0.0.1
                    # resolution.
                    url=f"https://127.0.0.1:{port}/feed.ics",
                ),
                validator=validator,
                detector=HostProviderDetector(),
                envelope=envelope_real,
                clock=frozen_clock,
            )
        assert exc_info.value.code == "ical_url_private_address"

    def test_happy_path_with_private_allow(
        self,
        db_session: Session,
        ics_server: ThreadingHTTPServer,
        envelope_real: Aes256GcmEnvelope,
        frozen_clock: FrozenClock,
    ) -> None:
        """With the test-only escape hatch, the full flow persists + encrypts.

        Exercises:

        * real :class:`Aes256GcmEnvelope` encrypt + decrypt round trip
          (the ``get_plaintext_url`` assertion at the end),
        * HttpxIcalValidator orchestrating fetch + redirect-loop +
          body-sniff + Content-Type gate,
        * service-layer audit row + encrypted URL on disk.
        """
        _ws, prop_id, ctx = _bootstrap(db_session)
        port = ics_server.server_port
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                fetcher=_HttpOnlyFetcher(),
                allow_private_addresses=True,
            )
        )
        url = f"https://127.0.0.1:{port}/feed.ics"

        view = register_feed(
            db_session,
            ctx,
            body=IcalFeedCreate(property_id=prop_id, url=url),
            validator=validator,
            detector=HostProviderDetector(),
            envelope=envelope_real,
            clock=frozen_clock,
        )

        assert view.enabled is True
        assert view.provider == "custom"  # loopback → generic → custom

        # Row landed, URL envelope-encrypted (not plaintext).
        row = db_session.get(IcalFeed, view.id)
        assert row is not None
        assert row.url != url
        assert b"127.0.0.1" not in row.url.encode("latin-1")

        # Plaintext is still recoverable via the only legal reveal path.
        plain = get_plaintext_url(
            db_session, ctx, feed_id=view.id, envelope=envelope_real
        )
        assert plain == url

    def test_insecure_scheme_fails_before_fetch(
        self,
        db_session: Session,
        ics_server: ThreadingHTTPServer,
        envelope_real: Aes256GcmEnvelope,
        frozen_clock: FrozenClock,
    ) -> None:
        """A plain ``http://`` URL trips the scheme gate up-front."""
        _ws, prop_id, ctx = _bootstrap(db_session)
        port = ics_server.server_port
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                fetcher=_HttpOnlyFetcher(),
                allow_private_addresses=True,
            )
        )
        with pytest.raises(IcalUrlInvalid) as exc_info:
            register_feed(
                db_session,
                ctx,
                body=IcalFeedCreate(
                    property_id=prop_id,
                    url=f"http://127.0.0.1:{port}/feed.ics",
                ),
                validator=validator,
                detector=HostProviderDetector(),
                envelope=envelope_real,
                clock=frozen_clock,
            )
        assert exc_info.value.code == "ical_url_insecure_scheme"
