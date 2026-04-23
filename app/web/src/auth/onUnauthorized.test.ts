import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { createOnUnauthorized, isAuthEndpoint, sanitizeNext } from "./onUnauthorized";
import {
  __resetAuthStoreForTests,
  getAuthState,
  setAuthenticated,
} from "./authStore";
import type { AuthMe } from "./types";

const SAMPLE_USER: AuthMe = {
  user_id: "01HZ...USER",
  display_name: "Bea",
  email: "bea@example.com",
  available_workspaces: [],
  current_workspace_id: null,
};

beforeEach(() => {
  __resetAuthStoreForTests();
});

afterEach(() => {
  __resetAuthStoreForTests();
});

describe("isAuthEndpoint", () => {
  it("classifies anonymous-OK auth endpoints as 'do not redirect'", () => {
    expect(isAuthEndpoint("/api/v1/auth/me")).toBe(true);
    expect(isAuthEndpoint("/api/v1/auth/logout")).toBe(true);
    expect(isAuthEndpoint("/api/v1/auth/passkey/login/start")).toBe(true);
    expect(isAuthEndpoint("/api/v1/auth/passkey/login/finish")).toBe(true);
    // Canonical signup passkey ceremony is mounted under /api/v1/signup/passkey/*
    // (§03 "Self-serve signup"; the retired /auth/passkey/signup/register/*
    // parallel surface was removed per cd-ju0q).
    expect(isAuthEndpoint("/api/v1/signup/passkey/start")).toBe(true);
    expect(isAuthEndpoint("/api/v1/signup/passkey/finish")).toBe(true);
    expect(isAuthEndpoint("/api/v1/auth/recover/start")).toBe(true);
    expect(isAuthEndpoint("/api/v1/auth/magic/send")).toBe(true);
    expect(isAuthEndpoint("/api/v1/signup/start")).toBe(true);
    expect(isAuthEndpoint("/api/v1/invites/01HZ_TOK")).toBe(true);
    expect(isAuthEndpoint("/api/v1/invites/01HZ_TOK/accept")).toBe(true);
  });

  it("redirects when a 401 hits an AUTHENTICATED passkey-register endpoint (session expired mid-ceremony)", () => {
    // The `add-another-passkey` flow targets a logged-in user — a 401
    // means the cookie is stale, and we must kick the user to /login.
    // A previous predicate that matched the whole `/auth/passkey/`
    // prefix would silently swallow the redirect and leave the user
    // staring at a non-responsive form.
    expect(isAuthEndpoint("/api/v1/auth/passkey/register/start")).toBe(false);
    expect(isAuthEndpoint("/api/v1/auth/passkey/register/finish")).toBe(false);
    expect(isAuthEndpoint("/w/acme/api/v1/auth/passkey/register/start")).toBe(false);
  });

  it("ignores query / fragment suffixes", () => {
    // A caller that tacks on `?trace=X` for telemetry shouldn't break
    // the predicate.
    expect(isAuthEndpoint("/api/v1/auth/me?fresh=1")).toBe(true);
    expect(isAuthEndpoint("/api/v1/auth/logout#goodbye")).toBe(true);
  });

  it("classifies normal API surfaces as 'do redirect'", () => {
    expect(isAuthEndpoint("/api/v1/tasks")).toBe(false);
    expect(isAuthEndpoint("/w/acme/api/v1/tasks")).toBe(false);
    expect(isAuthEndpoint("/api/v1/me")).toBe(false);
    expect(isAuthEndpoint("/api/v1/me/avatar")).toBe(false);
    expect(isAuthEndpoint("/api/v1/me/tokens")).toBe(false);
    expect(isAuthEndpoint("/admin/api/v1/me")).toBe(false);
    // Email change / revert are authenticated surfaces — session
    // expiration mid-flow must redirect to /login.
    expect(isAuthEndpoint("/api/v1/me/email/change_request")).toBe(false);
    expect(isAuthEndpoint("/api/v1/auth/email/verify")).toBe(false);
  });
});

describe("sanitizeNext", () => {
  it("accepts in-app absolute paths with query + hash intact", () => {
    expect(sanitizeNext("/today")).toBe("/today");
    expect(sanitizeNext("/property/abc?tab=tasks#row=12")).toBe("/property/abc?tab=tasks#row=12");
  });

  it("rejects protocol-relative and external URLs", () => {
    expect(sanitizeNext("//evil.example/steal")).toBeNull();
    expect(sanitizeNext("https://evil.example/")).toBeNull();
    expect(sanitizeNext("http://localhost/today")).toBeNull();
  });

  it("rejects protocol-like URIs even when prefixed with a slash", () => {
    expect(sanitizeNext("/javascript:alert(1)")).toBeNull();
    expect(sanitizeNext("/ javascript:alert(1)")).toBeNull(); // whitespace padding
    expect(sanitizeNext("/data:text/html,<script>")).toBeNull();
    expect(sanitizeNext("/vbscript:msgbox")).toBeNull();
  });

  it("rejects bare scheme strings (no leading slash)", () => {
    expect(sanitizeNext("javascript:alert(1)")).toBeNull();
    expect(sanitizeNext("data:text/html,foo")).toBeNull();
  });

  it("rejects the backslash same-origin-bypass trick", () => {
    expect(sanitizeNext("/\\evil.example")).toBeNull();
    expect(sanitizeNext("/\\/evil.example")).toBeNull();
  });

  it("collapses a self-referential /login next (avoids infinite round-trip)", () => {
    expect(sanitizeNext("/login")).toBeNull();
    expect(sanitizeNext("/login?next=%2Fevil")).toBeNull();
    expect(sanitizeNext("/login#anchor")).toBeNull();
  });

  it("returns null for null / empty / whitespace", () => {
    expect(sanitizeNext(null)).toBeNull();
    expect(sanitizeNext(undefined)).toBeNull();
    expect(sanitizeNext("")).toBeNull();
    expect(sanitizeNext("   ")).toBeNull();
  });
});

describe("createOnUnauthorized", () => {
  it("clears the auth store, drops the query cache, and navigates to /login with a `next` query param", () => {
    setAuthenticated(SAMPLE_USER);
    const qc = new QueryClient();
    qc.setQueryData(["w", "acme", "tasks"], { stale: true });
    const navigate = vi.fn();

    const handler = createOnUnauthorized({
      navigate,
      queryClient: qc,
      getCurrentLocation: () => "/property/abc?tab=tasks",
    });
    handler(401, "/w/acme/api/v1/tasks");

    expect(getAuthState().status).toBe("unauthenticated");
    expect(getAuthState().user).toBeNull();
    expect(qc.getQueryData(["w", "acme", "tasks"])).toBeUndefined();
    expect(navigate).toHaveBeenCalledWith(
      "/login?next=%2Fproperty%2Fabc%3Ftab%3Dtasks",
      { replace: true },
    );
  });

  it("redirects without `?next=` when the user is already on /login", () => {
    const qc = new QueryClient();
    const navigate = vi.fn();
    const handler = createOnUnauthorized({
      navigate,
      queryClient: qc,
      getCurrentLocation: () => "/login",
    });
    handler(401, "/w/acme/api/v1/tasks");
    expect(navigate).toHaveBeenCalledWith("/login", { replace: true });
  });

  it("does not redirect when the 401 came from an auth endpoint", () => {
    setAuthenticated(SAMPLE_USER);
    const qc = new QueryClient();
    const navigate = vi.fn();
    const handler = createOnUnauthorized({
      navigate,
      queryClient: qc,
      getCurrentLocation: () => "/today",
    });

    handler(401, "/api/v1/auth/me");
    handler(401, "/api/v1/auth/passkey/login/finish");
    handler(401, "/api/v1/signup/verify");

    expect(navigate).not.toHaveBeenCalled();
    // Auth state untouched — the auth probe / login / signup flows
    // own their own state transitions.
    expect(getAuthState().status).toBe("authenticated");
  });

  it("uses window.location by default when getCurrentLocation is omitted", () => {
    const qc = new QueryClient();
    const navigate = vi.fn();
    // jsdom defaults to http://localhost/. Set a path so we can prove
    // the default reads from window.location.
    window.history.replaceState({}, "", "/dashboard?widget=open");
    const handler = createOnUnauthorized({ navigate, queryClient: qc });
    handler(401, "/api/v1/tasks");
    expect(navigate).toHaveBeenCalledWith(
      "/login?next=%2Fdashboard%3Fwidget%3Dopen",
      { replace: true },
    );
  });

  it("drops an unsafe `here` (protocol-relative / external) and redirects to bare /login", () => {
    // If `window.location` somehow resolves to an off-origin URL (e.g.
    // a hand-patched test harness), the handler must still land the
    // user on the in-app /login and never encode the bogus value.
    const qc = new QueryClient();
    const navigate = vi.fn();
    const handler = createOnUnauthorized({
      navigate,
      queryClient: qc,
      getCurrentLocation: () => "//evil.example/steal",
    });
    handler(401, "/api/v1/tasks");
    expect(navigate).toHaveBeenCalledWith("/login", { replace: true });
  });

  it("drops a `javascript:`-shaped `here` (defence in depth)", () => {
    const qc = new QueryClient();
    const navigate = vi.fn();
    const handler = createOnUnauthorized({
      navigate,
      queryClient: qc,
      getCurrentLocation: () => "/javascript:alert(1)",
    });
    handler(401, "/api/v1/tasks");
    expect(navigate).toHaveBeenCalledWith("/login", { replace: true });
  });

  it("does not redirect when a 401 hits /api/v1/auth/passkey/login/start", () => {
    // Login-ceremony 401s are protocol-level (rate limit, no credential),
    // not a session-expired event. The LoginPage owns the recovery.
    const qc = new QueryClient();
    const navigate = vi.fn();
    const handler = createOnUnauthorized({
      navigate,
      queryClient: qc,
      getCurrentLocation: () => "/login",
    });
    handler(401, "/api/v1/auth/passkey/login/start");
    expect(navigate).not.toHaveBeenCalled();
  });

  it("DOES redirect when a 401 hits /api/v1/auth/passkey/register/start (session expired)", () => {
    // The `add-another-passkey` flow is authenticated; a 401 means the
    // session cookie is stale and the user must re-sign-in.
    setAuthenticated(SAMPLE_USER);
    const qc = new QueryClient();
    const navigate = vi.fn();
    const handler = createOnUnauthorized({
      navigate,
      queryClient: qc,
      getCurrentLocation: () => "/me",
    });
    handler(401, "/api/v1/auth/passkey/register/start");
    expect(getAuthState().status).toBe("unauthenticated");
    expect(navigate).toHaveBeenCalledWith("/login?next=%2Fme", { replace: true });
  });
});
