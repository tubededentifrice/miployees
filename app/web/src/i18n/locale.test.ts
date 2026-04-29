import { describe, expect, it } from "vitest";
import { DEFAULT_LOCALE, PSEUDO_LOCALE, resolveLocale, toSupportedLocale } from "@/i18n";

describe("locale negotiation", () => {
  it("normalizes supported locale tags", () => {
    expect(toSupportedLocale("en")).toBe(DEFAULT_LOCALE);
    expect(toSupportedLocale("en_GB")).toBe(DEFAULT_LOCALE);
    expect(toSupportedLocale("qps-ploc")).toBe(PSEUDO_LOCALE);
    expect(toSupportedLocale("fr-FR")).toBeNull();
  });

  it.each([
    {
      name: "query pseudo-locale",
      input: {
        search: "?locale=qps-ploc",
        preferredLocale: "en-US",
        navigatorLanguages: ["fr-FR"],
        workspaceDefaultLocale: "en-US",
      },
      expected: PSEUDO_LOCALE,
    },
    {
      name: "preferred locale",
      input: {
        search: "",
        preferredLocale: "en-US",
        navigatorLanguages: ["fr-FR"],
        workspaceDefaultLocale: "en-US",
      },
      expected: DEFAULT_LOCALE,
    },
    {
      name: "navigator languages",
      input: {
        search: "",
        preferredLocale: null,
        navigatorLanguages: ["fr-FR", "en-GB"],
        workspaceDefaultLocale: "en-US",
      },
      expected: DEFAULT_LOCALE,
    },
    {
      name: "workspace default",
      input: {
        search: "",
        preferredLocale: null,
        navigatorLanguages: ["fr-FR"],
        workspaceDefaultLocale: "en-US",
      },
      expected: DEFAULT_LOCALE,
    },
    {
      name: "final fallback",
      input: {
        search: "",
        preferredLocale: null,
        navigatorLanguages: ["fr-FR"],
        workspaceDefaultLocale: "fr-FR",
      },
      expected: DEFAULT_LOCALE,
    },
  ])("uses the $name precedence step", ({ input, expected }) => {
    expect(resolveLocale(input)).toBe(expected);
  });
});
