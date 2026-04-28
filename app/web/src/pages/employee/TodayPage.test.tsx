import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { ReactElement } from "react";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import {
  __clearOfflineQueueForTests,
  __listQueuedMutationsForTests,
  __resetOfflineQueueForTests,
  drainOfflineQueue,
} from "@/lib/offlineQueue";
import {
  __resetQueryKeyGetterForTests,
  registerQueryKeyWorkspaceGetter,
} from "@/lib/queryKeys";
import { installFakeIndexedDb } from "@/test/fakeIndexedDb";
import TodayPage from "./TodayPage";

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
    const path = paths.find((candidate) => pathname.endsWith(candidate));
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
      <MemoryRouter initialEntries={["/today"]}>
        <Routes>
          <Route path="/today" element={<><TodayPage /><LocationProbe /></>} />
          <Route path="/task/:tid" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function LocationProbe(): ReactElement {
  const loc = useLocation();
  return <span data-testid="location">{loc.pathname}</span>;
}

function me(overrides: Record<string, unknown> = {}): unknown {
  return {
    role: "worker",
    theme: "system",
    agent_sidebar_collapsed: false,
    employee: {
      id: "emp1",
      user_id: "u1",
      first_name: "Ari",
      last_name: "Worker",
      email: "ari@example.test",
      phone: null,
      avatar_url: null,
    },
    manager_name: "Mina",
    today: "2026-04-28",
    now: "2026-04-28T10:00:00Z",
    user_id: "u1",
    agent_approval_mode: "confirm",
    current_workspace_id: "ws1",
    available_workspaces: [],
    client_binding_org_ids: [],
    is_deployment_admin: false,
    is_deployment_owner: false,
    ...overrides,
  };
}

function property(): unknown {
  return {
    id: "p1",
    name: "Villa Sud",
    city: "Nice",
    timezone: "Europe/Paris",
    color: "moss",
    kind: "str",
    areas: ["Kitchen", "Bedroom"],
    evidence_policy: "inherit",
    country: "FR",
    locale: "fr",
    settings_override: {},
    client_org_id: null,
    owner_user_id: null,
  };
}

function task(id: string, overrides: Record<string, unknown> = {}): unknown {
  return {
    id,
    workspace_id: "ws1",
    title: "Task " + id,
    property_id: "p1",
    area_id: "Kitchen",
    priority: "normal",
    state: "pending",
    scheduled_for_utc: "2026-04-28T09:30:00Z",
    duration_minutes: 30,
    photo_evidence: "disabled",
    linked_instruction_ids: [],
    inventory_consumption_json: {},
    assigned_user_id: "u1",
    created_by: "u1",
    is_personal: false,
    checklist: [],
    ...overrides,
  };
}

function taskPage(tasks: unknown[]): unknown {
  return { data: tasks, next_cursor: null, has_more: false };
}

let restoreIndexedDb: (() => void) | null = null;

beforeEach(async () => {
  restoreIndexedDb = installFakeIndexedDb();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  __resetOfflineQueueForTests();
  registerWorkspaceSlugGetter(() => "acme");
  registerQueryKeyWorkspaceGetter(() => "acme");
  await __clearOfflineQueueForTests();
  Object.defineProperty(navigator, "onLine", {
    configurable: true,
    value: true,
  });
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  __resetQueryKeyGetterForTests();
  __resetOfflineQueueForTests();
  restoreIndexedDb?.();
  restoreIndexedDb = null;
  vi.restoreAllMocks();
});

describe("TodayPage", () => {
  it("loads real API data and groups now, upcoming, and completed tasks", async () => {
    const env = installFetch({
      "/api/v1/me": [{ body: me() }],
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/tasks": [
        {
          body: taskPage([
            task("now", { title: "Turn over kitchen", scheduled_for_utc: "2026-04-28T09:30:00Z" }),
            task("later", { title: "Stock pantry", scheduled_for_utc: "2026-04-28T13:00:00Z" }),
            task("done", { title: "Open shutters", state: "done", scheduled_for_utc: "2026-04-28T08:00:00Z" }),
          ]),
        },
      ],
    });

    try {
      render(<Harness />);

      expect(await screen.findByText("Turn over kitchen")).toBeInTheDocument();
      expect(screen.getByText("Upcoming today · 1")).toBeInTheDocument();
      expect(screen.getByText("Stock pantry")).toBeInTheDocument();
      expect(screen.getByText("Completed today")).toBeInTheDocument();
      expect(screen.getByText("Open shutters")).toBeInTheDocument();
      expect(screen.getAllByText("Villa Sud").length).toBeGreaterThan(0);
      const tasksCall = env.calls.find((call) => call.url.includes("/api/v1/tasks?"));
      const params = new URL(tasksCall!.url, "http://crewday.test").searchParams;
      expect(params.get("assignee_user_id")).toBe("u1");
      expect(params.get("scheduled_for_utc_gte")).toBe("2026-04-28T00:00:00.000Z");
      expect(params.get("scheduled_for_utc_lt")).toBe("2026-04-29T00:00:00.000Z");
    } finally {
      env.restore();
    }
  });

  it("does not show skipped or cancelled tasks as actionable today tasks", async () => {
    const env = installFetch({
      "/api/v1/me": [{ body: me() }],
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/tasks": [
        {
          body: taskPage([
            task("skipped", {
              title: "Skipped inspection",
              state: "skipped",
              scheduled_for_utc: "2026-04-28T09:00:00Z",
            }),
            task("cancelled", {
              title: "Cancelled errand",
              state: "cancelled",
              scheduled_for_utc: "2026-04-28T09:15:00Z",
            }),
            task("next", {
              title: "Refresh towels",
              scheduled_for_utc: "2026-04-28T13:00:00Z",
            }),
          ]),
        },
      ],
    });

    try {
      render(<Harness />);

      expect(await screen.findByText("All done for now. Nice work.")).toBeInTheDocument();
      expect(screen.getByText("Refresh towels")).toBeInTheDocument();
      expect(screen.queryByText("Skipped inspection")).not.toBeInTheDocument();
      expect(screen.queryByText("Cancelled errand")).not.toBeInTheDocument();
    } finally {
      env.restore();
    }
  });

  it("quick-completes a non-photo task online with an optimistic move to completed", async () => {
    const env = installFetch({
      "/api/v1/me": [{ body: me() }],
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/tasks": [
        {
          body: taskPage([
            task("t1", { title: "Reset towels", scheduled_for_utc: "2026-04-28T09:00:00Z" }),
          ]),
        },
        {
          body: taskPage([
            task("t1", { title: "Reset towels", state: "done", scheduled_for_utc: "2026-04-28T09:00:00Z" }),
          ]),
        },
      ],
      "/api/v1/tasks/t1/complete": [
        {
          body: {
            task_id: "t1",
            state: "done",
            completed_at: "2026-04-28T10:05:00Z",
            completed_by_user_id: "u1",
            reason: null,
          },
        },
      ],
    });

    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Start" }));

      await waitFor(() => {
        const completeCall = env.calls.find((call) => call.url.endsWith("/api/v1/tasks/t1/complete"));
        expect(completeCall).toBeDefined();
        expect(completeCall!.init.method).toBe("POST");
        expect(completeCall!.init.body).toBe(JSON.stringify({ photo_evidence_ids: [] }));
      });
      await waitFor(() => expect(screen.getByText("Completed today")).toBeInTheDocument());
      expect(screen.getByText("Reset towels")).toBeInTheDocument();
    } finally {
      env.restore();
    }
  });

  it("keeps the non-photo now card title navigable while exposing quick-complete", async () => {
    const env = installFetch({
      "/api/v1/me": [{ body: me() }],
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/tasks": [
        {
          body: taskPage([
            task("t1", { title: "Reset towels", scheduled_for_utc: "2026-04-28T09:00:00Z" }),
          ]),
        },
      ],
    });

    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("link", { name: /reset towels/i }));

      expect(screen.getByTestId("location")).toHaveTextContent("/task/t1");
      expect(env.calls.some((call) => call.url.endsWith("/api/v1/tasks/t1/complete"))).toBe(false);
      await expect(__listQueuedMutationsForTests()).resolves.toHaveLength(0);
    } finally {
      env.restore();
    }
  });

  it("rolls back the optimistic completion when the online request fails", async () => {
    const env = installFetch({
      "/api/v1/me": [{ body: me() }],
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/tasks": [
        {
          body: taskPage([
            task("t1", { title: "Reset linens", scheduled_for_utc: "2026-04-28T09:00:00Z" }),
          ]),
        },
      ],
      "/api/v1/tasks/t1/complete": [
        {
          status: 500,
          body: {
            type: "https://crewday.dev/errors/internal",
            title: "Internal server error",
            status: 500,
          },
        },
      ],
    });

    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Start" }));

      await waitFor(() => {
        expect(env.calls.some((call) => call.url.endsWith("/api/v1/tasks/t1/complete"))).toBe(true);
      });
      await waitFor(() => {
        expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
      });
      expect(screen.getByText("Reset linens")).toBeInTheDocument();
      expect(screen.queryByText("All done for now. Nice work.")).not.toBeInTheDocument();
    } finally {
      env.restore();
    }
  });

  it("queues quick-complete offline and keeps the optimistic completed state", async () => {
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: false,
    });
    const env = installFetch({
      "/api/v1/me": [{ body: me() }],
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/tasks": [
        {
          body: taskPage([
            task("t1", { title: "Check boiler", scheduled_for_utc: "2026-04-28T09:00:00Z" }),
          ]),
        },
      ],
    });

    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Start" }));

      await waitFor(async () => {
        const queued = await __listQueuedMutationsForTests();
        expect(queued).toHaveLength(1);
        expect(queued[0]).toMatchObject({
          kind: "task.complete",
          method: "POST",
          path: "/w/acme/api/v1/tasks/t1/complete",
          body: { photo_evidence_ids: [] },
        });
        expect(queued[0]!.idempotencyKey).toBe("offline:" + queued[0]!.storageKey);
      });
      expect(env.calls.some((call) => call.url.endsWith("/api/v1/tasks/t1/complete"))).toBe(false);
      expect(screen.getByText("Check boiler")).toBeInTheDocument();
      expect(screen.getByText("Completed today")).toBeInTheDocument();
    } finally {
      env.restore();
    }
  });

  it("invalidates today's tasks after an offline completion replays", async () => {
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      value: false,
    });
    const env = installFetch({
      "/api/v1/me": [{ body: me() }],
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/tasks": [
        {
          body: taskPage([
            task("t1", { title: "Queue boiler check", scheduled_for_utc: "2026-04-28T09:00:00Z" }),
          ]),
        },
        {
          body: taskPage([
            task("t1", {
              title: "Queued boiler complete",
              state: "done",
              scheduled_for_utc: "2026-04-28T09:00:00Z",
            }),
          ]),
        },
      ],
      "/api/v1/tasks/t1/complete": [
        {
          body: {
            task_id: "t1",
            state: "done",
            completed_at: "2026-04-28T10:05:00Z",
            completed_by_user_id: "u1",
            reason: null,
          },
        },
      ],
    });

    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("button", { name: "Start" }));

      let idempotencyKey = "";
      await waitFor(async () => {
        const queued = await __listQueuedMutationsForTests();
        expect(queued).toHaveLength(1);
        idempotencyKey = queued[0]!.idempotencyKey;
      });

      Object.defineProperty(navigator, "onLine", {
        configurable: true,
        value: true,
      });
      await drainOfflineQueue({ scheduleRetry: false });

      await waitFor(() => expect(screen.getByText("Queued boiler complete")).toBeInTheDocument());
      const replayCall = env.calls.find((call) => call.url.endsWith("/api/v1/tasks/t1/complete"));
      expect((replayCall!.init.headers as Record<string, string>)["Idempotency-Key"]).toBe(idempotencyKey);
    } finally {
      env.restore();
    }
  });

  it("routes photo-required tasks to detail instead of completing them from Today", async () => {
    const env = installFetch({
      "/api/v1/me": [{ body: me() }],
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/tasks": [
        {
          body: taskPage([
            task("photo", {
              title: "Photograph minibar",
              photo_evidence: "required",
              scheduled_for_utc: "2026-04-28T09:00:00Z",
            }),
          ]),
        },
      ],
    });

    try {
      render(<Harness />);

      fireEvent.click(await screen.findByRole("link", { name: /photograph minibar/i }));

      expect(screen.getByTestId("location")).toHaveTextContent("/task/photo");
      expect(env.calls.some((call) => call.url.endsWith("/api/v1/tasks/photo/complete"))).toBe(false);
      await expect(__listQueuedMutationsForTests()).resolves.toHaveLength(0);
    } finally {
      env.restore();
    }
  });

  it("renders an error state when today's task load fails", async () => {
    const env = installFetch({
      "/api/v1/me": [{ body: me() }],
      "/api/v1/properties": [{ body: [property()] }],
      "/api/v1/tasks": [
        {
          status: 500,
          body: {
            type: "https://crewday.dev/errors/internal",
            title: "Internal server error",
            status: 500,
          },
        },
      ],
    });

    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
    } finally {
      env.restore();
    }
  });

  it("renders an error state when the current user load fails", async () => {
    const env = installFetch({
      "/api/v1/me": [
        {
          status: 500,
          body: {
            type: "https://crewday.dev/errors/internal",
            title: "Internal server error",
            status: 500,
          },
        },
      ],
      "/api/v1/properties": [{ body: [property()] }],
    });

    try {
      render(<Harness />);

      expect(await screen.findByText("Failed to load.")).toBeInTheDocument();
      expect(env.calls.some((call) => call.url.includes("/api/v1/tasks?"))).toBe(false);
    } finally {
      env.restore();
    }
  });
});
