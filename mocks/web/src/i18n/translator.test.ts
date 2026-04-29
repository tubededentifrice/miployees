import { describe, expect, it } from "vitest";
import { DEFAULT_LOCALE, PSEUDO_LOCALE, createTranslator, t, type MessageKey } from "@/i18n";

describe("translator", () => {
  it("resolves the English login title", () => {
    expect(t("login.title")).toBe("Sign in with your passkey");
  });

  it("interpolates typed parameters", () => {
    expect(createTranslator(DEFAULT_LOCALE)("i18n.testGreeting", { name: "Ana" })).toBe("Hello, Ana!");
  });

  it("throws for missing keys in dev mode", () => {
    const devT = createTranslator(DEFAULT_LOCALE, { missingKeyMode: "throw" });
    expect(() => devT("missing.key" as MessageKey)).toThrow("Missing i18n key: missing.key");
  });

  it("returns the key for missing keys in prod mode", () => {
    const prodT = createTranslator(DEFAULT_LOCALE, { missingKeyMode: "return-key" });
    expect(prodT("missing.key" as MessageKey)).toBe("missing.key");
  });

  it("accent-stresses and inflates the pseudo-locale catalog", () => {
    const english = t("login.title");
    const pseudo = createTranslator(PSEUDO_LOCALE)("login.title");

    expect(pseudo).not.toBe(english);
    expect(pseudo.length).toBeGreaterThan(english.length);
    expect(pseudo).toMatch(/[íéŵ]/);
  });

  it("keeps placeholders intact before interpolation", () => {
    expect(createTranslator(PSEUDO_LOCALE)("i18n.testGreeting", { name: "Ana" })).toContain("Ana");
  });
});
