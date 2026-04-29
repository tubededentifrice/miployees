import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { displayAuditRow } from "@/pages/admin/auditRows";
import type { AdminAuditListResponse, AuditEntry } from "@/types/api";

const ACTOR_TONE: Record<AuditEntry["actor_kind"], "moss" | "sky" | "ghost"> = {
  user: "moss",
  agent: "sky",
  system: "ghost",
};

export default function AdminAuditPage() {
  const q = useQuery({
    queryKey: qk.adminAudit(),
    queryFn: () => fetchJson<AdminAuditListResponse>("/admin/api/v1/audit"),
  });
  const sub =
    "Deployment-scope audit — scope_kind='deployment' rows only. Each action ties back to its admin actor via actor_id.";
  if (q.isPending) return <DeskPage title="Audit log" sub={sub}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Audit log" sub={sub}>Failed to load.</DeskPage>;
  const rows = q.data.data.map(displayAuditRow);

  return (
    <DeskPage title="Audit log" sub={sub}>
      <div className="panel">
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>When</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Target</th>
              <th>Via</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr key={idx}>
                <td className="mono">{new Date(row.at).toLocaleString()}</td>
                <td>
                  <Chip tone={ACTOR_TONE[row.actor_kind]} size="sm">{row.actor_kind}</Chip>{" "}
                  {row.actor}
                  {row.actor_was_owner_member ? <span className="muted"> · owner</span> : null}
                </td>
                <td>
                  <code className="inline-code">{row.action}</code>
                  {row.actor_action_key && (
                    <div className="table__sub">via {row.actor_action_key}</div>
                  )}
                </td>
                <td className="mono">{row.target}</td>
                <td className="muted">{row.via}</td>
                <td className="muted">{row.reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
