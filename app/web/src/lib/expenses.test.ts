import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import {
  fetchAllExpenseClaims,
  fetchExpenseClaimsPage,
  mapExpenseClaimPayload,
  type ExpenseClaimPayload,
} from "@/lib/expenses";
import type { Expense } from "@/types/expense";

// The helper unwraps the `{data, next_cursor, has_more}` envelope from
// `GET /api/v1/expenses` and projects each row into the SPA's
// `Expense` shape. The tests below stub `fetch` directly because
// `fetchJson` only ever calls `fetch(url, init)` — same approach as
// `lib/api.test.ts`, so a single fake transport covers every case
// without pulling in msw.

interface FakeResponse {
  status?: number;
  body: unknown;
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
    const status = next.status ?? 200;
    const ok = status >= 200 && status < 300;
    const text =
      typeof next.body === "string"
        ? next.body
        : next.body === null || next.body === undefined
          ? ""
          : JSON.stringify(next.body);
    return {
      ok,
      status,
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

function payload(overrides: Partial<ExpenseClaimPayload> = {}): ExpenseClaimPayload {
  return {
    id: "claim-1",
    workspace_id: "ws-1",
    work_engagement_id: "we-1",
    vendor: "Carrefour",
    purchased_at: "2026-04-20T10:00:00Z",
    currency: "EUR",
    total_amount_cents: 1234,
    category: "supplies",
    property_id: null,
    note_md: "Cleaning supplies",
    state: "submitted",
    submitted_at: "2026-04-21T08:30:00Z",
    decided_by: null,
    decided_at: null,
    decision_note_md: null,
    created_at: "2026-04-20T10:05:00Z",
    deleted_at: null,
    attachments: [],
    ...overrides,
  };
}

beforeEach(() => {
  __resetApiProvidersForTests();
  registerWorkspaceSlugGetter(() => "acme");
  document.cookie = "crewday_csrf=; path=/; max-age=0";
});

afterEach(() => {
  __resetApiProvidersForTests();
});

describe("mapExpenseClaimPayload", () => {
  it("projects a wire row 1:1 into the SPA Expense shape", () => {
    const wire = payload({
      vendor: "Bricorama",
      total_amount_cents: 42_00,
      state: "approved",
      decided_by: "user-2",
      decided_at: "2026-04-22T09:00:00Z",
      attachments: [
        {
          id: "att-1",
          claim_id: "claim-1",
          blob_hash: "deadbeef".repeat(8),
          kind: "receipt",
          pages: 1,
          created_at: "2026-04-20T10:10:00Z",
        },
      ],
    });

    const out: Expense = mapExpenseClaimPayload(wire);

    expect(out.id).toBe(wire.id);
    expect(out.vendor).toBe("Bricorama");
    expect(out.total_amount_cents).toBe(4200);
    expect(out.state).toBe("approved");
    expect(out.decided_by).toBe("user-2");
    expect(out.attachments).toHaveLength(1);
    expect(out.attachments[0]).toEqual(wire.attachments[0]);
  });

  it("returns a fresh attachment array (no shared reference)", () => {
    // The helper rebuilds the object so a future mapping step (a
    // derived field, a normalisation pass) cannot mutate the
    // upstream payload. Lock in the contract here.
    const wire = payload({
      attachments: [
        {
          id: "att-1",
          claim_id: "claim-1",
          blob_hash: "0".repeat(64),
          kind: "receipt",
          pages: null,
          created_at: "2026-04-20T10:10:00Z",
        },
      ],
    });

    const out = mapExpenseClaimPayload(wire);
    expect(out.attachments).not.toBe(wire.attachments);
  });
});

describe("fetchExpenseClaimsPage", () => {
  it("unwraps the data/next_cursor/has_more envelope", async () => {
    const wire = payload();
    const env = installFetch([
      {
        body: {
          data: [wire],
          next_cursor: "cur-2",
          has_more: true,
        },
      },
    ]);
    try {
      const page = await fetchExpenseClaimsPage();
      expect(env.calls).toHaveLength(1);
      // Workspace prefix is applied by `fetchJson`/`resolveApiPath`.
      expect(env.calls[0]!.url).toBe("/w/acme/api/v1/expenses");
      expect(page.next_cursor).toBe("cur-2");
      expect(page.has_more).toBe(true);
      expect(page.data).toHaveLength(1);
      expect(page.data[0]!.vendor).toBe(wire.vendor);
    } finally {
      env.restore();
    }
  });

  it("propagates user_id, state, cursor, and limit as query parameters", async () => {
    const env = installFetch([
      { body: { data: [], next_cursor: null, has_more: false } },
    ]);
    try {
      await fetchExpenseClaimsPage({
        userId: "user-2",
        state: "approved",
        cursor: "cur-1",
        limit: 10,
      });
      const url = env.calls[0]!.url;
      // URL ordering is the URLSearchParams insertion order; pin
      // each param so a future param addition shows in the test.
      expect(url).toBe(
        "/w/acme/api/v1/expenses?user_id=user-2&state=approved&cursor=cur-1&limit=10",
      );
    } finally {
      env.restore();
    }
  });

  it("emits the bare /api/v1/expenses path when no filters are set", async () => {
    const env = installFetch([
      { body: { data: [], next_cursor: null, has_more: false } },
    ]);
    try {
      await fetchExpenseClaimsPage();
      expect(env.calls[0]!.url).toBe("/w/acme/api/v1/expenses");
    } finally {
      env.restore();
    }
  });

  it("returns an empty list when the server reports no rows", async () => {
    const env = installFetch([
      { body: { data: [], next_cursor: null, has_more: false } },
    ]);
    try {
      const page = await fetchExpenseClaimsPage();
      expect(page.data).toEqual([]);
      expect(page.next_cursor).toBeNull();
      expect(page.has_more).toBe(false);
    } finally {
      env.restore();
    }
  });
});

describe("fetchAllExpenseClaims", () => {
  it("walks every page until has_more is false", async () => {
    const a = payload({ id: "claim-1" });
    const b = payload({ id: "claim-2" });
    const c = payload({ id: "claim-3" });
    const env = installFetch([
      { body: { data: [a], next_cursor: "cur-2", has_more: true } },
      { body: { data: [b], next_cursor: "cur-3", has_more: true } },
      { body: { data: [c], next_cursor: null, has_more: false } },
    ]);
    try {
      const out = await fetchAllExpenseClaims();
      expect(out.map((x) => x.id)).toEqual(["claim-1", "claim-2", "claim-3"]);
      // Each follow-up call carries the previous page's cursor.
      expect(env.calls[0]!.url).toBe("/w/acme/api/v1/expenses");
      expect(env.calls[1]!.url).toBe("/w/acme/api/v1/expenses?cursor=cur-2");
      expect(env.calls[2]!.url).toBe("/w/acme/api/v1/expenses?cursor=cur-3");
    } finally {
      env.restore();
    }
  });

  it("stops walking when has_more is true but next_cursor is null", async () => {
    // Defensive against a server bug where the envelope reports
    // more pages but doesn't supply a cursor — bailing out is
    // safer than a forever-loop.
    const a = payload({ id: "claim-1" });
    const env = installFetch([
      { body: { data: [a], next_cursor: null, has_more: true } },
    ]);
    try {
      const out = await fetchAllExpenseClaims();
      expect(out).toHaveLength(1);
      expect(env.calls).toHaveLength(1);
    } finally {
      env.restore();
    }
  });

  it("returns an empty array when the first page is empty", async () => {
    const env = installFetch([
      { body: { data: [], next_cursor: null, has_more: false } },
    ]);
    try {
      const out = await fetchAllExpenseClaims();
      expect(out).toEqual([]);
    } finally {
      env.restore();
    }
  });

  it("forwards the filter options on every page request", async () => {
    const a = payload({ id: "a" });
    const b = payload({ id: "b" });
    const env = installFetch([
      { body: { data: [a], next_cursor: "cur-2", has_more: true } },
      { body: { data: [b], next_cursor: null, has_more: false } },
    ]);
    try {
      await fetchAllExpenseClaims({ state: "submitted" });
      expect(env.calls[0]!.url).toBe(
        "/w/acme/api/v1/expenses?state=submitted",
      );
      expect(env.calls[1]!.url).toBe(
        "/w/acme/api/v1/expenses?state=submitted&cursor=cur-2",
      );
    } finally {
      env.restore();
    }
  });

  it("throws after 50 pages instead of looping forever", async () => {
    // Defence-in-depth against a server bug that keeps reporting
    // `has_more: true` with a fresh cursor on every page. Bounding
    // the walk surfaces the loop instead of wedging the tab and
    // exhausting `fetch` quotas. The cap (50) is documented inline
    // in the helper; a future tweak should update both sides.
    const responses = Array.from({ length: 51 }, (_, i) => ({
      body: {
        data: [payload({ id: `claim-${i}` })],
        next_cursor: `cur-${i + 1}`,
        has_more: true,
      },
    }));
    const env = installFetch(responses);
    try {
      await expect(fetchAllExpenseClaims()).rejects.toThrow(
        /exceeded 50 pages/,
      );
      // Hit the cap and stopped — never made the 51st call.
      expect(env.calls).toHaveLength(50);
    } finally {
      env.restore();
    }
  });
});
