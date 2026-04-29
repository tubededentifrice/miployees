export const DEFAULT_LOCALE = "en-US";
export const PSEUDO_LOCALE = "qps-ploc";

export const SUPPORTED_LOCALES = [DEFAULT_LOCALE, PSEUDO_LOCALE] as const;

export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number];

export interface LocaleResolutionInput {
  search?: string;
  preferredLocale?: string | null;
  navigatorLanguages?: readonly string[];
  workspaceDefaultLocale?: string | null;
}

function currentSearch(): string {
  return typeof window === "undefined" ? "" : window.location.search;
}

function currentNavigatorLanguages(): readonly string[] {
  if (typeof navigator === "undefined") return [];
  return navigator.languages.length > 0 ? navigator.languages : [navigator.language];
}

export function toSupportedLocale(value: string | null | undefined): SupportedLocale | null {
  if (!value) return null;
  const normalized = value.trim().replaceAll("_", "-").toLowerCase();
  if (normalized === PSEUDO_LOCALE) return PSEUDO_LOCALE;
  if (normalized === "en" || normalized.startsWith("en-")) return DEFAULT_LOCALE;
  return null;
}

export function resolveLocale(input: LocaleResolutionInput = {}): SupportedLocale {
  const params = new URLSearchParams(input.search ?? currentSearch());
  const queryLocale = toSupportedLocale(params.get("locale"));
  if (queryLocale) return queryLocale;

  const preferredLocale = toSupportedLocale(input.preferredLocale);
  if (preferredLocale) return preferredLocale;

  const languages = input.navigatorLanguages ?? currentNavigatorLanguages();
  for (const language of languages) {
    const locale = toSupportedLocale(language);
    if (locale) return locale;
  }

  return toSupportedLocale(input.workspaceDefaultLocale) ?? DEFAULT_LOCALE;
}
