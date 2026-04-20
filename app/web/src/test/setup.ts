import "@testing-library/jest-dom/vitest";

// Polyfill EventSource in jsdom so SseProvider can mount in tests
// without crashing. Real SSE is covered by Playwright.
if (typeof (globalThis as { EventSource?: unknown }).EventSource === "undefined") {
  class NoopEventSource {
    close(): void { /* noop */ }
    addEventListener(): void { /* noop */ }
    removeEventListener(): void { /* noop */ }
  }
  (globalThis as { EventSource: unknown }).EventSource = NoopEventSource;
}

// Polyfill matchMedia in jsdom so ThemeProvider can resolve
// "system" → "light"/"dark" without crashing. Prefers-color-scheme
// is never truly exercised in tests (jsdom has no real user agent);
// the stub returns a permanent "light" match so `systemTheme()` is
// deterministic.
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList;
}
