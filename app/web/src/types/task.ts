// crewday — JSON API types: tasks, checklists, templates, schedules,
// instructions, issues, scheduler calendar, availability overrides.

import type { Booking } from "./booking";
import type { Leave } from "./employee";

export type TaskStatus = "scheduled" | "pending" | "in_progress" | "completed" | "skipped" | "cancelled" | "overdue";
export type TaskPriority = "low" | "normal" | "high" | "urgent";
export type PhotoEvidence = "disabled" | "optional" | "required";

export interface ChecklistItem {
  label: string;
  done?: boolean;
  guest_visible?: boolean;
  // §06 "Checklist template shape": per-item RRULE filter.
  // Populated when the item is a materialised instance of a template
  // item that carried an RRULE.
  key?: string;
  required?: boolean;
  rrule?: string | null;
  dtstart_local?: string | null;
}

// §06 Task template checklist shape — authoring-side shape carried on
// task_template.checklist_template_json items. Distinct from the
// runtime per-task row: this is the template rule, including the
// optional RRULE filter.
export interface ChecklistTemplateItem {
  key: string;
  text: string;
  required: boolean;
  guest_visible?: boolean;
  rrule?: string | null;
  dtstart_local?: string | null;
}

export interface Task {
  id: string;
  title: string;
  property_id: string;
  area: string;
  assignee_id: string;
  scheduled_start: string;
  estimated_minutes: number;
  priority: TaskPriority;
  status: TaskStatus;
  checklist: ChecklistItem[];
  photo_evidence: PhotoEvidence;
  evidence_policy: "inherit" | "require" | "optional" | "forbid";
  instructions_ids: string[];
  template_id: string | null;
  schedule_id: string | null;
  turnover_bundle_id: string | null;
  asset_id: string | null;
  settings_override: Record<string, unknown>;
  assigned_user_id: string;
  workspace_id: string;
  // §06 "Self-created and personal tasks". Private to creator +
  // workspace owners when `is_personal` (see §15 RLS).
  created_by: string;
  is_personal: boolean;
}

export interface TaskComment {
  id: string;
  task_id: string;
  // v1 collapses author_kind to user|agent|system (§02).
  author_kind: "user" | "agent" | "system";
  author_id: string;
  body_md: string;
  created_at: string;
}

export interface TaskTemplate {
  id: string;
  name: string;
  description: string;
  role: string;
  duration_minutes: number;
  property_scope: "any" | "one" | "listed";
  photo_evidence: PhotoEvidence;
  priority: TaskPriority;
  checklist: ChecklistItem[];
}

export interface Schedule {
  id: string;
  name: string;
  template_id: string;
  property_id: string;
  rrule_human: string;
  default_assignee_id: string | null;
  backup_assignee_user_ids: string[];
  duration_minutes: number;
  active_from: string;
  paused: boolean;
}

export interface Instruction {
  id: string;
  title: string;
  scope: "global" | "property" | "area";
  property_id: string | null;
  area: string | null;
  tags: string[];
  body_md: string;
  version: number;
  updated_at: string;
}

export interface Issue {
  id: string;
  reported_by: string;
  property_id: string;
  area: string;
  severity: "low" | "normal" | "high" | "urgent";
  category: "damage" | "broken" | "supplies" | "safety" | "other";
  title: string;
  body: string;
  reported_at: string;
  status: "open" | "in_progress" | "resolved" | "wont_fix";
}

// §06 — per-property recurring rota (Schedule ruleset).
export interface ScheduleRuleset {
  id: string;
  workspace_id: string;
  name: string;
}

export interface ScheduleRulesetSlot {
  id: string;
  schedule_ruleset_id: string;
  weekday: number; // 0..6 (Mon..Sun, ISO)
  starts_local: string; // "HH:MM"
  ends_local: string;
}

export interface ScheduleAssignment {
  id: string;
  user_id: string | null;
  work_role_id: string | null;
  property_id: string;
  schedule_ruleset_id: string | null;
}

export interface SchedulerUserView {
  id: string;
  first_name: string;
  display_name?: string;
}

export interface SchedulerTaskView {
  id: string;
  title: string;
  property_id: string;
  user_id: string;
  scheduled_start: string;
  estimated_minutes: number;
  priority: TaskPriority;
  status: TaskStatus;
}

export interface SchedulerCalendarPayload {
  window: { from: string; to: string };
  rulesets: ScheduleRuleset[];
  slots: ScheduleRulesetSlot[];
  assignments: ScheduleAssignment[];
  tasks: SchedulerTaskView[];
  users: SchedulerUserView[];
  properties: { id: string; name: string; timezone: string }[];
}

// §06 — per-date override of the user's weekly availability.
export type AvailabilityOverrideCategory =
  | "off"
  | "custom_hours"
  | "extend"
  | "extra_day";

export interface AvailabilityOverride {
  id: string;
  user_id: string;
  workspace_id: string;
  date: string; // ISO date
  available: boolean;
  starts_local: string | null; // "HH:MM" when available with custom hours
  ends_local: string | null;
  reason: string | null;
  approval_required: boolean;
  approved_at: string | null;
  approved_by: string | null;
  created_at: string;
}

export interface SelfWeeklyAvailabilitySlot {
  weekday: number; // 0..6 ISO
  starts_local: string | null;
  ends_local: string | null;
}

// §12 GET /api/v1/me/schedule — self-only calendar feed for /schedule (§14).
export interface MySchedulePayload {
  window: { from: string; to: string };
  user_id: string;
  weekly_availability: SelfWeeklyAvailabilitySlot[];
  rulesets: ScheduleRuleset[];
  slots: ScheduleRulesetSlot[];
  assignments: ScheduleAssignment[];
  tasks: SchedulerTaskView[];
  properties: { id: string; name: string; timezone: string }[];
  leaves: Leave[];
  overrides: AvailabilityOverride[];
  // §09 — worker's booking rows covering the window, any status. The
  // day drawer renders them inline with amend / decline actions; the
  // page header surfaces a banner counting pending_approval +
  // pending_amend rows.
  bookings: Booking[];
}
