// Unit tests for the SSE dispatcher + connection helper.
//
// Scope:
// - `INVALIDATIONS` table — every event kind in spec §14 "Event kinds"
//   must have an entry, and each entry must fire the expected
//   `invalidateQueries` / `setQueryData` calls.
// - Unknown kinds — warned via `console.warn`, dispatcher does not
//   throw (a throw inside `EventSource.onmessage` kills the listener
//   chain on some browsers).
// - Backoff — exponential 1s → 30s cap with ±20% jitter; helper is
//   deterministic when driven with a seeded RNG.
// - `connectEventStream` lifecycle — state machine transitions,
//   reconnect on hard close, workspace teardown, and the browser's
//   `Last-Event-ID` posture (we do NOT override the header; the
//   browser echoes it on reconnect automatically).

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import {
  BACKOFF_INITIAL_MS,
  BACKOFF_JITTER,
  BACKOFF_MAX_MS,
  INVALIDATIONS,
  backoffDelayMs,
  connectEventStream,
  dispatchSseEvent,
  parseSseMessage,
  type EventKind,
  type SseEvent,
  type SseStatus,
} from "@/lib/sse";
import {
  __resetQueryKeyGetterForTests,
  qk,
  registerQueryKeyWorkspaceGetter,
} from "@/lib/queryKeys";

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
}

function makeEvent<K extends EventKind>(
  kind: K,
  data: Record<string, unknown> = {},
  overrides: Partial<SseEvent> = {},
): SseEvent {
  return {
    id: overrides.id ?? "tok-1",
    kind,
    workspace_id: overrides.workspace_id ?? "w_test",
    data,
  };
}

beforeEach(() => {
  __resetQueryKeyGetterForTests();
  registerQueryKeyWorkspaceGetter(() => "acme");
});

afterEach(() => {
  __resetQueryKeyGetterForTests();
});

// ---------------------------------------------------------------------------
// INVALIDATIONS coverage + shape
// ---------------------------------------------------------------------------

describe("INVALIDATIONS — coverage", () => {
  // Authoritative list mirroring spec §14 "Event kinds" + the kinds
  // the mocks dispatcher recognises. Drift from the server emitter
  // (`app/api/transport/sse.py`) is flagged in the cd-y4g5 handoff,
  // not silently resolved by trimming this list.
  const EXPECTED_KINDS: readonly EventKind[] = [
    "tick",
    "agent.message.appended",
    "agent.turn.started",
    "agent.turn.finished",
    "agent.action.pending",
    "task.created",
    "task.assigned",
    "task.updated",
    "task.completed",
    "task.skipped",
    "task.overdue",
    "stay.upcoming",
    "stay_task_bundle.upserted",
    "stay_task_bundle.deleted",
    "approval.decided",
    "approval.resolved",
    "expense.created",
    "expense.submitted",
    "expense.cancelled",
    "expense.approved",
    "expense.rejected",
    "expense.reimbursed",
    "expense.decided",
    "asset_action.performed",
    "schedule_ruleset.upserted",
    "schedule_ruleset.deleted",
    "booking.created",
    "booking.amended",
    "booking.declined",
    "booking.approved",
    "booking.rejected",
    "booking.cancelled",
    "booking.reassigned",
    "shift.ended",
    "time.shift.changed",
    "admin.audit.appended",
    "workspace.changed",
  ];

  it("has an entry for every expected event kind", () => {
    for (const kind of EXPECTED_KINDS) {
      expect(INVALIDATIONS[kind]).toBeTypeOf("function");
    }
  });

  it("has no entries we didn't account for (no unexpected drift)", () => {
    const actual = Object.keys(INVALIDATIONS).sort();
    const expected = [...EXPECTED_KINDS].sort();
    expect(actual).toEqual(expected);
  });
});

describe("INVALIDATIONS — per-kind behaviour", () => {
  it("tick is a no-op", () => {
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS.tick(makeEvent("tick"), qc);
    expect(spy).not.toHaveBeenCalled();
  });

  it("task.created invalidates tasks, today, dashboard, my-schedule", () => {
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["task.created"](makeEvent("task.created"), qc);
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([
        qk.tasks(),
        qk.today(),
        qk.dashboard(),
        ["my-schedule"],
      ]),
    );
    // `refetchType: "active"` keeps idle queries cheap.
    for (const call of spy.mock.calls) {
      expect(call[0]?.refetchType).toBe("active");
    }
  });

  it("task.assigned invalidates tasks, today, dashboard, my-schedule", () => {
    // §06 — a task assignment lands on the worker's Today list, the
    // task list filter, the dashboard counter, AND the bidirectional
    // `/schedule` agenda (cd-ops1). Per-kind assertion to guard
    // against drift in the handler key set.
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["task.assigned"](makeEvent("task.assigned"), qc);
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([
        qk.tasks(),
        qk.today(),
        qk.dashboard(),
        ["my-schedule"],
      ]),
    );
  });

  it("task.overdue invalidates tasks, today, dashboard, my-schedule", () => {
    // §06 — an overdue transition flips the chip + reshuffles the
    // dashboard "needs attention" tile + recolors the schedule cell
    // (cd-ops1). Per-kind assertion.
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["task.overdue"](makeEvent("task.overdue"), qc);
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([
        qk.tasks(),
        qk.today(),
        qk.dashboard(),
        ["my-schedule"],
      ]),
    );
  });

  it("stay.upcoming invalidates stays + dashboard + my-schedule", () => {
    // §04 — a freshly synced upcoming stay refreshes the stays list,
    // the dashboard's upcoming-stays tile, and the worker's
    // `/schedule` agenda (cd-ops1: stay → bookings → cells).
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["stay.upcoming"](makeEvent("stay.upcoming"), qc);
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([qk.stays(), qk.dashboard(), ["my-schedule"]]),
    );
  });

  it("stay_task_bundle.{upserted,deleted} invalidates calendar + stays + my-schedule", () => {
    // §14 scheduler — bundle changes ripple into the calendar feed
    // (any mounted window), the stays list, and the worker's
    // `/schedule` agenda (cd-ops1). Per-kind assertion.
    for (const kind of [
      "stay_task_bundle.upserted",
      "stay_task_bundle.deleted",
    ] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { bundle: { id: "b1" } }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([
          ["scheduler-calendar"],
          qk.stays(),
          ["my-schedule"],
        ]),
      );
    }
  });

  it("task.{completed,skipped} also invalidates my-schedule", () => {
    // cd-ops1 — completing or skipping a task has to drop the chip
    // (or recolor it) on the worker's `/schedule` cells.
    for (const kind of ["task.completed", "task.skipped"] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { task_id: "t1" }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(expect.arrayContaining([["my-schedule"]]));
    }
  });

  it("approval.{decided,resolved} also invalidates my-schedule + leaves + meOverrides", () => {
    // cd-ops1 — until the backend emits dedicated `user_leave.*` and
    // `user_availability_override.*` events, the leave + override
    // approval flow rides on `approval.{decided,resolved}`. The
    // `/schedule` agenda has to recolor the day cell when the
    // request flips approved/rejected.
    for (const kind of ["approval.decided", "approval.resolved"] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](
        makeEvent(kind, { id: "ar1", decision: "approved" }),
        qc,
      );
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([
          ["my-schedule"],
          qk.leaves(),
          qk.meOverrides(),
        ]),
      );
    }
  });

  it("task.updated invalidates the per-row detail key by task_id", () => {
    // cd-m0hz — the canonical `TaskUpdated` event publishes
    // `{task_id, changed_fields}` only. The dispatcher treats it as a
    // pure invalidation signal: any mounted `/task/:id` page refetches
    // via REST under the normal per-row authz path.
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["task.updated"](
      makeEvent("task.updated", {
        task_id: "t1",
        changed_fields: ["title"],
      }),
      qc,
    );
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(expect.arrayContaining([qk.task("t1")]));
  });

  it("task.updated never reaches into a non-existent payload.task field", () => {
    // Regression guard for cd-m0hz. Before the fix the dispatcher
    // read `payload.task.id`, which threw on the canonical wire shape
    // (no `task` field). The fix reads `task_id`; calling the
    // reducer on the canonical payload must not throw.
    const qc = makeClient();
    expect(() =>
      INVALIDATIONS["task.updated"](
        makeEvent("task.updated", {
          task_id: "t1",
          changed_fields: ["scheduled_for_local"],
        }),
        qc,
      ),
    ).not.toThrow();
  });

  it("task.updated also invalidates my-schedule + tasks + today + dashboard", () => {
    // cd-ops1 — a task rename or property reassignment has to refresh
    // the `/schedule` cell chip; the dashboard counters can flip too
    // (e.g. priority change), and the worker's Today + Tasks lists
    // both surface the change.
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["task.updated"](
      makeEvent("task.updated", {
        task_id: "t1",
        changed_fields: ["title"],
      }),
      qc,
    );
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([
        qk.task("t1"),
        qk.tasks(),
        qk.today(),
        qk.dashboard(),
        ["my-schedule"],
      ]),
    );
  });

  it("agent.message.appended appends to the scope's chat log", () => {
    const qc = makeClient();
    INVALIDATIONS["agent.message.appended"](
      makeEvent("agent.message.appended", {
        scope: "manager",
        message: { id: "m1", body: "hi" },
      }),
      qc,
    );
    const log = qc.getQueryData<{ id: string }[]>(qk.agentManagerLog());
    expect(log).toEqual([{ id: "m1", body: "hi" }]);
  });

  it("agent.message.appended on scope=task uses the per-task log key", () => {
    const qc = makeClient();
    INVALIDATIONS["agent.message.appended"](
      makeEvent("agent.message.appended", {
        scope: "task",
        task_id: "t42",
        message: { id: "m1", body: "hi" },
      }),
      qc,
    );
    const log = qc.getQueryData<{ id: string }[]>(qk.agentTaskChat("t42"));
    expect(log).toEqual([{ id: "m1", body: "hi" }]);
    // Manager log should be untouched.
    expect(qc.getQueryData(qk.agentManagerLog())).toBeUndefined();
  });

  it("agent.turn.started/finished flips the typing flag for the scope", () => {
    const qc = makeClient();
    INVALIDATIONS["agent.turn.started"](
      makeEvent("agent.turn.started", { scope: "manager", started_at: "x" }),
      qc,
    );
    expect(qc.getQueryData(qk.agentTyping("manager"))).toBe(true);
    INVALIDATIONS["agent.turn.finished"](
      makeEvent("agent.turn.finished", {
        scope: "manager",
        outcome: "replied",
      }),
      qc,
    );
    expect(qc.getQueryData(qk.agentTyping("manager"))).toBe(false);
  });

  it("agent.action.pending drops the typing flag AND invalidates approvals", () => {
    const qc = makeClient();
    // Pre-set typing so we can observe it dropping.
    INVALIDATIONS["agent.turn.started"](
      makeEvent("agent.turn.started", { scope: "manager", started_at: "x" }),
      qc,
    );
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["agent.action.pending"](
      makeEvent("agent.action.pending", {
        scope: "manager",
        approval_request_id: "ar1",
      }),
      qc,
    );
    expect(qc.getQueryData(qk.agentTyping("manager"))).toBe(false);
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([qk.approvals(), qk.dashboard()]),
    );
  });

  it("expense.{approved,rejected,reimbursed,decided} invalidates expense roots", () => {
    for (const kind of [
      "expense.approved",
      "expense.rejected",
      "expense.reimbursed",
      "expense.decided",
    ] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { id: "e1", status: "approved" }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([
          qk.expenses("all"),
          qk.expenses("mine"),
          qk.dashboard(),
        ]),
      );
    }
  });

  it("expense.{approved,rejected,reimbursed,decided} also invalidates the worker's pending-reimbursement total", () => {
    // §09 "Amount owed to the employee" — the worker's
    // pending-reimbursement total has to shrink/grow the instant a
    // claim crosses any decision boundary, so the four decision-side
    // kinds all refresh `expensesPendingReimbursement("me")`.
    for (const kind of [
      "expense.approved",
      "expense.rejected",
      "expense.reimbursed",
      "expense.decided",
    ] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { id: "e1", status: "approved" }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([qk.expensesPendingReimbursement("me")]),
      );
    }
  });

  it("expense.created invalidates the worker's `mine` list + dashboard only", () => {
    // Drafting a claim shouldn't churn the workspace-wide list
    // (which only shows submitted+ claims) — keep the cache cheap.
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["expense.created"](
      makeEvent("expense.created", { id: "e1", status: "draft" }),
      qc,
    );
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([qk.expenses("mine"), qk.dashboard()]),
    );
    // The `all` list and the per-user pending total are not touched.
    expect(called).not.toContainEqual(qk.expenses("all"));
    expect(called).not.toContainEqual(qk.expensesPendingReimbursement("me"));
  });

  it("expense.submitted invalidates `mine` + `all` + dashboard", () => {
    // Submission moves the claim into the manager queue, so the
    // workspace-wide list has to refresh too.
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["expense.submitted"](
      makeEvent("expense.submitted", { id: "e1", status: "submitted" }),
      qc,
    );
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([
        qk.expenses("mine"),
        qk.expenses("all"),
        qk.dashboard(),
      ]),
    );
  });

  it("expense.cancelled invalidates `mine` + `all` + dashboard", () => {
    // Withdrawal can affect either the worker's own list or the
    // manager queue depending on whether the claim was still draft;
    // refreshing both is the cheapest way to stay correct without
    // peeking at the prior status.
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["expense.cancelled"](
      makeEvent("expense.cancelled", { id: "e1", status: "cancelled" }),
      qc,
    );
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([
        qk.expenses("mine"),
        qk.expenses("all"),
        qk.dashboard(),
      ]),
    );
  });

  it("task.{completed,skipped} also invalidates the history `tasks` tab", () => {
    // §14 worker history — a newly wrapped-up task should refresh the
    // employee's History → Tasks feed without the page having to remount.
    for (const kind of ["task.completed", "task.skipped"] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { task_id: "t1" }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(expect.arrayContaining([qk.history("tasks")]));
    }
  });

  it("task.{completed,skipped} invalidates the per-row detail key by task_id", () => {
    // cd-m0hz — completion/skip events publish `{task_id, ...}` only;
    // dispatcher invalidates `qk.task(task_id)` so a mounted detail
    // page refetches.
    for (const kind of ["task.completed", "task.skipped"] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { task_id: "t1" }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(expect.arrayContaining([qk.task("t1")]));
    }
  });

  it("expense.{approved,rejected,reimbursed,decided} also invalidates the history `expenses` tab", () => {
    for (const kind of [
      "expense.approved",
      "expense.rejected",
      "expense.reimbursed",
      "expense.decided",
    ] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { id: "e1", status: "approved" }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([qk.history("expenses")]),
      );
    }
  });

  it("approval.{decided,resolved} also invalidates the history `leaves` + `expenses` tabs", () => {
    // §14 worker history — an approved leave or expense should surface
    // in the respective tab of `/history` without a page remount.
    for (const kind of ["approval.decided", "approval.resolved"] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { id: "ar1", decision: "approved" }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([qk.history("leaves"), qk.history("expenses")]),
      );
    }
  });

  it("asset_action.performed invalidates the one asset + the list", () => {
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["asset_action.performed"](
      makeEvent("asset_action.performed", {
        asset_id: "a1",
        action: { id: "ax" },
      }),
      qc,
    );
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(
      expect.arrayContaining([qk.asset("a1"), qk.assets()]),
    );
  });

  it("schedule_ruleset.{upserted,deleted} invalidates rulesets + calendar", () => {
    for (const kind of [
      "schedule_ruleset.upserted",
      "schedule_ruleset.deleted",
    ] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { ruleset: { id: "r1" } }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([qk.scheduleRulesets(), ["scheduler-calendar"]]),
      );
    }
  });

  it("booking.* invalidates my-schedule root + bookings + dashboard", () => {
    for (const kind of [
      "booking.created",
      "booking.amended",
      "booking.declined",
      "booking.approved",
      "booking.rejected",
      "booking.cancelled",
      "booking.reassigned",
    ] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind, { booking: { id: "b1" } }), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([
          ["my-schedule"],
          qk.bookings(),
          qk.dashboard(),
        ]),
      );
    }
  });

  it("shift.* + time.shift.* invalidate schedule + dashboard", () => {
    for (const kind of ["shift.ended", "time.shift.changed"] as const) {
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      INVALIDATIONS[kind](makeEvent(kind), qc);
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([["my-schedule"], qk.dashboard()]),
      );
    }
  });

  it("admin.audit.appended invalidates the deployment-scope audit list", () => {
    // §12 SSE — `/admin/events` only carries deployment-scope audit
    // rows, so the dispatcher just invalidates `qk.adminAudit()` and
    // lets TanStack Query refetch the page list.
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["admin.audit.appended"](
      makeEvent("admin.audit.appended"),
      qc,
    );
    const called = spy.mock.calls.map((c) => c[0]?.queryKey);
    expect(called).toEqual(expect.arrayContaining([qk.adminAudit()]));
  });

  it("workspace.changed invalidates everything", () => {
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    INVALIDATIONS["workspace.changed"](makeEvent("workspace.changed"), qc);
    // The big-hammer call has no queryKey filter.
    expect(spy).toHaveBeenCalled();
    expect(spy.mock.calls[0]?.[0]?.queryKey).toBeUndefined();
  });

  it("every invalidation passes refetchType: 'active'", () => {
    // §14 "SSE-driven invalidation" — refetchType must be "active"
    // so background queries stay cheap. Regression guard.
    for (const kind of Object.keys(INVALIDATIONS) as EventKind[]) {
      if (kind === "tick" || kind === "workspace.changed") continue;
      const qc = makeClient();
      const spy = vi.spyOn(qc, "invalidateQueries");
      // Provide a minimally-shaped payload so handlers that read
      // into the data object don't crash on undefined access.
      const data: Record<string, unknown> = {
        task_id: "t1",
        changed_fields: [],
        task: { id: "t1" },
        message: { id: "m1" },
        scope: "manager",
        id: "x1",
        asset_id: "a1",
        action: { id: "ax" },
        ruleset: { id: "r1" },
        booking: { id: "b1" },
      };
      INVALIDATIONS[kind](makeEvent(kind, data), qc);
      for (const call of spy.mock.calls) {
        expect(call[0]?.refetchType).toBe("active");
      }
    }
  });
});

// ---------------------------------------------------------------------------
// dispatchSseEvent — unknown kinds
// ---------------------------------------------------------------------------

describe("dispatchSseEvent — unknown kinds", () => {
  it("logs a warning and does not throw on an unknown kind", () => {
    const qc = makeClient();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      expect(() =>
        dispatchSseEvent(
          { id: "x", kind: "unknown.thing" as EventKind, workspace_id: "w", data: {} },
          qc,
        ),
      ).not.toThrow();
      expect(warn).toHaveBeenCalledWith(
        expect.stringContaining("unknown event kind"),
      );
    } finally {
      warn.mockRestore();
    }
  });

  it("swallows handler exceptions and logs to console.error", () => {
    const qc = makeClient();
    const err = vi.spyOn(console, "error").mockImplementation(() => undefined);
    // Force `invalidateQueries` to throw; the dispatcher must not
    // bubble the error up to `EventSource.onmessage`.
    vi.spyOn(qc, "invalidateQueries").mockImplementation(() => {
      throw new Error("boom");
    });
    try {
      expect(() =>
        dispatchSseEvent(makeEvent("task.created"), qc),
      ).not.toThrow();
      expect(err).toHaveBeenCalled();
    } finally {
      err.mockRestore();
    }
  });
});

// ---------------------------------------------------------------------------
// parseSseMessage
// ---------------------------------------------------------------------------

describe("parseSseMessage", () => {
  it("parses a valid frame with workspace_id + kind", () => {
    const parsed = parseSseMessage(
      "task.created",
      "tok-5",
      JSON.stringify({ kind: "task.created", workspace_id: "w1", task: { id: "t1" } }),
    );
    expect(parsed).not.toBeNull();
    expect(parsed?.kind).toBe("task.created");
    expect(parsed?.id).toBe("tok-5");
    expect(parsed?.workspace_id).toBe("w1");
    expect(parsed?.data.task).toEqual({ id: "t1" });
  });

  it("returns null for non-JSON data", () => {
    expect(parseSseMessage("task.created", "tok", "not json")).toBeNull();
  });

  it("returns null for a JSON array (not an object)", () => {
    expect(parseSseMessage("task.created", "tok", "[1,2,3]")).toBeNull();
  });

  it("returns null for JSON null", () => {
    expect(parseSseMessage("task.created", "tok", "null")).toBeNull();
  });

  it("falls back to the data.kind when the SSE event header is empty", () => {
    const parsed = parseSseMessage(
      "",
      "tok",
      JSON.stringify({ kind: "task.updated", workspace_id: "w" }),
    );
    expect(parsed?.kind).toBe("task.updated");
  });
});

// ---------------------------------------------------------------------------
// backoffDelayMs
// ---------------------------------------------------------------------------

describe("backoffDelayMs", () => {
  const noJitter = (): number => 0.5; // maps to jitterFactor = 1.0

  it("returns 1s on the first attempt with no jitter", () => {
    expect(backoffDelayMs(0, noJitter)).toBe(BACKOFF_INITIAL_MS);
  });

  it("doubles each attempt: 1s → 2s → 4s → 8s → 16s", () => {
    expect(backoffDelayMs(0, noJitter)).toBe(1_000);
    expect(backoffDelayMs(1, noJitter)).toBe(2_000);
    expect(backoffDelayMs(2, noJitter)).toBe(4_000);
    expect(backoffDelayMs(3, noJitter)).toBe(8_000);
    expect(backoffDelayMs(4, noJitter)).toBe(16_000);
  });

  it("caps at 30s on attempt 5+", () => {
    expect(backoffDelayMs(5, noJitter)).toBe(BACKOFF_MAX_MS);
    expect(backoffDelayMs(6, noJitter)).toBe(BACKOFF_MAX_MS);
    expect(backoffDelayMs(20, noJitter)).toBe(BACKOFF_MAX_MS);
  });

  it("applies ±20% jitter around the base", () => {
    // rng() = 0 → jitterFactor = 0.8
    expect(backoffDelayMs(0, () => 0)).toBeCloseTo(
      BACKOFF_INITIAL_MS * (1 - BACKOFF_JITTER),
      5,
    );
    // rng() = 0.999… → jitterFactor → 1.2
    const upper = backoffDelayMs(0, () => 0.999);
    expect(upper).toBeGreaterThan(BACKOFF_INITIAL_MS);
    expect(upper).toBeLessThanOrEqual(
      BACKOFF_INITIAL_MS * (1 + BACKOFF_JITTER),
    );
  });

  it("never exceeds BACKOFF_MAX_MS even with maximum upward jitter", () => {
    // An rng() that pushes the cap past 30s must still be clamped.
    expect(backoffDelayMs(10, () => 0.999)).toBeLessThanOrEqual(BACKOFF_MAX_MS);
  });

  it("clamps negative attempts to the initial delay", () => {
    expect(backoffDelayMs(-1, noJitter)).toBe(BACKOFF_INITIAL_MS);
  });
});

// ---------------------------------------------------------------------------
// connectEventStream — lifecycle
// ---------------------------------------------------------------------------

/**
 * Minimal `EventSource` double that records construction URLs,
 * listener registrations, and dispatches hand-crafted frames.
 */
class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;

  url: string;
  withCredentials = false;
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((evt: MessageEvent<string>) => void) | null = null;
  listeners = new Map<string, ((evt: MessageEvent<string>) => void)[]>();
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(kind: string, fn: (evt: MessageEvent<string>) => void): void {
    const bucket = this.listeners.get(kind) ?? [];
    bucket.push(fn);
    this.listeners.set(kind, bucket);
  }

  removeEventListener(
    kind: string,
    fn: (evt: MessageEvent<string>) => void,
  ): void {
    const bucket = this.listeners.get(kind);
    if (!bucket) return;
    this.listeners.set(
      kind,
      bucket.filter((h) => h !== fn),
    );
  }

  close(): void {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }

  /** Simulate a successful connection. */
  fireOpen(): void {
    this.readyState = FakeEventSource.OPEN;
    this.onopen?.();
  }

  /** Simulate a hard failure (server dropped us). */
  fireHardError(): void {
    this.readyState = FakeEventSource.CLOSED;
    this.onerror?.();
  }

  /** Simulate a transient error the browser will retry on its own. */
  fireTransientError(): void {
    this.readyState = FakeEventSource.OPEN;
    this.onerror?.();
  }

  /** Dispatch a frame to a specific kind + lastEventId. */
  fire(kind: string, lastEventId: string, data: string): void {
    const evt = {
      type: kind,
      lastEventId,
      data,
    } as unknown as MessageEvent<string>;
    const listeners = this.listeners.get(kind) ?? [];
    for (const l of listeners) l(evt);
  }

  static instances: FakeEventSource[] = [];
  static reset(): void {
    FakeEventSource.instances = [];
  }
}

describe("connectEventStream — URLs + lifecycle", () => {
  beforeEach(() => FakeEventSource.reset());

  const factory = (url: string): EventSource =>
    new FakeEventSource(url) as unknown as EventSource;

  it("opens /events when no slug is set", () => {
    const qc = makeClient();
    const conn = connectEventStream({ slug: null, qc, eventSourceFactory: factory });
    expect(FakeEventSource.instances[0]?.url).toBe("/events");
    conn.close();
  });

  it("opens /w/<slug>/events when a slug is set", () => {
    const qc = makeClient();
    const conn = connectEventStream({
      slug: "acme",
      qc,
      eventSourceFactory: factory,
    });
    expect(FakeEventSource.instances[0]?.url).toBe("/w/acme/events");
    conn.close();
  });

  it("close() is idempotent and closes the underlying EventSource", () => {
    const qc = makeClient();
    const statuses: SseStatus[] = [];
    const conn = connectEventStream({
      slug: null,
      qc,
      onStatus: (s) => statuses.push(s),
      eventSourceFactory: factory,
    });
    conn.close();
    conn.close();
    expect(FakeEventSource.instances[0]?.closed).toBe(true);
    expect(statuses).toContain("closed");
  });

  it("emits status transitions: connecting → open → reconnecting → connecting → open", () => {
    vi.useFakeTimers();
    const statuses: SseStatus[] = [];
    const qc = makeClient();
    const conn = connectEventStream({
      slug: "acme",
      qc,
      onStatus: (s) => statuses.push(s),
      eventSourceFactory: factory,
      rng: () => 0.5,
    });
    try {
      FakeEventSource.instances[0]?.fireOpen();
      FakeEventSource.instances[0]?.fireHardError();
      // Backoff fires after 1s
      vi.advanceTimersByTime(1_000);
      FakeEventSource.instances[1]?.fireOpen();
      expect(statuses).toEqual([
        "connecting",
        "open",
        "reconnecting",
        "connecting",
        "open",
      ]);
    } finally {
      conn.close();
      vi.useRealTimers();
    }
  });

  it("passes the frame through the dispatcher on message", () => {
    const qc = makeClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    const conn = connectEventStream({
      slug: "acme",
      qc,
      eventSourceFactory: factory,
    });
    try {
      const es = FakeEventSource.instances[0];
      expect(es).toBeDefined();
      es?.fire(
        "task.created",
        "tok-1",
        JSON.stringify({ kind: "task.created", workspace_id: "w" }),
      );
      const called = spy.mock.calls.map((c) => c[0]?.queryKey);
      expect(called).toEqual(
        expect.arrayContaining([qk.tasks(), qk.today()]),
      );
    } finally {
      conn.close();
    }
  });

  it("reports lastEventId through onLastEventId", () => {
    const qc = makeClient();
    const ids: string[] = [];
    const conn = connectEventStream({
      slug: "acme",
      qc,
      onLastEventId: (id) => ids.push(id),
      eventSourceFactory: factory,
    });
    try {
      const es = FakeEventSource.instances[0];
      es?.fire(
        "task.created",
        "abc-7",
        JSON.stringify({ kind: "task.created", workspace_id: "w" }),
      );
      expect(ids).toEqual(["abc-7"]);
    } finally {
      conn.close();
    }
  });

  it("does NOT reconnect on a transient error (readyState === OPEN)", () => {
    vi.useFakeTimers();
    const qc = makeClient();
    const conn = connectEventStream({
      slug: null,
      qc,
      eventSourceFactory: factory,
    });
    try {
      FakeEventSource.instances[0]?.fireOpen();
      FakeEventSource.instances[0]?.fireTransientError();
      // Even after the whole backoff ladder, no second EventSource
      // is constructed: the browser is retrying on its own.
      vi.advanceTimersByTime(60_000);
      expect(FakeEventSource.instances).toHaveLength(1);
    } finally {
      conn.close();
      vi.useRealTimers();
    }
  });

  it("does not override the default Last-Event-ID behaviour", () => {
    // The browser's `EventSource` echoes `Last-Event-ID` on reconnect
    // automatically — we never construct the EventSource with a
    // custom header or pass the id back in via URL. This test is a
    // defensive regression: assert we pass only the URL and the
    // `withCredentials` init to the default factory.
    const originalSource = globalThis.EventSource;
    const constructorCalls: { url: string; init: EventSourceInit | undefined }[] =
      [];
    class CapturingES extends FakeEventSource {
      constructor(url: string, init?: EventSourceInit) {
        super(url);
        this.withCredentials = init?.withCredentials ?? false;
        constructorCalls.push({ url, init });
      }
    }
    (globalThis as { EventSource: unknown }).EventSource = CapturingES;
    try {
      const qc = makeClient();
      const conn = connectEventStream({ slug: "acme", qc });
      expect(constructorCalls).toHaveLength(1);
      const call = constructorCalls[0];
      expect(call?.url).toBe("/w/acme/events");
      expect(call?.init?.withCredentials).toBe(true);
      // No custom headers. `EventSourceInit` on the DOM only allows
      // `withCredentials`; asserting no extra keys guards future
      // polyfill abuse.
      const initKeys = Object.keys(call?.init ?? {});
      expect(initKeys.sort()).toEqual(["withCredentials"]);
      conn.close();
    } finally {
      (globalThis as { EventSource: unknown }).EventSource = originalSource;
    }
  });
});

describe("connectEventStream — backoff ladder", () => {
  beforeEach(() => FakeEventSource.reset());

  const factory = (url: string): EventSource =>
    new FakeEventSource(url) as unknown as EventSource;

  it("follows 1s → 2s → 4s → 8s → 16s → 30s (cap) with frozen RNG", () => {
    vi.useFakeTimers();
    const qc = makeClient();
    const conn = connectEventStream({
      slug: null,
      qc,
      eventSourceFactory: factory,
      rng: () => 0.5,
    });
    try {
      const ladder = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];
      for (let i = 0; i < ladder.length; i += 1) {
        expect(FakeEventSource.instances.length).toBe(i + 1);
        FakeEventSource.instances[i]?.fireHardError();
        // Nothing reconnects before the ladder step.
        vi.advanceTimersByTime(ladder[i]! - 1);
        expect(FakeEventSource.instances.length).toBe(i + 1);
        vi.advanceTimersByTime(1);
        expect(FakeEventSource.instances.length).toBe(i + 2);
      }
    } finally {
      conn.close();
      vi.useRealTimers();
    }
  });

  it("caps the backoff at 30s even past attempt 5", () => {
    vi.useFakeTimers();
    const qc = makeClient();
    const conn = connectEventStream({
      slug: null,
      qc,
      eventSourceFactory: factory,
      rng: () => 0.5,
    });
    try {
      // Climb the ladder up to 30s, then force two more failures;
      // both should wait exactly 30s.
      const ladder = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];
      for (const step of ladder) {
        const live = FakeEventSource.instances.at(-1);
        live?.fireHardError();
        vi.advanceTimersByTime(step);
      }
      // Next drops are all capped.
      for (let i = 0; i < 2; i += 1) {
        const before = FakeEventSource.instances.length;
        FakeEventSource.instances.at(-1)?.fireHardError();
        vi.advanceTimersByTime(29_999);
        expect(FakeEventSource.instances.length).toBe(before);
        vi.advanceTimersByTime(1);
        expect(FakeEventSource.instances.length).toBe(before + 1);
      }
    } finally {
      conn.close();
      vi.useRealTimers();
    }
  });

  it("resets the backoff ladder after a successful open", () => {
    // `onopen` must zero the attempt counter so a long-lived session
    // doesn't take 30s to recover from a single late blip.
    vi.useFakeTimers();
    const qc = makeClient();
    const conn = connectEventStream({
      slug: null,
      qc,
      eventSourceFactory: factory,
      rng: () => 0.5,
    });
    try {
      // Climb one rung.
      FakeEventSource.instances[0]?.fireHardError();
      vi.advanceTimersByTime(1_000);
      expect(FakeEventSource.instances).toHaveLength(2);

      FakeEventSource.instances[1]?.fireOpen();
      // Drop again — must reconnect at 1s, not at 2s.
      FakeEventSource.instances[1]?.fireHardError();
      vi.advanceTimersByTime(1_000);
      expect(FakeEventSource.instances).toHaveLength(3);
    } finally {
      conn.close();
      vi.useRealTimers();
    }
  });

  it("close() cancels a pending reconnect timer", () => {
    vi.useFakeTimers();
    const qc = makeClient();
    const conn = connectEventStream({
      slug: null,
      qc,
      eventSourceFactory: factory,
      rng: () => 0.5,
    });
    FakeEventSource.instances[0]?.fireHardError();
    conn.close();
    // Advance past the first rung — no new connection should appear.
    vi.advanceTimersByTime(60_000);
    expect(FakeEventSource.instances).toHaveLength(1);
    vi.useRealTimers();
  });
});

describe("connectEventStream — workspace switch", () => {
  beforeEach(() => FakeEventSource.reset());

  const factory = (url: string): EventSource =>
    new FakeEventSource(url) as unknown as EventSource;

  it("close() then re-open at a new URL mimics a workspace switch", () => {
    // The provider closes the old connection and opens a new one on
    // slug change; the lib-level helper's responsibility is to make
    // both operations clean (no leaked timers, no double-listeners).
    const qc = makeClient();
    const first = connectEventStream({
      slug: "a",
      qc,
      eventSourceFactory: factory,
    });
    expect(FakeEventSource.instances[0]?.url).toBe("/w/a/events");
    first.close();
    expect(FakeEventSource.instances[0]?.closed).toBe(true);

    const second = connectEventStream({
      slug: "b",
      qc,
      eventSourceFactory: factory,
    });
    expect(FakeEventSource.instances[1]?.url).toBe("/w/b/events");
    expect(FakeEventSource.instances[1]?.closed).toBe(false);
    second.close();
  });
});
