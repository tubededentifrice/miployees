import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import type { AdminWorkspaceRow, AdminWorkspacesResponse } from "@/types/api";

const VERIFICATION_TONE: Record<
  AdminWorkspaceRow["verification_state"],
  "moss" | "sky" | "sand" | "ghost"
> = {
  trusted: "moss",
  human_verified: "sky",
  email_verified: "sand",
  unverified: "ghost",
  archived: "ghost",
};

interface TrustResponse {
  id: string;
  verification_state: AdminWorkspaceRow["verification_state"];
}

interface ArchiveResponse {
  id: string;
  archived_at: string;
}

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

function updateWorkspace(
  current: AdminWorkspacesResponse | undefined,
  id: string,
  update: (workspace: AdminWorkspaceRow) => AdminWorkspaceRow,
): AdminWorkspacesResponse | undefined {
  if (!current) return current;
  return {
    workspaces: current.workspaces.map((workspace) =>
      workspace.id === id ? update(workspace) : workspace,
    ),
  };
}

export default function AdminWorkspacesPage() {
  const qc = useQueryClient();
  const [editingCap, setEditingCap] = useState<string | null>(null);
  const [draftCap, setDraftCap] = useState("");

  const wsQ = useQuery({
    queryKey: qk.adminWorkspaces(),
    queryFn: () => fetchJson<AdminWorkspacesResponse>("/admin/api/v1/workspaces"),
  });

  const trust = useMutation({
    mutationFn: (id: string) =>
      fetchJson<TrustResponse>(`/admin/api/v1/workspaces/${id}/trust`, {
        method: "POST",
      }),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: qk.adminWorkspaces() });
      const previous = qc.getQueryData<AdminWorkspacesResponse>(
        qk.adminWorkspaces(),
      );
      qc.setQueryData<AdminWorkspacesResponse>(qk.adminWorkspaces(), (current) =>
        updateWorkspace(current, id, (workspace) => ({
          ...workspace,
          verification_state: "trusted",
        })),
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      if (context?.previous) {
        qc.setQueryData(qk.adminWorkspaces(), context.previous);
      }
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.adminWorkspaces() });
    },
  });

  const archive = useMutation({
    mutationFn: (id: string) =>
      fetchJson<ArchiveResponse>(`/admin/api/v1/workspaces/${id}/archive`, {
        method: "POST",
      }),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: qk.adminWorkspaces() });
      const previous = qc.getQueryData<AdminWorkspacesResponse>(
        qk.adminWorkspaces(),
      );
      const archivedAt = new Date().toISOString();
      qc.setQueryData<AdminWorkspacesResponse>(qk.adminWorkspaces(), (current) =>
        updateWorkspace(current, id, (workspace) => ({
          ...workspace,
          archived_at: archivedAt,
        })),
      );
      return { previous };
    },
    onError: (_err, _id, context) => {
      if (context?.previous) {
        qc.setQueryData(qk.adminWorkspaces(), context.previous);
      }
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.adminWorkspaces() });
    },
  });

  const setCap = useMutation({
    mutationFn: ({ id, capCents }: { id: string; capCents: number }) =>
      fetchJson<UsageCapResponse>(`/admin/api/v1/usage/workspaces/${id}/cap`, {
        method: "PUT",
        body: { cap_cents_30d: capCents },
      }),
    onMutate: async ({ id, capCents }) => {
      await qc.cancelQueries({ queryKey: qk.adminWorkspaces() });
      const previous = qc.getQueryData<AdminWorkspacesResponse>(
        qk.adminWorkspaces(),
      );
      qc.setQueryData<AdminWorkspacesResponse>(qk.adminWorkspaces(), (current) =>
        updateWorkspace(current, id, (workspace) => ({
          ...workspace,
          cap_cents_30d: capCents,
        })),
      );
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        qc.setQueryData(qk.adminWorkspaces(), context.previous);
      }
    },
    onSuccess: () => {
      setEditingCap(null);
      setDraftCap("");
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.adminWorkspaces() });
      qc.invalidateQueries({ queryKey: qk.adminUsageWorkspaces() });
      qc.invalidateQueries({ queryKey: qk.adminUsageSummary() });
    },
  });

  const sub =
    "Every workspace on this deployment. Promote verification, archive on owner request, or drill into usage.";

  if (wsQ.isPending) return <DeskPage title="Workspaces" sub={sub}><Loading /></DeskPage>;
  if (!wsQ.data) return <DeskPage title="Workspaces" sub={sub}>Failed to load.</DeskPage>;

  const active = wsQ.data.workspaces.filter((w) => !w.archived_at);
  const archived = wsQ.data.workspaces.filter((w) => w.archived_at);
  const capCents = editingCap ? dollarsToCents(draftCap) : null;

  return (
    <DeskPage title="Workspaces" sub={sub}>
      <div className="panel">
        <header className="panel__head"><h2>Active ({active.length})</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Workspace</th>
              <th>Plan</th>
              <th>Verification</th>
              <th>Properties</th>
              <th>Members</th>
              <th>30d spend</th>
              <th>Created</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {active.map((w) => (
              <tr key={w.id}>
                <td>
                  {w.name}
                  <div className="table__sub">/w/{w.slug}</div>
                </td>
                <td><Chip tone={w.plan === "free" ? "ghost" : "sky"} size="sm">{w.plan}</Chip></td>
                <td>
                  <Chip tone={VERIFICATION_TONE[w.verification_state]} size="sm">
                    {w.verification_state}
                  </Chip>
                </td>
                <td className="mono">{w.properties_count}</td>
                <td className="mono">{w.members_count}</td>
                <td className="mono">
                  {formatMoney(w.spent_cents_30d, "USD")}
                  <span className="muted"> / </span>
                  {editingCap === w.id ? (
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
                    <span className="muted">
                      {formatMoney(w.cap_cents_30d, "USD")}
                    </span>
                  )}
                </td>
                <td className="mono muted">{w.created_at}</td>
                <td>
                  <div className="inline-actions">
                    {w.verification_state !== "trusted" && (
                      <button
                        type="button"
                        className="btn btn--ghost btn--sm"
                        disabled={trust.isPending}
                        onClick={() => trust.mutate(w.id)}
                      >
                        Trust
                      </button>
                    )}
                    {editingCap === w.id ? (
                      <>
                        <button
                          type="button"
                          className="btn btn--moss btn--sm"
                          disabled={setCap.isPending || capCents === null}
                          onClick={() => {
                            if (capCents !== null) {
                              setCap.mutate({ id: w.id, capCents });
                            }
                          }}
                        >
                          Save
                        </button>
                        <button
                          type="button"
                          className="btn btn--ghost btn--sm"
                          onClick={() => {
                            setEditingCap(null);
                            setDraftCap("");
                          }}
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        className="btn btn--ghost btn--sm"
                        onClick={() => {
                          setEditingCap(w.id);
                          setDraftCap(centsToDollars(w.cap_cents_30d));
                        }}
                      >
                        Edit cap
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn btn--rust btn--sm"
                      disabled={archive.isPending}
                      onClick={() => {
                        if (confirm(`Archive ${w.name}? Owner can restore from backup.`)) {
                          archive.mutate(w.id);
                        }
                      }}
                    >
                      Archive
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {archived.length > 0 && (
        <div className="panel">
          <header className="panel__head"><h2>Archived ({archived.length})</h2></header>
          <table className="table">
            <thead>
              <tr>
                <th>Workspace</th>
                <th>Plan</th>
                <th>Archived on</th>
              </tr>
            </thead>
            <tbody>
              {archived.map((w) => (
                <tr key={w.id}>
                  <td>
                    {w.name}
                    <div className="table__sub">/w/{w.slug}</div>
                  </td>
                  <td className="muted">{w.plan}</td>
                  <td className="mono muted">{w.archived_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </DeskPage>
  );
}
