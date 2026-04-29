import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import type { ReactElement } from "react";
import { __resetApiProvidersForTests } from "@/lib/api";
import { __resetQueryKeyGetterForTests } from "@/lib/queryKeys";
import WorkspacesPage from "./WorkspacesPage";

interface FakeResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

interface DeferredResponse {
  promise: Promise<FakeResponse>;
  resolve: (response: FakeResponse) => void;
}

function deferredResponse(): DeferredResponse {
  let resolve!: (response: FakeResponse) => void;
  const promise = new Promise<FakeResponse>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function installFetch(
  scripted: Record<string, Array<FakeResponse | Promise<FakeResponse>>>,
): {
  calls: FetchCall[];
  restore: () => void;
} {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const queues: Record<string, Array<FakeResponse | Promise<FakeResponse>>> = {};
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
    const response = await next;
    const status = response.status ?? 200;
    const ok = status >= 200 && status < 300;
    return {
      ok,
      status,
      statusText: ok ? "OK" : "Error",
      text: async () => JSON.stringify(response.body),
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
      <MemoryRouter initialEntries={["/admin/workspaces"]}>
        <WorkspacesPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function workspaces(overrides: Record<string, unknown> = {}): unknown {
  return {
    workspaces: [
      {
        id: "ws_1",
        slug: "smoke",
        name: "Smoke House",
        plan: "free",
        verification_state: "unverified",
        properties_count: 3,
        members_count: 4,
        spent_cents_30d: 600,
        cap_cents_30d: 1000,
        archived_at: null,
        created_at: "2026-04-01T00:00:00+00:00",
      },
      {
        id: "ws_2",
        slug: "archive",
        name: "Archive House",
        plan: "pro",
        verification_state: "human_verified",
        properties_count: 1,
        members_count: 2,
        spent_cents_30d: 75,
        cap_cents_30d: 500,
        archived_at: "2026-04-20T00:00:00+00:00",
        created_at: "2026-03-01T00:00:00+00:00",
      },
    ],
    ...overrides,
  };
}

function smokeArchived(): unknown {
  return workspaces({
    workspaces: [
      {
        id: "ws_1",
        slug: "smoke",
        name: "Smoke House",
        plan: "free",
        verification_state: "unverified",
        properties_count: 3,
        members_count: 4,
        spent_cents_30d: 600,
        cap_cents_30d: 1000,
        archived_at: "2026-04-29T12:00:00.000Z",
        created_at: "2026-04-01T00:00:00+00:00",
      },
    ],
  });
}

function installPageFetch(extra: Record<string, Array<FakeResponse | Promise<FakeResponse>>> = {}) {
  return installFetch({
    "/admin/api/v1/workspaces": [{ body: workspaces() }],
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

describe("Admin WorkspacesPage", () => {
  it("renders active and archived workspace rows from the API envelope", async () => {
    const fetcher = installPageFetch();
    try {
      render(<Harness />);

      expect(await screen.findByText("Smoke House")).toBeInTheDocument();
      expect(screen.getByText("Active (1)")).toBeInTheDocument();
      expect(screen.getByText("Archived (1)")).toBeInTheDocument();

      const row = rowFor("Smoke House");
      expect(within(row).getByText("/w/smoke")).toBeInTheDocument();
      expect(within(row).getByText("free")).toBeInTheDocument();
      expect(within(row).getByText("unverified")).toBeInTheDocument();
      expect(within(row).getByText("3")).toBeInTheDocument();
      expect(within(row).getByText("4")).toBeInTheDocument();
      expect(within(row).getByText("$6.00")).toBeInTheDocument();
      expect(within(row).getByText("$10.00")).toBeInTheDocument();

      const archivedRow = rowFor("Archive House");
      expect(within(archivedRow).getByText("/w/archive")).toBeInTheDocument();
      expect(within(archivedRow).getByText("pro")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("trusts a workspace optimistically and rolls back when the request fails", async () => {
    const trustResponse = deferredResponse();
    const fetcher = installPageFetch({
      "/admin/api/v1/workspaces": [{ body: workspaces() }, { body: workspaces() }],
      "/admin/api/v1/workspaces/ws_1/trust": [trustResponse.promise],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      const row = rowFor("Smoke House");
      fireEvent.click(within(row).getByRole("button", { name: "Trust" }));

      expect(await within(row).findByText("trusted")).toBeInTheDocument();
      trustResponse.resolve({ status: 500, body: { detail: "boom" } });

      await waitFor(() => {
        expect(within(row).getByText("unverified")).toBeInTheDocument();
      });
      const post = fetcher.calls.find((call) =>
        call.url.endsWith("/admin/api/v1/workspaces/ws_1/trust"),
      );
      expect(post?.init.method).toBe("POST");
    } finally {
      fetcher.restore();
    }
  });

  it("archives a workspace after confirmation with the mock confirmation text", async () => {
    const archiveResponse = deferredResponse();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetcher = installFetch({
      "/admin/api/v1/workspaces": [
        {
          body: workspaces({
            workspaces: [
              {
                id: "ws_1",
                slug: "smoke",
                name: "Smoke House",
                plan: "free",
                verification_state: "unverified",
                properties_count: 3,
                members_count: 4,
                spent_cents_30d: 600,
                cap_cents_30d: 1000,
                archived_at: null,
                created_at: "2026-04-01T00:00:00+00:00",
              },
            ],
          }),
        },
        { body: smokeArchived() },
      ],
      "/admin/api/v1/workspaces/ws_1/archive": [archiveResponse.promise],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      fireEvent.click(screen.getByRole("button", { name: "Archive" }));

      expect(confirm).toHaveBeenCalledWith(
        "Archive Smoke House? Owner can restore from backup.",
      );
      expect(await screen.findByText("Active (0)")).toBeInTheDocument();
      expect(screen.getByText("Archived (1)")).toBeInTheDocument();
      archiveResponse.resolve({
        body: { id: "ws_1", archived_at: "2026-04-29T12:00:00.000Z" },
      });

      await waitFor(() => {
        const post = fetcher.calls.find((call) =>
          call.url.endsWith("/admin/api/v1/workspaces/ws_1/archive"),
        );
        expect(post?.init.method).toBe("POST");
      });
    } finally {
      fetcher.restore();
    }
  });

  it("rolls back the archive optimism when the owners-only endpoint returns 404", async () => {
    const archiveResponse = deferredResponse();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetcher = installPageFetch({
      "/admin/api/v1/workspaces": [{ body: workspaces() }, { body: workspaces() }],
      "/admin/api/v1/workspaces/ws_1/archive": [archiveResponse.promise],
    });
    try {
      render(<Harness />);
      await screen.findByText("Smoke House");

      const row = rowFor("Smoke House");
      fireEvent.click(within(row).getByRole("button", { name: "Archive" }));

      expect(confirm).toHaveBeenCalledWith(
        "Archive Smoke House? Owner can restore from backup.",
      );
      expect(await screen.findByText("Active (0)")).toBeInTheDocument();
      archiveResponse.resolve({ status: 404, body: { error: "not_found" } });

      expect(await screen.findByText("Active (1)")).toBeInTheDocument();
      expect(screen.getByText("Archived (1)")).toBeInTheDocument();
      expect(rowFor("Smoke House")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });

  it("saves cap edits through the usage cap endpoint and updates the row", async () => {
    const fetcher = installPageFetch({
      "/admin/api/v1/workspaces": [
        { body: workspaces() },
        {
          body: workspaces({
            workspaces: [
              {
                id: "ws_1",
                slug: "smoke",
                name: "Smoke House",
                plan: "free",
                verification_state: "unverified",
                properties_count: 3,
                members_count: 4,
                spent_cents_30d: 600,
                cap_cents_30d: 1250,
                archived_at: null,
                created_at: "2026-04-01T00:00:00+00:00",
              },
            ],
          }),
        },
      ],
      "/admin/api/v1/usage/workspaces/ws_1/cap": [
        { body: { workspace_id: "ws_1", cap_cents_30d: 1250 } },
      ],
      "/admin/api/v1/usage/workspaces": [{ body: { workspaces: [] } }],
      "/admin/api/v1/usage/summary": [
        {
          body: {
            window_label: "rolling 30 days",
            deployment_spend_cents_30d: 0,
            deployment_calls_30d: 0,
            workspace_count: 1,
            paused_workspace_count: 0,
            per_capability: [],
          },
        },
      ],
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
      fireEvent.change(input, { target: { value: "12.50" } });
      fireEvent.click(save);

      await waitFor(() => {
        const put = fetcher.calls.find((call) =>
          call.url.endsWith("/admin/api/v1/usage/workspaces/ws_1/cap"),
        );
        expect(put).toBeDefined();
        expect(put?.init.method).toBe("PUT");
        expect(jsonBody(put!)).toEqual({ cap_cents_30d: 1250 });
      });
      expect(await within(row).findByText("$12.50")).toBeInTheDocument();
    } finally {
      fetcher.restore();
    }
  });
});
