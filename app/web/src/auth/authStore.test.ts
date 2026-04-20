import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __resetAuthStoreForTests,
  getAuthState,
  setAuthenticated,
  setAuthToken,
  setLoading,
  setUnauthenticated,
  subscribeAuth,
} from "./authStore";
import type { AuthMe } from "./types";

const SAMPLE_USER: AuthMe = {
  user_id: "01HZ...USER",
  display_name: "Ada Lovelace",
  email: "ada@example.com",
  available_workspaces: [],
  current_workspace_id: null,
};

const TOKEN_STORAGE_KEY = "crewday_auth_token";

beforeEach(() => {
  __resetAuthStoreForTests();
  // jsdom persists sessionStorage across files — clear it explicitly so
  // the "init from storage" test owns the seed value.
  sessionStorage.clear();
});

afterEach(() => {
  __resetAuthStoreForTests();
  sessionStorage.clear();
});

describe("authStore — initial state", () => {
  it("starts in `loading` with no user and no token", () => {
    const s = getAuthState();
    expect(s.status).toBe("loading");
    expect(s.user).toBeNull();
    expect(s.token).toBeNull();
  });

  it("seeds the token from sessionStorage when the module is freshly evaluated", async () => {
    // The module reads sessionStorage on first evaluation. We can't
    // re-evaluate `./authStore` mid-test (ES module caches are
    // per-process), so instead we exercise the same code path
    // through `vi.resetModules()` + a fresh `import()`.
    sessionStorage.setItem(TOKEN_STORAGE_KEY, "mip_seeded");
    vi.resetModules();
    const fresh: typeof import("./authStore") = await import("./authStore");
    expect(fresh.getAuthState().token).toBe("mip_seeded");
    fresh.__resetAuthStoreForTests();
  });
});

describe("authStore — transitions", () => {
  it("setAuthenticated populates the user and emits to subscribers", () => {
    const seen: string[] = [];
    const unsub = subscribeAuth((s) => seen.push(s.status));
    setAuthenticated(SAMPLE_USER);
    expect(getAuthState().status).toBe("authenticated");
    expect(getAuthState().user).toEqual(SAMPLE_USER);
    expect(seen).toContain("authenticated");
    unsub();
  });

  it("setUnauthenticated clears the user, the token, and the persisted token", () => {
    setAuthenticated(SAMPLE_USER, "mip_token");
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe("mip_token");

    setUnauthenticated();
    expect(getAuthState().status).toBe("unauthenticated");
    expect(getAuthState().user).toBeNull();
    expect(getAuthState().token).toBeNull();
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("setLoading does not blank the existing user (re-probe stays quiet on the chrome)", () => {
    setAuthenticated(SAMPLE_USER);
    setLoading();
    expect(getAuthState().status).toBe("loading");
    // User stays so a refresh doesn't flicker the user menu.
    expect(getAuthState().user).toEqual(SAMPLE_USER);
  });

  it("setAuthToken persists without changing status", () => {
    setAuthenticated(SAMPLE_USER);
    setAuthToken("mip_pat");
    expect(getAuthState().status).toBe("authenticated");
    expect(getAuthState().token).toBe("mip_pat");
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe("mip_pat");
  });

  it("setAuthToken(null) clears the persisted token", () => {
    setAuthToken("mip_pat");
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBe("mip_pat");
    setAuthToken(null);
    expect(getAuthState().token).toBeNull();
    expect(sessionStorage.getItem(TOKEN_STORAGE_KEY)).toBeNull();
  });

  it("does not emit when nothing meaningful changed (idempotent unauthenticated)", () => {
    setUnauthenticated();
    let count = 0;
    const unsub = subscribeAuth(() => { count += 1; });
    setUnauthenticated();
    setUnauthenticated();
    expect(count).toBe(0);
    unsub();
  });

  it("subscribeAuth returns a working unsubscribe function", () => {
    let count = 0;
    const unsub = subscribeAuth(() => { count += 1; });
    setAuthenticated(SAMPLE_USER);
    expect(count).toBe(1);
    unsub();
    setUnauthenticated();
    expect(count).toBe(1);
  });

  it("survives a wedged sessionStorage on persist (Safari private mode)", () => {
    // Simulate a setItem that throws — the store must keep its
    // in-memory token even though persistence failed.
    const orig = Storage.prototype.setItem;
    Storage.prototype.setItem = (): never => { throw new Error("QuotaExceeded"); };
    try {
      expect(() => setAuthToken("mip_token_in_wedged_storage")).not.toThrow();
      expect(getAuthState().token).toBe("mip_token_in_wedged_storage");
    } finally {
      Storage.prototype.setItem = orig;
    }
  });

  it("survives a wedged sessionStorage on init-time getItem (sandboxed iframe / sec-restricted)", async () => {
    // Some environments (sandboxed iframes, Safari's tracking-
    // protection modes) surface `sessionStorage` as a getter that
    // throws SecurityError on access. The module must keep its
    // in-memory state clean instead of crashing at import time.
    const origGetItem = Storage.prototype.getItem;
    Storage.prototype.getItem = (): never => { throw new Error("SecurityError"); };
    try {
      vi.resetModules();
      const fresh: typeof import("./authStore") = await import("./authStore");
      const s = fresh.getAuthState();
      expect(s.status).toBe("loading");
      expect(s.token).toBeNull();
      fresh.__resetAuthStoreForTests();
    } finally {
      Storage.prototype.getItem = origGetItem;
    }
  });
});
