"""SSRF-guarded iCal URL validator.

Implements :class:`app.adapters.ical.ports.IcalValidator` against the
full §04 "SSRF guard" + §15 "SSRF" contract:

* **Scheme** — ``https://`` only. ``http``, ``file``, ``ftp``, ``data``,
  etc. are rejected with ``ical_url_insecure_scheme``.
* **Host resolution** — every address the host resolves to must be a
  public unicast IP. Loopback / link-local / RFC 1918 / multicast /
  reserved / CGNAT (``100.64.0.0/10``) all trip
  ``ical_url_private_address``. The resolver walks the full
  getaddrinfo result set — a split-horizon DNS that returns one
  public and one private A record is rejected outright.
* **DNS rebinding defeat** — we resolve once, pick a single address
  from the allowed set, and open the TCP connection **directly to
  that IP** via :class:`http.client.HTTPSConnection`. The ``Host:``
  header carries the original hostname so TLS SNI + virtual-host
  routing still work; the kernel never re-resolves between our
  check and the connect. That defeats the classic "CNAME at TTL 0
  flips to 127.0.0.1 on the second lookup" attack.
* **Redirects** — at most 5 follows, same-origin only. A cross-origin
  3xx aborts with ``ical_url_cross_origin_redirect``.
* **Size / timeout** — 2 MB body cap
  (``ical_url_oversize``) + 10 s total deadline
  (``ical_url_timeout``). The deadline covers connect + TLS + request
  + response; the fetcher re-checks remaining time each chunk.
* **Content-Type** — accepts ``text/calendar``, ``text/plain``,
  ``application/calendar+json``, or anything whose first non-blank
  bytes start with ``BEGIN:VCALENDAR`` (some providers send
  ``application/octet-stream``).

Implementation is split into three seams:

* :class:`Resolver` — pluggable DNS resolver (production →
  :func:`_system_resolver`; tests → deterministic stubs).
* :class:`Fetcher` — pluggable HTTPS fetcher that speaks to a pinned
  IP + host (production → :func:`_stdlib_fetch`; tests → stubs that
  never open a socket).
* :class:`HttpxIcalValidator` — orchestrator. Name retained for the
  task brief; the impl uses stdlib :mod:`http.client` because
  DNS-pinning through :mod:`httpx` requires subclassing its
  transport + connection-pool internals (brittle across versions).

See ``docs/specs/04-properties-and-stays.md`` §"SSRF guard",
``docs/specs/15-security-privacy.md`` §"SSRF".
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit, urlunsplit

from app.adapters.ical.ports import IcalValidation, IcalValidationError, IcalValidator

__all__ = [
    "DEFAULT_ALLOWED_CONTENT_TYPES",
    "FetchResponse",
    "Fetcher",
    "HttpxIcalValidator",
    "IcalValidator",
    "IcalValidatorConfig",
    "Resolver",
    "is_public_ip",
    "resolve_public_address",
]

# -----------------------------------------------------------------------------
# Limits
# -----------------------------------------------------------------------------

_MAX_BODY_BYTES = 2 * 1024 * 1024  # §04 — 2 MB probe cap.
_TOTAL_TIMEOUT_SECONDS = 10.0  # §04 — 10 s combined deadline.
_MAX_REDIRECTS = 5  # §04 — "allow max 5" redirect hops.
_DEFAULT_PORT_HTTPS = 443

# Content-Type prefixes we accept without sniffing the body.
DEFAULT_ALLOWED_CONTENT_TYPES: tuple[str, ...] = (
    "text/calendar",
    "text/plain",
    "application/calendar+json",
)

# First bytes we treat as "looks like a VCALENDAR body".
_ICS_MAGIC: bytes = b"BEGIN:VCALENDAR"

# Redirect statuses we honour. A 304 is not a redirect; the caller
# handles conditional requests separately.
_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})


# -----------------------------------------------------------------------------
# Error codes — §04 "SSRF guard" vocabulary
# -----------------------------------------------------------------------------

_CODE_INSECURE_SCHEME = "ical_url_insecure_scheme"
_CODE_PRIVATE_ADDRESS = "ical_url_private_address"
_CODE_CROSS_ORIGIN_REDIRECT = "ical_url_cross_origin_redirect"
_CODE_OVERSIZE = "ical_url_oversize"
_CODE_TIMEOUT = "ical_url_timeout"
_CODE_UNREACHABLE = "ical_url_unreachable"
_CODE_BAD_CONTENT = "ical_url_bad_content"
_CODE_MALFORMED = "ical_url_malformed"


# -----------------------------------------------------------------------------
# Resolver
# -----------------------------------------------------------------------------


Resolver = Callable[[str, int], Iterable[str]]


def _system_resolver(host: str, port: int) -> Iterable[str]:
    """Default resolver — :func:`socket.getaddrinfo`.

    Returns every unique address string ``getaddrinfo`` emits,
    preserving order so a split-horizon DNS can't mask a private
    address by ordering it second.
    """
    try:
        infos = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise IcalValidationError(
            _CODE_UNREACHABLE, f"DNS resolution failed for {host!r}: {exc}"
        ) from exc
    seen: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not isinstance(sockaddr, tuple) or len(sockaddr) < 2:
            continue
        ip = sockaddr[0]
        if isinstance(ip, str) and ip not in seen:
            seen.append(ip)
    return seen


# -----------------------------------------------------------------------------
# Public IP check
# -----------------------------------------------------------------------------


# CGNAT (RFC 6598) — not caught by ``ipaddress.IPv4Address.is_private``;
# we add an explicit CIDR check.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def is_public_ip(ip_str: str) -> bool:
    """Return ``True`` iff ``ip_str`` is a routable public unicast address.

    Rejects loopback, link-local, RFC 1918, multicast, reserved,
    unspecified, and CGNAT. Uses :mod:`ipaddress` where possible
    and augments with an explicit CGNAT CIDR.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_private
    ):
        return False
    # CGNAT (RFC 6598) is classified by the stdlib as "not private"
    # but also "not global" — we treat it as non-public per §04.
    return not (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_V4)


def resolve_public_address(host: str, port: int, *, resolver: Resolver) -> str:
    """Resolve ``host`` and return the first public IP, or raise.

    Rejects the whole lookup if ANY returned address is non-public —
    a mixed result set is the classic DNS-rebinding signal (a later
    re-resolve could easily flip to the private leg).
    """
    addresses = list(resolver(host, port))
    if not addresses:
        raise IcalValidationError(
            _CODE_UNREACHABLE, f"DNS returned no addresses for {host!r}"
        )
    for ip in addresses:
        if not is_public_ip(ip):
            raise IcalValidationError(
                _CODE_PRIVATE_ADDRESS,
                f"host {host!r} resolved to non-public address {ip!r}",
            )
    return addresses[0]


# -----------------------------------------------------------------------------
# Fetcher seam
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Outcome of a single HTTPS GET against a pinned IP.

    ``body`` is capped at the validator's ``max_body_bytes``; the
    fetcher raises :class:`IcalValidationError` with
    ``ical_url_oversize`` rather than ever returning a larger body.
    """

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class Fetcher:
    """Port: issue one HTTPS GET with SSRF-pinned IP + deadline.

    Concrete production impl is :class:`StdlibHttpsFetcher`. Tests
    pass a stub that synthesises :class:`FetchResponse` objects so
    SSRF-rejection paths can be exercised without a real TLS server.
    """

    def fetch(
        self,
        parsed: SplitResult,
        resolved_ip: str,
        *,
        deadline: float,
        max_body_bytes: int,
    ) -> FetchResponse:
        """Open an HTTPS connection to ``resolved_ip`` bearing ``Host:``.

        ``deadline`` is an absolute :func:`time.monotonic` instant.
        ``max_body_bytes`` caps the body; exceeding the cap raises
        :class:`IcalValidationError` with ``ical_url_oversize``.
        """
        raise NotImplementedError  # pragma: no cover


class _IpPinnedHTTPSConnection(http.client.HTTPSConnection):
    """``HTTPSConnection`` that TCP-connects to a pre-resolved IP.

    Stdlib's :class:`http.client.HTTPSConnection` uses ``self.host``
    for both the TCP connect target and the TLS SNI / cert-verify
    server-name. We need to split those: the TCP target is the
    pinned IP (what defeats DNS rebinding) but the TLS handshake
    must still present the original hostname as SNI so the cert
    verifies correctly. We override :meth:`connect` to open the
    socket ourselves against the pinned IP, then wrap it with the
    *original* hostname as ``server_hostname`` so SNI + certificate
    verification both resolve against the caller's intent rather
    than the raw IP (which would fail verification for every real
    public cert).

    The naive approach — "swap ``self.host`` for the super().connect()
    call, restore after" — is a subtle bug: stdlib's
    :meth:`HTTPSConnection.connect` reads ``self.host`` *after* the
    TCP connect (for ``server_hostname``), so the hostname restore
    in a ``finally`` block happens too late to influence the TLS
    wrap. We have to open the socket ourselves.

    We do not support HTTP CONNECT-style tunnels — iCal feeds don't
    route through proxies in this codebase. A tunnel-aware variant
    would need to proxy-CONNECT to the upstream using the *host*
    (not the pinned IP) and re-validate the proxy's resolution
    against the SSRF guard.
    """

    def __init__(
        self,
        *,
        host: str,
        resolved_ip: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host=host, port=port, timeout=timeout, context=context)
        self._resolved_ip = resolved_ip
        # Keep a direct reference so we don't have to touch stdlib's
        # ``_context`` private attribute from :meth:`connect`. Our own
        # attribute survives any future refactor of stdlib internals.
        self._ssl_context = context

    def connect(self) -> None:
        # Open the TCP connection to the pinned IP ourselves; never
        # let stdlib re-resolve ``self.host``. Then wrap the socket
        # with the *original* hostname as ``server_hostname`` so SNI
        # + certificate verification both see the domain name the
        # caller asked for. If we passed the pinned IP to
        # ``wrap_socket`` every request would fail verification
        # because public certs are issued for hostnames, not IPs.
        sock = socket.create_connection(
            (self._resolved_ip, self.port),
            timeout=self.timeout,
        )
        self.sock = self._ssl_context.wrap_socket(sock, server_hostname=self.host)


class StdlibHttpsFetcher(Fetcher):
    """Default :class:`Fetcher` backed by :mod:`http.client` + :mod:`ssl`.

    Connects to the pinned IP via :class:`_IpPinnedHTTPSConnection`.
    TLS SNI + cert verification still work because the original
    hostname is used for the TLS wrap step. The ``Host:`` header
    carries the original hostname for virtual-host routing.
    """

    def fetch(
        self,
        parsed: SplitResult,
        resolved_ip: str,
        *,
        deadline: float,
        max_body_bytes: int,
    ) -> FetchResponse:
        host = parsed.hostname or ""
        port = parsed.port if parsed.port is not None else _DEFAULT_PORT_HTTPS
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IcalValidationError(
                _CODE_TIMEOUT, f"deadline exceeded before connect to {host!r}"
            )
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        conn: http.client.HTTPSConnection = _IpPinnedHTTPSConnection(
            host=host,
            resolved_ip=resolved_ip,
            port=port,
            timeout=remaining,
            context=ctx,
        )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            request_headers = {
                "Host": _host_header(host, port),
                "User-Agent": "crewday-ical-validator/1.0",
                "Accept": "text/calendar, text/plain;q=0.9, */*;q=0.5",
            }
            try:
                conn.request("GET", path, headers=request_headers)
                response = conn.getresponse()
            except TimeoutError as exc:
                raise IcalValidationError(
                    _CODE_TIMEOUT, f"timeout during request to {host!r}"
                ) from exc
            except (http.client.HTTPException, OSError) as exc:
                raise IcalValidationError(
                    _CODE_UNREACHABLE,
                    f"HTTP error talking to {host!r}: {exc}",
                ) from exc
            status = response.status
            resp_headers: tuple[tuple[str, str], ...] = tuple(response.getheaders())
            content_length = _parse_int_header(resp_headers, "Content-Length")
            if content_length is not None and content_length > max_body_bytes:
                raise IcalValidationError(
                    _CODE_OVERSIZE,
                    f"response body too large: {content_length} bytes "
                    f"(cap {max_body_bytes})",
                )
            body = _read_body_capped(response, deadline, max_body_bytes)
            return FetchResponse(status=status, headers=resp_headers, body=body)
        finally:
            conn.close()


def _read_body_capped(
    response: http.client.HTTPResponse, deadline: float, cap: int
) -> bytes:
    """Read up to ``cap`` bytes, failing loudly on oversize / deadline."""
    chunks: list[bytes] = []
    total = 0
    chunk_size = 64 * 1024
    while True:
        if time.monotonic() > deadline:
            raise IcalValidationError(
                _CODE_TIMEOUT, "deadline exceeded while reading response body"
            )
        try:
            # Read one byte past the cap so we can detect "body exceeds
            # the cap" without a Content-Length header.
            chunk = response.read(min(chunk_size, cap - total + 1))
        except TimeoutError as exc:
            raise IcalValidationError(
                _CODE_TIMEOUT, "timeout reading response body"
            ) from exc
        except OSError as exc:
            raise IcalValidationError(_CODE_UNREACHABLE, f"read failed: {exc}") from exc
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise IcalValidationError(
                _CODE_OVERSIZE, f"response body exceeded cap of {cap} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


# -----------------------------------------------------------------------------
# Scheme + origin helpers
# -----------------------------------------------------------------------------


def _require_https(parsed: SplitResult) -> None:
    """Reject any scheme other than ``https``."""
    if parsed.scheme.lower() != "https":
        raise IcalValidationError(
            _CODE_INSECURE_SCHEME,
            f"only https:// URLs are accepted; got {parsed.scheme!r}",
        )


def _origin_of(parsed: SplitResult) -> tuple[str, str, int]:
    """Return ``(scheme, host, port)`` normalised for origin comparison."""
    host = (parsed.hostname or "").lower()
    port = parsed.port if parsed.port is not None else _DEFAULT_PORT_HTTPS
    return (parsed.scheme.lower(), host, port)


# -----------------------------------------------------------------------------
# Validator config + implementation
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IcalValidatorConfig:
    """Tunables for :class:`HttpxIcalValidator`."""

    max_body_bytes: int = _MAX_BODY_BYTES
    total_timeout_seconds: float = _TOTAL_TIMEOUT_SECONDS
    max_redirects: int = _MAX_REDIRECTS
    allowed_content_types: tuple[str, ...] = DEFAULT_ALLOWED_CONTENT_TYPES
    # Resolver hook — exposed so tests can swap in a deterministic
    # lookup (e.g. "first call returns 203.0.113.1, second returns
    # 127.0.0.1" to exercise the DNS-rebind guard).
    resolver: Resolver = field(default=_system_resolver)
    fetcher: Fetcher = field(default_factory=StdlibHttpsFetcher)
    # Test-only escape hatch: allow localhost / private targets.
    # Production default is ``False``; integration tests flip it to
    # ``True`` while pointing at a fake iCal server bound to 127.0.0.1.
    allow_private_addresses: bool = False


class HttpxIcalValidator:
    """Concrete :class:`app.adapters.ical.ports.IcalValidator`.

    Uses stdlib :mod:`http.client` under the hood (see module
    docstring). The class name is kept as ``HttpxIcalValidator`` to
    match the cd-1ai brief; callers depend on the
    :class:`IcalValidator` Protocol and are agnostic to the impl
    choice.
    """

    def __init__(self, config: IcalValidatorConfig | None = None) -> None:
        self._config = config if config is not None else IcalValidatorConfig()

    def validate(self, url: str) -> IcalValidation:
        """Run the §04 validation + probe pipeline on ``url``."""
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise IcalValidationError(
                _CODE_MALFORMED, f"unparseable URL {url!r}: {exc}"
            ) from exc
        if not parsed.scheme:
            raise IcalValidationError(
                _CODE_MALFORMED, f"URL is missing scheme: {url!r}"
            )
        # Scheme gate fires before the host gate — ``file:///x`` has a
        # scheme ``file`` and no host, but we want the specific
        # "insecure scheme" error there rather than "malformed".
        _require_https(parsed)
        if not parsed.hostname:
            raise IcalValidationError(_CODE_MALFORMED, f"URL is missing host: {url!r}")

        deadline = time.monotonic() + self._config.total_timeout_seconds
        current = parsed
        last_response: FetchResponse | None = None
        resolved_ip = ""
        for hop in range(self._config.max_redirects + 1):
            resolved_ip = self._resolve(current)
            response = self._config.fetcher.fetch(
                current,
                resolved_ip,
                deadline=deadline,
                max_body_bytes=self._config.max_body_bytes,
            )
            last_response = response
            if response.status in _REDIRECT_STATUSES:
                if hop >= self._config.max_redirects:
                    raise IcalValidationError(
                        _CODE_UNREACHABLE,
                        f"exceeded redirect cap ({self._config.max_redirects})",
                    )
                location = _first_header(response.headers, "Location")
                if location is None:
                    raise IcalValidationError(
                        _CODE_UNREACHABLE,
                        f"{response.status} redirect had no Location header",
                    )
                target = urlsplit(_resolve_relative(current, location))
                _require_https(target)
                if _origin_of(target) != _origin_of(current):
                    raise IcalValidationError(
                        _CODE_CROSS_ORIGIN_REDIRECT,
                        f"redirect crosses origin: {current.geturl()!r} "
                        f"→ {target.geturl()!r}",
                    )
                current = target
                continue
            if response.status >= 400:
                raise IcalValidationError(
                    _CODE_UNREACHABLE,
                    f"upstream returned HTTP {response.status}",
                )
            break
        else:  # pragma: no cover — loop exits via break or raise above.
            raise IcalValidationError(
                _CODE_UNREACHABLE, "redirect loop did not terminate"
            )

        # ``last_response`` is always set by the loop body above;
        # ``mypy`` needs the explicit narrowing since the loop's exit
        # path isn't obvious to the checker.
        assert last_response is not None
        content_type = _first_header(last_response.headers, "Content-Type")
        parseable = _looks_like_ics(last_response.body, content_type, self._config)
        if not parseable and content_type is not None:
            raise IcalValidationError(
                _CODE_BAD_CONTENT,
                f"unexpected Content-Type {content_type!r} and body is "
                "not a VCALENDAR envelope",
            )

        return IcalValidation(
            url=urlunsplit(parsed),
            resolved_ip=resolved_ip,
            content_type=content_type,
            parseable_ics=parseable,
            bytes_read=len(last_response.body),
        )

    def _resolve(self, parsed: SplitResult) -> str:
        """Return a pinned IP for ``parsed`` or raise."""
        port = parsed.port if parsed.port is not None else _DEFAULT_PORT_HTTPS
        host = parsed.hostname or ""
        addresses = list(self._config.resolver(host, port))
        if not addresses:
            raise IcalValidationError(
                _CODE_UNREACHABLE, f"DNS returned no addresses for {host!r}"
            )
        if self._config.allow_private_addresses:
            return addresses[0]
        for ip in addresses:
            if not is_public_ip(ip):
                raise IcalValidationError(
                    _CODE_PRIVATE_ADDRESS,
                    f"host {host!r} resolved to non-public address {ip!r}",
                )
        return addresses[0]


# -----------------------------------------------------------------------------
# Header / content-sniff helpers
# -----------------------------------------------------------------------------


def _first_header(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    """Return the first header matching ``name`` (case-insensitive)."""
    lowered = name.lower()
    for key, value in headers:
        if key.lower() == lowered:
            return value
    return None


def _parse_int_header(headers: tuple[tuple[str, str], ...], name: str) -> int | None:
    """Return the first ``name`` header parsed as int, or ``None``."""
    raw = _first_header(headers, name)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _host_header(host: str, port: int) -> str:
    """Build the ``Host`` request header from host + port."""
    if port == _DEFAULT_PORT_HTTPS:
        return host
    return f"{host}:{port}"


def _resolve_relative(base: SplitResult, location: str) -> str:
    """Resolve a ``Location`` header against the current URL."""
    parsed = urlsplit(location)
    if parsed.scheme and parsed.netloc:
        return location
    if parsed.netloc and not parsed.scheme:
        return urlunsplit((base.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return urlunsplit(
        (
            base.scheme,
            base.netloc,
            parsed.path or base.path,
            parsed.query,
            "",
        )
    )


def _looks_like_ics(
    body: bytes, content_type: str | None, config: IcalValidatorConfig
) -> bool:
    """Return ``True`` if ``body`` should be treated as an ICS envelope."""
    if content_type is not None:
        mime = content_type.split(";", 1)[0].strip().lower()
        for allowed in config.allowed_content_types:
            if mime == allowed:
                return True
    stripped = body.lstrip()
    return stripped[: len(_ICS_MAGIC)] == _ICS_MAGIC
