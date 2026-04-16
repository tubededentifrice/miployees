/**
 * Locale-aware money formatting. Replaces all hardcoded currency symbols
 * across the mock SPA.
 *
 * Minor-unit counts come from the ISO-4217 static table below; the
 * formatter never hardcodes "divide by 100".
 */

const MINOR_UNITS: Record<string, number> = {
  JPY: 0,
  KRW: 0,
  VND: 0,
  BHD: 3,
  KWD: 3,
  OMR: 3,
};

export function formatMoney(
  minorAmount: number,
  currency: string,
  locale = "en-US",
): string {
  const digits = MINOR_UNITS[currency] ?? 2;
  const value = minorAmount / 10 ** digits;
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}
