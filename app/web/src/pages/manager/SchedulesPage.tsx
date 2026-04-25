import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Avatar, Chip, Loading } from "@/components/common";
import type { Employee, Property, Schedule, TaskTemplate } from "@/types/api";

interface SchedulesPayload {
  schedules: Schedule[];
  templates_by_id: Record<string, TaskTemplate>;
}

function fmtSince(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { month: "short", year: "numeric" });
}

export default function SchedulesPage() {
  const schedQ = useQuery({
    queryKey: qk.schedules(),
    queryFn: () => fetchJson<SchedulesPayload>("/api/v1/schedules"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });

  const sub = "\"Create a task from this template on these dates at these times.\" RRULE-backed, timezone-aware.";
  const actions = <button className="btn btn--moss">+ New schedule</button>;

  if (schedQ.isPending || propsQ.isPending || empsQ.isPending) {
    return <DeskPage title="Schedules" sub={sub} actions={actions}><Loading /></DeskPage>;
  }
  if (!schedQ.data || !propsQ.data || !empsQ.data) {
    return <DeskPage title="Schedules" sub={sub} actions={actions}>Failed to load.</DeskPage>;
  }

  const propsById = new Map(propsQ.data.map((p) => [p.id, p]));
  const empsById = new Map(empsQ.data.map((e) => [e.id, e]));
  const { schedules, templates_by_id } = schedQ.data;

  return (
    <DeskPage title="Schedules" sub={sub} actions={actions}>
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Name</th><th>Template</th><th>Property</th><th>Recurrence</th>
              <th>Default assignee</th><th>Duration</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {schedules.map((s) => {
              const p = propsById.get(s.property_id);
              const tpl = templates_by_id[s.template_id];
              const emp = s.default_assignee_id ? empsById.get(s.default_assignee_id) : undefined;
              return (
                <tr key={s.id}>
                  <td>
                    <strong>{s.name}</strong>
                    <div className="table__sub">since {fmtSince(s.active_from)}</div>
                  </td>
                  <td>{tpl?.name ?? "—"}</td>
                  <td>{p && <Chip tone={p.color} size="sm">{p.name}</Chip>}</td>
                  <td className="table__sub">{s.rrule_human}</td>
                  <td>
                    {emp ? (
                      <><Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} /> {emp.name.split(" ")[0]}</>
                    ) : "—"}
                  </td>
                  <td className="mono">{s.duration_minutes} min</td>
                  <td>
                    <Chip tone={s.paused ? "sand" : "moss"} size="sm">
                      {s.paused ? "Paused" : "Active"}
                    </Chip>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Preview — next 7 days</h2></header>
        <ul className="task-list task-list--desk">
          {schedules.filter((s) => !s.paused).map((s) => {
            const p = propsById.get(s.property_id);
            return (
              <li key={s.id} className="task-row">
                <span className="task-row__time mono">{s.rrule_human}</span>
                <span className="task-row__title"><strong>{s.name}</strong></span>
                {p && <Chip tone={p.color} size="sm">{p.name}</Chip>}
                <Chip tone="ghost" size="sm">{s.duration_minutes}m</Chip>
              </li>
            );
          })}
        </ul>
      </div>
    </DeskPage>
  );
}
