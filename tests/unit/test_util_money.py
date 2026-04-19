"""Tests for :mod:`app.util.money`."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.util.money import CurrencyMismatchError, Money


class TestConstruction:
    def test_happy_path(self) -> None:
        m = Money(100, "EUR")
        assert m.amount == 100
        assert m.currency == "EUR"

    @pytest.mark.parametrize(
        "code",
        ["eur", "EU", "EURO", "E1R", "EU R", "", "€€€"],
    )
    def test_rejects_invalid_currency(self, code: str) -> None:
        with pytest.raises(ValueError, match="ISO-4217"):
            Money(100, code)

    def test_rejects_unknown_currency(self) -> None:
        with pytest.raises(ValueError, match="unsupported currency"):
            Money(100, "XYZ")

    def test_rejects_non_int_amount(self) -> None:
        with pytest.raises(TypeError, match="must be int"):
            Money(Decimal("1.50"), "EUR")  # type: ignore[arg-type]

    def test_rejects_bool_amount(self) -> None:
        with pytest.raises(TypeError, match="must be int"):
            Money(True, "EUR")  # type: ignore[arg-type]

    def test_is_frozen(self) -> None:
        m = Money(100, "EUR")
        with pytest.raises(FrozenInstanceError):
            m.amount = 200  # type: ignore[misc]


class TestHelpers:
    def test_zero(self) -> None:
        assert Money.zero("EUR") == Money(0, "EUR")

    def test_from_major_two_decimals(self) -> None:
        assert Money.from_major(Decimal("1.23"), "EUR") == Money(123, "EUR")

    def test_from_major_rounds_half_even(self) -> None:
        # 1.005 EUR → 100.5 cents → rounds to 100 (even), not 101.
        assert Money.from_major(Decimal("1.005"), "EUR") == Money(100, "EUR")
        # 1.015 EUR → 101.5 cents → rounds to 102 (even).
        assert Money.from_major(Decimal("1.015"), "EUR") == Money(102, "EUR")

    def test_from_major_zero_decimal_currency(self) -> None:
        assert Money.from_major(Decimal("1000"), "JPY") == Money(1000, "JPY")

    def test_from_major_three_decimal_currency(self) -> None:
        # BHD has 3 minor-unit digits, so 1.234 BHD == 1234 fils.
        assert Money.from_major(Decimal("1.234"), "BHD") == Money(1234, "BHD")


class TestArithmetic:
    def test_addition(self) -> None:
        assert Money(100, "EUR") + Money(200, "EUR") == Money(300, "EUR")

    def test_subtraction(self) -> None:
        assert Money(300, "EUR") - Money(100, "EUR") == Money(200, "EUR")

    def test_negation(self) -> None:
        assert -Money(100, "EUR") == Money(-100, "EUR")

    def test_addition_across_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError) as exc:
            _ = Money(100, "EUR") + Money(100, "USD")
        assert exc.value.lhs == "EUR"
        assert exc.value.rhs == "USD"

    def test_subtraction_across_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            _ = Money(100, "EUR") - Money(100, "USD")

    def test_multiplication_by_int(self) -> None:
        assert Money(100, "EUR") * 3 == Money(300, "EUR")

    def test_multiplication_by_decimal_rounds_half_even(self) -> None:
        # 100 cents * 1.005 = 100.5 → rounds to 100 (even).
        assert Money(100, "EUR") * Decimal("1.005") == Money(100, "EUR")
        # 101 cents * 1.005 = 101.505 → rounds to 102 (even).
        assert Money(101, "EUR") * Decimal("1.005") == Money(102, "EUR")

    def test_right_multiplication(self) -> None:
        assert 3 * Money(100, "EUR") == Money(300, "EUR")

    def test_multiplication_rejects_float(self) -> None:
        with pytest.raises(TypeError, match="int or Decimal"):
            _ = Money(100, "EUR") * 1.5  # type: ignore[operator]

    def test_multiplication_rejects_bool(self) -> None:
        with pytest.raises(TypeError, match="int or Decimal"):
            _ = Money(100, "EUR") * True  # type: ignore[operator]


class TestComparison:
    def test_equality_same_currency(self) -> None:
        assert Money(100, "EUR") == Money(100, "EUR")
        assert Money(100, "EUR") != Money(200, "EUR")

    def test_equality_different_type_is_not_equal(self) -> None:
        # A Money is never equal to a non-Money, and no exception is raised.
        assert Money(100, "EUR") != 100
        assert Money(100, "EUR") != "EUR 100"

    def test_equality_cross_currency_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            _ = Money(100, "EUR") == Money(100, "USD")

    def test_ordering_same_currency(self) -> None:
        small = Money(100, "EUR")
        big = Money(200, "EUR")
        assert small < big
        assert small <= big
        assert big > small
        assert big >= small
        assert small <= Money(100, "EUR")
        assert small >= Money(100, "EUR")

    def test_ordering_cross_currency_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            _ = Money(100, "EUR") < Money(100, "USD")
        with pytest.raises(CurrencyMismatchError):
            _ = Money(100, "EUR") <= Money(100, "USD")
        with pytest.raises(CurrencyMismatchError):
            _ = Money(100, "EUR") > Money(100, "USD")
        with pytest.raises(CurrencyMismatchError):
            _ = Money(100, "EUR") >= Money(100, "USD")

    def test_hash_matches_equality(self) -> None:
        a = Money(100, "EUR")
        b = Money(100, "EUR")
        assert hash(a) == hash(b)
        assert {a, b} == {a}

    def test_hash_distinguishes_currency(self) -> None:
        # Hashes should (almost surely) differ across currencies so
        # mismatched entries don't collide in a dict keyed on Money.
        assert hash(Money(100, "EUR")) != hash(Money(100, "USD"))


class TestFormat:
    def test_eur_two_decimals(self) -> None:
        assert Money(100, "EUR").format() == "\u20ac1.00"
        assert Money(12345, "EUR").format() == "\u20ac123.45"

    def test_usd(self) -> None:
        assert Money(100, "USD").format() == "$1.00"

    def test_gbp(self) -> None:
        assert Money(100, "GBP").format() == "\u00a31.00"

    def test_jpy_zero_decimals(self) -> None:
        assert Money(1000, "JPY").format() == "\u00a51000"

    def test_negative_amount(self) -> None:
        assert Money(-100, "EUR").format() == "-\u20ac1.00"

    def test_fallback_for_unknown_symbol(self) -> None:
        # SEK has a 2-decimal exponent but no symbol mapping — the
        # formatter should fall back to the ISO code.
        assert Money(100, "SEK").format() == "SEK 1.00"

    def test_rejects_unknown_locale(self) -> None:
        with pytest.raises(ValueError, match="unsupported locale"):
            Money(100, "EUR").format(locale="fr")
