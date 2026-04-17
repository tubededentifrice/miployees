import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, EmptyState, Loading, ProgressBar } from "@/components/common";
import TaskListCard from "@/components/TaskListCard";
import type { Property, Task } from "@/types/api";

interface TodayPayload {
  now_task: Task | null;
  upcoming: Task[];
  completed: Task[];
  properties: Property[];
}

function ctaLabel(t: Task): string {
  if (t.status === "pending") return "Start";
  if (t.photo_evidence === "required") return "Complete with photo";
  return "Mark done";
}

function hhmm(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function TodayPage() {
  const q = useQuery({
    queryKey: qk.today(),
    queryFn: () => fetchJson<TodayPayload>("/api/v1/today"),
  });

  if (q.isPending) return <section className="phone__section"><Loading /></section>;
  if (q.isError || !q.data) return <section className="phone__section"><EmptyState>Failed to load.</EmptyState></section>;

  const { now_task, upcoming, completed, properties } = q.data;
  const propsById = new Map(properties.map((p) => [p.id, p]));

  return (
    <>
      <section className="phone__section phone__section--hero">
        <h2 className="section-title">Now</h2>
        {now_task ? (
          <NowCard task={now_task} property={propsById.get(now_task.property_id)!} />
        ) : (
          <EmptyState glyph="✓" variant="celebrate">All done for now. Nice work.</EmptyState>
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
              <TaskListCard task={t} property={propsById.get(t.property_id)!} />
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
            {completed.map((t) => {
              const prop = propsById.get(t.property_id)!;
              return (
                <li key={t.id}>
                  <Link to={"/task/" + t.id} className="task-card task-card--compact task-card--done">
                    <div className="task-card__head">
                      <span className="task-card__when">✓ {hhmm(t.scheduled_start)}</span>
                      <Chip tone={prop.color} size="sm">{prop.name}</Chip>
                    </div>
                    <div className="task-card__title task-card__title--sm">{t.title}</div>
                  </Link>
                </li>
              );
            })}
          </ul>
        </details>
      </section>
    </>
  );
}

function NowCard({ task, property }: { task: Task; property: Property }) {
  const doneSteps = task.checklist.filter((i) => i.done).length;
  const total = task.checklist.length;
  const pct = total > 0 ? Math.round((doneSteps / total) * 100) : 0;
  return (
    <Link to={"/task/" + task.id} className="task-card task-card--now">
      <div className="task-card__head">
        <Chip tone={property.color}>{property.name}</Chip>
        {(task.priority === "high" || task.priority === "urgent") && (
          <Chip tone="rust">{cap(task.priority)} priority</Chip>
        )}
        {task.photo_evidence === "required" && (
          <Chip tone="sand" size="sm">📷 photo required</Chip>
        )}
        <span className="task-card__when">{hhmm(task.scheduled_start)} · {task.estimated_minutes} min</span>
      </div>
      <h3 className="task-card__title">{task.title}</h3>
      <div className="task-card__meta">{task.area}</div>
      {total > 0 && (
        <div className="task-card__progress">
          <ProgressBar value={pct} />
          <span className="progress-label">{doneSteps}/{total} steps</span>
        </div>
      )}
      <div className="task-card__cta">{ctaLabel(task)} →</div>
    </Link>
  );
}

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
