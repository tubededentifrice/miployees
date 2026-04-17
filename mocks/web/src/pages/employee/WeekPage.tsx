import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { EmptyState, Loading } from "@/components/common";
import TaskListCard from "@/components/TaskListCard";
import type { Property, Task } from "@/types/api";

interface WeekPayload {
  tasks: Task[];
  properties: Property[];
}

export default function WeekPage() {
  const q = useQuery({
    queryKey: qk.week(),
    queryFn: () => fetchJson<WeekPayload>("/api/v1/week"),
  });

  if (q.isPending) return <section className="phone__section"><Loading /></section>;
  if (q.isError || !q.data) {
    return <section className="phone__section"><EmptyState>Failed to load.</EmptyState></section>;
  }

  const { tasks, properties } = q.data;
  const propsById = new Map(properties.map((p) => [p.id, p]));

  return (
    <section className="phone__section">
      <h2 className="section-title">This week</h2>
      <ul className="task-list">
        {tasks.map((t) => {
          const prop = propsById.get(t.property_id);
          if (!prop) return null;
          return (
            <li key={t.id}>
              <TaskListCard task={t} property={prop} showWeekday showStatus />
            </li>
          );
        })}
      </ul>
    </section>
  );
}
