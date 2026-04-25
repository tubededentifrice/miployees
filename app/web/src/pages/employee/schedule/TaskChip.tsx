// Task chip rendered inside a `DayTimeline` block (`/schedule`,
// §14 "Schedule view"). A chip is a positioned <Link> pointing at
// `/task/{id}`, styled by status modifier. Chips can render either
// inside a property-tinted block (a booking or rota slot) or as
// orphans pinned to the rota column when no parent block covers
// their clock time.
//
// Kept in its own file so the chip styling + a11y attributes (which
// are touched on every CSS change) don't bloat the timeline render.

import { Link } from "react-router-dom";
import type { SchedulerTaskView } from "@/types/api";
import { timeOfTask } from "./lib/dateHelpers";

export function TaskChipLink({
  task,
  tint,
  solid,
  idx,
  orphan,
  standalone,
  top,
}: {
  task: SchedulerTaskView;
  tint: string;
  solid: string;
  idx: number;
  orphan?: boolean;
  /** Orphan tasks live in the rota column (no parent block). The
   *  --standalone modifier keeps them visually distinct and sets the
   *  background against the column, not the tinted block. */
  standalone?: boolean;
  /** Pixels down from the positioned ancestor (block for in-block
   *  chips, rota column for orphans). Computed from the task's
   *  clock-time start so the chip sits on the right hour. */
  top: number;
}) {
  return (
    <Link
      to={"/task/" + task.id}
      className={
        `schedule-day__chip schedule-day__chip--${task.status}`
        + (orphan ? " schedule-day__chip--orphan" : "")
        + (standalone ? " schedule-day__chip--standalone" : "")
      }
      style={{
        top: `${top}px`,
        "--rota-tint": tint,
        "--rota-tint-solid": solid,
        "--rota-idx": idx,
      } as React.CSSProperties}
      data-property={task.property_id}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      <span className="schedule-day__chip-time">{timeOfTask(task.scheduled_start)}</span>
      <span className="schedule-day__chip-title">{task.title}</span>
    </Link>
  );
}
