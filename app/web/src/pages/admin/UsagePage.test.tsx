import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import UsagePage from "./UsagePage";

interface FakeResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function installFetch(scripted: Record<string, FakeResponse[]>): {
  calls: FetchCall[];
  restore: () => void;
} {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [path, responses] of Object.entries(scripted)) {
    queues[path] = [...responses];
  }
  const paths = Object.keys(queues).sort((a, b) => b.length - a.length);
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const pathname = new URL(resolved, "http://crewday.test").pathname;
    const path = paths.find((candidate) => pathname === candidate);
    if (!path) throw new Error(`Unscripted fetch: ${resolved}`);
    const next = queues[path]!.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    const status = next.status ?? 200;
    const ok = status >= 200 && status < 300;
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
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

function Harness(): ReactElement {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/admin/usage"]}>
        <UsagePage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function summary(overrides: Record<string, unknown> = {}): unknown {
  return {
    window_label: "rolling 30 days",
    deployment_spend_cents_30d: 1850,
    deployment_calls_30d: 42,
    workspace_count: 2,
    paused_workspace_count: 1,
    per_capability: [
      { capability: "chat.manager", spend_cents_30d: 1200, calls_30d: 25 },
      { capability: "chat.employee", spend_cents_30d: 650, calls_30d: 17 },
    ],
    ...overrides,
  };
}

function workspaces(overrides: Record<string, unknown> = {}): unknown {
  return {
    workspaces: [
      {
        workspace_id: "ws_1",
        slug: "smoke",
        name: "Smoke House",
        cap_cents_30d: 1000,
        spent_cents_30d: 600,
        percent: 60,
        paused: false,
      },
      {
        workspace_id: "ws_2",
        slug: "paused",
        name: "Paused House",
        cap_cents_30d: 500,
        spent_cents_30d: 750,
        percent: 100,
        paused: true,
      },
    ],
    ...overrides,
  };
}

function workspaceMeta(overrides: Record<string, unknown> = {}): unknown {
  return {
    workspaces: [
      {
        id: "ws_1",
        slug: "smoke",
        name: "Smoke House",
        plan: "free",
        verification_state: "trusted",
        members_count: 4,
        archived_at: null,
        created_at: "2026-04-01T00:00:00+00:00",
      },
      {
        id: "ws_2",
        slug: "paused",
        name: "Paused House",
        plan: "trial",
        verification_state: "human_verified",
        members_count: 2,
        archived_at: null,
        created_at: "2026-04-01T00:00:00+00:00",
      },
    ],
    ...overrides,
  };
}

function installPageFetch(extra: Record<string, FakeResponse[]> = {}) {
  return installFetch({
    "/admin/api/v1/usage/summary": [{ body: summary() }],
    "/admin/api/v1/usage/workspaces": [{ body: workspaces() }],
    "/admin/api/v1/workspaces": [{ body: workspaceMeta() }],
    ...extra,
  });
}

function rowFor(text: string): HTMLTableRowElement {
  const row = screen.getByText(text).closest("tr");
  if (!(row instanceof HTMLTableRowElement)) throw new Error(`No row for ${text}`);
  return row;
}

function jsonBody(call: FetchCall): unknown {
  return JSON.parse(String(call.init.body));
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
});

describe("Admin UsagePage", () => {
  it("renders usage summary, workspace rows, and capability ledger from API envelopes", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Smoke House")).toBeInTheDocument();
      expect(screen.getByText("$18.50")).toBeInTheDocument();
      expect(screen.getByText("42")).toBeInTheDocument();
      expect(screen.getAllByText("chat.manager")).toHaveLength(2);
      expect(screen.getAllByText("paused").length).toBeGreaterThan(0);

      const row = rowFor("Smoke House");
      expect(within(row).getByText("trusted")).toBeInTheDocument();
      expect(within(row).getByText("$6.00")).toBeInTheDocument();
      expect(within(row).getByText("$10.00")).toBeInTheDocument();
      expect(within(row).getByText("60%")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("saves cap edits through the real PUT body and updates the row optimistically", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/usage/workspaces/ws_1/cap": [
        { body: { workspace_id: "ws_1", cap_cents_30d: 1500 } },
      ],
      "/admin/api/v1/usage/workspaces": [
        { body: workspaces() },
        {
          body: workspaces({
            workspaces: [
              {
                workspace_id: "ws_1",
                slug: "smoke",
                name: "Smoke House",
                cap_cents_30d: 1500,
                spent_cents_30d: 600,
                percent: 40,
                paused: false,
              },
            ],
          }),
        },
      ],
      "/admin/api/v1/usage/summary": [{ body: summary() }, { body: summary() }],
      "/admin/api/v1/workspaces": [{ body: workspaceMeta() }, { body: workspaceMeta() }],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      const row = rowFor("Smoke House");
      fireEvent.click(within(row).getByRole("button", { name: "Edit cap" }));
      const input = within(row).getByDisplayValue("10.00");
      const save = within(row).getByRole("button", { name: "Save" });
      fireEvent.change(input, { target: { value: "" } });
      expect(save).toBeDisabled();
      fireEvent.change(input, {
        target: { value: "15.00" },
      });
      fireEvent.click(save);

      await waitFor(() => {
        const put = fetcher.calls.find((call) =>
          call.url.endsWith("/admin/api/v1/usage/workspaces/ws_1/cap"),
        );
        expect(put).toBeDefined();
        expect(put?.init.method).toBe("PUT");
        expect(jsonBody(put!)).toEqual({ cap_cents_30d: 1500 });
      });
      expect(await within(row).findByText("$15.00")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });
});
