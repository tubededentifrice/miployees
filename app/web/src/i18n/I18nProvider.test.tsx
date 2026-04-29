import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { I18nProvider, PSEUDO_LOCALE, useI18n } from "@/i18n";

function Probe() {
  const { locale, t } = useI18n();
  return (
    <>
      <div data-testid="locale">{locale}</div>
      <div data-testid="title">{t("login.title")}</div>
    </>
  );
}

describe("I18nProvider", () => {
  it("round-trips qps-ploc from the query string", () => {
    render(
      <I18nProvider
        search="?locale=qps-ploc"
        preferredLocale="en-US"
        navigatorLanguages={["en-US"]}
        workspaceDefaultLocale="en-US"
      >
        <Probe />
      </I18nProvider>,
    );

    const title = screen.getByTestId("title").textContent ?? "";
    expect(screen.getByTestId("locale")).toHaveTextContent(PSEUDO_LOCALE);
    expect(document.documentElement.lang).toBe(PSEUDO_LOCALE);
    expect(document.documentElement.dataset.locale).toBe(PSEUDO_LOCALE);
    expect(title).toMatch(/[íéŵ]/);
    expect(title.length).toBeGreaterThan("Sign in with your passkey".length);
  });
});
