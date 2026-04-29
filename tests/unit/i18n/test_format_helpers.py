from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.i18n import format_currency, format_date, format_number


def test_format_currency_uses_locale_symbol_position_and_separators() -> None:
    assert format_currency(Decimal("1234.56"), "EUR", locale="en-US") == "€1,234.56"
    assert format_currency(Decimal("1234.56"), "EUR", locale="fr-FR") == (
        "1\u202f234,56\u00a0€"
    )


def test_format_currency_uses_iso_minor_units() -> None:
    assert format_currency(Decimal("1234"), "JPY", locale="en-US") == "¥1,234"
    assert format_currency(Decimal("1234.567"), "BHD", locale="en-US") == (
        "BHD1,234.567"
    )


def test_format_number_uses_locale_separators() -> None:
    assert format_number(Decimal("1234.56"), locale="en-US") == "1,234.56"
    assert format_number(Decimal("1234.56"), locale="fr-FR") == "1\u202f234,56"


def test_format_date_uses_locale_ordering() -> None:
    value = date(2026, 4, 29)
    assert format_date(value, locale="en-US") == "Apr 29, 2026"
    assert format_date(value, locale="fr-FR") == "29 avr. 2026"
