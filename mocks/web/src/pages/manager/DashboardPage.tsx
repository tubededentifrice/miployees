import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Avatar, Chip, Loading, Panel, StatCard } from "@/components/common";
import { fmtTime } from "@/lib/dates";
import {
  APPROVAL_RISK_TONE,
  ISSUE_SEVERITY_TONE,
  ISSUE_STATUS_TONE,
  TASK_STATUS_TONE,
} from "@/lib/tones";
import type { DashboardPayload as Dashboard, Me } from "@/types/api";

export default function DashboardPage() {
  const d = useQuery({ queryKey: qk.dashboard(), queryFn: () => fetchJson<Dashboard>("/api/v1/dashboard") });
  const me = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const qc = useQueryClient();

  const decideApproval = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      fetchJson("/api/v1/approvals/" + id + "/" + decision, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.dashboard() }),
  });
  const decideLeave = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "approve" | "reject" }) =>
      fetchJson("/api/v1/leaves/" + id + "/" + decision, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.dashboard() }),
  });

  if (d.isPending || me.isPending) return <DeskPage title="Dashboard"><Loading /></DeskPage>;
  if (!d.data || !me.data) return <DeskPage title="Dashboard">Failed to load.</DeskPage>;

  const {
    on_booking, by_status, pending_approvals, pending_leaves, open_issues, stays_today,
    properties, employees,
  } = d.data;
  const propsById = new Map(properties.map((p) => [p.id, p]));
  const empsById = new Map(employees.map((e) => [e.id, e]));
  const totalToday =
    by_status.completed.length + by_status.in_progress.length + by_status.pending.length;
  const firstName = me.data.manager_name.split(" ")[0];
  const todayLong = new Date(me.data.today).toLocaleDateString("en-GB", {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });

  return (
    <DeskPage
      title="Dashboard"
      sub={`Good morning, ${firstName} · ${todayLong} · ${properties.length} properties · 5 staff · ${totalToday} tasks today`}
      actions={<button className="btn btn--moss">+ New task</button>}
      overflow={[
        {
          label: "Broadcast message",
          onSelect: () => undefined,
        },
      ]}
    >
      <section className="grid grid--stats">
        <StatCard
          label="Tasks today"
          value={<>{by_status.completed.length}<span className="stat-card__divider">/</span>{totalToday}</>}
          sub="completed"
        />
        <StatCard label="Working now" value={on_booking.length} sub="of 5 staff" />
        <StatCard
          label="Approvals"
          value={pending_approvals.length}
          sub="agent actions awaiting review"
          warn={pending_approvals.length > 0}
        />
        <StatCard label="Stays in house" value={stays_today.length} sub={`across ${properties.length} properties`} />
      </section>

      <section className="grid grid--split">
        <Panel title="Today's tasks" right={<Link className="link" to="/properties">By property →</Link>}>
          <table className="table">
            <thead>
              <tr>
                <th>Time</th><th>Task</th><th>Property</th><th>Assignee</th><th>Status</th>
              </tr>
            </thead>
            <tbody>
              {[...by_status.in_progress, ...by_status.pending, ...by_status.completed].map((t) => {
                const prop = propsById.get(t.property_id);
                const emp = empsById.get(t.assignee_id);
                return (
                  <tr key={t.id}>
                    <td className="mono">{fmtTime(t.scheduled_start)}</td>
                    <td><strong>{t.title}</strong><div className="table__sub">{t.area}</div></td>
                    <td>{prop && <Chip tone={prop.color} size="sm">{prop.name}</Chip>}</td>
                    <td>
                      {emp && <><Avatar url={emp.avatar_url} initials={emp.avatar_initials} size="xs" alt={emp.name} /> {emp.name.split(" ")[0]}</>}
                    </td>
                    <td><Chip tone={TASK_STATUS_TONE[t.status]} size="sm">{t.status.replace("_", " ")}</Chip></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>

        <Panel title="Agent approvals" right={<Link className="link" to="/approvals">All →</Link>}>
          <ul className="approval-list">
            {pending_approvals.map((a) => (
              <li key={a.id} className={"approval approval--" + a.risk}>
                <div className="approval__head">
                  <Chip tone="ghost" size="sm">{a.agent}</Chip>
                  <Chip tone={APPROVAL_RISK_TONE[a.risk]} size="sm">{a.risk} risk</Chip>
                </div>
                <div className="approval__title"><strong>{a.action}</strong> · {a.target}</div>
                <div className="approval__reason">{a.reason}</div>
                <div className="approval__actions">
                  <button
                    className="btn btn--moss btn--sm"
                    type="button"
                    onClick={() => decideApproval.mutate({ id: a.id, decision: "approve" })}
                  >
                    Approve
                  </button>
                  <button
                    className="btn btn--ghost btn--sm"
                    type="button"
                    onClick={() => decideApproval.mutate({ id: a.id, decision: "reject" })}
                  >
                    Reject
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      </section>

      <section className="grid grid--split">
        <Panel title="Open issues" right={<span className="muted">{open_issues.length}</span>}>
          <ul className="issue-list">
            {open_issues.map((i) => {
              const reporter = empsById.get(i.reported_by);
              const prop = propsById.get(i.property_id);
              return (
                <li key={i.id} className="issue-row">
                  <div>
                    <strong>{i.title}</strong>
                    <div className="table__sub">
                      {reporter?.name.split(" ")[0]} · {prop?.name} · {i.area}
                    </div>
                  </div>
                  <Chip tone={ISSUE_SEVERITY_TONE[i.severity]} size="sm">{i.severity}</Chip>
                  <Chip tone={ISSUE_STATUS_TONE[i.status]} size="sm">{i.status.replace("_", " ")}</Chip>
                </li>
              );
            })}
          </ul>
        </Panel>

        <Panel title="Pending leaves" right={<Link className="link" to="/leaves">All →</Link>}>
          <ul className="task-list task-list--desk">
            {pending_leaves.length === 0 && (
              <li className="empty-state empty-state--quiet">No pending leave requests.</li>
            )}
            {pending_leaves.map((lv) => {
              const emp = empsById.get(lv.employee_id);
              const range =
                new Date(lv.starts_on).toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) +
                " → " +
                new Date(lv.ends_on).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
              return (
                <li key={lv.id} className="task-row">
                  <span className="task-row__time mono">{range}</span>
                  <span className="task-row__title">
                    <strong>{emp?.name}</strong>
                    <span className="task-row__area">{lv.category} · {lv.note}</span>
                  </span>
                  <span>
                    <button
                      className="btn btn--sm btn--moss"
                      type="button"
                      onClick={() => decideLeave.mutate({ id: lv.id, decision: "approve" })}
                    >
                      Approve
                    </button>{" "}
                    <button
                      className="btn btn--sm btn--ghost"
                      type="button"
                      onClick={() => decideLeave.mutate({ id: lv.id, decision: "reject" })}
                    >
                      Reject
                    </button>
                  </span>
                </li>
              );
            })}
          </ul>
        </Panel>
      </section>
    </DeskPage>
  );
}
