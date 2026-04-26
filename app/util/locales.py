"""Shipped BCP-47 locale allow-list + validator (shared kernel).

A workspace's ``default_locale`` (§02 "workspaces") chooses
formatting and the i18n bundle for everything rendered to the
operator UI: number formats, date formats, currency layout. The set
we accept is the **shipped locale list** — the locales we have
translated bundles for and have tested. A typo (``frFR``, ``en_US``)
or an unsupported locale (``ja-JP`` while we don't ship a Japanese
bundle) must surface at the boundary as a 422 with a field path,
rather than corrupting downstream rendering or silently falling back
to ``en``.

Shape rules (BCP-47 narrowed for the v1 surface):

* lowercase language sub-tag (2 letters, ASCII alpha) — ``en``,
  ``fr``, ``de``, …
* optional ``-`` separator + uppercase region sub-tag (2 letters
  or 3 digits) — ``fr-FR``, ``en-US``, ``es-419``.
* nothing else — no script sub-tag, no variant, no extension.
* the resulting tag must appear in :data:`SHIPPED_LOCALES`.

The shipped list intentionally starts narrow: the v1 self-host
audience is overwhelmingly anglophone + francophone, with a thin
slice of Spanish / German / Italian / Portuguese (Iberian + LATAM)
operators. A locale missing from the list surfaces a hard 422; the
remediation is to ship the matching i18n bundle and add the entry.

See ``docs/specs/02-domain-model.md`` §"workspaces" /
§"Settings cascade" and ``docs/specs/14-web-frontend.md``
§"Workspace settings".
"""

from __future__ import annotations

import re

__all__ = [
    "BCP_47_PATTERN",
    "SHIPPED_LOCALES",
    "is_valid_locale",
    "normalise_locale",
]


# Regex for the shape we accept. Anchored at both ends. Region is
# 2 ASCII uppercase letters (ISO-3166-1 alpha-2) OR 3 digits (UN M.49
# region codes — ``es-419`` for Latin-American Spanish is the only
# numeric region we ship).
BCP_47_PATTERN: re.Pattern[str] = re.compile(r"^[a-z]{2}(-[A-Z]{2}|-\d{3})?$")


# Shipped locale set. Ordered by language family in the source for
# review; runtime equality lookup is O(1) via the frozenset.
SHIPPED_LOCALES: frozenset[str] = frozenset(
    {
        # English family.
        "en",
        "en-US",
        "en-GB",
        "en-AU",
        "en-CA",
        "en-NZ",
        # French family.
        "fr",
        "fr-FR",
        "fr-CA",
        "fr-BE",
        "fr-CH",
        # Spanish family — Castilian + LATAM bundle.
        "es",
        "es-ES",
        "es-MX",
        "es-419",
        # German family.
        "de",
        "de-DE",
        "de-AT",
        "de-CH",
        # Italian.
        "it",
        "it-IT",
        # Portuguese — Iberian + Brazilian.
        "pt",
        "pt-PT",
        "pt-BR",
        # Dutch family.
        "nl",
        "nl-NL",
        "nl-BE",
    }
)


def is_valid_locale(value: str) -> bool:
    """Return ``True`` iff ``value`` matches the BCP-47 shape AND is shipped.

    Two-step check: shape via :data:`BCP_47_PATTERN` (cheap), then
    membership in :data:`SHIPPED_LOCALES`. A well-shaped tag we don't
    ship still returns ``False`` so the caller can surface a 422 with
    "locale not supported" rather than letting an untranslated render
    leak through.
    """
    if not BCP_47_PATTERN.match(value):
        return False
    return value in SHIPPED_LOCALES


def normalise_locale(value: str) -> str:
    """Lower-case the language tag + upper-case the region tag.

    Pure shape transform — does not validate against the shipped set.
    Combine with :func:`is_valid_locale` for the full check. Returns
    the input unchanged when no ``-`` separator is present.
    """
    stripped = value.strip()
    if "-" not in stripped:
        return stripped.lower()
    head, _, tail = stripped.partition("-")
    return f"{head.lower()}-{tail.upper()}"
