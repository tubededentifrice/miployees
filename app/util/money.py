"""Monetary value object.

Money is an immutable pair of ``(minor_units: int, currency: str)``.
All arithmetic happens in :class:`decimal.Decimal` and rounds half-to-
even to the currency's minor-unit precision at the boundary. Floats
never enter the type.

See ``docs/specs/09-time-payroll-expenses.md`` for the rounding rule
and ``docs/specs/01-architecture.md`` §"Key runtime invariants" for
the context.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

__all__ = ["CurrencyMismatchError", "Money"]


class CurrencyMismatchError(Exception):
    """Raised on arithmetic or comparison between mismatched currencies."""

    def __init__(self, lhs: str, rhs: str) -> None:
        super().__init__(
            f"currency mismatch: {lhs!r} vs {rhs!r}; convert explicitly "
            "before combining"
        )
        self.lhs = lhs
        self.rhs = rhs


# ISO-4217 minor-unit exponents (how many decimal places the currency
# has). Deliberately a small, hand-curated subset — we add entries as
# we need them rather than pulling in the full ``babel`` dependency.
#
# Sources: ISO 4217:2015 + currency issuing central banks.
_EXPONENTS: Final[dict[str, int]] = {
    # 2-decimal currencies (the common case)
    "EUR": 2,
    "USD": 2,
    "GBP": 2,
    "CHF": 2,
    "CAD": 2,
    "AUD": 2,
    "NZD": 2,
    "SEK": 2,
    "NOK": 2,
    "DKK": 2,
    # 0-decimal currencies
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "CLP": 0,
    # 3-decimal currencies
    "BHD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
    "JOD": 3,
}

# Currency symbols for the ``"en"`` locale formatter. Kept minimal on
# purpose — non-listed currencies fall back to the ISO code.
_SYMBOLS_EN: Final[dict[str, str]] = {
    "EUR": "\u20ac",  # €
    "USD": "$",
    "GBP": "\u00a3",  # £
    "JPY": "\u00a5",  # ¥
    "CHF": "CHF ",
    "CAD": "CA$",
    "AUD": "A$",
    "NZD": "NZ$",
}


def _exponent(currency: str) -> int:
    try:
        return _EXPONENTS[currency]
    except KeyError as exc:
        raise ValueError(
            f"unsupported currency {currency!r}; add its minor-unit "
            "exponent to app.util.money._EXPONENTS"
        ) from exc


def _validate_code(currency: str) -> None:
    # ISO-4217 alpha codes are 3 uppercase A-Z letters. ``isupper``
    # alone isn't enough (digits pass that in combination with a cased
    # letter), so we also require ``isalpha`` + ASCII range.
    if (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise ValueError(
            f"invalid ISO-4217 currency code {currency!r}; "
            "expected 3 uppercase ASCII letters"
        )


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """Immutable monetary amount.

    ``amount`` is stored as an integer number of minor units (cents for
    EUR, yen for JPY, fils for BHD). ``currency`` is a 3-letter
    uppercase ISO-4217 code.
    """

    amount: int
    currency: str

    def __post_init__(self) -> None:
        _validate_code(self.currency)
        # Ensures the currency has a known exponent — fail fast at
        # construction instead of at format/multiply time.
        _exponent(self.currency)
        if not isinstance(self.amount, int) or isinstance(self.amount, bool):
            raise TypeError(
                f"Money.amount must be int, got {type(self.amount).__name__}"
            )

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------
    @classmethod
    def zero(cls, currency: str) -> Money:
        """Return ``Money(0, currency)`` with validation."""
        return cls(0, currency)

    @classmethod
    def from_major(cls, major: Decimal, currency: str) -> Money:
        """Build from a major-unit Decimal (e.g. ``Decimal("1.23")``).

        Rounds half-to-even to the currency's minor-unit precision.
        """
        _validate_code(currency)
        exp = _exponent(currency)
        # Scale up to minor units, then snap to an integer with
        # banker's rounding. EUR 1.235 → 123.5 cents → 124 (even).
        minor = (major * (Decimal(10) ** exp)).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
        return cls(int(minor), currency)

    # ------------------------------------------------------------------
    # Arithmetic
    # ------------------------------------------------------------------
    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __mul__(self, factor: int | Decimal) -> Money:
        if isinstance(factor, bool) or not isinstance(factor, int | Decimal):
            raise TypeError(
                "Money can only be multiplied by int or Decimal, "
                f"got {type(factor).__name__}"
            )
        product = (Decimal(self.amount) * Decimal(factor)).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
        return Money(int(product), self.currency)

    __rmul__ = __mul__

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency, other.currency)
        return self.amount == other.amount

    def __hash__(self) -> int:
        return hash((self.amount, self.currency))

    def __lt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount >= other.amount

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------
    def format(self, locale: str = "en") -> str:
        """Render for display.

        Only ``"en"`` is supported today; richer locale support will
        arrive with i18n (§18). We deliberately avoid pulling ``babel``
        in for this minimal case.
        """
        if locale != "en":
            raise ValueError(f"unsupported locale {locale!r}; only 'en' is implemented")

        exp = _exponent(self.currency)
        major = Decimal(self.amount).scaleb(-exp) if exp else Decimal(self.amount)
        # Fix the number of decimal places to the currency's exponent
        # so ``format("EUR", 100) == "€1.00"`` rather than "€1".
        quantized = major.quantize(
            Decimal(1).scaleb(-exp) if exp else Decimal(1),
            rounding=ROUND_HALF_EVEN,
        )
        sign = "-" if quantized < 0 else ""
        abs_str = format(abs(quantized), "f")
        symbol = _SYMBOLS_EN.get(self.currency, f"{self.currency} ")
        return f"{sign}{symbol}{abs_str}"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency, other.currency)
