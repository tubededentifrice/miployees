import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetApiProvidersForTests, registerWorkspaceSlugGetter } from "@/lib/api";
import {
  __clearOfflineQueueForTests,
  __listQueuedMutationsForTests,
  __resetOfflineQueueForTests,
  drainOfflineQueue,
} from "@/lib/offlineQueue";
import { qk } from "@/lib/queryKeys";
import { useDecideMutation } from "@/lib/useDecideMutation";
import { installFakeIndexedDb } from "@/test/fakeIndexedDb";

interface Row {
  id: string;
  status: "pending" | "approved";
}

let restoreIndexedDb: (() => void) | null = null;

beforeEach(async () => {
  restoreIndexedDb = installFakeIndexedDb();
  __resetApiProvidersForTests();
  __resetOfflineQueueForTests();
  await __clearOfflineQueueForTests();
  Object.defineProperty(window.navigator, "onLine", {
    configurable: true,
    value: false,
  });
});

afterEach(() => {
  __resetApiProvidersForTests();
  __resetOfflineQueueForTests();
  restoreIndexedDb?.();
  restoreIndexedDb = null;
  vi.restoreAllMocks();
});

describe("useDecideMutation offline queueing", () => {
  it("queues the decision without fetching when the browser is offline", async () => {
    registerWorkspaceSlugGetter(() => "acme");
    const fetchSpy = vi.fn();
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const queryKey = qk.tasks();
    qc.setQueryData<Row[]>(queryKey, [{ id: "t1", status: "pending" }]);

    const { result } = renderHook(
      () => useDecideMutation<Row[], "approved">({
        queryKey,
        endpoint: (id, decision) => `/api/v1/tasks/${id}/${decision}`,
        applyOptimistic: (prev, id, decision) =>
          prev.map((row) => row.id === id ? { ...row, status: decision } : row),
      }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={qc}>{children}</QueryClientProvider>
        ),
      },
    );

    await act(async () => {
      await result.current.mutateAsync({ id: "t1", decision: "approved" });
      await result.current.mutateAsync({ id: "t1", decision: "approved" });
    });

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(qc.getQueryData<Row[]>(queryKey)).toEqual([{ id: "t1", status: "approved" }]);
    expect(qc.getQueryState(queryKey)?.isInvalidated).toBe(false);
    await expect(__listQueuedMutationsForTests()).resolves.toMatchObject([
      {
        id: "decision:/w/acme/api/v1/tasks/t1/approved",
        storageKey: "acme:decision:/w/acme/api/v1/tasks/t1/approved",
        kind: "decision",
        method: "POST",
        path: "/w/acme/api/v1/tasks/t1/approved",
        idempotencyKey: "decision:/w/acme/api/v1/tasks/t1/approved",
      },
    ]);
  });

  it("invalidates the optimistic query when a queued decision replays successfully", async () => {
    registerWorkspaceSlugGetter(() => "acme");
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const queryKey = qk.tasks();
    qc.setQueryData<Row[]>(queryKey, [{ id: "t1", status: "pending" }]);
    const { result } = renderHook(
      () => useDecideMutation<Row[], "approved">({
        queryKey,
        endpoint: (id, decision) => `/api/v1/tasks/${id}/${decision}`,
        applyOptimistic: (prev, id, decision) =>
          prev.map((row) => row.id === id ? { ...row, status: decision } : row),
      }),
      {
        wrapper: ({ children }: { children: ReactNode }) => (
          <QueryClientProvider client={qc}>{children}</QueryClientProvider>
        ),
      },
    );

    await act(async () => {
      await result.current.mutateAsync({ id: "t1", decision: "approved" });
    });
    Object.defineProperty(window.navigator, "onLine", {
      configurable: true,
      value: true,
    });
    const fetchSpy = vi.fn(async () => ({
      ok: true,
      status: 204,
      statusText: "No Content",
      text: async () => "",
    }) as Response);
    (globalThis as { fetch: typeof fetch }).fetch = fetchSpy as unknown as typeof fetch;

    await act(async () => {
      await drainOfflineQueue({ scheduleRetry: false });
    });

    expect(fetchSpy).toHaveBeenCalledOnce();
    expect(qc.getQueryState(queryKey)?.isInvalidated).toBe(true);
  });
});
