// Single EventSource, shared for the whole app. The hook in
// SseContext subscribes on mount, routes events to TanStack Query
// invalidations or optimistic cache updates, and tears down on unmount.

import type { QueryClient } from "@tanstack/react-query";
import type { AgentMessage, AssetAction, SseEvent, Task, ExpenseStatus } from "@/types/api";
import { qk } from "./queryKeys";

type TypedEvent = { type: SseEvent["event"]; data: string };

export function startEventStream(client: QueryClient): () => void {
  if (typeof EventSource === "undefined") return () => undefined;
  const es = new EventSource("/events", { withCredentials: true });

  const handler = (evt: MessageEvent<string>): void => {
    dispatch(client, { type: (evt as unknown as { type: SseEvent["event"] }).type, data: evt.data });
  };

  const events: SseEvent["event"][] = [
    "tick",
    "agent.message.appended",
    "task.updated",
    "task.completed",
    "task.skipped",
    "approval.decided",
    "expense.approved",
    "expense.rejected",
    "expense.reimbursed",
    "asset_action.performed",
  ];
  for (const ev of events) {
    es.addEventListener(ev, handler as EventListener);
  }

  return () => {
    for (const ev of events) {
      es.removeEventListener(ev, handler as EventListener);
    }
    es.close();
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
        scope: "employee" | "manager" | "task";
        task_id?: string;
        message: AgentMessage;
      };
      const key =
        payload.scope === "task" && payload.task_id
          ? qk.agentTaskChat(payload.task_id)
          : payload.scope === "employee"
          ? qk.agentEmployeeLog()
          : qk.agentManagerLog();
      client.setQueryData<AgentMessage[]>(key, (prev) =>
        prev ? [...prev, payload.message] : [payload.message],
      );
      return;
    }
    case "task.updated":
    case "task.completed":
    case "task.skipped": {
      const payload = data as { task: Task };
      // The task-detail query caches a wrapper `{ task, property, instructions }`,
      // so merge into the existing envelope; don't clobber it with a bare Task.
      client.setQueryData<{ task: Task } & Record<string, unknown>>(
        qk.task(payload.task.id),
        (prev) => (prev ? { ...prev, task: payload.task } : prev),
      );
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
  }
}
