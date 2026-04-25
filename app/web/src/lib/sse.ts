// Single EventSource feed for the workspace → TanStack Query invalidation
// bridge. One connection per tab (opened by `<SseProvider>`); every
// received frame passes through `dispatchSseEvent`, which looks up the
// event kind in `INVALIDATIONS` and fires the matching
// `queryClient.invalidateQueries(...)` or `setQueryData(...)` calls.
//
// Spec refs:
// - `docs/specs/14-web-frontend.md` §"SSE-driven invalidation" and
//   §"Event kinds" — every event kind listed there must have an
//   entry in `INVALIDATIONS` below.
// - `docs/specs/12-rest-api.md` §"SSE stream" — frame shape:
//   `id: <stream_token>-<seq>\nevent: <kind>\ndata: <json>\n\n`. The
//   composite `id` is opaque to the client; the browser's
//   `EventSource` sends it back via the standard `Last-Event-ID`
//   header on reconnect.
//
// Server authority: `app/api/transport/sse.py` is the canonical
// emitter. Its `_INVALIDATIONS` map only covers the kinds the backend
// currently publishes; this client ships a superset keyed on the full
// spec §"Event kinds" list so new backend emitters don't need a web
// deploy to land. Kinds the server never emits just never run —
// there's no harm in listing them.
//
// Server emits (today): task.created, task.assigned, task.completed,
// task.overdue, stay.upcoming, expense.approved, shift.ended,
// time.shift.changed. Additional kinds here (agent.*, booking.*,
// asset_action.*, schedule_ruleset.*, task.updated/skipped, approval.*)
// track the spec and the mock's dispatcher for when each backend
// emitter lands. Any drift is flagged in the cd-y4g5 handoff.

import type { QueryClient } from "@tanstack/react-query";
import type {
  AgentMessage,
  AgentTurnScope,
  AssetAction,
} from "@/types/api";
import { qk } from "./queryKeys";

// ---------------------------------------------------------------------------
// Event kinds + frame envelope
// ---------------------------------------------------------------------------

/**
 * Every event kind the SPA recognises. Union of string literals rather
 * than an enum so TypeScript narrows exhaustively through the
 * `INVALIDATIONS` table and the `switch` in `dispatchSseEvent`.
 *
 * `tick` is the server's optional heartbeat frame; harmless to receive
 * and invalidates nothing (real heartbeats are SSE `:` comments).
 *
 * Intentional aliases (kept as a trade-off):
 * - `approval.decided` + `approval.resolved` — the spec §14 names the
 *   event `approval.resolved`; the server and mocks emit
 *   `approval.decided` (the term that matches `/approvals/{id}/{decision}`
 *   in §12). Both are registered so either emitter shape Just Works and
 *   the SPA stays coherent across a partial rollout of either side.
 * - `expense.approved` / `expense.rejected` / `expense.reimbursed` +
 *   `expense.decided` — §14 names one bundled kind; the server and
 *   mocks emit three discrete kinds that carry the status transition.
 *   Both shapes fan out to the same surfaces (list, mine, dashboard).
 *
 * Any true drift (not an alias) is flagged in the cd-y4g5 handoff and
 * must be reconciled in the spec + server emitter + this table, not
 * silently here.
 */
export type EventKind =
  // Heartbeat — optional, no-op.
  | "tick"
  // Agent chat lifecycle (§11 "Agent turn lifecycle" + §14 "Agent
  // turn indicator").
  | "agent.message.appended"
  | "agent.turn.started"
  | "agent.turn.finished"
  | "agent.action.pending"
  // Tasks (§06).
  | "task.created"
  | "task.assigned"
  | "task.updated"
  | "task.completed"
  | "task.skipped"
  | "task.overdue"
  // Stays + stay task bundles (§04, §06).
  | "stay.upcoming"
  | "stay_task_bundle.upserted"
  | "stay_task_bundle.deleted"
  // Approvals (§11 HITL).
  | "approval.decided"
  | "approval.resolved"
  // Expenses (§09).
  | "expense.created"
  | "expense.submitted"
  | "expense.cancelled"
  | "expense.approved"
  | "expense.rejected"
  | "expense.reimbursed"
  | "expense.decided"
  // Assets (§21).
  | "asset_action.performed"
  // Schedule rulesets + scheduler calendar (§06, §14 scheduler).
  | "schedule_ruleset.upserted"
  | "schedule_ruleset.deleted"
  // Bookings (§09 booking lifecycle).
  | "booking.created"
  | "booking.amended"
  | "booking.declined"
  | "booking.approved"
  | "booking.rejected"
  | "booking.cancelled"
  | "booking.reassigned"
  // Shifts (§09 time + payroll).
  | "shift.ended"
  | "time.shift.changed"
  // Admin / deployment-scope audit (§12 SSE — `/admin/events`). The
  // server emits `admin.audit.appended` only for `scope_kind ==
  // 'deployment'` rows; the `/admin/audit` page invalidates its
  // cached list so a fresh row appears at the top without a full
  // re-render.
  | "admin.audit.appended"
  // Catch-all workspace invalidation — e.g. owner flips a workspace
  // setting that reshapes policy. Drops every cached query under
  // the active workspace.
  | "workspace.changed";

/**
 * A parsed SSE frame as the dispatcher sees it.
 *
 * `id` is the composite `<stream_token>-<seq>` string the server emits
 * — opaque to the client. The browser echoes it back on reconnect via
 * the standard `Last-Event-ID` header; we do not set the header
 * ourselves.
 *
 * `data` is whatever the server serialised. The per-kind dispatcher
 * down-casts it to a concrete shape.
 */
export interface SseEvent {
  id: string;
  kind: EventKind;
  workspace_id: string;
  data: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Agent-typing cache (§14 "Agent turn indicator")
// ---------------------------------------------------------------------------
//
// The `agent.turn.{started,finished}` pair drives a boolean cache the
// chat surfaces read via `useAgentTyping`. A 60 s local safety net
// drops the flag if a `finished` event is lost, matching the spec's
// "or a local 60-second timeout" bullet. Keyed per scope — two
// concurrent task-scoped chats don't share one indicator.

const TYPING_TIMEOUT_MS = 60_000;
const typingTimers = new Map<string, ReturnType<typeof setTimeout>>();

function typingKeySignature(scope: AgentTurnScope, taskId?: string): string {
  return scope === "task" && taskId ? `task:${taskId}` : scope;
}

function startTyping(qc: QueryClient, scope: AgentTurnScope, taskId?: string): void {
  const sig = typingKeySignature(scope, taskId);
  qc.setQueryData<boolean>(qk.agentTyping(scope, taskId), true);
  const prev = typingTimers.get(sig);
  if (prev) clearTimeout(prev);
  typingTimers.set(
    sig,
    setTimeout(() => {
      qc.setQueryData<boolean>(qk.agentTyping(scope, taskId), false);
      typingTimers.delete(sig);
    }, TYPING_TIMEOUT_MS),
  );
}

function stopTyping(qc: QueryClient, scope: AgentTurnScope, taskId?: string): void {
  const sig = typingKeySignature(scope, taskId);
  const prev = typingTimers.get(sig);
  if (prev) {
    clearTimeout(prev);
    typingTimers.delete(sig);
  }
  qc.setQueryData<boolean>(qk.agentTyping(scope, taskId), false);
}

/**
 * Clear every outstanding typing indicator. Called on
 * `EventSource.onopen` so a dropped session can't leave the bubble
 * stuck on reconnect (§14 "clears on SSE reconnect").
 */
export function clearAllTyping(qc: QueryClient): void {
  for (const [sig, handle] of typingTimers) {
    clearTimeout(handle);
    const [prefix, taskId] = sig.split(":");
    if (prefix === "task" && taskId) {
      qc.setQueryData<boolean>(qk.agentTyping("task", taskId), false);
    } else if (prefix === "employee" || prefix === "manager" || prefix === "admin") {
      qc.setQueryData<boolean>(qk.agentTyping(prefix), false);
    }
  }
  typingTimers.clear();
}

// ---------------------------------------------------------------------------
// Invalidation map
// ---------------------------------------------------------------------------

/**
 * Per-kind invalidation. Each entry receives the parsed frame and the
 * `QueryClient`; it fires whatever
 * `invalidateQueries` / `setQueryData` calls the spec says that kind
 * should trigger.
 *
 * `refetchType: "active"` keeps idle queries cheap — only queries
 * currently mounted under a component refetch; background entries
 * stay stale until a component remounts them (same as
 * `queryClient.invalidateQueries` default behaviour in v5, made
 * explicit here so the intent is visible at the call site).
 */
export type InvalidationHandler = (event: SseEvent, qc: QueryClient) => void;

// Narrow-cast helpers. `SseEvent.data` is `Record<string, unknown>`
// because the envelope is polymorphic; each handler knows its kind's
// shape and asserts through a named type. Centralising the cast keeps
// the shape labels visible at the call site (e.g. `asTaskPayload`)
// rather than sprinkling raw `as` casts through the handlers.
//
// None of these validate the payload — the server is trusted to
// emit the right shape. A malformed payload would surface as a type
// error in whatever query we re-populate, which is the same failure
// mode the mock has today.

/**
 * Wire shape for `task.updated`, `task.completed`, `task.skipped`.
 *
 * The canonical events (`app.events.types.TaskUpdated` /
 * `TaskCompleted` / `TaskSkipped`) carry only the foreign-key
 * identifier plus a few small scalars (`changed_fields`,
 * `completed_by`, `reason`) — never a rendered `Task` object.
 * Subscribers that need the title / description / property re-fetch
 * via REST under the normal per-row authz path. See
 * `docs/specs/06-tasks-and-scheduling.md` and the cd-m0hz handoff
 * for the rationale (PII leakage + payload bloat if we wired the
 * full task on every event).
 *
 * `changed_fields` is currently only set by `task.updated`; the
 * dispatcher leaves it unread for now (a no-op switch on the field
 * names is a follow-up once the SPA wants to narrow invalidations
 * per-field).
 */
interface TaskRefPayload {
  task_id: string;
}

interface AssetActionPayload {
  asset_id: string;
  action: AssetAction;
}

interface AgentMessagePayload {
  scope: AgentTurnScope;
  task_id?: string;
  message: AgentMessage;
}

interface AgentTurnStartedPayload {
  scope: AgentTurnScope;
  task_id?: string;
  started_at: string;
}

interface AgentTurnFinishedPayload {
  scope: AgentTurnScope;
  task_id?: string;
  finished_at?: string;
  outcome: "replied" | "action" | "error" | "timeout";
}

interface AgentActionPendingPayload {
  scope: AgentTurnScope;
  task_id?: string;
  approval_request_id: string;
}

function invalidate(qc: QueryClient, queryKey: readonly unknown[]): void {
  // `refetchType: "active"` keeps idle queries cheap (§14 "SSE-driven
  // invalidation"). Explicit here so the intent is visible at every
  // call site rather than relying on the v5 default.
  qc.invalidateQueries({ queryKey, refetchType: "active" });
}

export const INVALIDATIONS: Record<EventKind, InvalidationHandler> = {
  tick: () => undefined,

  "agent.message.appended": (event, qc) => {
    const payload = event.data as unknown as AgentMessagePayload;
    const key =
      payload.scope === "task" && payload.task_id
        ? qk.agentTaskChat(payload.task_id)
        : payload.scope === "admin"
          ? qk.adminAgentLog()
          : payload.scope === "employee"
            ? qk.agentEmployeeLog()
            : qk.agentManagerLog();
    qc.setQueryData<AgentMessage[]>(key, (prev) =>
      prev ? [...prev, payload.message] : [payload.message],
    );
    // Reply arriving resolves the turn even if `agent.turn.finished`
    // lags behind; drop the typing bubble on the same scope.
    stopTyping(qc, payload.scope, payload.task_id);
  },

  "agent.turn.started": (event, qc) => {
    const payload = event.data as unknown as AgentTurnStartedPayload;
    startTyping(qc, payload.scope, payload.task_id);
  },

  "agent.turn.finished": (event, qc) => {
    const payload = event.data as unknown as AgentTurnFinishedPayload;
    stopTyping(qc, payload.scope, payload.task_id);
  },

  "agent.action.pending": (event, qc) => {
    const payload = event.data as unknown as AgentActionPendingPayload;
    // The approval card lives on the chat surface for the scope; the
    // turn has resolved into an approval rather than a reply, so the
    // typing bubble drops (§14 "Agent turn indicator"). The
    // `/approvals` page refetches so the card lands both inline and
    // in the queue.
    stopTyping(qc, payload.scope, payload.task_id);
    invalidate(qc, qk.approvals());
    invalidate(qc, qk.dashboard());
  },

  "task.created": (_event, qc) => {
    invalidate(qc, qk.tasks());
    invalidate(qc, qk.today());
    invalidate(qc, qk.dashboard());
    // §14 worker schedule — the bidirectional infinite agenda keys
    // every loaded page under `["my-schedule", ...]`. A new task
    // landing in the worker's window has to surface as a chip without
    // a manual refresh, so we invalidate the prefix and let the
    // visible window refetch.
    invalidate(qc, ["my-schedule"]);
  },

  "task.assigned": (_event, qc) => {
    invalidate(qc, qk.tasks());
    invalidate(qc, qk.today());
    invalidate(qc, qk.dashboard());
    // §14 worker schedule — assignment can move a task into or out of
    // the worker's calendar; refresh the loaded window.
    invalidate(qc, ["my-schedule"]);
  },

  "task.updated": (event, qc) => {
    const payload = event.data as unknown as TaskRefPayload;
    // The canonical event carries `{task_id, changed_fields}` only —
    // there is no rendered `task` object on the wire (cd-m0hz). Treat
    // `task.updated` as a pure invalidation signal: the per-detail
    // envelope (`{ task, property, instructions }`) is dropped from
    // the cache, and any mounted detail page refetches under the
    // normal per-row authz path.
    invalidate(qc, qk.task(payload.task_id));
    invalidate(qc, qk.tasks());
    invalidate(qc, qk.today());
    invalidate(qc, qk.dashboard());
    // §14 worker schedule — the cell-level chip reads `title`,
    // `property_id`, and `scheduled_start` from the schedule payload,
    // so a rename or re-property doesn't surface without invalidating
    // the loaded windows.
    invalidate(qc, ["my-schedule"]);
  },

  "task.completed": (event, qc) => {
    const payload = event.data as unknown as TaskRefPayload;
    // Same posture as `task.updated`: the canonical `TaskCompleted`
    // event publishes `{task_id, completed_by}` only; subscribers
    // re-fetch via REST. Invalidate the per-row detail key alongside
    // the list / today / dashboard / history surfaces.
    invalidate(qc, qk.task(payload.task_id));
    invalidate(qc, qk.tasks());
    invalidate(qc, qk.today());
    invalidate(qc, qk.dashboard());
    // §14 worker history — a newly completed occurrence belongs in the
    // Tasks tab of the `/history` feed.
    invalidate(qc, qk.history("tasks"));
    // §14 worker schedule — drop the task chip from the day cell when
    // the worker checks it off. Keys are `["my-schedule", ...]` so
    // invalidate the prefix for every loaded window.
    invalidate(qc, ["my-schedule"]);
  },

  "task.skipped": (event, qc) => {
    const payload = event.data as unknown as TaskRefPayload;
    // Same posture as `task.updated`: the canonical `TaskSkipped`
    // event publishes `{task_id, reason}` only.
    invalidate(qc, qk.task(payload.task_id));
    invalidate(qc, qk.tasks());
    invalidate(qc, qk.today());
    invalidate(qc, qk.dashboard());
    // §14 worker history — skipped occurrences land in the Tasks tab too.
    invalidate(qc, qk.history("tasks"));
    // §14 worker schedule — skipping flips the chip status; the cell
    // needs a refresh to recolor it.
    invalidate(qc, ["my-schedule"]);
  },

  "task.overdue": (_event, qc) => {
    invalidate(qc, qk.tasks());
    invalidate(qc, qk.today());
    invalidate(qc, qk.dashboard());
    // §14 worker schedule — overdue chips read with a rust accent in
    // the cell; refresh so the colour matches the new status.
    invalidate(qc, ["my-schedule"]);
  },

  "stay.upcoming": (_event, qc) => {
    invalidate(qc, qk.stays());
    invalidate(qc, qk.dashboard());
    // §14 worker schedule — upcoming stays cut bookings via the
    // nightly materialiser (§09). The bookings/tasks then surface as
    // cells, so the worker's schedule has to refresh too.
    invalidate(qc, ["my-schedule"]);
  },

  "stay_task_bundle.upserted": (_event, qc) => {
    // §14 scheduler: `stay_task_bundle.*` invalidates the scheduler
    // calendar feed. The calendar key includes a window, so match by
    // root prefix to hit every mounted window.
    invalidate(qc, ["scheduler-calendar"]);
    invalidate(qc, qk.stays());
    // §14 worker schedule — bundle changes ripple into the worker's
    // tasks for the affected stay; refresh the loaded windows.
    invalidate(qc, ["my-schedule"]);
  },

  "stay_task_bundle.deleted": (_event, qc) => {
    invalidate(qc, ["scheduler-calendar"]);
    invalidate(qc, qk.stays());
    invalidate(qc, ["my-schedule"]);
  },

  "approval.decided": (_event, qc) => {
    invalidate(qc, qk.approvals());
    invalidate(qc, qk.dashboard());
    // §14 worker history — approved leaves surface on the Leaves tab;
    // approved expenses reach the Expenses tab via the `expense.*`
    // kinds below, but an approval resolution can be the source event
    // on some server builds so we invalidate both here defensively.
    invalidate(qc, qk.history("leaves"));
    invalidate(qc, qk.history("expenses"));
    // §14 worker schedule — approving a leave or availability
    // override flips the day's cell tone (sand "pending" → moss /
    // rust) and the badge text. The backend doesn't yet emit
    // dedicated `user_leave.*` / `user_availability_override.*`
    // kinds; until it does, `approval.decided` is the umbrella that
    // also covers the leave + override approval flow.
    invalidate(qc, ["my-schedule"]);
    invalidate(qc, qk.leaves());
    invalidate(qc, qk.meOverrides());
  },

  "approval.resolved": (_event, qc) => {
    // Spec §14 calls this `approval.resolved` (the older name for
    // the same decision event). Kept as an alias so a server build
    // emitting either name works unchanged.
    invalidate(qc, qk.approvals());
    invalidate(qc, qk.dashboard());
    invalidate(qc, qk.history("leaves"));
    invalidate(qc, qk.history("expenses"));
    // Same schedule + leave + override invalidations as
    // `approval.decided` above.
    invalidate(qc, ["my-schedule"]);
    invalidate(qc, qk.leaves());
    invalidate(qc, qk.meOverrides());
  },

  "expense.created": (_event, qc) => {
    // Worker just filed a draft (or the agent / manager filed one for
    // them). Only the worker's own list and dashboard care; the
    // workspace-wide `all` list isn't user-visible until the claim is
    // submitted, so we leave it alone to keep the cache cheap.
    invalidate(qc, qk.expenses("mine"));
    invalidate(qc, qk.dashboard());
  },

  "expense.submitted": (_event, qc) => {
    // Draft → submitted: the claim now appears in the manager's
    // approval queue and on the worker's "mine" list with an updated
    // status chip. `expenses("all")` is the manager-side list.
    invalidate(qc, qk.expenses("all"));
    invalidate(qc, qk.expenses("mine"));
    invalidate(qc, qk.dashboard());
  },

  "expense.cancelled": (_event, qc) => {
    // Worker withdrew a draft / submitted claim. Worker's own list
    // updates (the row disappears or moves to a cancelled chip) and
    // the dashboard recomputes its pending count.
    invalidate(qc, qk.expenses("all"));
    invalidate(qc, qk.expenses("mine"));
    invalidate(qc, qk.dashboard());
  },

  "expense.approved": (_event, qc) => {
    invalidate(qc, qk.expenses("all"));
    invalidate(qc, qk.expenses("mine"));
    // Approval is the moment the claim lands in "Owed to you" — refresh
    // the per-user pending-reimbursement total alongside the list.
    invalidate(qc, qk.expensesPendingReimbursement("me"));
    invalidate(qc, qk.dashboard());
    invalidate(qc, qk.history("expenses"));
  },

  "expense.rejected": (_event, qc) => {
    invalidate(qc, qk.expenses("all"));
    invalidate(qc, qk.expenses("mine"));
    // A reject doesn't change "Owed to you" itself, but if the claim
    // had previously bounced through approved → rejected (rare, but
    // possible on amend) the cached total would be stale. Cheap to
    // invalidate; safer than a divergent number on screen.
    invalidate(qc, qk.expensesPendingReimbursement("me"));
    invalidate(qc, qk.dashboard());
    invalidate(qc, qk.history("expenses"));
  },

  "expense.reimbursed": (_event, qc) => {
    invalidate(qc, qk.expenses("all"));
    invalidate(qc, qk.expenses("mine"));
    // Reimbursement removes the claim from the pending pool — refresh
    // the worker's "Owed to you" total so it shrinks the moment the
    // payslip lands.
    invalidate(qc, qk.expensesPendingReimbursement("me"));
    invalidate(qc, qk.dashboard());
    invalidate(qc, qk.history("expenses"));
  },

  "expense.decided": (_event, qc) => {
    // §14 bundle name covering both approve / reject so a server that
    // emits the bundled kind invalidates the same surfaces.
    invalidate(qc, qk.expenses("all"));
    invalidate(qc, qk.expenses("mine"));
    invalidate(qc, qk.expensesPendingReimbursement("me"));
    invalidate(qc, qk.dashboard());
    invalidate(qc, qk.history("expenses"));
  },

  "asset_action.performed": (event, qc) => {
    const payload = event.data as unknown as AssetActionPayload;
    invalidate(qc, qk.asset(payload.asset_id));
    invalidate(qc, qk.assets());
  },

  "schedule_ruleset.upserted": (_event, qc) => {
    invalidate(qc, qk.scheduleRulesets());
    invalidate(qc, ["scheduler-calendar"]);
  },

  "schedule_ruleset.deleted": (_event, qc) => {
    invalidate(qc, qk.scheduleRulesets());
    invalidate(qc, ["scheduler-calendar"]);
  },

  "booking.created": (_event, qc) => bookingInvalidations(qc),
  "booking.amended": (_event, qc) => bookingInvalidations(qc),
  "booking.declined": (_event, qc) => bookingInvalidations(qc),
  "booking.approved": (_event, qc) => bookingInvalidations(qc),
  "booking.rejected": (_event, qc) => bookingInvalidations(qc),
  "booking.cancelled": (_event, qc) => bookingInvalidations(qc),
  "booking.reassigned": (_event, qc) => bookingInvalidations(qc),

  "shift.ended": (_event, qc) => {
    invalidate(qc, ["my-schedule"]);
    invalidate(qc, qk.dashboard());
  },

  "time.shift.changed": (_event, qc) => {
    invalidate(qc, ["my-schedule"]);
    invalidate(qc, qk.dashboard());
  },

  "admin.audit.appended": (_event, qc) => {
    // §12 SSE — `/admin/events` only carries `scope_kind ==
    // 'deployment'` audit rows, so client-side filtering is
    // unnecessary: the server has already enforced the scope before
    // the frame leaves the deployment stream. The `/admin/audit`
    // page key (`["admin", "audit"]`) lives outside the workspace
    // namespace because admin is deployment-scope, not
    // workspace-scope (§14 "Admin shell").
    invalidate(qc, qk.adminAudit());
  },

  "workspace.changed": (_event, qc) => {
    // Big-hammer: a workspace-level setting reshaped policy. Every
    // cached query under the active workspace is suspect.
    qc.invalidateQueries({ refetchType: "active" });
  },
};

function bookingInvalidations(qc: QueryClient): void {
  // §09 booking lifecycle. `/schedule` keys include a window
  // (`["my-schedule", from, to]`), so invalidate by the root prefix
  // to catch every currently-mounted window.
  invalidate(qc, ["my-schedule"]);
  invalidate(qc, qk.bookings());
  invalidate(qc, qk.dashboard());
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

function isKnownKind(kind: string): kind is EventKind {
  return kind in INVALIDATIONS;
}

/**
 * Route a parsed frame through the `INVALIDATIONS` table.
 *
 * Unknown kinds are logged via `console.warn` and otherwise ignored —
 * this keeps forward compatibility one-way (new server → old client)
 * without throwing inside the event listener (a throw there bubbles
 * through `EventSource` and kills the `onmessage` callback chain on
 * some browsers).
 */
export function dispatchSseEvent(event: SseEvent, qc: QueryClient): void {
  if (!isKnownKind(event.kind)) {
    // eslint-disable-next-line no-console -- forward-compat diagnostic
    console.warn(`[sse] unknown event kind: ${event.kind}`);
    return;
  }
  const handler = INVALIDATIONS[event.kind];
  try {
    handler(event, qc);
  } catch (err) {
    // A handler bug must not take the SSE stream down. Log and move
    // on; the next event still reaches the dispatcher.
    // eslint-disable-next-line no-console -- handler-bug diagnostic
    console.error(`[sse] handler for ${event.kind} threw`, err);
  }
}

/**
 * Parse a raw SSE `MessageEvent` into the internal `SseEvent` shape.
 *
 * Returns `null` when the frame's `data` isn't valid JSON — the
 * server always emits JSON, so a non-JSON frame means either a proxy
 * garbled the stream or an older untyped emitter; dropping is safer
 * than blowing up the listener.
 */
export function parseSseMessage(
  kind: string,
  lastEventId: string,
  rawData: string,
): SseEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(rawData);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return null;
  }
  const data = parsed as Record<string, unknown>;
  const workspaceId = typeof data.workspace_id === "string" ? data.workspace_id : "";
  // The server always stamps `kind` into the payload; prefer the
  // outer SSE `event:` line (which is the wire contract) but fall
  // back to the payload field if a future transport drops the
  // outer header.
  const effectiveKind = kind || (typeof data.kind === "string" ? data.kind : "");
  return {
    id: lastEventId,
    kind: effectiveKind as EventKind,
    workspace_id: workspaceId,
    data,
  };
}

// ---------------------------------------------------------------------------
// Backoff
// ---------------------------------------------------------------------------

/**
 * Exponential backoff with ±20 % jitter, capped at 30 s. The attempt
 * count is 0-based for the *next* attempt:
 *
 * ```
 * attempt 0 → 1 s   (±20 %)
 * attempt 1 → 2 s
 * attempt 2 → 4 s
 * attempt 3 → 8 s
 * attempt 4 → 16 s
 * attempt 5 → 30 s (capped)
 * attempt 6+ → 30 s
 * ```
 *
 * Jitter spreads the herd when a server restart disconnects every
 * tab at once. The cap of 30 s keeps a wedged server from delaying
 * recovery for minutes while still giving the server room to
 * breathe between reconnects.
 */
export const BACKOFF_INITIAL_MS = 1_000;
export const BACKOFF_MAX_MS = 30_000;
export const BACKOFF_JITTER = 0.2;

export function backoffDelayMs(
  attempt: number,
  rng: () => number = Math.random,
): number {
  if (attempt < 0) return BACKOFF_INITIAL_MS;
  const base = Math.min(BACKOFF_INITIAL_MS * 2 ** attempt, BACKOFF_MAX_MS);
  // `rng()` is `[0, 1)`; map to `[-1, +1)` then scale to ±jitter.
  const jitterFactor = 1 + (rng() * 2 - 1) * BACKOFF_JITTER;
  const jittered = base * jitterFactor;
  // A stray RNG that returned exactly 0 would produce `base * 0.8`;
  // clamp the upper bound so the jitter can't push past `BACKOFF_MAX_MS`.
  return Math.max(0, Math.min(jittered, BACKOFF_MAX_MS));
}

// ---------------------------------------------------------------------------
// Connection helper
// ---------------------------------------------------------------------------

/**
 * Connection status reported by `useSseConnection`.
 *
 * - `connecting` — a fresh `EventSource` has been constructed but has
 *   not yet fired `onopen`.
 * - `open` — the transport is live (server flushed `retry:` + replay).
 * - `reconnecting` — a backoff timer is running before the next
 *   `new EventSource(...)`.
 * - `closed` — no stream active (pre-auth, post-logout, or
 *   `EventSource` unavailable in this runtime).
 */
export type SseStatus = "connecting" | "open" | "reconnecting" | "closed";

export interface ConnectOptions {
  /**
   * Active workspace slug, or `null` for the pre-workspace `/events`
   * fallback. Changing the slug closes the live stream and opens a
   * new one at the scoped URL; the `Last-Event-ID` is *not* carried
   * across the switch (different workspace = different event stream
   * on the server).
   */
  slug: string | null;
  /** Query client the dispatcher invalidates. */
  qc: QueryClient;
  /** Status callback; called on every state transition. */
  onStatus?: (status: SseStatus) => void;
  /** `Last-Event-ID` callback; called when a fresh id is received. */
  onLastEventId?: (id: string) => void;
  /**
   * Optional `EventSource` factory. Overridden by tests so the
   * connection state machine can be driven without a real
   * transport. Defaults to the global `EventSource`.
   */
  eventSourceFactory?: (url: string) => EventSource;
  /**
   * Optional RNG seed for backoff jitter (tests freeze it to
   * `() => 0.5` for deterministic timers).
   */
  rng?: () => number;
}

/**
 * Shape returned by `connectEventStream`. Call `close()` once (e.g.
 * from a `useEffect` cleanup) to tear the connection down and stop
 * all pending reconnects.
 */
export interface SseConnection {
  close: () => void;
}

function sseUrl(slug: string | null): string {
  return slug ? `/w/${slug}/events` : "/events";
}

// `EventSource.CLOSED = 2` in the DOM spec; we read the numeric
// literal so test polyfills that implement only the API surface (not
// the readonly statics) still exercise the same branch.
const EVENT_SOURCE_CLOSED = 2 as const;

/**
 * Open a self-healing SSE connection. Returns an `SseConnection` whose
 * `close()` method tears the connection down idempotently.
 *
 * Notes on `Last-Event-ID`:
 * - The browser's `EventSource` tracks the last-seen id per
 *   instance and sends it back via the standard `Last-Event-ID`
 *   header on reconnect. We don't override that behaviour — this
 *   helper creates a fresh `EventSource` on drop and lets the
 *   browser do the echo.
 * - On a workspace switch we tear the instance down and create a
 *   new one pointed at a different URL; the browser discards the
 *   old id, which is what we want (the new workspace's event
 *   sequence starts from zero on the server).
 */
export function connectEventStream(opts: ConnectOptions): SseConnection {
  const {
    slug,
    qc,
    onStatus,
    onLastEventId,
    eventSourceFactory,
    rng,
  } = opts;

  const url = sseUrl(slug);
  const factory =
    eventSourceFactory ??
    ((u: string): EventSource =>
      new EventSource(u, { withCredentials: true }));

  let es: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  let closed = false;

  const setStatus = (status: SseStatus): void => {
    if (onStatus) onStatus(status);
  };

  const handleMessage = (evt: MessageEvent<string>): void => {
    // `type` is the SSE `event:` header (defaults to `"message"` for
    // unnamed frames). `lastEventId` is whatever the browser parsed
    // off the `id:` line; empty string when the frame had no id.
    const kind = (evt as unknown as { type: string }).type;
    const lastEventId = (evt as unknown as { lastEventId?: string }).lastEventId ?? "";
    if (lastEventId && onLastEventId) onLastEventId(lastEventId);

    const parsed = parseSseMessage(kind, lastEventId, evt.data);
    if (!parsed) return;
    dispatchSseEvent(parsed, qc);
  };

  const attachListeners = (source: EventSource): void => {
    // One listener per kind — the browser doesn't dispatch named
    // events through `onmessage`. We also bind `onmessage` for
    // anonymous frames (e.g. `event: dropped`) so forward-compat
    // kinds still land in the dispatcher's "unknown" branch.
    const kinds: EventKind[] = Object.keys(INVALIDATIONS) as EventKind[];
    for (const kind of kinds) {
      source.addEventListener(kind, handleMessage as EventListener);
    }
    source.onmessage = handleMessage;
  };

  const connect = (): void => {
    if (closed) return;
    setStatus("connecting");
    es = factory(url);
    attachListeners(es);

    es.onopen = (): void => {
      attempt = 0;
      // Clear any stale typing indicators from the dropped session
      // (§14 "Agent turn indicator" — clears on SSE reconnect).
      clearAllTyping(qc);
      setStatus("open");
    };

    es.onerror = (): void => {
      // The browser opens `readyState === 2` (CLOSED) on a hard
      // failure and `1` (OPEN) on transient errors it retries on
      // its own. Only re-arm our backoff on a hard close, otherwise
      // we double-reconnect and race two streams.
      //
      // We read the numeric literal rather than `EventSource.CLOSED`
      // because the test polyfill doesn't carry the static (and we
      // want the dispatcher to behave identically whether the host
      // ships the full `EventSource` constants or just the API
      // surface we use).
      if (!es || es.readyState !== EVENT_SOURCE_CLOSED) return;
      es.close();
      es = null;
      if (closed) return;

      const delay = backoffDelayMs(attempt, rng);
      attempt += 1;
      setStatus("reconnecting");
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };
  };

  connect();

  return {
    close: (): void => {
      if (closed) return;
      closed = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (es) {
        es.close();
        es = null;
      }
      clearAllTyping(qc);
      setStatus("closed");
    },
  };
}

// ---------------------------------------------------------------------------
// Legacy API — one-shot start helper
// ---------------------------------------------------------------------------

/**
 * Start a single long-lived stream and return a teardown function.
 * Kept so existing callers (tests, the placeholder in `main.tsx`
 * history) compile; product code should prefer `<SseProvider>` from
 * `@/context/SseContext`.
 */
export function startEventStream(
  qc: QueryClient,
  slug: string | null = null,
): () => void {
  if (typeof EventSource === "undefined") return () => undefined;
  const conn = connectEventStream({ slug, qc });
  return () => conn.close();
}
