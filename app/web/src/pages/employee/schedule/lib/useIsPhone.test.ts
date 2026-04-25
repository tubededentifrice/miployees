// Unit tests for the schedule phone-vs-desktop breakpoint hook
// (cd-ops1).
//
// `useIsPhone` watches a `(max-width: 719px)` media query — the same
// breakpoint as `.schedule--phone` / `.schedule--desktop` in CSS.
// We stub `matchMedia` to assert both the initial read and the
// listener registration.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useIsPhone } from "./useIsPhone";

interface FakeMQL {
  matches: boolean;
  listeners: Set<(e: MediaQueryListEvent) => void>;
  addEventListener: (kind: "change", fn: (e: MediaQueryListEvent) => void) => void;
  removeEventListener: (kind: "change", fn: (e: MediaQueryListEvent) => void) => void;
  fire: (matches: boolean) => void;
}

function makeFakeMql(initialMatches: boolean): FakeMQL {
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  const mql: FakeMQL = {
    matches: initialMatches,
    listeners,
    addEventListener: (_kind, fn) => listeners.add(fn),
    removeEventListener: (_kind, fn) => listeners.delete(fn),
    fire: (matches) => {
      mql.matches = matches;
      for (const l of listeners) {
        l({ matches } as MediaQueryListEvent);
      }
    },
  };
  return mql;
}

const originalMatchMedia = window.matchMedia;
let lastMql: FakeMQL | null = null;

beforeEach(() => {
  lastMql = null;
  (window as unknown as { matchMedia: (q: string) => MediaQueryList }).matchMedia = (
    _q: string,
  ) => {
    const m = makeFakeMql(false);
    lastMql = m;
    return m as unknown as MediaQueryList;
  };
});

afterEach(() => {
  (window as unknown as { matchMedia: typeof window.matchMedia }).matchMedia = originalMatchMedia;
  vi.restoreAllMocks();
});

describe("useIsPhone", () => {
  it("returns the matchMedia result on first render", () => {
    (window as unknown as { matchMedia: (q: string) => MediaQueryList }).matchMedia = (
      _q: string,
    ) => {
      const m = makeFakeMql(true);
      lastMql = m;
      return m as unknown as MediaQueryList;
    };
    const { result } = renderHook(() => useIsPhone());
    expect(result.current).toBe(true);
  });

  it("flips when the media query fires a change", () => {
    const { result } = renderHook(() => useIsPhone());
    expect(result.current).toBe(false);
    act(() => {
      lastMql?.fire(true);
    });
    expect(result.current).toBe(true);
    act(() => {
      lastMql?.fire(false);
    });
    expect(result.current).toBe(false);
  });

  it("removes its listener on unmount", () => {
    const { unmount } = renderHook(() => useIsPhone());
    expect(lastMql?.listeners.size).toBe(1);
    unmount();
    expect(lastMql?.listeners.size).toBe(0);
  });
});
