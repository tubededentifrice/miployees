"""iCal adapter ports.

Two Protocols the domain layer (``app.domain.stays.ical_service``)
consumes:

* :class:`IcalValidator` — given a URL, run the §04 SSRF guard +
  reachability probe, and return a :class:`IcalValidation` summary
  that distinguishes "URL looks OK" from "URL looks OK and an ICS
  body came back". The domain uses the distinction to flip
  ``ical_feed.enabled`` only when we've actually parsed a VCALENDAR
  envelope.
* :class:`ProviderDetector` — given a URL host, return the canonical
  provider slug. A separate port because tests want to exercise the
  service with a deterministic detector without going through URL
  parsing.

Both are plain Protocols (structural subtyping); concrete adapters
live under ``app.adapters.ical.validator`` and
``app.adapters.ical.providers``.

See ``docs/specs/04-properties-and-stays.md`` §"iCal feed" / §"SSRF
guard" / §"Supported providers".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = [
    "IcalProvider",
    "IcalValidation",
    "IcalValidationError",
    "IcalValidator",
    "ProviderDetector",
]


# Canonical provider slugs. ``custom`` covers the §04 "Generic ICS"
# fallback + the Google Calendar bucket; the v1 ``ical_feed.provider``
# CHECK constraint only allows ``airbnb | vrbo | booking | custom``,
# so the domain layer collapses ``gcal`` → ``custom`` when it lands a
# row (noted in a Beads follow-up — see the docstring on
# :mod:`app.domain.stays.ical_service`).
IcalProvider = Literal["airbnb", "vrbo", "booking", "gcal", "generic"]


class IcalValidationError(Exception):
    """Raised when URL validation fails.

    ``code`` mirrors §04's ``ical_url_*`` error-code vocabulary so
    the domain layer can persist it verbatim in ``last_error`` for
    operator-facing UI. The message is free-form and may carry a
    redacted URL fragment; callers that log must route the message
    through the redactor.
    """

    __slots__ = ("code",)

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IcalValidation:
    """Result of a successful URL validation + probe.

    ``url`` is the canonicalised URL the validator intends to poll
    going forward (trailing-slash / IDN normalisation applied).
    ``parseable_ics`` is ``True`` iff the probe body started with
    ``BEGIN:VCALENDAR`` (or otherwise declared itself as text/
    calendar with a plausible body). Only then does the domain
    flip ``enabled`` on the feed.
    """

    url: str
    resolved_ip: str
    content_type: str | None
    parseable_ics: bool
    bytes_read: int


class IcalValidator(Protocol):
    """Port: SSRF-guarded URL validation + probe.

    The concrete implementation must:

    1. Reject non-``https`` schemes (``ical_url_insecure_scheme``).
    2. Resolve the host and reject private / loopback / link-local /
       multicast / reserved / CGNAT addresses
       (``ical_url_private_address``).
    3. Pin the resolved IP through the TCP connection to defeat
       DNS rebinding between resolve and connect.
    4. Enforce a 2 MB body cap (``ical_url_oversize``) and a 10 s
       total timeout (``ical_url_timeout``).
    5. Follow at most 5 same-origin redirects; reject cross-origin
       redirects (``ical_url_cross_origin_redirect``).
    6. Accept ``text/calendar``, ``text/plain``,
       ``application/calendar+json``, or anything whose first bytes
       look like ``BEGIN:VCALENDAR``.

    Raises :class:`IcalValidationError` with the appropriate
    ``code`` on any failure; returns :class:`IcalValidation` on
    success.
    """

    def validate(self, url: str) -> IcalValidation:
        """Validate ``url`` and probe for a reachable ICS body."""
        ...


class ProviderDetector(Protocol):
    """Port: auto-detect the provider slug from a URL."""

    def detect(self, url: str) -> IcalProvider:
        """Return the canonical provider slug for ``url``."""
        ...
