// Single EventSource, shared for the whole app. The hook in
// SseContext subscribes on mount, routes events to TanStack Query
// invalidations or optimistic cache updates, and tears down on unmount.

import type { QueryClient } from "@tanstack/react-query";
import type {
  AgentMessage,
  AgentTurnScope,
  AssetAction,
  SseEvent,
  ExpenseStatus,
} from "@/types/api";
import { qk } from "./queryKeys";
import { withBase } from "./api";

type TypedEvent = { type: SseEvent["event"]; data: string };

// §14 "Agent turn indicator" — 60 s local safety net so a dropped
// `agent.turn.finished` can never leave the typing bubble stuck. Keyed
// per scope (per task for task-scoped threads) to match `qk.agentTyping`.
const TYPING_TIMEOUT_MS = 60_000;
const typingTimers = new Map<string, ReturnType<typeof setTimeout>>();

function typingKeySignature(scope: AgentTurnScope, taskId?: string): string {
  return scope === "task" && taskId ? `task:${taskId}` : scope;
}

function startTyping(client: QueryClient, scope: AgentTurnScope, taskId?: string): void {
  const sig = typingKeySignature(scope, taskId);
  client.setQueryData<boolean>(qk.agentTyping(scope, taskId), true);
  const prev = typingTimers.get(sig);
  if (prev) clearTimeout(prev);
  typingTimers.set(
    sig,
    setTimeout(() => {
      client.setQueryData<boolean>(qk.agentTyping(scope, taskId), false);
      typingTimers.delete(sig);
    }, TYPING_TIMEOUT_MS),
  );
}

function stopTyping(client: QueryClient, scope: AgentTurnScope, taskId?: string): void {
  const sig = typingKeySignature(scope, taskId);
  const prev = typingTimers.get(sig);
  if (prev) {
    clearTimeout(prev);
    typingTimers.delete(sig);
  }
  client.setQueryData<boolean>(qk.agentTyping(scope, taskId), false);
}

function clearAllTyping(client: QueryClient): void {
  for (const [sig, handle] of typingTimers) {
    clearTimeout(handle);
    const [prefix, taskId] = sig.split(":");
    if (prefix === "task" && taskId) {
      client.setQueryData<boolean>(qk.agentTyping("task", taskId), false);
    } else {
      client.setQueryData<boolean>(qk.agentTyping(prefix as AgentTurnScope), false);
    }
  }
  typingTimers.clear();
}

export function startEventStream(client: QueryClient): () => void {
  if (typeof EventSource === "undefined") return () => undefined;
  const es = new EventSource(withBase("/events"), { withCredentials: true });

  const handler = (evt: MessageEvent<string>): void => {
    dispatch(client, { type: (evt as unknown as { type: SseEvent["event"] }).type, data: evt.data });
  };

  // Every reconnect of the `EventSource` drops any stale typing state
  // from the previous session (§14 "Agent turn indicator" — clears on
  // SSE reconnect). `onopen` fires on first connect too; no-op then
  // since the timer map is empty.
  es.onopen = () => clearAllTyping(client);

  const events: SseEvent["event"][] = [
    "tick",
    "agent.message.appended",
    "agent.turn.started",
    "agent.turn.finished",
    "task.updated",
    "task.completed",
    "task.skipped",
    "approval.decided",
    "expense.approved",
    "expense.rejected",
    "expense.reimbursed",
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
  ];
  for (const ev of events) {
    es.addEventListener(ev, handler as EventListener);
  }

  return () => {
    for (const ev of events) {
      es.removeEventListener(ev, handler as EventListener);
    }
    es.close();
    clearAllTyping(client);
  };
}

function dispatch(client: QueryClient, evt: TypedEvent): void {
  let data: unknown;
  try {
    data = JSON.parse(evt.data);
  } catch {
    return;
  }
  switch (evt.type) {
    case "tick":
      // heartbeat; nothing to do
      return;
    case "agent.message.appended": {
      const payload = data as {
        scope: AgentTurnScope;
        task_id?: string;
        message: AgentMessage;
      };
      const key =
        payload.scope === "task" && payload.task_id
          ? qk.agentTaskChat(payload.task_id)
          : payload.scope === "admin"
          ? qk.adminAgentLog()
          : payload.scope === "employee"
          ? qk.agentEmployeeLog()
          : qk.agentManagerLog();
      client.setQueryData<AgentMessage[]>(key, (prev) =>
        prev ? [...prev, payload.message] : [payload.message],
      );
      // A reply arriving means the turn resolved into a message;
      // drop the typing indicator on the same scope even if the
      // paired `agent.turn.finished` hasn't dispatched yet.
      stopTyping(client, payload.scope, payload.task_id);
      return;
    }
    case "agent.turn.started": {
      const payload = data as {
        scope: AgentTurnScope;
        task_id?: string;
        started_at: string;
      };
      startTyping(client, payload.scope, payload.task_id);
      return;
    }
    case "agent.turn.finished": {
      const payload = data as {
        scope: AgentTurnScope;
        task_id?: string;
        outcome: "replied" | "action" | "error" | "timeout";
      };
      stopTyping(client, payload.scope, payload.task_id);
      return;
    }
    case "task.updated":
    case "task.completed":
    case "task.skipped": {
      // The canonical events
      // (`app.events.types.{TaskUpdated,TaskCompleted,TaskSkipped}`)
      // carry `{task_id, ...}` only — never a rendered `Task` object
      // (cd-m0hz). Treat each kind as a pure invalidation signal:
      // invalidate the per-row detail key alongside the list / today /
      // dashboard surfaces, and any mounted page refetches via REST
      // under the normal per-row authz path.
      const payload = data as { task_id: string };
      client.invalidateQueries({ queryKey: qk.task(payload.task_id) });
      client.invalidateQueries({ queryKey: qk.tasks() });
      client.invalidateQueries({ queryKey: qk.today() });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    }
    case "approval.decided":
      client.invalidateQueries({ queryKey: qk.approvals() });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    case "expense.approved":
    case "expense.rejected":
    case "expense.reimbursed": {
      const _payload = data as { id: string; status: ExpenseStatus };
      void _payload;
      client.invalidateQueries({ queryKey: qk.expenses("all") });
      client.invalidateQueries({ queryKey: qk.expenses("mine") });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
    }
    case "asset_action.performed": {
      const payload = data as { asset_id: string; action: AssetAction };
      client.invalidateQueries({ queryKey: qk.asset(payload.asset_id) });
      client.invalidateQueries({ queryKey: qk.assets() });
      return;
    }
    case "schedule_ruleset.upserted":
    case "schedule_ruleset.deleted":
      client.invalidateQueries({ queryKey: qk.scheduleRulesets() });
      client.invalidateQueries({ queryKey: ["scheduler-calendar"] });
      return;
    case "booking.created":
    case "booking.amended":
    case "booking.declined":
    case "booking.approved":
    case "booking.rejected":
    case "booking.cancelled":
    case "booking.reassigned":
      // §09 booking lifecycle. `/schedule` keys include a window
      // (`["my-schedule", from, to]`), so invalidate by the root
      // prefix to catch every currently-mounted window.
      client.invalidateQueries({ queryKey: ["my-schedule"] });
      client.invalidateQueries({ queryKey: qk.bookings() });
      client.invalidateQueries({ queryKey: qk.dashboard() });
      return;
  }
}
