import { useQuery } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { ACTOR_KIND_TONE, GRANT_ROLE_TONE } from "@/lib/tones";
import type { AuditEntry } from "@/types/api";

function hms(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}
function dayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

export default function AuditPage() {
  const q = useQuery({
    queryKey: qk.audit(),
    queryFn: () => fetchJson<AuditEntry[]>("/api/v1/audit"),
  });

  const sub = "Append-only. Every mutation by a user (on the manager/worker/client surface), an agent, or the system. Actions taken by a member of the owners permission group carry a governance badge.";
  const overflow = [{ label: "Export JSONL", onSelect: () => undefined }];

  if (q.isPending) return <DeskPage title="Audit log" sub={sub} overflow={overflow}><Loading /></DeskPage>;
  if (!q.data) return <DeskPage title="Audit log" sub={sub} overflow={overflow}>Failed to load.</DeskPage>;

  const entries = q.data;
  const countBy = (kind: AuditEntry["actor_kind"]): number =>
    entries.filter((e) => e.actor_kind === kind).length;
  const countByGrant = (role: NonNullable<AuditEntry["actor_grant_role"]>): number =>
    entries.filter((e) => e.actor_grant_role === role).length;
  const governanceCount = entries.filter((e) => e.actor_was_owner_member).length;

  return (
    <DeskPage title="Audit log" sub={sub} overflow={overflow}>
      <section className="panel">
        <div className="desk-filters">
          <span className="chip chip--ghost chip--sm chip--active">All</span>
          <span className="chip chip--ghost chip--sm">User · {countBy("user")}</span>
          <span className="chip chip--ghost chip--sm">Agent · {countBy("agent")}</span>
          <span className="chip chip--ghost chip--sm">System · {countBy("system")}</span>
          <span className="chip chip--ghost chip--sm">Manager · {countByGrant("manager")}</span>
          <span className="chip chip--ghost chip--sm">Worker · {countByGrant("worker")}</span>
          <span className="chip chip--ghost chip--sm">Client · {countByGrant("client")}</span>
          <span className="chip chip--ghost chip--sm">Governance · {governanceCount}</span>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Via</th><th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e, idx) => (
              <tr key={idx}>
                <td className="mono">
                  {hms(e.at)}
                  <div className="table__sub">{dayMon(e.at)}</div>
                </td>
                <td>
                  <Chip tone={ACTOR_KIND_TONE[e.actor_kind]} size="sm">{e.actor_kind}</Chip>{" "}
                  {e.actor_grant_role ? (
                    <>
                      <Chip tone={GRANT_ROLE_TONE[e.actor_grant_role]} size="sm">{e.actor_grant_role}</Chip>{" "}
                    </>
                  ) : null}
                  {e.actor_was_owner_member ? (
                    <>
                      <Chip tone="moss" size="sm">owners</Chip>{" "}
                    </>
                  ) : null}
                  {e.actor}
                </td>
                <td className="mono">{e.action}</td>
                <td className="mono muted">{e.target}</td>
                <td><Chip tone="ghost" size="sm">{e.via}</Chip></td>
                <td className="table__sub">{e.reason ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </DeskPage>
  );
}
