"""Unit tests for :mod:`app.adapters.ical.validator` + ``providers``.

Covers cd-1ai's §04 "SSRF guard" and "Supported providers"
contracts:

* **Scheme** — only ``https://`` passes.
* **Private addresses** — loopback, link-local, multicast, RFC 1918,
  CGNAT, reserved, unspecified are all rejected. Each is a named
  test so a regression on a specific range surfaces in the failure
  name.
* **DNS rebinding** — the validator resolves **once** per hop and
  pins the result; a second call that would have returned a private
  IP never reaches the fetcher.
* **Redirects** — same-origin within cap is followed; cross-origin
  is rejected; exceeding the cap is rejected.
* **Body size** — over-cap responses raise ``ical_url_oversize``;
  under-cap is accepted.
* **Content-Type** — allow-listed MIME types pass; other MIME types
  with a ``BEGIN:VCALENDAR`` body pass via the sniff; non-ICS body
  with a non-allowed MIME fails.
* **Provider auto-detect** — table-driven, five providers + ``generic``
  fallback + look-alike rejection.

The validator is wired with a stub :class:`Fetcher` and a stub
resolver so tests never open a socket or call DNS.

See ``docs/specs/04-properties-and-stays.md`` §"SSRF guard" and
§"Supported providers".
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import SplitResult

import pytest

from app.adapters.ical.ports import IcalValidationError
from app.adapters.ical.providers import HostProviderDetector, detect_provider
from app.adapters.ical.validator import (
    Fetcher,
    FetchResponse,
    HttpxIcalValidator,
    IcalValidatorConfig,
    is_public_ip,
    resolve_public_address,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _fixed_resolver(ips: list[str]) -> Callable[[str, int], Iterable[str]]:
    """Resolver stub that always returns ``ips`` regardless of input."""

    def _resolve(host: str, port: int) -> Iterable[str]:
        return list(ips)

    return _resolve


def _sequence_resolver(
    sequence: list[list[str]],
) -> Callable[[str, int], Iterable[str]]:
    """Resolver stub that returns a different address set per call.

    Used for the DNS-rebind scenario — first call returns a public
    IP, second call would return a private IP. The validator must
    pin the first result and never consult the resolver again
    within a single hop.
    """
    calls: list[list[str]] = list(sequence)

    def _resolve(host: str, port: int) -> Iterable[str]:
        if not calls:
            raise AssertionError("resolver called more times than expected")
        return calls.pop(0)

    return _resolve


@dataclass
class StubFetcher(Fetcher):
    """Table-driven :class:`Fetcher` stub.

    ``responses`` is a list consumed in order; one entry per fetch
    call. ``calls`` records what the validator asked for so tests
    can assert DNS-pinning (``resolved_ip`` pass-through) and
    same-origin redirect handling.
    """

    responses: list[FetchResponse]
    calls: list[tuple[str, str]] = field(default_factory=list)

    def fetch(
        self,
        parsed: SplitResult,
        resolved_ip: str,
        *,
        deadline: float,
        max_body_bytes: int,
    ) -> FetchResponse:
        self.calls.append((parsed.geturl(), resolved_ip))
        if not self.responses:
            raise AssertionError("StubFetcher: no more canned responses")
        return self.responses.pop(0)


def _ics_body(event_uid: str = "x") -> bytes:
    """Minimal VCALENDAR body used as the happy-path fixture."""
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{event_uid}\r\n"
        "DTSTART:20260424T120000Z\r\n"
        "DTEND:20260425T120000Z\r\n"
        "SUMMARY:Test\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ).encode()


def _ok(
    body: bytes = b"",
    *,
    content_type: str | None = "text/calendar",
    status: int = 200,
) -> FetchResponse:
    headers: tuple[tuple[str, str], ...] = ()
    if content_type is not None:
        headers = (("Content-Type", content_type),)
    return FetchResponse(status=status, headers=headers, body=body)


def _redirect(location: str, *, status: int = 302) -> FetchResponse:
    return FetchResponse(
        status=status,
        headers=(("Location", location),),
        body=b"",
    )


# ---------------------------------------------------------------------------
# Public-IP classifier
# ---------------------------------------------------------------------------


class TestIsPublicIp:
    """``is_public_ip`` matches §04's rejection set."""

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",  # loopback v4
            "127.1.2.3",  # any 127/8
            "::1",  # loopback v6
            "10.0.0.5",  # RFC 1918
            "10.255.255.255",
            "172.16.0.1",  # RFC 1918 mid-range
            "172.31.255.255",
            "192.168.1.1",  # RFC 1918 home
            "169.254.169.254",  # link-local v4 (cloud metadata!)
            "fe80::1",  # link-local v6
            "224.0.0.1",  # multicast v4
            "ff00::1",  # multicast v6
            "100.64.0.1",  # CGNAT
            "100.127.255.255",  # CGNAT end
            "0.0.0.0",  # unspecified v4
            "::",  # unspecified v6
            "240.0.0.1",  # reserved v4
            "not-an-ip",  # garbage
        ],
    )
    def test_rejected(self, ip: str) -> None:
        assert is_public_ip(ip) is False

    @pytest.mark.parametrize(
        "ip",
        [
            "1.1.1.1",  # Cloudflare public
            "8.8.8.8",  # Google public
            "2606:4700:4700::1111",  # Cloudflare v6
        ],
    )
    def test_accepted(self, ip: str) -> None:
        assert is_public_ip(ip) is True


class TestResolvePublicAddress:
    """``resolve_public_address`` rejects mixed + private result sets."""

    def test_all_public_returns_first(self) -> None:
        ip = resolve_public_address(
            "example.com",
            443,
            resolver=_fixed_resolver(["1.1.1.1", "2606:4700::1"]),
        )
        assert ip == "1.1.1.1"

    def test_mixed_public_private_rejected(self) -> None:
        with pytest.raises(IcalValidationError) as exc_info:
            resolve_public_address(
                "example.com",
                443,
                resolver=_fixed_resolver(["1.1.1.1", "127.0.0.1"]),
            )
        assert exc_info.value.code == "ical_url_private_address"

    def test_empty_result_rejected(self) -> None:
        with pytest.raises(IcalValidationError) as exc_info:
            resolve_public_address("example.com", 443, resolver=_fixed_resolver([]))
        assert exc_info.value.code == "ical_url_unreachable"


# ---------------------------------------------------------------------------
# Validator — scheme / DNS / rebind
# ---------------------------------------------------------------------------


class TestSchemeGate:
    """§04 "Scheme" — only https:// passes."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/feed.ics",
            "file:///etc/passwd",
            "ftp://example.com/feed.ics",
            "data:text/plain,BEGIN:VCALENDAR",
            "gopher://example.com/1",
        ],
    )
    def test_non_https_rejected(self, url: str) -> None:
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=StubFetcher(responses=[]),
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate(url)
        assert exc_info.value.code == "ical_url_insecure_scheme"

    def test_https_malformed_rejected(self) -> None:
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=StubFetcher(responses=[]),
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate("https://")
        assert exc_info.value.code == "ical_url_malformed"


@pytest.mark.parametrize(
    ("label", "ip", "code"),
    [
        # ---- IPv4 ranges ----
        ("loopback-v4", "127.0.0.1", "ical_url_private_address"),
        ("loopback-v4-high", "127.1.2.3", "ical_url_private_address"),
        ("rfc1918-10", "10.1.2.3", "ical_url_private_address"),
        ("rfc1918-172", "172.16.5.10", "ical_url_private_address"),
        ("rfc1918-192", "192.168.1.5", "ical_url_private_address"),
        ("link-local-v4", "169.254.169.254", "ical_url_private_address"),
        ("multicast-v4", "224.1.2.3", "ical_url_private_address"),
        ("reserved-v4", "240.0.0.1", "ical_url_private_address"),
        ("cgnat", "100.64.1.1", "ical_url_private_address"),
        ("unspecified-v4", "0.0.0.0", "ical_url_private_address"),
        # ---- IPv6 ranges ----
        ("loopback-v6", "::1", "ical_url_private_address"),
        ("unspecified-v6", "::", "ical_url_private_address"),
        ("link-local-v6", "fe80::1", "ical_url_private_address"),
        ("multicast-v6", "ff00::1", "ical_url_private_address"),
        ("uniquelocal-v6", "fc00::1", "ical_url_private_address"),
        ("documentation-v6", "2001:db8::1", "ical_url_private_address"),
        # ---- Dual-stack / mapped — an attacker's DNS can return a
        # v4-mapped v6 that points at v4 loopback. Must still reject.
        ("v4mapped-loopback", "::ffff:127.0.0.1", "ical_url_private_address"),
        ("v4mapped-rfc1918", "::ffff:10.0.0.1", "ical_url_private_address"),
    ],
)
def test_private_address_rejected(label: str, ip: str, code: str) -> None:
    """Each SSRF range collapses to ``ical_url_private_address``."""
    validator = HttpxIcalValidator(
        IcalValidatorConfig(
            resolver=_fixed_resolver([ip]),
            fetcher=StubFetcher(responses=[]),
        )
    )
    with pytest.raises(IcalValidationError) as exc_info:
        validator.validate("https://attacker.test/feed.ics")
    assert exc_info.value.code == code


class TestDnsRebindPin:
    """The validator pins the resolved IP so a second DNS call can't flip it."""

    def test_resolver_called_once_per_hop_v4(self) -> None:
        """A non-redirect response consumes exactly one resolver call.

        If the validator re-resolved between resolve and fetch, the
        sequence resolver would fire twice; we'd then also have to
        provide a private-IP fallback. Asserting the single call is
        the direct observation of "we pinned the first answer".
        """
        fetcher = StubFetcher(responses=[_ok(_ics_body())])
        # First call: public. Second call: private. If the validator
        # re-resolved mid-request, it would land on 127.0.0.1 and
        # raise ``ical_url_private_address``; instead it pins the
        # first call's result.
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_sequence_resolver([["1.1.1.1"], ["127.0.0.1"]]),
                fetcher=fetcher,
            )
        )
        validation = validator.validate("https://example.com/feed.ics")
        assert validation.resolved_ip == "1.1.1.1"
        assert len(fetcher.calls) == 1

    def test_resolver_called_once_per_hop_v6(self) -> None:
        """IPv6 rebind is equally defeated: public v6 first, ``::1`` second."""
        fetcher = StubFetcher(responses=[_ok(_ics_body())])
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_sequence_resolver(
                    [["2606:4700:4700::1111"], ["::1"]],
                ),
                fetcher=fetcher,
            )
        )
        validation = validator.validate("https://example.com/feed.ics")
        assert validation.resolved_ip == "2606:4700:4700::1111"
        assert len(fetcher.calls) == 1

    def test_mixed_family_rebind(self) -> None:
        """First-call v4 public pins even if a v6 private would be offered next."""
        fetcher = StubFetcher(responses=[_ok(_ics_body())])
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_sequence_resolver([["8.8.8.8"], ["::1"]]),
                fetcher=fetcher,
            )
        )
        validation = validator.validate("https://example.com/feed.ics")
        assert validation.resolved_ip == "8.8.8.8"
        assert len(fetcher.calls) == 1


class TestPinnedConnectionSni:
    """``_IpPinnedHTTPSConnection`` uses the hostname (not IP) as SNI.

    The TCP connect target must be the pinned IP (SSRF defeat), but
    the TLS handshake's ``server_hostname`` must be the original
    hostname — otherwise public certs fail verification and every
    production request 500s.
    """

    def test_server_hostname_is_the_original_host(self) -> None:
        """Stub out socket + ssl and assert ``wrap_socket`` sees the hostname."""
        from unittest.mock import MagicMock, patch

        import app.adapters.ical.validator as validator_mod
        from app.adapters.ical.validator import _IpPinnedHTTPSConnection

        ctx = MagicMock(spec=ssl.SSLContext)
        ctx.wrap_socket = MagicMock(return_value=MagicMock())
        conn = _IpPinnedHTTPSConnection(
            host="example.com",
            resolved_ip="203.0.113.5",
            port=443,
            timeout=5.0,
            context=ctx,
        )

        fake_sock = MagicMock()
        with patch.object(
            validator_mod.socket,
            "create_connection",
            return_value=fake_sock,
        ) as create:
            conn.connect()

        # TCP connect went to the pinned IP, not the hostname.
        ((target,), _kwargs) = create.call_args
        assert target == ("203.0.113.5", 443)
        # TLS wrap used the *hostname* as server_hostname, not the IP.
        ctx.wrap_socket.assert_called_once()
        _args, kwargs = ctx.wrap_socket.call_args
        assert kwargs["server_hostname"] == "example.com"


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


class TestRedirects:
    """§04 "Redirects" — same-origin only, within cap."""

    def test_same_origin_redirect_followed(self) -> None:
        fetcher = StubFetcher(
            responses=[
                _redirect("https://example.com/newpath.ics"),
                _ok(_ics_body()),
            ]
        )
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
            )
        )
        validation = validator.validate("https://example.com/feed.ics")
        assert validation.parseable_ics
        # Second call URL reflects the Location redirect target.
        assert fetcher.calls[1][0] == "https://example.com/newpath.ics"

    def test_cross_origin_redirect_rejected(self) -> None:
        """Different host on the redirect → ``ical_url_cross_origin_redirect``."""
        fetcher = StubFetcher(responses=[_redirect("https://evil.test/feed.ics")])
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate("https://example.com/feed.ics")
        assert exc_info.value.code == "ical_url_cross_origin_redirect"

    def test_scheme_downgrade_redirect_rejected(self) -> None:
        """A redirect to ``http://`` fails the scheme gate."""
        fetcher = StubFetcher(responses=[_redirect("http://example.com/feed.ics")])
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate("https://example.com/feed.ics")
        assert exc_info.value.code == "ical_url_insecure_scheme"

    def test_redirect_cap_exhausted(self) -> None:
        """More than ``max_redirects`` → ``ical_url_unreachable``."""
        # 3 redirects + cap = 2 => should fail on the third redirect.
        fetcher = StubFetcher(
            responses=[
                _redirect("https://example.com/a"),
                _redirect("https://example.com/b"),
                _redirect("https://example.com/c"),
            ]
        )
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
                max_redirects=2,
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate("https://example.com/feed.ics")
        assert exc_info.value.code == "ical_url_unreachable"

    def test_redirect_without_location_rejected(self) -> None:
        """A 302 with no ``Location`` header is garbage upstream."""
        fetcher = StubFetcher(
            responses=[
                FetchResponse(status=302, headers=(), body=b""),
            ]
        )
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate("https://example.com/feed.ics")
        assert exc_info.value.code == "ical_url_unreachable"


# ---------------------------------------------------------------------------
# Body size + timeouts
# ---------------------------------------------------------------------------


class TestSizeCap:
    """§04 "Limits" — 2 MB body cap."""

    def test_body_under_cap_accepted(self) -> None:
        body = _ics_body() + b"X" * (1024 * 100)  # 100 KiB of padding
        fetcher = StubFetcher(responses=[_ok(body)])
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
                max_body_bytes=2 * 1024 * 1024,
            )
        )
        validation = validator.validate("https://example.com/feed.ics")
        assert validation.parseable_ics
        assert validation.bytes_read == len(body)

    def test_fetcher_raises_oversize(self) -> None:
        """The fetcher is what raises; here we check propagation."""

        @dataclass
        class OversizeFetcher(Fetcher):
            def fetch(
                self,
                parsed: SplitResult,
                resolved_ip: str,
                *,
                deadline: float,
                max_body_bytes: int,
            ) -> FetchResponse:
                raise IcalValidationError(
                    "ical_url_oversize", "response body too large: 3MB"
                )

        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=OversizeFetcher(),
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate("https://example.com/feed.ics")
        assert exc_info.value.code == "ical_url_oversize"


class TestTimeout:
    """§04 "Limits" — 10 s deadline."""

    def test_deadline_passed_before_connect(self) -> None:
        """A 0-second deadline raises at the first resolve→fetch step."""

        @dataclass
        class BlockingFetcher(Fetcher):
            def fetch(
                self,
                parsed: SplitResult,
                resolved_ip: str,
                *,
                deadline: float,
                max_body_bytes: int,
            ) -> FetchResponse:
                # The validator passes the deadline through; a fetcher
                # that checks it finds the budget already gone.
                if time.monotonic() >= deadline:
                    raise IcalValidationError(
                        "ical_url_timeout",
                        "deadline exceeded before connect",
                    )
                return _ok(_ics_body())  # pragma: no cover

        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=BlockingFetcher(),
                total_timeout_seconds=0.0,
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate("https://example.com/feed.ics")
        assert exc_info.value.code == "ical_url_timeout"


# ---------------------------------------------------------------------------
# Content-Type handling
# ---------------------------------------------------------------------------


class TestContentType:
    """§04 "Content-Type sniff" — text/calendar + body-sniff fallback."""

    def test_text_calendar_accepted(self) -> None:
        fetcher = StubFetcher(
            responses=[_ok(_ics_body(), content_type="text/calendar")]
        )
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
            )
        )
        validation = validator.validate("https://example.com/feed.ics")
        assert validation.parseable_ics

    def test_text_calendar_with_charset_accepted(self) -> None:
        fetcher = StubFetcher(
            responses=[_ok(_ics_body(), content_type="text/calendar; charset=utf-8")]
        )
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
            )
        )
        validation = validator.validate("https://example.com/feed.ics")
        assert validation.parseable_ics

    def test_octet_stream_with_ics_body_accepted(self) -> None:
        """Providers sometimes send ``application/octet-stream`` with ICS."""
        fetcher = StubFetcher(
            responses=[_ok(_ics_body(), content_type="application/octet-stream")]
        )
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
            )
        )
        validation = validator.validate("https://example.com/feed.ics")
        assert validation.parseable_ics

    def test_non_ics_with_non_allowed_mime_rejected(self) -> None:
        """HTML body with text/html MIME fails ``ical_url_bad_content``."""
        fetcher = StubFetcher(
            responses=[_ok(b"<html>oops</html>", content_type="text/html")]
        )
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate("https://example.com/feed.ics")
        assert exc_info.value.code == "ical_url_bad_content"


# ---------------------------------------------------------------------------
# HTTP-level errors
# ---------------------------------------------------------------------------


class TestHttpErrors:
    """Non-2xx / non-3xx responses collapse to ``ical_url_unreachable``."""

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
    def test_error_statuses_rejected(self, status: int) -> None:
        fetcher = StubFetcher(responses=[_ok(b"", status=status)])
        validator = HttpxIcalValidator(
            IcalValidatorConfig(
                resolver=_fixed_resolver(["1.1.1.1"]),
                fetcher=fetcher,
            )
        )
        with pytest.raises(IcalValidationError) as exc_info:
            validator.validate("https://example.com/feed.ics")
        assert exc_info.value.code == "ical_url_unreachable"


# ---------------------------------------------------------------------------
# Provider auto-detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://www.airbnb.com/calendar/ical/123.ics", "airbnb"),
        ("https://airbnb.com/ical/abc", "airbnb"),
        ("https://www.vrbo.com/icalendar/1234.ics", "vrbo"),
        ("https://www.expedia.com/ical/1234.ics", "vrbo"),
        ("https://www.homeaway.com/ical/1234.ics", "vrbo"),
        ("https://admin.booking.com/hotel/fr/ical.html?t=1", "booking"),
        ("https://calendar.google.com/calendar/ical/abc/public/basic.ics", "gcal"),
        # Subdomain under calendar.google.com also matches gcal.
        ("https://www.calendar.google.com/ical/x", "gcal"),
        ("https://example.com/feed.ics", "generic"),
        ("https://self-hosted.mycal.local/feed.ics", "generic"),
    ],
)
def test_detect_provider(url: str, provider: str) -> None:
    assert detect_provider(url) == provider


class TestDetectProviderLookalikes:
    """Hostname-suffix matching must not confuse look-alike domains."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://airbnb.com.attacker.tld/feed.ics",
            "https://fakeairbnb.com/feed.ics",
            "https://notbooking.com/feed.ics",
        ],
    )
    def test_lookalike_rejected(self, url: str) -> None:
        assert detect_provider(url) == "generic"

    def test_no_host_returns_generic(self) -> None:
        assert detect_provider("not-a-url") == "generic"


class TestHostProviderDetector:
    """The structural wrapper delegates to :func:`detect_provider`."""

    def test_delegates(self) -> None:
        det = HostProviderDetector()
        assert det.detect("https://www.airbnb.com/ical/123.ics") == "airbnb"
        assert det.detect("https://example.test/feed.ics") == "generic"
