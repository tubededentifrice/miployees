import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import {
  buildExpenseClaimCreatePayload,
  fetchActiveEngagementId,
  fetchAllExpenseClaims,
  fetchExpenseClaimsPage,
  mapExpenseClaimPayload,
  type ExpenseClaimFormInput,
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

  it("emits ?mine=true when the explicit self-only flag is set (cd-qcj2)", async () => {
    // The worker "Recent expenses" panel passes `mine: true` so the
    // server pins the listing to the caller without checking the
    // `expenses.approve` cap. Pin the URL exactly so a regression
    // (param renamed, default flipped) shows up here.
    const env = installFetch([
      { body: { data: [], next_cursor: null, has_more: false } },
    ]);
    try {
      await fetchExpenseClaimsPage({ mine: true });
      expect(env.calls[0]!.url).toBe("/w/acme/api/v1/expenses?mine=true");
    } finally {
      env.restore();
    }
  });

  it("does not emit ?mine= when the flag is omitted or false", async () => {
    // `mine: false` is the same as omission — neither emits the
    // param, since the server's default is already "caller's own
    // claims". Sending `mine=false` would still be valid but would
    // bloat the URL and create cache-key drift in TanStack Query.
    const env = installFetch([
      { body: { data: [], next_cursor: null, has_more: false } },
      { body: { data: [], next_cursor: null, has_more: false } },
    ]);
    try {
      await fetchExpenseClaimsPage();
      await fetchExpenseClaimsPage({ mine: false });
      expect(env.calls[0]!.url).toBe("/w/acme/api/v1/expenses");
      expect(env.calls[1]!.url).toBe("/w/acme/api/v1/expenses");
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

  it("carries mine=true across every page during a multi-page walk (cd-qcj2)", async () => {
    // Regression guard: if the per-page call ever drops `mine: true`
    // on the second page, a worker with >1 page of claims would 403
    // mid-walk (since the second page would silently fall through to
    // the workspace-wide branch). Pin both URLs.
    const a = payload({ id: "a" });
    const b = payload({ id: "b" });
    const env = installFetch([
      { body: { data: [a], next_cursor: "cur-2", has_more: true } },
      { body: { data: [b], next_cursor: null, has_more: false } },
    ]);
    try {
      const out = await fetchAllExpenseClaims({ mine: true });
      expect(out).toHaveLength(2);
      expect(env.calls[0]!.url).toBe("/w/acme/api/v1/expenses?mine=true");
      expect(env.calls[1]!.url).toBe(
        "/w/acme/api/v1/expenses?mine=true&cursor=cur-2",
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

// ---------------------------------------------------------------------------
// Create payload builder (cd-7mpx)
// ---------------------------------------------------------------------------
//
// `buildExpenseClaimCreatePayload` is the single place the worker
// form's local state is projected onto the wire shape from
// `app/domain/expenses/claims.py:417`. The tests pin every conversion
// rule (cents, ISO datetime, optional property pin, currency
// uppercasing, validation) so a future tweak that drifts from the
// server contract shows up here before it ships.

function input(over: Partial<ExpenseClaimFormInput> = {}): ExpenseClaimFormInput {
  return {
    work_engagement_id: "we-1",
    vendor: "Carrefour",
    purchased_on: "2026-04-20",
    amount: "12.50",
    currency: "EUR",
    category: "supplies",
    property_id: "",
    note_md: "Cleaning supplies",
    ...over,
  };
}

describe("buildExpenseClaimCreatePayload", () => {
  it("projects the form state into the ExpenseClaimCreate wire shape", () => {
    const out = buildExpenseClaimCreatePayload(input());
    expect(out.work_engagement_id).toBe("we-1");
    expect(out.vendor).toBe("Carrefour");
    expect(out.currency).toBe("EUR");
    expect(out.category).toBe("supplies");
    expect(out.note_md).toBe("Cleaning supplies");
    // `total_amount_cents` is integer cents: 12.50 → 1250.
    expect(out.total_amount_cents).toBe(1250);
    // `purchased_at` is a `Z`-suffixed ISO string — the server's
    // DTO rejects naive timestamps. The anchor is local-noon of
    // the picked calendar date, which round-trips back through
    // `toLocaleDateString` in the worker's timezone without DST or
    // ±12h tz drift.
    expect(out.purchased_at).toMatch(/Z$/);
    const round = new Date(out.purchased_at);
    expect(round.getFullYear()).toBe(2026);
    expect(round.getMonth()).toBe(3); // April (0-indexed)
    expect(round.getDate()).toBe(20);
    expect(round.getHours()).toBe(12);
    // No property pin selected → the field is absent on the wire,
    // not present-with-empty-string.
    expect("property_id" in out).toBe(false);
    // Locks the full key set so a future drift is loud here.
    expect(Object.keys(out).sort()).toEqual([
      "category",
      "currency",
      "note_md",
      "purchased_at",
      "total_amount_cents",
      "vendor",
      "work_engagement_id",
    ]);
  });

  it("rounds to the nearest cent rather than truncating", () => {
    // `Math.round` is half-away-from-zero on positive reals, so a
    // worker who types "12.567" lands on 1257 — the closest cent —
    // not 1256 (truncation) or a 1.000000001-style float artefact.
    expect(buildExpenseClaimCreatePayload(input({ amount: "12.567" })).total_amount_cents)
      .toBe(1257);
    expect(buildExpenseClaimCreatePayload(input({ amount: "12.564" })).total_amount_cents)
      .toBe(1256);
  });

  it("uppercases lowercase currency input so the server sees the canonical 3-letter code", () => {
    const out = buildExpenseClaimCreatePayload(input({ currency: "usd" }));
    expect(out.currency).toBe("USD");
  });

  it("includes property_id when the form selected a property", () => {
    const out = buildExpenseClaimCreatePayload(input({ property_id: "prop-7" }));
    expect(out.property_id).toBe("prop-7");
  });

  it("trims a whitespace-only property_id back to omitted (treats it as 'unset')", () => {
    const out = buildExpenseClaimCreatePayload(input({ property_id: "   " }));
    expect("property_id" in out).toBe(false);
  });

  it("forwards note_md verbatim, including an empty string", () => {
    const out = buildExpenseClaimCreatePayload(input({ note_md: "" }));
    expect(out.note_md).toBe("");
  });

  it("rejects an empty vendor — the server's min_length=1 would otherwise 422", () => {
    expect(() => buildExpenseClaimCreatePayload(input({ vendor: "   " })))
      .toThrow(/vendor/);
  });

  it("rejects a missing work_engagement_id so the form surfaces it locally", () => {
    expect(() => buildExpenseClaimCreatePayload(input({ work_engagement_id: "" })))
      .toThrow(/work_engagement_id/);
  });

  it("rejects a non-positive amount — the server's gt=0 would otherwise fire", () => {
    expect(() => buildExpenseClaimCreatePayload(input({ amount: "0" })))
      .toThrow(/positive/);
    expect(() => buildExpenseClaimCreatePayload(input({ amount: "-5" })))
      .toThrow(/positive/);
  });

  it("rejects an unparseable amount", () => {
    expect(() => buildExpenseClaimCreatePayload(input({ amount: "not-a-number" })))
      .toThrow(/number/);
  });

  it("rejects a malformed purchased_on so a typo doesn't reach the server", () => {
    expect(() => buildExpenseClaimCreatePayload(input({ purchased_on: "2026/04/20" })))
      .toThrow(/YYYY-MM-DD/);
  });

  it("rejects a non-3-letter currency (the server enforces ISO-3 length)", () => {
    expect(() => buildExpenseClaimCreatePayload(input({ currency: "EU" })))
      .toThrow(/3-letter/);
    expect(() => buildExpenseClaimCreatePayload(input({ currency: "EURO" })))
      .toThrow(/3-letter/);
  });

  it("trims vendor surrounding whitespace before sending", () => {
    const out = buildExpenseClaimCreatePayload(input({ vendor: "  Carrefour  " }));
    expect(out.vendor).toBe("Carrefour");
  });
});

describe("fetchActiveEngagementId", () => {
  it("returns the first non-archived engagement id", async () => {
    const env = installFetch([
      {
        body: {
          data: [
            {
              id: "we-1",
              user_id: "u-1",
              workspace_id: "ws-1",
              archived_on: null,
            },
          ],
          next_cursor: null,
          has_more: false,
        },
      },
    ]);
    try {
      const id = await fetchActiveEngagementId("u-1");
      expect(id).toBe("we-1");
      // The helper passes both filters so the server returns the
      // narrowest possible page.
      expect(env.calls[0]!.url).toBe(
        "/w/acme/api/v1/work_engagements?user_id=u-1&active=true",
      );
    } finally {
      env.restore();
    }
  });

  it("skips archived rows even when the server returns one", async () => {
    // Defence-in-depth: the `active=true` filter should already
    // exclude archived rows server-side, but the SPA double-checks
    // so a server bug doesn't bind a worker's expense claim to a
    // wound-down engagement.
    const env = installFetch([
      {
        body: {
          data: [
            {
              id: "we-old",
              user_id: "u-1",
              workspace_id: "ws-1",
              archived_on: "2026-01-01",
            },
            {
              id: "we-new",
              user_id: "u-1",
              workspace_id: "ws-1",
              archived_on: null,
            },
          ],
          next_cursor: null,
          has_more: false,
        },
      },
    ]);
    try {
      const id = await fetchActiveEngagementId("u-1");
      expect(id).toBe("we-new");
    } finally {
      env.restore();
    }
  });

  it("returns null when the user has no active engagement", async () => {
    const env = installFetch([
      { body: { data: [], next_cursor: null, has_more: false } },
    ]);
    try {
      const id = await fetchActiveEngagementId("u-1");
      expect(id).toBeNull();
    } finally {
      env.restore();
    }
  });
});
