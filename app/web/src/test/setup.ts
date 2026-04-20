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
