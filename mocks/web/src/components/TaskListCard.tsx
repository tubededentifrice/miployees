import { Link } from "react-router-dom";
import { Chip, Dot } from "@/components/common";
import type { Property, Task } from "@/types/api";

// Compact split card used by the Today and Week task lists. Left side
// carries the title + meta (area · min [· status]); right side carries
// the scheduled time, property chip, and priority/photo dots.
export default function TaskListCard({
  task,
  property,
  showWeekday = false,
  showStatus = false,
}: {
  task: Task;
  property: Property | null;
  showWeekday?: boolean;
  showStatus?: boolean;
}) {
  const when = formatWhen(task.scheduled_start, showWeekday);
  const metaBase = task.area
    ? `${task.area} · ${task.estimated_minutes} min`
    : `${task.estimated_minutes} min`;
  const meta = showStatus ? `${metaBase} · ${task.status}` : metaBase;
  const cls =
    "task-card task-card--compact task-card--split" +
    (task.status === "completed" ? " task-card--done" : "") +
    (task.is_personal ? " task-card--personal" : "");

  return (
    <Link to={"/task/" + task.id} className={cls}>
      <div className="task-card__main">
        <div className="task-card__title task-card__title--sm">{task.title}</div>
        <div className="task-card__meta">{meta}</div>
      </div>
      <div className="task-card__aside">
        <span className="task-card__when">{when}</span>
        {property ? (
          <Chip tone={property.color} size="sm">{property.name}</Chip>
        ) : task.is_personal ? (
          <Chip tone="ghost" size="sm">Personal</Chip>
        ) : null}
        {(task.priority === "high" || task.priority === "urgent") && <Dot tone="rust" />}
        {task.photo_evidence === "required" && <Dot tone="sand" />}
      </div>
    </Link>
  );
}

function formatWhen(iso: string, withWeekday: boolean): string {
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (!withWeekday) return time;
  const day = d.toLocaleDateString([], { weekday: "short" });
  return day + " " + time;
}
