import { useParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { Chip, Loading } from "@/components/common";
import { AssetIcon } from "@/components/AssetIcon";
import PageHeader from "@/components/PageHeader";
import type { AssetDetailPayload } from "@/types/api";

function fmtDate(iso: string | null): string {
  if (!iso) return "\u2014";
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

export default function EmployeeAssetPage() {
  const { aid = "" } = useParams<{ aid: string }>();
  const qc = useQueryClient();

  const q = useQuery({
    queryKey: qk.asset(aid),
    queryFn: () => fetchJson<AssetDetailPayload>("/api/v1/assets/" + aid),
    enabled: aid !== "",
  });

  const markDone = useMutation({
    mutationFn: (actionId: string) =>
      fetchJson("/api/v1/assets/" + aid + "/actions/" + actionId + "/complete", { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.asset(aid) });
    },
  });

  if (q.isPending) return (
    <>
      <PageHeader title="Asset" />
      <section className="phone__section"><Loading /></section>
    </>
  );
  if (!q.data) return (
    <>
      <PageHeader title="Asset" />
      <section className="phone__section">Failed to load asset.</section>
    </>
  );

  const { asset, asset_type, property, actions, documents } = q.data;
  const manuals = documents.filter((d) => d.kind === "manual");

  return (
    <>
      <PageHeader title={asset.name} />
      <section className="phone__section">
        <div className="task-detail__head">
          <div className="task-detail__chips">
            <Chip tone={property.color} size="sm">{property.name}</Chip>
            {asset.area && <Chip tone="ghost" size="sm">{asset.area}</Chip>}
            {asset_type && (
              <Chip tone="ghost" size="sm">
                <AssetIcon name={asset_type.icon_name} size={14} />
                {asset_type.name}
              </Chip>
            )}
          </div>
          <p className="task-detail__meta">
            {[asset.make, asset.model].filter(Boolean).join(" ") || "No make/model"}
          </p>
        </div>
      </section>

      {asset.guest_instructions && (
        <section className="phone__section">
          <h2 className="section-title">Guest instructions</h2>
          <p>{asset.guest_instructions}</p>
        </section>
      )}

      {actions.length > 0 && (
        <section className="phone__section">
          <h2 className="section-title">Maintenance actions</h2>
          {actions.map((a) => {
            const overdue = a.next_due_on && a.next_due_on < "2026-04-15";
            const dueSoon = a.next_due_on && !overdue && a.next_due_on <= "2026-04-30";
            return (
              <div key={a.id} className="action-row">
                <span className="action-row__label">{a.label}</span>
                <span>
                  {overdue ? (
                    <Chip tone="rust" size="sm">overdue</Chip>
                  ) : dueSoon ? (
                    <Chip tone="sand" size="sm">{fmtDate(a.next_due_on)}</Chip>
                  ) : (
                    <Chip tone="moss" size="sm">{fmtDate(a.next_due_on)}</Chip>
                  )}
                </span>
                <span className="action-row__interval">
                  {a.interval_days ? `${a.interval_days}d` : "\u2014"}
                </span>
                <button
                  className="btn btn--sm btn--moss"
                  onClick={() => markDone.mutate(a.id)}
                  disabled={markDone.isPending}
                >
                  Done
                </button>
              </div>
            );
          })}
        </section>
      )}

      {manuals.length > 0 && (
        <section className="phone__section">
          <h2 className="section-title">Manuals</h2>
          {manuals.map((d) => (
            <div key={d.id} className="doc-row">
              <span className="doc-thumb">&#x1F4D6;</span>
              <span>{d.title}</span>
              <span className="muted">{d.size_kb} KB</span>
              <span></span>
              <span></span>
            </div>
          ))}
        </section>
      )}
    </>
  );
}
