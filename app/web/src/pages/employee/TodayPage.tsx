import { Link } from "react-router-dom";
import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import {
  enqueueMutation,
  isBrowserOnline,
  subscribeOfflineQueueReplay,
} from "@/lib/offlineQueue";
import { qk } from "@/lib/queryKeys";
import { Camera, Check } from "lucide-react";
import { Chip, EmptyState, Loading, ProgressBar } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import TaskListCard from "@/components/TaskListCard";
import NewTaskButton from "@/components/NewTaskModal";
import { fmtTime } from "@/lib/dates";
import { cap } from "@/lib/strings";
import type { Me, PhotoEvidence, Property, Task, TaskPriority, TaskStatus } from "@/types/api";

type ApiTaskState =
  | "scheduled"
  | "pending"
  | "in_progress"
  | "done"
  | "skipped"
  | "cancelled"
  | "overdue";

interface ApiChecklistItem {
  label?: string;
  text?: string;
  done?: boolean;
  checked?: boolean;
  guest_visible?: boolean;
  key?: string;
  required?: boolean;
}

interface ApiTask {
  id: string;
  title: string;
  workspace_id?: string;
  template_id?: string | null;
  schedule_id?: string | null;
  property_id?: string | null;
  area?: string | null;
  area_id?: string | null;
  priority: TaskPriority;
  state?: ApiTaskState;
  status?: TaskStatus;
  scheduled_for_utc?: string;
  scheduled_for_local?: string;
  scheduled_start?: string;
  duration_minutes?: number | null;
  estimated_minutes?: number;
  photo_evidence: PhotoEvidence;
  evidence_policy?: Task["evidence_policy"];
  linked_instruction_ids?: string[];
  instructions_ids?: string[];
  assigned_user_id?: string | null;
  assignee_id?: string | null;
  created_by?: string | null;
  is_personal?: boolean;
  asset_id?: string | null;
  settings_override?: Record<string, unknown>;
  checklist?: ApiChecklistItem[];
}

interface TaskListResponse {
  data: ApiTask[];
  next_cursor: string | null;
  has_more: boolean;
}

interface TaskStatePayload {
  task_id: string;
  state: ApiTaskState;
  completed_at: string | null;
  completed_by_user_id: string | null;
  reason: string | null;
}

interface TodayPayload {
  now_task: Task | null;
  upcoming: Task[];
  completed: Task[];
  nowIso: string;
}

type CompleteResult =
  | { queued: false; payload: TaskStatePayload }
  | { queued: true; taskId: string };

interface CompleteContext {
  previous: TodayPayload | undefined;
}

function ctaLabel(t: Task): string {
  if (t.status === "pending") return "Start";
  if (t.photo_evidence === "required") return "Complete with photo";
  return "Mark done";
}

export default function TodayPage() {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const properties = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const today = useQuery({
    queryKey: qk.today(),
    queryFn: () => fetchToday(me.data!),
    enabled: Boolean(me.data),
  });

  useEffect(
    () =>
      subscribeOfflineQueueReplay((entry) => {
        if (entry.kind !== "task.complete") return;
        qc.invalidateQueries({ queryKey: qk.today() });
        qc.invalidateQueries({ queryKey: qk.tasks() });
      }),
    [qc],
  );

  const complete = useMutation<CompleteResult, Error, Task, CompleteContext>({
    mutationFn: async (task) => {
      const path = "/api/v1/tasks/" + task.id + "/complete";
      const body = { photo_evidence_ids: [] };
      if (!isBrowserOnline()) {
        await enqueueMutation({
          kind: "task.complete",
          method: "POST",
          path,
          body,
        });
        return { queued: true, taskId: task.id };
      }
      const payload = await fetchJson<TaskStatePayload>(path, { method: "POST", body });
      return { queued: false, payload };
    },
    onMutate: async (task) => {
      await qc.cancelQueries({ queryKey: qk.today() });
      const previous = qc.getQueryData<TodayPayload>(qk.today());
      qc.setQueryData<TodayPayload>(qk.today(), (current) =>
        current ? markCompleted(current, task.id) : current,
      );
      return { previous };
    },
    onError: (_err, _task, context) => {
      if (context?.previous) qc.setQueryData(qk.today(), context.previous);
    },
    onSuccess: (result) => {
      if (result.queued) return;
      qc.invalidateQueries({ queryKey: qk.today() });
      qc.invalidateQueries({ queryKey: qk.task(result.payload.task_id) });
      qc.invalidateQueries({ queryKey: qk.tasks() });
    },
  });

  const header = (
    <PageHeader
      title="Today"
      sub={me.data ? formatHeaderDate(me.data.today) : null}
      actions={<NewTaskButton />}
    />
  );

  if (me.isPending || properties.isPending || (me.data && today.isPending)) {
    return <>{header}<section className="phone__section"><Loading /></section></>;
  }
  if (me.isError || properties.isError || today.isError || !today.data) {
    return <>{header}<section className="phone__section"><EmptyState>Failed to load.</EmptyState></section></>;
  }

  const { now_task, upcoming, completed } = today.data;
  const propsById = new Map(properties.data.map((p) => [p.id, p]));

  return (
    <>
      {header}
      <section className="phone__section phone__section--hero">
        <h2 className="section-title">Now</h2>
        {now_task ? (
          <NowCard
            task={now_task}
            property={propsById.get(now_task.property_id) ?? null}
            completePending={complete.isPending}
            onComplete={() => complete.mutate(now_task)}
          />
        ) : (
          <EmptyState glyph={<Check size={28} strokeWidth={2} aria-hidden="true" />} variant="celebrate">
            All done for now. Nice work.
          </EmptyState>
        )}
      </section>

      <section className="phone__section">
        <h2 className="section-title">Upcoming today · {upcoming.length}</h2>
        <ul className="task-list">
          {upcoming.length === 0 && (
            <li className="empty-state empty-state--quiet">Nothing else scheduled.</li>
          )}
          {upcoming.map((t) => (
            <li key={t.id}>
              <TaskListCard task={t} property={propsById.get(t.property_id) ?? null} />
            </li>
          ))}
        </ul>
      </section>

      <section className="phone__section">
        <details className="completed-group">
          <summary>
            <span>Completed today</span>
            <Chip tone="ghost" size="sm">{String(completed.length)}</Chip>
          </summary>
          <ul className="task-list">
            {completed.map((t) => (
              <li key={t.id}>
                <TaskListCard task={t} property={propsById.get(t.property_id) ?? null} />
              </li>
            ))}
          </ul>
        </details>
      </section>
    </>
  );
}

function NowCard({
  task,
  property,
  completePending,
  onComplete,
}: {
  task: Task;
  property: Property | null;
  completePending: boolean;
  onComplete: () => void;
}) {
  const doneSteps = task.checklist.filter((i) => i.done).length;
  const total = task.checklist.length;
  const pct = total > 0 ? Math.round((doneSteps / total) * 100) : 0;
  const cls = "task-card task-card--now" + (task.is_personal ? " task-card--personal" : "");
  const body = (
    <>
      <div className="task-card__head">
        {property ? (
          <Chip tone={property.color}>{property.name}</Chip>
        ) : task.is_personal ? (
          <Chip tone="ghost">Personal</Chip>
        ) : null}
        {(task.priority === "high" || task.priority === "urgent") && (
          <Chip tone="rust">{cap(task.priority)} priority</Chip>
        )}
        {task.photo_evidence === "required" && (
          <Chip tone="sand"><Camera size={12} strokeWidth={1.8} aria-hidden="true" /> photo required</Chip>
        )}
        <span className="task-card__when">{fmtTime(task.scheduled_start)} · {task.estimated_minutes} min</span>
      </div>
      <h3 className="task-card__title">{task.title}</h3>
      {task.area && <div className="task-card__meta">{task.area}</div>}
      {total > 0 && (
        <div className="task-card__progress">
          <ProgressBar value={pct} />
          <span className="progress-label">{doneSteps}/{total} steps</span>
        </div>
      )}
    </>
  );

  if (task.photo_evidence === "required") {
    return (
      <Link to={"/task/" + task.id} className={cls}>
        {body}
        <div className="task-card__cta">{ctaLabel(task)} {"->"}</div>
      </Link>
    );
  }

  return (
    <article className={cls}>
      <Link to={"/task/" + task.id} className="task-card__body-link">
        {body}
      </Link>
      <button
        type="button"
        className="task-card__cta"
        disabled={completePending}
        onClick={onComplete}
      >
        {completePending ? "Completing..." : ctaLabel(task)}
      </button>
    </article>
  );
}

async function fetchToday(me: Me): Promise<TodayPayload> {
  const window = todayUtcWindow(me.today);
  const params = new URLSearchParams({
    scheduled_for_utc_gte: window.gte,
    scheduled_for_utc_lt: window.lt,
    limit: "100",
  });
  if (me.user_id) params.set("assignee_user_id", me.user_id);
  const page = await fetchJson<TaskListResponse>("/api/v1/tasks?" + params.toString());
  return groupToday(page.data.map(normalizeTask), me.now);
}

function groupToday(tasks: Task[], nowIso: string): TodayPayload {
  const sorted = [...tasks].sort((a, b) => a.scheduled_start.localeCompare(b.scheduled_start));
  const completed = sorted.filter((task) => task.status === "completed");
  const active = sorted.filter((task) => !isTerminalStatus(task.status));
  const nowMs = new Date(nowIso).getTime();
  const now_task = active.find((task) => new Date(task.scheduled_start).getTime() <= nowMs) ?? null;
  const upcoming = active.filter((task) => task.id !== now_task?.id);
  return { now_task, upcoming, completed, nowIso };
}

function markCompleted(today: TodayPayload, taskId: string): TodayPayload {
  const all = [today.now_task, ...today.upcoming, ...today.completed].filter(
    (task): task is Task => task !== null,
  );
  const updated = all.map((task) =>
    task.id === taskId ? { ...task, status: "completed" as const } : task,
  );
  return groupToday(updated, today.nowIso);
}

function normalizeTask(task: ApiTask): Task {
  const state = task.state ?? statusToState(task.status) ?? "pending";
  const scheduledStart =
    task.scheduled_for_utc ?? task.scheduled_start ?? task.scheduled_for_local ?? new Date().toISOString();
  return {
    id: task.id,
    title: task.title,
    property_id: task.property_id ?? "",
    area: task.area ?? task.area_id ?? "",
    assignee_id: task.assignee_id ?? task.assigned_user_id ?? "",
    scheduled_start: scheduledStart,
    estimated_minutes: task.duration_minutes ?? task.estimated_minutes ?? 30,
    priority: task.priority,
    status: stateToStatus(state),
    checklist: (task.checklist ?? []).map((item) => ({
      label: item.label ?? item.text ?? "",
      done: item.done ?? item.checked ?? false,
      guest_visible: item.guest_visible,
      key: item.key,
      required: item.required,
    })).filter((item) => item.label),
    photo_evidence: task.photo_evidence,
    evidence_policy: task.evidence_policy ?? evidencePolicyFromPhoto(task.photo_evidence),
    instructions_ids: task.instructions_ids ?? task.linked_instruction_ids ?? [],
    template_id: task.template_id ?? null,
    schedule_id: task.schedule_id ?? null,
    turnover_bundle_id: null,
    asset_id: task.asset_id ?? null,
    settings_override: task.settings_override ?? {},
    assigned_user_id: task.assigned_user_id ?? task.assignee_id ?? "",
    workspace_id: task.workspace_id ?? "",
    created_by: task.created_by ?? "",
    is_personal: task.is_personal ?? false,
  };
}

function todayUtcWindow(today: string): { gte: string; lt: string } {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(today);
  const start = match
    ? new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])))
    : new Date(today);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  return { gte: start.toISOString(), lt: end.toISOString() };
}

function formatHeaderDate(today: string): string {
  return new Date(today + "T00:00:00").toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

function statusToState(status: TaskStatus | undefined): ApiTaskState | null {
  if (!status) return null;
  return status === "completed" ? "done" : status;
}

function stateToStatus(state: ApiTaskState): TaskStatus {
  return state === "done" ? "completed" : state;
}

function isTerminalStatus(status: TaskStatus): boolean {
  return status === "completed" || status === "skipped" || status === "cancelled";
}

function evidencePolicyFromPhoto(photo: PhotoEvidence): Task["evidence_policy"] {
  if (photo === "required") return "require";
  if (photo === "optional") return "optional";
  return "forbid";
}
