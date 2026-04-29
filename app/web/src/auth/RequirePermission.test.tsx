import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { type ReactNode } from "react";
import { WorkspaceProvider } from "@/context/WorkspaceContext";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import * as preferences from "@/lib/preferences";
import appSource from "../App.tsx?raw";
import { __resetAuthStoreForTests } from "./useAuth";
import { setAuthenticated } from "./authStore";
import { RequirePermission } from "./RequirePermission";
import type { AuthMe } from "./types";

const USER: AuthMe = {
  user_id: "usr_1",
  display_name: "Mina",
  email: "mina@example.com",
  available_workspaces: [
    {
      workspace: {
        id: "ws_1",
        name: "Acme",
        timezone: "UTC",
        default_currency: "USD",
        default_country: "US",
        default_locale: "en",
      },
      grant_role: "manager",
      binding_org_id: null,
      source: "workspace_grant",
    },
  ],
  current_workspace_id: "ws_1",
};

interface FakeResponse {
  status: number;
  body: unknown;
}

function installFetch(responses: FakeResponse[]) {
  const calls: string[] = [];
  const original = globalThis.fetch;
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push(resolved);
    const next = responses.shift();
    if (!next) {
      throw new Error(`Unexpected fetch call: ${resolved}`);
    }
    return {
      ok: next.status >= 200 && next.status < 300,
      status: next.status,
      statusText: next.status >= 200 && next.status < 300 ? "OK" : "Error",
      text: async () => JSON.stringify(next.body),
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

function App({ children }: { children?: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/approvals"]}>
        <WorkspaceProvider>
          <Routes>
            <Route element={<RequirePermission actionKey="approvals.read" />}>
              <Route element={<GuardedLayout />}>
                <Route path="/approvals" element={children ?? <div>approval desk</div>} />
              </Route>
            </Route>
          </Routes>
        </WorkspaceProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function GuardedLayout() {
  return (
    <div>
      <div>manager shell</div>
      <Outlet />
    </div>
  );
}

beforeEach(() => {
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.spyOn(preferences, "readWorkspaceCookie").mockReturnValue("ws_1");
  setAuthenticated(USER);
});

afterEach(() => {
  cleanup();
  __resetAuthStoreForTests();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  vi.restoreAllMocks();
});

describe("<RequirePermission>", () => {
  it("wraps the real approvals route before the manager shell", () => {
    expect(appSource).toMatch(
      /<Route element={<RequirePermission actionKey="approvals\.read" \/>}>\s*<Route element={<ManagerLayout \/>}>\s*<Route path="\/approvals" element={<ApprovalsPage \/>} \/>/,
    );
  });

  it("holds the route while the resolver is loading", () => {
    const original = globalThis.fetch;
    (globalThis as { fetch: typeof fetch }).fetch = vi.fn(
      () => new Promise<Response>(() => {}),
    ) as unknown as typeof fetch;
    try {
      render(<App />);
      expect(screen.getByRole("status")).toHaveTextContent("Checking permissions");
      expect(screen.queryByText("approval desk")).toBeNull();
      expect(screen.queryByText("manager shell")).toBeNull();
    } finally {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    }
  });

  it("renders children after an allow decision", async () => {
    const fake = installFetch([
      {
        status: 200,
        body: {
          effect: "allow",
          source_layer: "default_allow",
          source_rule_id: null,
          matched_groups: ["managers"],
        },
      },
    ]);
    try {
      render(<App />);
      expect(await screen.findByText("approval desk")).toBeInTheDocument();
      expect(screen.getByText("manager shell")).toBeInTheDocument();
      expect(fake.calls[0]).toContain("/w/ws_1/api/v1/permissions/resolved/self?");
      expect(fake.calls[0]).toContain("action_key=approvals.read");
      expect(fake.calls[0]).toContain("scope_id=ws_1");
    } finally {
      fake.restore();
    }
  });

  it("renders the standard 403 panel after a deny decision", async () => {
    const fake = installFetch([
      {
        status: 200,
        body: {
          effect: "deny",
          source_layer: "no_match",
          source_rule_id: null,
          matched_groups: [],
        },
      },
    ]);
    try {
      render(<App />);
      expect(await screen.findByRole("alert")).toHaveTextContent("Access denied");
      expect(screen.queryByText("approval desk")).toBeNull();
      expect(screen.queryByText("manager shell")).toBeNull();
    } finally {
      fake.restore();
    }
  });

  it("fails closed when the resolver errors", async () => {
    const fake = installFetch([
      {
        status: 500,
        body: {
          type: "https://crewday.dev/errors/internal",
          title: "Internal error",
          status: 500,
          detail: "boom",
        },
      },
    ]);
    try {
      render(<App />);
      await waitFor(() => {
        expect(screen.getByRole("alert")).toHaveTextContent("Access denied");
      });
      expect(screen.queryByText("approval desk")).toBeNull();
      expect(screen.queryByText("manager shell")).toBeNull();
    } finally {
      fake.restore();
    }
  });
});
