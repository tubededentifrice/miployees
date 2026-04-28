import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __clearOfflineQueueForTests,
  __listQueuedMutationsForTests,
  __resetOfflineQueueForTests,
  drainOfflineQueue,
  enqueueMutation,
  pendingMutationCount,
  startOfflineQueueReplay,
  subscribePendingMutationCount,
} from "@/lib/offlineQueue";
import { __resetApiProvidersForTests, registerWorkspaceSlugGetter } from "@/lib/api";
import { installFakeIndexedDb } from "@/test/fakeIndexedDb";

interface FakeResponse {
  status: number;
  body?: unknown;
}

function installFetch(responses: FakeResponse[]): {
  calls: Array<{ url: string; init: RequestInit }>;
  restore: () => void;
} {
  const original = globalThis.fetch;
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const next = responses.shift();
    if (!next) throw new Error(`Unexpected fetch call: ${resolved}`);
    return {
      ok: next.status >= 200 && next.status < 300,
      status: next.status,
      statusText: next.status >= 200 && next.status < 300 ? "OK" : "Error",
      text: async () => next.body === undefined ? "" : JSON.stringify(next.body),
    } as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

let restoreIndexedDb: (() => void) | null = null;

beforeEach(async () => {
  restoreIndexedDb = installFakeIndexedDb();
  __resetApiProvidersForTests();
  __resetOfflineQueueForTests();
  await __clearOfflineQueueForTests();
});

afterEach(() => {
  __resetApiProvidersForTests();
  __resetOfflineQueueForTests();
  restoreIndexedDb?.();
  restoreIndexedDb = null;
  vi.restoreAllMocks();
});

describe("offline queue", () => {
  it("enqueues pending mutations in IndexedDB with scoped paths and stable idempotency keys", async () => {
    registerWorkspaceSlugGetter(() => "acme");

    const entry = await enqueueMutation({
      id: "q1",
      kind: "decision",
      method: "POST",
      path: "/api/v1/tasks/t1/approve",
      body: { note: "ok" },
      createdAt: 100,
    });

    expect(entry).toMatchObject({
      id: "q1",
      workspaceSlug: "acme",
      storageKey: "acme:q1",
      kind: "decision",
      method: "POST",
      path: "/w/acme/api/v1/tasks/t1/approve",
      body: { note: "ok" },
      idempotencyKey: "offline:acme:q1",
      createdAt: 100,
      attempts: 0,
      nextAttemptAt: 100,
    });
    await expect(__listQueuedMutationsForTests()).resolves.toHaveLength(1);
  });

  it("notifies pending-count subscribers after enqueue", async () => {
    const observed: number[] = [];
    const unsubscribe = subscribePendingMutationCount((count) => observed.push(count));

    await enqueueMutation({
      id: "q1",
      kind: "decision",
      method: "POST",
      path: "/api/v1/tasks/t1/approve",
    });

    expect(observed).toContain(1);
    unsubscribe();
  });

  it("counts pending mutations only for the active workspace", async () => {
    await enqueueMutation({
      id: "q1",
      kind: "decision",
      method: "POST",
      path: "/w/acme/api/v1/tasks/t1/approve",
    });
    await enqueueMutation({
      id: "q1",
      kind: "decision",
      method: "POST",
      path: "/w/beta/api/v1/tasks/t1/approve",
    });

    await expect(pendingMutationCount("acme")).resolves.toBe(1);
    await expect(pendingMutationCount("beta")).resolves.toBe(1);
  });

  it("replays due entries FIFO, sends the stored idempotency key, and removes successes", async () => {
    registerWorkspaceSlugGetter(() => "acme");
    await enqueueMutation({
      id: "first",
      kind: "decision",
      method: "POST",
      path: "/api/v1/tasks/t1/approve",
      idempotencyKey: "stable-first",
      createdAt: 100,
    });
    await enqueueMutation({
      id: "second",
      kind: "decision",
      method: "PATCH",
      path: "/api/v1/tasks/t2",
      body: { done: true },
      idempotencyKey: "stable-second",
      createdAt: 101,
    });
    const { calls, restore } = installFetch([
      { status: 204 },
      { status: 200, body: { ok: true } },
    ]);
    try {
      const result = await drainOfflineQueue({ now: () => 200, scheduleRetry: false });

      expect(result).toEqual({ attempted: 2, succeeded: 2, failed: 0 });
      expect(calls.map((call) => call.url)).toEqual([
        "/w/acme/api/v1/tasks/t1/approve",
        "/w/acme/api/v1/tasks/t2",
      ]);
      expect((calls[0]!.init.headers as Record<string, string>)["Idempotency-Key"]).toBe("stable-first");
      expect((calls[1]!.init.headers as Record<string, string>)["Idempotency-Key"]).toBe("stable-second");
      await expect(__listQueuedMutationsForTests()).resolves.toEqual([]);
    } finally {
      restore();
    }
  });

  it("backs off a failed replay and keeps the same idempotency key for the next attempt", async () => {
    registerWorkspaceSlugGetter(() => "acme");
    await enqueueMutation({
      id: "q1",
      kind: "decision",
      method: "POST",
      path: "/api/v1/tasks/t1/approve",
      idempotencyKey: "stable-q1",
      createdAt: 100,
    });
    const { calls, restore } = installFetch([{ status: 500, body: { detail: "nope" } }]);
    try {
      const result = await drainOfflineQueue({ now: () => 1_000, scheduleRetry: false });
      const [entry] = await __listQueuedMutationsForTests();

      expect(result).toEqual({ attempted: 1, succeeded: 0, failed: 1 });
      expect(entry).toMatchObject({
        id: "q1",
        attempts: 1,
        nextAttemptAt: 2_000,
        idempotencyKey: "stable-q1",
      });
      expect((calls[0]!.init.headers as Record<string, string>)["Idempotency-Key"]).toBe("stable-q1");
    } finally {
      restore();
    }
  });

  it("does not install duplicate online replay listeners", async () => {
    Object.defineProperty(window.navigator, "onLine", {
      configurable: true,
      value: false,
    });
    registerWorkspaceSlugGetter(() => "acme");
    await enqueueMutation({
      id: "q1",
      kind: "decision",
      method: "POST",
      path: "/api/v1/tasks/t1/approve",
      idempotencyKey: "stable-q1",
      createdAt: 100,
    });
    const { calls, restore } = installFetch([{ status: 204 }]);
    const stopFirst = startOfflineQueueReplay();
    const stopSecond = startOfflineQueueReplay();
    try {
      await new Promise((resolve) => setTimeout(resolve, 0));
      Object.defineProperty(window.navigator, "onLine", {
        configurable: true,
        value: true,
      });
      window.dispatchEvent(new Event("online"));
      await vi.waitFor(() => expect(calls).toHaveLength(1));
      expect(stopSecond).toBe(stopFirst);
    } finally {
      stopFirst();
      restore();
    }
  });
});
