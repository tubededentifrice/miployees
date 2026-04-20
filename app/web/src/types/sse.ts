// crewday — JSON API types: SSE event shapes. The server emits
// JSON-serialised payloads under different event names; the
// SseContext dispatches on `event` and feeds `data` into TanStack
// Query invalidations.

import type { AgentMessage } from "./messaging";
import type { Task, ScheduleRuleset } from "./task";
import type { ExpenseStatus } from "./expense";
import type { AssetAction } from "./asset";
import type { Booking } from "./booking";

export type AgentTurnScope = "employee" | "manager" | "admin" | "task";

export type SseEvent =
  | { event: "tick"; data: { now: string } }
  | {
      event: "agent.message.appended";
      data: {
        scope: "employee" | "manager" | "admin" | "task";
        /** Present when `scope === "task"`; identifies which task the message belongs to. */
        task_id?: string;
        message: AgentMessage;
      };
    }
  | {
      // §11 "Agent turn lifecycle" — bracket the server-side agent turn
      // so clients can render the typing indicator (§14).
      event: "agent.turn.started";
      data: {
        scope: AgentTurnScope;
        task_id?: string;
        started_at: string;
      };
    }
  | {
      event: "agent.turn.finished";
      data: {
        scope: AgentTurnScope;
        task_id?: string;
        finished_at: string;
        outcome: "replied" | "action" | "error" | "timeout";
      };
    }
  | { event: "task.updated"; data: { task: Task } }
  | { event: "task.completed"; data: { task: Task } }
  | { event: "task.skipped"; data: { task: Task; reason: string | null } }
  | { event: "approval.decided"; data: { id: string; decision: "approve" | "reject" } }
  | { event: "expense.approved"; data: { id: string; status: ExpenseStatus } }
  | { event: "expense.rejected"; data: { id: string; status: ExpenseStatus } }
  | { event: "expense.reimbursed"; data: { id: string; status: ExpenseStatus } }
  | { event: "asset_action.performed"; data: { asset_id: string; action: AssetAction } }
  | { event: "schedule_ruleset.upserted"; data: { ruleset: ScheduleRuleset } }
  | { event: "schedule_ruleset.deleted"; data: { id: string } }
  // §09 booking lifecycle — every mutation from any tab (including
  // another manager approving an amend) fans out to every connected
  // client so `/schedule` and `/dashboard` stay coherent without a
  // poll. One dispatch case invalidates the shared roots.
  | { event: "booking.created"; data: { booking: Booking } }
  | { event: "booking.amended"; data: { booking: Booking } }
  | { event: "booking.declined"; data: { booking: Booking } }
  | { event: "booking.approved"; data: { booking: Booking } }
  | { event: "booking.rejected"; data: { booking: Booking } }
  | { event: "booking.cancelled"; data: { booking: Booking } }
  | { event: "booking.reassigned"; data: { booking: Booking } };
