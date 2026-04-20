// crewday — JSON API types: aggregated payloads for /dashboard
// and /history pages.

import type { Employee } from "./employee";
import type { Task, Issue } from "./task";
import type { ApprovalRequest } from "./approval";
import type { Expense } from "./expense";
import type { Leave } from "./employee";
import type { Stay, Property } from "./property";

export interface HistoryPayload {
  tab: "tasks" | "chats" | "expenses" | "leaves";
  tasks: Task[];
  expenses: Expense[];
  leaves: Leave[];
  chats: { id: string; title: string; last_at: string; summary: string }[];
}

export interface DashboardPayload {
  on_booking: Employee[];
  by_status: { completed: Task[]; in_progress: Task[]; pending: Task[] };
  pending_approvals: ApprovalRequest[];
  pending_expenses: Expense[];
  pending_leaves: Leave[];
  open_issues: Issue[];
  stays_today: Stay[];
  properties: Property[];
  employees: Employee[];
}
