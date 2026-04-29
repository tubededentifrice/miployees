import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import DashboardPage from "./DashboardPage";

interface FakeResponse {
  body: unknown;
}

function installFetch(scripted: Record<string, FakeResponse[]>): () => void {
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [path, responses] of Object.entries(scripted)) {
    queues[path] = [...responses];
  }
  const spy = vi.fn(async (url: string | URL | Request) => {
    const resolved = typeof url === "string" ? url : url.toString();
    const pathname = new URL(resolved, "http://crewday.test").pathname;
    const next = queues[pathname]?.shift();
    if (!next) throw new Error(`Unscripted fetch: ${resolved}`);
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => JSON.stringify(next.body),
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return () => {
    (globalThis as { fetch: typeof fetch }).fetch = original;
  };
}

function Harness(): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/admin/dashboard"]}>
        <DashboardPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("Admin DashboardPage", () => {
  it("excludes archived workspaces from pressure rows", async () => {
    const restore = installFetch({
      "/admin/api/v1/usage/summary": [
        {
          body: {
            window_label: "rolling 30 days",
            deployment_spend_cents_30d: 2500,
            deployment_calls_30d: 50,
            workspace_count: 2,
            paused_workspace_count: 1,
            per_capability: [],
          },
        },
      ],
      "/admin/api/v1/usage/workspaces": [
        {
          body: {
            workspaces: [
              {
                workspace_id: "ws_active",
                slug: "active",
                name: "Active House",
                cap_cents_30d: 1000,
                spent_cents_30d: 800,
                percent: 80,
                paused: false,
              },
              {
                workspace_id: "ws_archived",
                slug: "archived",
                name: "Archived House",
                cap_cents_30d: 1000,
                spent_cents_30d: 1000,
                percent: 100,
                paused: true,
              },
            ],
          },
        },
      ],
      "/admin/api/v1/workspaces": [
        {
          body: {
            workspaces: [
              {
                id: "ws_active",
                slug: "active",
                name: "Active House",
                plan: "free",
                verification_state: "trusted",
                members_count: 2,
                archived_at: null,
                created_at: "2026-04-01T00:00:00+00:00",
              },
              {
                id: "ws_archived",
                slug: "archived",
                name: "Archived House",
                plan: "free",
                verification_state: "trusted",
                members_count: 2,
                archived_at: "2026-04-02T00:00:00+00:00",
                created_at: "2026-04-01T00:00:00+00:00",
              },
            ],
          },
        },
      ],
      "/admin/api/v1/audit": [
        { body: { data: [], next_cursor: null, has_more: false } },
      ],
    });
    try {
      render(<Harness />);

      expect(await screen.findByText("Active House")).toBeInTheDocument();
      expect(screen.queryByText("Archived House")).not.toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
