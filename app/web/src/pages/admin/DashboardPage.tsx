import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading, ProgressBar, StatCard } from "@/components/common";
import { displayAuditRow } from "@/pages/admin/auditRows";
import type {
  AdminAuditListResponse,
  AdminUsageSummary,
  AdminUsageWorkspacesResponse,
  AdminWorkspacesResponse,
} from "@/types/api";

export default function AdminDashboardPage() {
  const summaryQ = useQuery({
    queryKey: qk.adminUsageSummary(),
    queryFn: () => fetchJson<AdminUsageSummary>("/admin/api/v1/usage/summary"),
  });
  const workspacesQ = useQuery({
    queryKey: qk.adminUsageWorkspaces(),
    queryFn: () =>
      fetchJson<AdminUsageWorkspacesResponse>("/admin/api/v1/usage/workspaces"),
  });
  const workspaceMetaQ = useQuery({
    queryKey: qk.adminWorkspaces(),
    queryFn: () => fetchJson<AdminWorkspacesResponse>("/admin/api/v1/workspaces"),
  });
  const auditQ = useQuery({
    queryKey: qk.adminAudit(),
    queryFn: () => fetchJson<AdminAuditListResponse>("/admin/api/v1/audit"),
  });

  const sub =
    "Deployment-wide health: spend, workspaces that need attention, and what changed recently.";

  if (
    summaryQ.isPending ||
    workspacesQ.isPending ||
    workspaceMetaQ.isPending ||
    auditQ.isPending
  ) {
    return <DeskPage title="Administration" sub={sub}><Loading /></DeskPage>;
  }
  if (
    !summaryQ.data ||
    !workspacesQ.data ||
    !workspaceMetaQ.data ||
    !auditQ.data
  ) {
    return <DeskPage title="Administration" sub={sub}>Failed to load.</DeskPage>;
  }

  const sum = summaryQ.data;
  const workspaceMeta = new Map(
    workspaceMetaQ.data.workspaces.map((w) => [w.id, w]),
  );
  const workspaces = workspacesQ.data.workspaces.filter((w) => {
    const meta = workspaceMeta.get(w.workspace_id);
    return meta?.archived_at == null;
  });
  const audit = auditQ.data.data.slice(0, 6).map(displayAuditRow);

  const paused = workspaces.filter((w) => w.paused);
  const stressed = workspaces
    .filter((w) => !w.paused && w.percent >= 70)
    .sort((a, b) => b.percent - a.percent);

  return (
    <DeskPage title="Administration" sub={sub}>
      <section className="grid grid--stats">
        <StatCard
          label="30d LLM spend"
          value={formatMoney(sum.deployment_spend_cents_30d, "USD")}
          sub={sum.window_label}
        />
        <StatCard
          label="Workspaces"
          value={sum.workspace_count}
          sub={paused.length > 0 ? paused.length + " paused" : "all healthy"}
          warn={paused.length > 0}
        />
        <StatCard
          label="Calls (30d)"
          value={sum.deployment_calls_30d.toLocaleString()}
          sub={"across " + sum.per_capability.length + " capabilities"}
        />
        <StatCard
          label="Default model"
          value="gemma-4-31b-it"
          sub="via OpenRouter"
        />
      </section>

      {(paused.length > 0 || stressed.length > 0) && (
        <div className="panel">
          <header className="panel__head">
            <h2>Workspaces to watch</h2>
            <Link className="btn btn--ghost" to="/admin/usage">Open Usage</Link>
          </header>
          <table className="table">
            <thead>
              <tr>
                <th>Workspace</th>
                <th>State</th>
                <th>30d spend</th>
                <th>Cap</th>
                <th>Usage</th>
              </tr>
            </thead>
            <tbody>
              {[...paused, ...stressed].map((w) => (
                <tr key={w.workspace_id}>
                  <td>
                    <Link to={"/admin/workspaces"} className="table__link">
                      {w.name}
                    </Link>
                    <div className="table__sub">{w.slug}</div>
                  </td>
                  <td>
                    {w.paused
                      ? <Chip tone="rust" size="sm">paused</Chip>
                      : <Chip tone="sand" size="sm">{w.percent}%</Chip>}
                  </td>
                  <td className="mono">{formatMoney(w.spent_cents_30d, "USD")}</td>
                  <td className="mono">{formatMoney(w.cap_cents_30d, "USD")}</td>
                  <td>
                    <ProgressBar value={w.percent} slim />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="panel">
        <header className="panel__head">
          <h2>Recent deployment audit</h2>
          <Link className="btn btn--ghost" to="/admin/audit">Open Audit log</Link>
        </header>
        <table className="table">
          <thead>
            <tr>
              <th>When</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Target</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {audit.map((row, idx) => (
              <tr key={idx}>
                <td className="mono">{new Date(row.at).toLocaleString()}</td>
                <td>{row.actor}</td>
                <td><code className="inline-code">{row.action}</code></td>
                <td className="mono">{row.target}</td>
                <td className="muted">{row.reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
