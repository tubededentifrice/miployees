import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  __resetApiProvidersForTests,
  fetchJson,
  registerAuthTokenGetter,
  registerOnUnauthorized,
  registerWorkspaceSlugGetter,
  resolveApiPath,
} from "@/lib/api";

// A tiny spy-fetch we install in place of the global. `fetchJson` only
// ever calls `fetch(url, init)` so this level of fakery is enough to
// assert on URL rewriting, headers, and error mapping without pulling
// in msw for what is really a pure-data-layer test.
interface FakeResponse {
  status: number;
  statusText?: string;
  ok?: boolean;
  body: unknown;
  contentType?: string;
}

function installFetch(responses: FakeResponse[]): {
  calls: Array<{ url: string; init: RequestInit }>;
  restore: () => void;
} {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const next = responses.shift();
    if (!next) throw new Error(`Unexpected fetch call: ${resolved}`);
    const status = next.status;
    const ok = next.ok ?? (status >= 200 && status < 300);
    const text =
      typeof next.body === "string"
        ? next.body
        : next.body === null || next.body === undefined
          ? ""
          : JSON.stringify(next.body);
    return {
      ok,
      status,
      statusText: next.statusText ?? (ok ? "OK" : "Error"),
      text: async () => text,
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

beforeEach(() => {
  __resetApiProvidersForTests();
  // Reset document.cookie so CSRF / bearer tests are hermetic. jsdom
  // persists cookies across tests otherwise.
  document.cookie = "crewday_csrf=; path=/; max-age=0";
});

afterEach(() => {
  __resetApiProvidersForTests();
});

describe("resolveApiPath", () => {
  it("rewrites /api/v1/... to /w/<slug>/api/v1/... when a slug is active", () => {
    expect(resolveApiPath("/api/v1/tasks", "acme")).toBe("/w/acme/api/v1/tasks");
    expect(resolveApiPath("/api/v1/me/avatar", "team-42")).toBe("/w/team-42/api/v1/me/avatar");
  });

  it("leaves the path unchanged when no slug is active", () => {
    expect(resolveApiPath("/api/v1/me", null)).toBe("/api/v1/me");
  });

  it("passes already-workspace-scoped paths through untouched", () => {
    // A component that built the URL explicitly (e.g. for a different
    // tenant in a cross-workspace widget) should not get double-prefixed.
    expect(resolveApiPath("/w/other/api/v1/tasks", "acme")).toBe("/w/other/api/v1/tasks");
  });

  it("passes /admin paths through untouched — the admin shell is deployment-scope", () => {
    expect(resolveApiPath("/admin/api/v1/me", "acme")).toBe("/admin/api/v1/me");
  });

  it("passes absolute URLs through untouched", () => {
    expect(resolveApiPath("https://example.test/oauth", "acme")).toBe("https://example.test/oauth");
  });

  it("does not rewrite non-API relative paths even when a slug is set", () => {
    // /switch/manager, /theme/set, /events etc. are preference and SSE
    // endpoints and must not gain a workspace prefix.
    expect(resolveApiPath("/switch/manager", "acme")).toBe("/switch/manager");
    expect(resolveApiPath("/events", "acme")).toBe("/events");
  });
});

describe("fetchJson URL building", () => {
  it("uses the workspace slug from the registered getter", async () => {
    registerWorkspaceSlugGetter(() => "acme");
    const { calls, restore } = installFetch([{ status: 200, body: { ok: true } }]);
    try {
      await fetchJson("/api/v1/tasks");
      expect(calls[0]!.url).toBe("/w/acme/api/v1/tasks");
    } finally {
      restore();
    }
  });

  it("re-reads the slug on every call (workspace switch)", async () => {
    let slug: string | null = "first";
    registerWorkspaceSlugGetter(() => slug);
    const { calls, restore } = installFetch([
      { status: 200, body: {} },
      { status: 200, body: {} },
    ]);
    try {
      await fetchJson("/api/v1/me");
      slug = "second";
      await fetchJson("/api/v1/me");
      expect(calls[0]!.url).toBe("/w/first/api/v1/me");
      expect(calls[1]!.url).toBe("/w/second/api/v1/me");
    } finally {
      restore();
    }
  });

  it("sends `/api/v1/...` unprefixed when the slug getter returns null (login, workspace picker)", async () => {
    // Default getter (post-reset) returns null — never register one.
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      await fetchJson("/api/v1/me");
      expect(calls[0]!.url).toBe("/api/v1/me");
    } finally {
      restore();
    }
  });

  it("captures the slug at send time, not at key-build time (in-flight workspace switch stays on the old slug)", async () => {
    // A request that starts on slug `first` must complete against
    // `/w/first/...` even if the user switches to `second` before the
    // response lands. Redirecting mid-flight would split an idempotent
    // POST across two tenants.
    let slug: string | null = "first";
    registerWorkspaceSlugGetter(() => slug);
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      const pending = fetchJson("/api/v1/me");
      slug = "second"; // Switch after the URL has already been resolved.
      await pending;
      expect(calls[0]!.url).toBe("/w/first/api/v1/me");
    } finally {
      restore();
    }
  });
});

describe("fetchJson headers", () => {
  it("sends Accept: application/json and same-origin credentials on GET", async () => {
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      await fetchJson("/api/v1/me");
      const headers = calls[0]!.init.headers as Record<string, string>;
      expect(headers.Accept).toBe("application/json");
      expect(calls[0]!.init.credentials).toBe("same-origin");
      // No Content-Type on a GET without a body.
      expect(headers["Content-Type"]).toBeUndefined();
    } finally {
      restore();
    }
  });

  it("stringifies JSON bodies and sets Content-Type", async () => {
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      await fetchJson("/api/v1/tasks", { method: "POST", body: { title: "New" } });
      const headers = calls[0]!.init.headers as Record<string, string>;
      expect(headers["Content-Type"]).toBe("application/json");
      expect(calls[0]!.init.body).toBe(JSON.stringify({ title: "New" }));
    } finally {
      restore();
    }
  });

  it("lets the browser set the multipart boundary for FormData bodies", async () => {
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      const fd = new FormData();
      fd.append("file", new Blob(["x"], { type: "image/webp" }), "a.webp");
      await fetchJson("/api/v1/me/avatar", { method: "POST", body: fd });
      const headers = calls[0]!.init.headers as Record<string, string>;
      // `fetchJson` must NOT set Content-Type for FormData — the
      // browser needs to attach `boundary=...` itself.
      expect(headers["Content-Type"]).toBeUndefined();
      expect(calls[0]!.init.body).toBe(fd);
    } finally {
      restore();
    }
  });

  it("echoes the CSRF cookie on non-GET requests", async () => {
    document.cookie = "crewday_csrf=tok-abc; path=/";
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      await fetchJson("/api/v1/me", { method: "POST" });
      const headers = calls[0]!.init.headers as Record<string, string>;
      expect(headers["X-CSRF"]).toBe("tok-abc");
    } finally {
      restore();
    }
  });

  it("echoes the CSRF cookie on every state-changing method (PUT, PATCH, DELETE)", async () => {
    document.cookie = "crewday_csrf=tok-xyz; path=/";
    const { calls, restore } = installFetch([
      { status: 200, body: {} },
      { status: 200, body: {} },
      { status: 200, body: {} },
    ]);
    try {
      await fetchJson("/api/v1/x", { method: "PUT" });
      await fetchJson("/api/v1/x", { method: "PATCH" });
      await fetchJson("/api/v1/x", { method: "DELETE" });
      for (const call of calls) {
        expect((call.init.headers as Record<string, string>)["X-CSRF"]).toBe("tok-xyz");
      }
    } finally {
      restore();
    }
  });

  it("survives a malformed (un-decodable) CSRF cookie by sending the raw value", async () => {
    // jsdom accepts the raw bytes as-is. A `%zz` sequence would crash
    // `decodeURIComponent`; the fetcher must fall back to the raw
    // string so the wrapper doesn't throw before reaching fetch.
    document.cookie = "crewday_csrf=%zz-broken; path=/";
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      await fetchJson("/api/v1/me", { method: "POST" });
      const headers = calls[0]!.init.headers as Record<string, string>;
      expect(headers["X-CSRF"]).toBe("%zz-broken");
    } finally {
      restore();
    }
  });

  it("does not echo CSRF on GET", async () => {
    document.cookie = "crewday_csrf=tok-abc; path=/";
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      await fetchJson("/api/v1/me");
      const headers = calls[0]!.init.headers as Record<string, string>;
      expect(headers["X-CSRF"]).toBeUndefined();
    } finally {
      restore();
    }
  });

  it("injects Authorization: Bearer when the auth-token getter returns a token", async () => {
    registerAuthTokenGetter(() => "mip_k_secret");
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      await fetchJson("/api/v1/me");
      const headers = calls[0]!.init.headers as Record<string, string>;
      expect(headers.Authorization).toBe("Bearer mip_k_secret");
    } finally {
      restore();
    }
  });

  it("omits Authorization when the auth-token getter returns null", async () => {
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      await fetchJson("/api/v1/me");
      const headers = calls[0]!.init.headers as Record<string, string>;
      expect(headers.Authorization).toBeUndefined();
    } finally {
      restore();
    }
  });

  it("merges caller-supplied headers without clobbering defaults", async () => {
    const { calls, restore } = installFetch([{ status: 200, body: {} }]);
    try {
      await fetchJson("/api/v1/me", {
        method: "POST",
        body: "raw",
        headers: { "Content-Type": "text/plain", "X-Agent-Page": "/today" },
      });
      const headers = calls[0]!.init.headers as Record<string, string>;
      // Caller's Content-Type wins over the default JSON.
      expect(headers["Content-Type"]).toBe("text/plain");
      expect(headers["X-Agent-Page"]).toBe("/today");
      expect(headers.Accept).toBe("application/json");
    } finally {
      restore();
    }
  });
});

describe("fetchJson error mapping", () => {
  it("throws ApiError with parsed RFC 7807 fields on a validation 422", async () => {
    const problem = {
      type: "https://crewday.dev/errors/validation",
      title: "Validation error",
      status: 422,
      detail: "Field 'title' is required.",
      errors: [{ loc: ["body", "title"], msg: "field required", type: "value_error.missing" }],
    };
    const { restore } = installFetch([{ status: 422, body: problem, statusText: "Unprocessable Entity" }]);
    try {
      await expect(fetchJson("/api/v1/tasks", { method: "POST", body: {} })).rejects.toSatisfy((err: unknown) => {
        expect(err).toBeInstanceOf(ApiError);
        const e = err as ApiError;
        expect(e.status).toBe(422);
        expect(e.message).toBe("Field 'title' is required.");
        expect(e.detail).toBe("Field 'title' is required.");
        expect(e.title).toBe("Validation error");
        expect(e.type).toBe("validation");
        expect(e.fieldErrors).toHaveLength(1);
        expect(e.fieldErrors[0]!.msg).toBe("field required");
        return true;
      });
    } finally {
      restore();
    }
  });

  it("surfaces approval_required extension fields on a 409", async () => {
    const problem = {
      type: "https://crewday.dev/errors/approval_required",
      title: "Approval required",
      status: 409,
      approval_request_id: "01HZ...",
      expires_at: "2026-04-20T10:00:00Z",
    };
    const { restore } = installFetch([{ status: 409, body: problem }]);
    try {
      await expect(fetchJson("/api/v1/tasks/x", { method: "POST" })).rejects.toSatisfy((err: unknown) => {
        expect(err).toBeInstanceOf(ApiError);
        const e = err as ApiError;
        expect(e.type).toBe("approval_required");
        expect(e.problem?.approval_request_id).toBe("01HZ...");
        expect(e.problem?.expires_at).toBe("2026-04-20T10:00:00Z");
        return true;
      });
    } finally {
      restore();
    }
  });

  it("falls back to statusText for non-JSON error bodies", async () => {
    const { restore } = installFetch([
      { status: 502, body: "Bad Gateway", statusText: "Bad Gateway", contentType: "text/plain" },
    ]);
    try {
      await expect(fetchJson("/api/v1/me")).rejects.toSatisfy((err: unknown) => {
        expect(err).toBeInstanceOf(ApiError);
        const e = err as ApiError;
        expect(e.status).toBe(502);
        // Plain-text body is surfaced as the message.
        expect(e.message).toBe("Bad Gateway");
        expect(e.problem).toBeNull();
        return true;
      });
    } finally {
      restore();
    }
  });

  it("falls back to HTTP <status> when the response is empty and statusText is blank", async () => {
    const { restore } = installFetch([{ status: 500, body: "", statusText: "" }]);
    try {
      await expect(fetchJson("/api/v1/me")).rejects.toSatisfy((err: unknown) => {
        expect(err).toBeInstanceOf(ApiError);
        expect((err as ApiError).message).toBe("HTTP 500");
        return true;
      });
    } finally {
      restore();
    }
  });

  it("preserves the bare `type` value when the server emits a non-canonical URI", async () => {
    // Third-party gateway / upstream may emit its own type URI.
    // `type` should return the last path segment when it matches a
    // `/errors/<name>` tail; otherwise fall back to the raw string so
    // callers can still compare on exact equality.
    const problem = { type: "urn:example:boom", title: "Gone wrong", status: 500 };
    const { restore } = installFetch([{ status: 500, body: problem }]);
    try {
      await expect(fetchJson("/api/v1/me")).rejects.toSatisfy((err: unknown) => {
        expect((err as ApiError).type).toBe("urn:example:boom");
        return true;
      });
    } finally {
      restore();
    }
  });
});

describe("fetchJson — 401 onUnauthorized seam", () => {
  it("invokes the registered handler with the response status and the resolved URL", async () => {
    registerWorkspaceSlugGetter(() => "acme");
    const calls: Array<{ status: number; path: string }> = [];
    registerOnUnauthorized((status, path) => calls.push({ status, path }));

    const { restore } = installFetch([{ status: 401, body: { detail: "session expired" } }]);
    try {
      await expect(fetchJson("/api/v1/tasks")).rejects.toBeInstanceOf(ApiError);
    } finally {
      restore();
    }
    expect(calls).toHaveLength(1);
    expect(calls[0]).toEqual({ status: 401, path: "/w/acme/api/v1/tasks" });
  });

  it("still throws ApiError after invoking the handler (TanStack Query path stays intact)", async () => {
    registerOnUnauthorized(() => undefined);
    const { restore } = installFetch([{ status: 401, body: { detail: "expired" } }]);
    try {
      await expect(fetchJson("/api/v1/me")).rejects.toSatisfy((err: unknown) => {
        expect(err).toBeInstanceOf(ApiError);
        expect((err as ApiError).status).toBe(401);
        return true;
      });
    } finally {
      restore();
    }
  });

  it("does not invoke the handler on non-401 errors", async () => {
    let count = 0;
    registerOnUnauthorized(() => { count += 1; });
    const { restore } = installFetch([
      { status: 403, body: {} },
      { status: 422, body: {} },
      { status: 500, body: {} },
    ]);
    try {
      await expect(fetchJson("/api/v1/me")).rejects.toBeInstanceOf(ApiError);
      await expect(fetchJson("/api/v1/me", { method: "POST" })).rejects.toBeInstanceOf(ApiError);
      await expect(fetchJson("/api/v1/me")).rejects.toBeInstanceOf(ApiError);
    } finally {
      restore();
    }
    expect(count).toBe(0);
  });

  it("survives a handler that throws — the original ApiError still surfaces", async () => {
    // A buggy handler must not mask the underlying 401. We log via
    // console.error and rethrow the ApiError as usual.
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    registerOnUnauthorized(() => { throw new Error("handler crashed"); });
    const { restore } = installFetch([{ status: 401, body: {} }]);
    try {
      await expect(fetchJson("/api/v1/me")).rejects.toSatisfy((err: unknown) => {
        expect(err).toBeInstanceOf(ApiError);
        expect((err as ApiError).status).toBe(401);
        return true;
      });
      // Assert before mockRestore — `restore()` clears the spy's call
      // history, so checking it after the cleanup hits a 0-call spy.
      expect(errSpy).toHaveBeenCalledWith("onUnauthorized handler threw", expect.any(Error));
    } finally {
      restore();
      errSpy.mockRestore();
    }
  });

  it("clears the handler when registerOnUnauthorized is called with null", async () => {
    let count = 0;
    registerOnUnauthorized(() => { count += 1; });
    registerOnUnauthorized(null);

    const { restore } = installFetch([{ status: 401, body: {} }]);
    try {
      await expect(fetchJson("/api/v1/me")).rejects.toBeInstanceOf(ApiError);
    } finally {
      restore();
    }
    expect(count).toBe(0);
  });
});

describe("fetchJson success parsing", () => {
  it("returns parsed JSON as T", async () => {
    const { restore } = installFetch([{ status: 200, body: { id: "u1", name: "Ada" } }]);
    try {
      const data = await fetchJson<{ id: string; name: string }>("/api/v1/me");
      expect(data.id).toBe("u1");
      expect(data.name).toBe("Ada");
    } finally {
      restore();
    }
  });

  it("returns null for a 200 with an empty body", async () => {
    const { restore } = installFetch([{ status: 204, body: "" }]);
    try {
      const data = await fetchJson<unknown>("/api/v1/me");
      expect(data).toBeNull();
    } finally {
      restore();
    }
  });
});
