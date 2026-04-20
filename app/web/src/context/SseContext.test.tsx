import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SseProvider } from "@/context/SseContext";
import { WorkspaceProvider, useWorkspace } from "@/context/WorkspaceContext";
import { type ReactNode, useEffect, useState } from "react";

// Collect every EventSource constructed during a test so we can assert
// on URLs and tear-down. The shape mimics the NoopEventSource polyfill
// in `src/test/setup.ts` but captures the URL and adds a `close` spy.
interface FakeEs {
  url: string;
  withCredentials: boolean;
  closed: boolean;
  readyState: number;
  close: () => void;
  addEventListener: () => void;
  removeEventListener: () => void;
  onopen: (() => void) | null;
  onerror: (() => void) | null;
}

const created: FakeEs[] = [];

class TestEventSource {
  static readonly CLOSED = 2;
  static readonly OPEN = 1;
  readonly url: string;
  readonly withCredentials: boolean;
  closed = false;
  readyState: number = TestEventSource.OPEN;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string, init?: { withCredentials?: boolean }) {
    this.url = url;
    this.withCredentials = init?.withCredentials ?? false;
    created.push(this as unknown as FakeEs);
  }

  close(): void {
    this.closed = true;
  }

  addEventListener(): void {
    /* noop */
  }

  removeEventListener(): void {
    /* noop */
  }
}

const originalEventSource = (globalThis as { EventSource?: unknown }).EventSource;

beforeEach(() => {
  created.length = 0;
  (globalThis as { EventSource: unknown }).EventSource = TestEventSource;
});

afterEach(() => {
  cleanup();
  (globalThis as { EventSource?: unknown }).EventSource = originalEventSource as typeof EventSource;
});

function Providers({ children }: { children: ReactNode }) {
  // Lazy init so the QueryClient is stable across rerenders; an
  // unstable client churns `useQueryClient()` inside `SseProvider`
  // and spuriously reopens the stream, which hides real regressions.
  const [qc] = useState(() => new QueryClient());
  return (
    <QueryClientProvider client={qc}>
      <WorkspaceProvider>
        <SseProvider>{children}</SseProvider>
      </WorkspaceProvider>
    </QueryClientProvider>
  );
}

// Helper that mounts inside WorkspaceProvider so the test can trigger
// a workspace switch via the public hook without reaching into the
// context internals.
function Switcher({ slug }: { slug: string | null }) {
  const { setWorkspaceId } = useWorkspace();
  useEffect(() => {
    if (slug) setWorkspaceId(slug);
  }, [slug, setWorkspaceId]);
  return null;
}

describe("<SseProvider>", () => {
  it("opens /events when no workspace is selected", () => {
    render(
      <Providers>
        <div />
      </Providers>,
    );
    expect(created).toHaveLength(1);
    expect(created[0]!.url).toBe("/events");
    expect(created[0]!.withCredentials).toBe(true);
  });

  it("re-opens /w/<slug>/events when the workspace is selected", () => {
    const { rerender } = render(
      <Providers>
        <Switcher slug={null} />
      </Providers>,
    );
    expect(created).toHaveLength(1);
    expect(created[0]!.url).toBe("/events");

    act(() => {
      rerender(
        <Providers>
          <Switcher slug="acme" />
        </Providers>,
      );
    });

    // The first stream is closed on slug change, and a new one is
    // opened at the workspace-scoped path.
    const scoped = created.find((e) => e.url === "/w/acme/events");
    expect(scoped).toBeDefined();
    expect(created[0]!.closed).toBe(true);
  });

  it("closes the stream on unmount", () => {
    const { unmount } = render(
      <Providers>
        <div />
      </Providers>,
    );
    expect(created).toHaveLength(1);
    expect(created[0]!.closed).toBe(false);

    act(() => {
      unmount();
    });

    expect(created[0]!.closed).toBe(true);
  });

  it("no-ops when EventSource is unavailable", () => {
    // Pre-unmount the default render() by doing nothing here; we
    // simulate a browser that lacks EventSource (old WebView, some
    // tests). The provider must tolerate it without throwing.
    vi.stubGlobal("EventSource", undefined);
    const reset = () => vi.unstubAllGlobals();
    try {
      expect(() =>
        render(
          <Providers>
            <div />
          </Providers>,
        ),
      ).not.toThrow();
      expect(created).toHaveLength(0);
    } finally {
      reset();
    }
  });

  it("reconnects with exponential backoff after a hard close", () => {
    // Fake timers so we can observe the 1s → 2s → 4s ladder without
    // waiting real wall-clock. The provider only schedules a new
    // connect when readyState === CLOSED, so we simulate that before
    // firing onerror.
    vi.useFakeTimers();
    try {
      render(
        <Providers>
          <div />
        </Providers>,
      );
      expect(created).toHaveLength(1);

      // Hard close the first stream.
      const first = created[0]! as unknown as {
        readyState: number;
        onerror: (() => void) | null;
      };
      first.readyState = TestEventSource.CLOSED;
      act(() => {
        first.onerror?.();
      });

      // Nothing should reconnect until the first 1s backoff elapses.
      expect(created).toHaveLength(1);
      act(() => {
        vi.advanceTimersByTime(1_000);
      });
      expect(created).toHaveLength(2);

      // Second drop — backoff should now be 2s.
      const second = created[1]! as unknown as {
        readyState: number;
        onerror: (() => void) | null;
      };
      second.readyState = TestEventSource.CLOSED;
      act(() => {
        second.onerror?.();
      });
      act(() => {
        vi.advanceTimersByTime(1_000);
      });
      expect(created).toHaveLength(2); // still waiting
      act(() => {
        vi.advanceTimersByTime(1_000);
      });
      expect(created).toHaveLength(3); // 2s elapsed
    } finally {
      vi.useRealTimers();
    }
  });

  it("resets the backoff ladder after a successful open", () => {
    // After onopen fires, the next drop should wait 1s again — not
    // continue climbing the previous ladder. This prevents a
    // long-running session from taking 30s to recover from a brief
    // blip.
    vi.useFakeTimers();
    try {
      render(
        <Providers>
          <div />
        </Providers>,
      );
      const first = created[0]! as unknown as {
        readyState: number;
        onopen: (() => void) | null;
        onerror: (() => void) | null;
      };

      // Climb one rung (drop → reconnect at 1s → backoff now 2s).
      first.readyState = TestEventSource.CLOSED;
      act(() => {
        first.onerror?.();
      });
      act(() => {
        vi.advanceTimersByTime(1_000);
      });
      expect(created).toHaveLength(2);

      // Second stream opens successfully → ladder resets.
      const second = created[1]! as unknown as {
        readyState: number;
        onopen: (() => void) | null;
        onerror: (() => void) | null;
      };
      act(() => {
        second.onopen?.();
      });

      // Now drop — should reconnect at 1s (not 2s).
      second.readyState = TestEventSource.CLOSED;
      act(() => {
        second.onerror?.();
      });
      act(() => {
        vi.advanceTimersByTime(1_000);
      });
      expect(created).toHaveLength(3);
    } finally {
      vi.useRealTimers();
    }
  });
});

// Keep a tiny acceptance probe for RoleContext / ThemeContext /
// WorkspaceContext so accidental shape drift fails a test, not a
// downstream layout typecheck. The hooks throw if used outside their
// provider, which is the contract layouts rely on.
import { useRole, RoleProvider } from "@/context/RoleContext";
import { useTheme, ThemeProvider } from "@/context/ThemeContext";
import { renderHook } from "@testing-library/react";

describe("context hooks — shape regression", () => {
  it("useRole exposes { role, setRole } inside <RoleProvider>", () => {
    const { result } = renderHook(() => useRole(), {
      wrapper: ({ children }) => <RoleProvider>{children}</RoleProvider>,
    });
    expect(result.current.role).toBeDefined();
    expect(typeof result.current.setRole).toBe("function");
  });

  it("useTheme exposes { theme, resolved, setTheme, toggle } inside <ThemeProvider>", () => {
    const { result } = renderHook(() => useTheme(), {
      wrapper: ({ children }) => <ThemeProvider>{children}</ThemeProvider>,
    });
    expect(result.current.theme).toBeDefined();
    expect(["light", "dark"]).toContain(result.current.resolved);
    expect(typeof result.current.setTheme).toBe("function");
    expect(typeof result.current.toggle).toBe("function");
  });

  it("useWorkspace exposes { workspaceId, setWorkspaceId } inside <WorkspaceProvider>", () => {
    const qc = new QueryClient();
    const { result } = renderHook(() => useWorkspace(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={qc}>
          <WorkspaceProvider>{children}</WorkspaceProvider>
        </QueryClientProvider>
      ),
    });
    // `workspaceId` is `string | null`; the placeholder cookie reader
    // returns null, so this is the baseline for the scaffold.
    expect(result.current.workspaceId).toBeNull();
    expect(typeof result.current.setWorkspaceId).toBe("function");
  });
});
