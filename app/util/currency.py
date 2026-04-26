"""ISO-4217 currency allow-list + validator (shared kernel).

The set of currencies we accept on a workspace / property / claim is a
single allow-list: a typo (``EURO``, ``UDS``, ``GPB``) must surface at
the boundary instead of corrupting downstream conversions. Keeping
the set in one place means the workspace-settings service, the
property service, and the expense-claim service all agree on what is
and is not valid.

The list intentionally covers the common reserve currencies, every
G20 economy, the GCC + India + Israel + Egypt for the Middle East,
the largest Southeast-Asian and LATAM economies, and the
3-decimal-minor-unit currencies §02 §"Money" calls out (so the
integer-cents convention divides by 1000, not 100). New entries pay a
tiny memory cost; missing entries surface as a hard 422 to real
users.

Coverage rationale: vacation-rental / household-manager workspaces
routinely span North America, Europe, the Gulf, India, LATAM, and
Southeast Asia (a workspace running villas in Bali bills owners in
AUD, pays cleaners in IDR, and reimburses guests in EUR).

A future migration that adds a real ``currency`` table (cd-* TBD)
will collapse this constant into a DB lookup; until then the shared
allow-list stays a frozenset.

See ``docs/specs/02-domain-model.md`` §"Money" /
§"workspaces" / §"Settings cascade".
"""

from __future__ import annotations

__all__ = ["ISO_4217_ALLOWLIST", "is_valid_currency", "normalise_currency"]


# ISO-4217 allow-list. Keep the comment block grouping intact so future
# entries land beside their region.
ISO_4217_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Reserve / G7 currencies.
        "USD",
        "EUR",
        "GBP",
        "CAD",
        "AUD",
        "JPY",
        "CHF",
        "NZD",
        # Nordic.
        "SEK",
        "NOK",
        "DKK",
        "ISK",
        # Central / Eastern Europe.
        "PLN",
        "CZK",
        "HUF",
        "RON",
        "BGN",
        "HRK",
        "TRY",
        # Asia-Pacific finance hubs.
        "SGD",
        "HKD",
        "TWD",
        "KRW",
        "CNY",
        # South + Southeast Asia.
        "INR",
        "IDR",
        "MYR",
        "THB",
        "PHP",
        "VND",
        # Middle East — GCC + Israel + Egypt.
        "AED",
        "SAR",
        "QAR",
        "ILS",
        "EGP",
        # Africa.
        "ZAR",
        # LATAM.
        "MXN",
        "BRL",
        "ARS",
        "CLP",
        "COP",
        "PEN",
        # 3-decimal minor-unit currencies.
        "BHD",
        "JOD",
        "KWD",
        "OMR",
        "TND",
    }
)


def is_valid_currency(value: str) -> bool:
    """Return ``True`` iff ``value`` is a known ISO-4217 alpha-3 code.

    Accepts only canonical (uppercase) codes — callers normalise via
    :func:`normalise_currency` before checking when their input is
    free-form. Returns ``False`` on any shape problem (length, casing,
    non-alpha, non-ASCII, non-allow-list).
    """
    return (
        len(value) == 3
        and value.isascii()
        and value.isalpha()
        and value.isupper()
        and value in ISO_4217_ALLOWLIST
    )


def normalise_currency(value: str) -> str:
    """Return ``value`` upper-cased and stripped, ready for validation.

    Pure shape transform — does not check membership. Combine with
    :func:`is_valid_currency` for the full check.
    """
    return value.strip().upper()
