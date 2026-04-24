"""Provider auto-detection from URL host.

§04 "Supported providers" lists four canonical channels plus a
generic fallback. The detector is a cheap hostname match — we
do not read the body, since the domain layer's provider-override
knob lets the operator nail the slug when auto-detect guesses
wrong.

Table:

* ``*.airbnb.com``                         → ``airbnb``
* ``*.vrbo.com`` / ``*.homeaway.com`` /
  ``*.expedia.com``                        → ``vrbo``
* ``*.booking.com``                        → ``booking``
* ``*.calendar.google.com`` /
  ``calendar.google.com``                  → ``gcal``
* anything else                            → ``generic``

The ``vrbo`` bucket aliases Expedia Partner-hosted feeds because the
§04 "Supported providers" list explicitly groups them — Expedia
owns VRBO / HomeAway and their exported ICS feeds share the same
shape.

See ``docs/specs/04-properties-and-stays.md`` §"Supported providers".
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.adapters.ical.ports import IcalProvider

__all__ = ["HostProviderDetector", "detect_provider"]


# Matching is by **suffix** on the lowercased host so ``foo.airbnb.com``
# and ``airbnb.com`` both hit, but a look-alike like
# ``airbnb.com.attacker.tld`` does not (the attacker-controlled host
# doesn't end in ``.airbnb.com``). Each entry is ``(suffix, provider)``
# — order matters only for disjoint suffixes; we use a tuple so the
# sequence stays stable for tests.
_PROVIDER_SUFFIXES: tuple[tuple[str, IcalProvider], ...] = (
    ("calendar.google.com", "gcal"),
    ("airbnb.com", "airbnb"),
    ("vrbo.com", "vrbo"),
    ("homeaway.com", "vrbo"),
    ("expedia.com", "vrbo"),
    ("booking.com", "booking"),
)


def detect_provider(url: str) -> IcalProvider:
    """Return the canonical provider slug for ``url``.

    Falls back to ``"generic"`` for unknown hosts. Safe to call on a
    URL that hasn't been validated yet — the function does not
    resolve DNS or connect to anything; it's purely a hostname
    string match.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return "generic"
    for suffix, provider in _PROVIDER_SUFFIXES:
        # ``host == suffix`` matches the bare apex; ``host.endswith(
        # "." + suffix)`` matches any subdomain. Combining them as a
        # single guarded endswith is tempting but subtly wrong on
        # ``x.com`` vs ``a.xx.com`` boundaries — the explicit apex
        # check keeps intent obvious.
        if host == suffix or host.endswith("." + suffix):
            return provider
    return "generic"


class HostProviderDetector:
    """Structural implementation of :class:`ProviderDetector`.

    Exists as a thin class so tests that want to stub the port with
    a table-driven detector can pass a different object of the same
    shape. Production wires this class directly.
    """

    def detect(self, url: str) -> IcalProvider:
        return detect_provider(url)
