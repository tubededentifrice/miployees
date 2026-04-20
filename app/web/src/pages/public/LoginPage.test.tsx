// crewday — LoginPage component test.
//
// What this covers:
//   1. Happy path — click "Use passkey" → mocked `navigator.credentials
//      .get` resolves → the passkey ceremony finishes → the auth store
//      flips to authenticated → `<Navigate>` fires to the sanitised
//      `?next=` path (closes cd-g5c2 for the consumption point).
//   2. Error branches — `PasskeyCancelledError` surfaces an inline
//      info notice; `PasskeyUnsupportedError` surfaces a danger notice;
//      `ApiError` 429 surfaces the rate-limit copy. The button re-arms
//      after each failure.
//   3. `sanitizeNext` wiring — a crafted `?next=https://evil.example/`
//      does NOT survive into `<Navigate>`; the user lands on the
//      role-appropriate landing page instead.
//
// What this does NOT cover (and why):
//   - The Playwright pixel-diff against `.playwright-mcp/mocks-public/
//     LoginPage.tsx.png` — that's cd-4z54's e2e acceptance, run by the
//     Director. A component-level snapshot here would duplicate the
//     check in jsdom where CSS is only minimally applied.
//   - The WebAuthn decode/encode paths — owned by
//     `passkey.test.ts`; this test mounts LoginPage against the real
//     `@/auth` module so we catch a wiring regression (e.g. the page
//     bypassing `loginWithPasskey()`) but we don't re-verify the
//     helpers themselves.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactElement, type ReactNode } from "react";
import LoginPage from "./LoginPage";
import { AuthProvider, __resetAuthStoreForTests } from "@/auth";
import { __resetApiProvidersForTests } from "@/lib/api";

// ── Test harness ──────────────────────────────────────────────────

interface FakeResponse {
  status: number;
  body?: unknown;
}

/**
 * Wire a scripted `fetch`. The LoginPage goes through the real
 * `@/auth` module → `/lib/api`, so the initial `/auth/me` probe and
 * the subsequent passkey-login POSTs are all observable here. Each
 * URL suffix has its own FIFO queue so the test can assert on the
 * order of a multi-request flow without fighting a mutable
 * `responses[]`.
 */
function installFetch(scripted: Record<string, FakeResponse[]>): {
  calls: Array<{ url: string; init: RequestInit }>;
  restore: () => void;
} {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [k, v] of Object.entries(scripted)) queues[k] = [...v];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const suffix = Object.keys(queues).find((s) => resolved.endsWith(s));
    if (!suffix) throw new Error(`Unscripted fetch: ${resolved}`);
    const next = queues[suffix]!.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    const ok = next.status >= 200 && next.status < 300;
    const text = next.body === undefined ? "" : JSON.stringify(next.body);
    return {
      ok,
      status: next.status,
      statusText: ok ? "OK" : "Error",
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

/**
 * Duck-typed WebAuthn assertion that satisfies `encodeAssertion()`'s
 * structural check (`type === "public-key"`, `rawId instanceof
 * ArrayBuffer`, `response` is an object). jsdom doesn't define
 * `PublicKeyCredential`, so a real instance is impossible here.
 */
function fakeAssertion(): Credential {
  const buf = (...vals: number[]): ArrayBuffer => new Uint8Array(vals).buffer;
  return {
    id: "fake-credential-id",
    rawId: buf(0xaa, 0xbb),
    type: "public-key",
    response: {
      authenticatorData: buf(0x01),
      clientDataJSON: buf(0x02),
      signature: buf(0x03),
      userHandle: buf(0x04),
    },
    getClientExtensionResults: () => ({}),
    authenticatorAttachment: "platform",
  } as unknown as Credential;
}

/** Install a scripted `navigator.credentials.get`. Returns the spy. */
function installCredentialsGet(
  behaviour: () => Promise<Credential> | Credential,
): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async () => behaviour());
  const nav = globalThis.navigator as unknown as {
    credentials?: { get?: unknown };
  };
  if (!nav.credentials) {
    (nav as { credentials: unknown }).credentials = {};
  }
  (nav.credentials as { get: unknown }).get = spy;
  return spy;
}

function LocationProbe({ testid }: { testid: string }): ReactElement {
  const loc = useLocation();
  return <span data-testid={testid}>{loc.pathname + loc.search}</span>;
}

function Harness({
  initial,
  children,
}: {
  initial: string;
  children?: ReactNode;
}): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/today" element={<LocationProbe testid="landed-today" />} />
            <Route path="/dashboard" element={<LocationProbe testid="landed-dashboard" />} />
            <Route path="/portfolio" element={<LocationProbe testid="landed-portfolio" />} />
            <Route path="/property/abc" element={<LocationProbe testid="landed-property" />} />
            <Route path="*" element={<LocationProbe testid="landed-other" />} />
          </Routes>
          {children}
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

/** Flush a single microtask so the bootstrap probe / fetch-then-fetch chain settles. */
async function flush(): Promise<void> {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

beforeEach(() => {
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  vi.unstubAllGlobals();
  // Clear the `navigator.credentials.get` stub so the next test starts
  // from a clean slate — `installCredentialsGet` replaces a property
  // on a shared global; leaving it behind is a cross-test leak.
  const nav = globalThis.navigator as unknown as { credentials?: { get?: unknown } };
  if (nav.credentials) delete (nav.credentials as { get?: unknown }).get;
});

// ── Tests ─────────────────────────────────────────────────────────

describe("<LoginPage> — happy path", () => {
  it("runs the ceremony, calls useAuth().loginWithPasskey, and navigates to the sanitised ?next", async () => {
    const { calls, restore } = installFetch({
      // Bootstrap probe: no session yet.
      "/api/v1/auth/me": [
        { status: 401, body: { detail: "no session" } },
        // After login, `loginWithPasskey` re-fetches /auth/me to hydrate the user.
        {
          status: 200,
          body: {
            user_id: "01HZ_USER",
            display_name: "Maria",
            email: "maria@example.com",
            available_workspaces: [
              {
                workspace: { id: "ws_1", name: "Villa Sud", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
                grant_role: "worker",
                binding_org_id: null,
                source: "workspace_grant",
              },
            ],
            current_workspace_id: null,
          },
        },
      ],
      "/api/v1/auth/passkey/login/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
      ],
      "/api/v1/auth/passkey/login/finish": [
        { status: 200, body: { user_id: "01HZ_USER" } },
      ],
    });
    const getSpy = installCredentialsGet(() => fakeAssertion());

    try {
      render(<Harness initial="/login?next=%2Fproperty%2Fabc" />);
      await flush();

      // Button renders with the mock's copy (`Use passkey`).
      const button = screen.getByTestId("login-passkey") as HTMLButtonElement;
      expect(button.textContent).toContain("Use passkey");

      await act(async () => {
        fireEvent.click(button);
        // Let the two-request chain (start, navigator.get, finish, /me) settle.
        await new Promise((r) => setTimeout(r, 0));
      });
      // Second microtask flush — the ceremony chains two awaits.
      await flush();

      // WebAuthn helper was called exactly once.
      expect(getSpy).toHaveBeenCalledTimes(1);
      // Login start + finish hit the right endpoints in the right order.
      const paths = calls.map((c) => c.url);
      expect(paths).toContain("/api/v1/auth/passkey/login/start");
      expect(paths).toContain("/api/v1/auth/passkey/login/finish");
      expect(paths.indexOf("/api/v1/auth/passkey/login/start"))
        .toBeLessThan(paths.indexOf("/api/v1/auth/passkey/login/finish"));

      // Redirected to the `next` param (sanitised, same-origin path).
      expect(screen.getByTestId("landed-property").textContent).toBe("/property/abc");
    } finally {
      restore();
    }
  });

  it("falls back to the role-appropriate landing when no ?next is provided (worker → /today)", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [
        { status: 401, body: { detail: "no session" } },
        {
          status: 200,
          body: {
            user_id: "01HZ_USER",
            display_name: "Maria",
            email: "maria@example.com",
            available_workspaces: [
              {
                workspace: { id: "ws_1", name: "Villa Sud", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
                grant_role: "worker",
                binding_org_id: null,
                source: "workspace_grant",
              },
            ],
            current_workspace_id: null,
          },
        },
      ],
      "/api/v1/auth/passkey/login/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
      ],
      "/api/v1/auth/passkey/login/finish": [
        { status: 200, body: { user_id: "01HZ_USER" } },
      ],
    });
    installCredentialsGet(() => fakeAssertion());

    try {
      render(<Harness initial="/login" />);
      await flush();
      await act(async () => {
        fireEvent.click(screen.getByTestId("login-passkey"));
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      expect(screen.getByTestId("landed-today").textContent).toBe("/today");
    } finally {
      restore();
    }
  });

  it("routes managers to /dashboard when no ?next is provided", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [
        { status: 401, body: { detail: "no session" } },
        {
          status: 200,
          body: {
            user_id: "01HZ_MGR",
            display_name: "Élodie",
            email: "elodie@example.com",
            available_workspaces: [
              {
                workspace: { id: "ws_1", name: "Villa Sud", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
                grant_role: "manager",
                binding_org_id: null,
                source: "workspace_grant",
              },
            ],
            current_workspace_id: null,
          },
        },
      ],
      "/api/v1/auth/passkey/login/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
      ],
      "/api/v1/auth/passkey/login/finish": [
        { status: 200, body: { user_id: "01HZ_MGR" } },
      ],
    });
    installCredentialsGet(() => fakeAssertion());

    try {
      render(<Harness initial="/login" />);
      await flush();
      await act(async () => {
        fireEvent.click(screen.getByTestId("login-passkey"));
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      expect(screen.getByTestId("landed-dashboard").textContent).toBe("/dashboard");
    } finally {
      restore();
    }
  });
});

describe("<LoginPage> — error branches", () => {
  it("surfaces a soft inline notice when the user cancels the passkey prompt", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/auth/passkey/login/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
      ],
    });
    installCredentialsGet(() => {
      throw new DOMException("The operation either timed out or was not allowed.", "NotAllowedError");
    });

    try {
      render(<Harness initial="/login" />);
      await flush();

      const button = screen.getByTestId("login-passkey") as HTMLButtonElement;
      await act(async () => {
        fireEvent.click(button);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      const notice = screen.getByTestId("login-error");
      expect(notice.textContent).toContain("Passkey prompt closed");
      expect(notice.className).toContain("login__notice");
      expect(notice.className).not.toContain("login__notice--danger");
      // Button re-arms so the user can retry.
      expect(button.disabled).toBe(false);
    } finally {
      restore();
    }
  });

  it("surfaces a danger notice when the server rate-limits the login (429)", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/auth/passkey/login/start": [
        { status: 429, body: { type: "rate_limited", title: "Slow down", detail: "Too many attempts." } },
      ],
    });
    // The ceremony never reaches navigator.credentials.get because /start fails first.
    installCredentialsGet(() => fakeAssertion());

    try {
      render(<Harness initial="/login" />);
      await flush();

      await act(async () => {
        fireEvent.click(screen.getByTestId("login-passkey"));
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      const notice = screen.getByTestId("login-error");
      expect(notice.textContent).toContain("Too many sign-in attempts");
      expect(notice.className).toContain("login__notice--danger");
    } finally {
      restore();
    }
  });

  it("surfaces a danger notice when the browser doesn't support WebAuthn", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [{ status: 401, body: { detail: "no session" } }],
      "/api/v1/auth/passkey/login/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
      ],
    });
    installCredentialsGet(() => {
      throw new DOMException("WebAuthn unavailable in this context.", "SecurityError");
    });

    try {
      render(<Harness initial="/login" />);
      await flush();

      await act(async () => {
        fireEvent.click(screen.getByTestId("login-passkey"));
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      const notice = screen.getByTestId("login-error");
      expect(notice.textContent).toContain("can't use a passkey");
      expect(notice.className).toContain("login__notice--danger");
    } finally {
      restore();
    }
  });
});

describe("<LoginPage> — sanitizeNext wiring (closes cd-g5c2)", () => {
  it("drops an off-origin ?next=https://evil.example/ and lands on the role home instead", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [
        { status: 401, body: { detail: "no session" } },
        {
          status: 200,
          body: {
            user_id: "01HZ_USER",
            display_name: "Maria",
            email: "maria@example.com",
            available_workspaces: [
              {
                workspace: { id: "ws_1", name: "Villa Sud", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
                grant_role: "worker",
                binding_org_id: null,
                source: "workspace_grant",
              },
            ],
            current_workspace_id: null,
          },
        },
      ],
      "/api/v1/auth/passkey/login/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
      ],
      "/api/v1/auth/passkey/login/finish": [
        { status: 200, body: { user_id: "01HZ_USER" } },
      ],
    });
    installCredentialsGet(() => fakeAssertion());

    try {
      render(<Harness initial="/login?next=https%3A%2F%2Fevil.example%2F" />);
      await flush();
      await act(async () => {
        fireEvent.click(screen.getByTestId("login-passkey"));
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      // The crafted `next` was dropped by `sanitizeNext`; the user lands
      // on `/today` (worker role home), NOT an off-origin URL and NOT
      // an absolute path concocted from the crafted value.
      expect(screen.getByTestId("landed-today").textContent).toBe("/today");
      // Defensive: none of the "landed-other" fallbacks fired with an
      // attacker-controlled path.
      expect(screen.queryByTestId("landed-other")).toBeNull();
    } finally {
      restore();
    }
  });

  it("drops protocol-relative //evil.example/ and a javascript: scheme", async () => {
    const { restore } = installFetch({
      "/api/v1/auth/me": [
        { status: 401, body: { detail: "no session" } },
        {
          status: 200,
          body: {
            user_id: "01HZ_USER",
            display_name: "Maria",
            email: "maria@example.com",
            available_workspaces: [
              {
                workspace: { id: "ws_1", name: "Villa Sud", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
                grant_role: "worker",
                binding_org_id: null,
                source: "workspace_grant",
              },
            ],
            current_workspace_id: null,
          },
        },
      ],
      "/api/v1/auth/passkey/login/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
      ],
      "/api/v1/auth/passkey/login/finish": [
        { status: 200, body: { user_id: "01HZ_USER" } },
      ],
    });
    installCredentialsGet(() => fakeAssertion());

    try {
      render(<Harness initial="/login?next=%2F%2Fevil.example%2F" />);
      await flush();
      await act(async () => {
        fireEvent.click(screen.getByTestId("login-passkey"));
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      expect(screen.getByTestId("landed-today").textContent).toBe("/today");
    } finally {
      restore();
    }
  });
});

describe("<LoginPage> — concurrency guard", () => {
  it("coalesces rapid double-clicks into a single ceremony", async () => {
    // The button's `disabled` attribute only blocks the *next* click
    // after React commits the pending state. A burst of synchronous
    // clicks in the same tick can bypass that — the inflight-ref guard
    // is what stops them from firing two `/passkey/login/start` POSTs.
    const { calls, restore } = installFetch({
      "/api/v1/auth/me": [
        { status: 401, body: { detail: "no session" } },
        {
          status: 200,
          body: {
            user_id: "01HZ_USER",
            display_name: "Maria",
            email: "maria@example.com",
            available_workspaces: [
              {
                workspace: { id: "ws_1", name: "Villa Sud", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
                grant_role: "worker",
                binding_org_id: null,
                source: "workspace_grant",
              },
            ],
            current_workspace_id: null,
          },
        },
      ],
      // Only ONE `/start` response is scripted. A second POST would
      // throw "No more responses for: …" and fail the test — which is
      // precisely the regression we want to catch.
      "/api/v1/auth/passkey/login/start": [
        { status: 200, body: { challenge_id: "ch_1", options: { challenge: "AQID" } } },
      ],
      "/api/v1/auth/passkey/login/finish": [
        { status: 200, body: { user_id: "01HZ_USER" } },
      ],
    });
    installCredentialsGet(() => fakeAssertion());

    try {
      render(<Harness initial="/login" />);
      await flush();

      const button = screen.getByTestId("login-passkey") as HTMLButtonElement;
      await act(async () => {
        fireEvent.click(button);
        // Second click in the same synchronous tick, before React
        // commits `disabled={pending}`. The ref guard must coalesce
        // both clicks into a single in-flight ceremony.
        fireEvent.click(button);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      const startCalls = calls.filter((c) => c.url.endsWith("/api/v1/auth/passkey/login/start"));
      expect(startCalls.length).toBe(1);
    } finally {
      restore();
    }
  });
});

describe("<LoginPage> — already-signed-in bounce", () => {
  it("redirects straight to the role landing if the user is already authenticated on mount", async () => {
    // Bootstrap probe returns 200 — the user already has a session,
    // typically from a browser back/forward that re-surfaced /login.
    const { restore } = installFetch({
      "/api/v1/auth/me": [
        {
          status: 200,
          body: {
            user_id: "01HZ_MGR",
            display_name: "Élodie",
            email: "elodie@example.com",
            available_workspaces: [
              {
                workspace: { id: "ws_1", name: "Villa Sud", timezone: "UTC", default_currency: "EUR", default_country: "FR", default_locale: "fr" },
                grant_role: "manager",
                binding_org_id: null,
                source: "workspace_grant",
              },
            ],
            current_workspace_id: null,
          },
        },
      ],
    });

    try {
      render(<Harness initial="/login" />);
      await flush();
      expect(screen.getByTestId("landed-dashboard").textContent).toBe("/dashboard");
    } finally {
      restore();
    }
  });
});
