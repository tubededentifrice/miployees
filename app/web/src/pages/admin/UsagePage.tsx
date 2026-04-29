import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading, ProgressBar, StatCard } from "@/components/common";
import type {
  AdminUsageSummary,
  AdminUsageWorkspacesResponse,
  AdminWorkspacesResponse,
} from "@/types/api";

interface UsageCapResponse {
  workspace_id: string;
  cap_cents_30d: number;
}

function dollarsToCents(value: string): number | null {
  if (value.trim() === "") return null;
  const dollars = Number(value);
  if (!Number.isFinite(dollars) || dollars < 0) return null;
  return Math.round(dollars * 100);
}

function centsToDollars(value: number): string {
  return (value / 100).toFixed(2);
}

function usagePercent(spentCents: number, capCents: number): number {
  if (capCents <= 0) return 100;
  return Math.min(100, Math.floor((spentCents * 100) / capCents));
}

export default function AdminUsagePage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [draftCap, setDraftCap] = useState<string>("");

  const summaryQ = useQuery({
    queryKey: qk.adminUsageSummary(),
    queryFn: () => fetchJson<AdminUsageSummary>("/admin/api/v1/usage/summary"),
  });
  const rowsQ = useQuery({
    queryKey: qk.adminUsageWorkspaces(),
    queryFn: () =>
      fetchJson<AdminUsageWorkspacesResponse>("/admin/api/v1/usage/workspaces"),
  });
  const workspaceMetaQ = useQuery({
    queryKey: qk.adminWorkspaces(),
    queryFn: () => fetchJson<AdminWorkspacesResponse>("/admin/api/v1/workspaces"),
  });

  const setCap = useMutation({
    mutationFn: ({ id, capCents }: { id: string; capCents: number }) =>
      fetchJson<UsageCapResponse>(`/admin/api/v1/usage/workspaces/${id}/cap`, {
        method: "PUT",
        body: { cap_cents_30d: capCents },
      }),
    onMutate: async ({ id, capCents }) => {
      await qc.cancelQueries({ queryKey: qk.adminUsageWorkspaces() });
      const previous = qc.getQueryData<AdminUsageWorkspacesResponse>(
        qk.adminUsageWorkspaces(),
      );
      qc.setQueryData<AdminUsageWorkspacesResponse>(
        qk.adminUsageWorkspaces(),
        (current) => {
          if (!current) return current;
          return {
            workspaces: current.workspaces.map((w) =>
              w.workspace_id === id
                ? {
                    ...w,
                    cap_cents_30d: capCents,
                    percent: usagePercent(w.spent_cents_30d, capCents),
                    paused: capCents === 0 || w.spent_cents_30d >= capCents,
                  }
                : w,
            ),
          };
        },
      );
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        qc.setQueryData(qk.adminUsageWorkspaces(), context.previous);
      }
    },
    onSuccess: () => {
      setEditing(null);
      setDraftCap("");
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.adminUsageWorkspaces() });
      qc.invalidateQueries({ queryKey: qk.adminUsageSummary() });
      qc.invalidateQueries({ queryKey: qk.adminWorkspaces() });
    },
  });

  const sub =
    "Rolling-30-day LLM spend per workspace. Adjust a workspace's cap to raise or tighten its envelope.";

  if (summaryQ.isPending || rowsQ.isPending || workspaceMetaQ.isPending) {
    return <DeskPage title="Usage" sub={sub}><Loading /></DeskPage>;
  }
  if (!summaryQ.data || !rowsQ.data || !workspaceMetaQ.data) {
    return <DeskPage title="Usage" sub={sub}>Failed to load.</DeskPage>;
  }

  const sum = summaryQ.data;
  const rows = rowsQ.data.workspaces;
  const workspaceMeta = new Map(
    workspaceMetaQ.data.workspaces.map((w) => [w.id, w]),
  );
  const topCapability = sum.per_capability[0];
  const capCents = editing ? dollarsToCents(draftCap) : null;

  return (
    <DeskPage title="Usage" sub={sub}>
      <section className="grid grid--stats">
        <StatCard
          label="30d spend"
          value={formatMoney(sum.deployment_spend_cents_30d, "USD")}
          sub={sum.window_label}
        />
        <StatCard
          label="Workspaces"
          value={sum.workspace_count}
          sub={sum.paused_workspace_count + " paused"}
          warn={sum.paused_workspace_count > 0}
        />
        <StatCard
          label="Calls (30d)"
          value={sum.deployment_calls_30d.toLocaleString()}
        />
        <StatCard
          label="Top capability"
          value={topCapability?.capability ?? "—"}
          sub={
            topCapability
              ? formatMoney(topCapability.spend_cents_30d, "USD")
              : undefined
          }
        />
      </section>

      <div className="panel">
        <header className="panel__head"><h2>Per workspace</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Workspace</th>
              <th>Plan</th>
              <th>Verification</th>
              <th>30d spend</th>
              <th>Cap</th>
              <th>Usage</th>
              <th>State</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((w) => {
              const meta = workspaceMeta.get(w.workspace_id);
              const plan = meta?.plan ?? "free";
              return (
                <tr key={w.workspace_id}>
                  <td>
                    {meta?.name ?? w.name}
                    <div className="table__sub">{meta?.slug ?? w.slug}</div>
                  </td>
                  <td>
                    <Chip tone={plan === "free" ? "ghost" : "sky"} size="sm">
                      {plan}
                    </Chip>
                  </td>
                  <td className="muted">{meta?.verification_state ?? "—"}</td>
                  <td className="mono">
                    {formatMoney(w.spent_cents_30d, "USD")}
                  </td>
                  <td>
                    {editing === w.workspace_id ? (
                      <input
                        className="input input--inline"
                        type="number"
                        step="0.01"
                        min="0"
                        max="10000"
                        value={draftCap}
                        onChange={(e) => setDraftCap(e.target.value)}
                        autoFocus
                      />
                    ) : (
                      <span className="mono">
                        {formatMoney(w.cap_cents_30d, "USD")}
                      </span>
                    )}
                  </td>
                  <td>
                    <ProgressBar value={w.percent} slim />
                    <span className="muted"> {w.percent}%</span>
                  </td>
                  <td>
                    {w.paused
                      ? <Chip tone="rust" size="sm">paused</Chip>
                      : <Chip tone="moss" size="sm">active</Chip>}
                  </td>
                  <td>
                    {editing === w.workspace_id ? (
                      <div className="inline-actions">
                        <button
                          type="button"
                          className="btn btn--moss btn--sm"
                          disabled={setCap.isPending || capCents === null}
                          onClick={() => {
                            if (capCents !== null) {
                              setCap.mutate({ id: w.workspace_id, capCents });
                            }
                          }}
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          onClick={() => {
                            setEditing(null);
                            setDraftCap("");
                          }}
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        className="btn btn--ghost btn--sm"
                        onClick={() => {
                          setEditing(w.workspace_id);
                          setDraftCap(centsToDollars(w.cap_cents_30d));
                        }}
                      >
                        Edit cap
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Per capability (30d)</h2></header>
        <table className="table">
          <thead>
            <tr>
              <th>Capability</th><th>Calls</th><th>Spend</th>
            </tr>
          </thead>
          <tbody>
            {sum.per_capability
              .slice()
              .sort((a, b) => b.spend_cents_30d - a.spend_cents_30d)
              .map((c) => (
                <tr key={c.capability}>
                  <td><code className="inline-code">{c.capability}</code></td>
                  <td className="mono">{c.calls_30d.toLocaleString()}</td>
                  <td className="mono">
                    {formatMoney(c.spend_cents_30d, "USD")}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
