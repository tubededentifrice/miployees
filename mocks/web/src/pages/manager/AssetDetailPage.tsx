import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { AssetIcon } from "@/components/AssetIcon";
import type {
  AssetAction,
  AssetCondition,
  AssetDetailPayload,
  AssetDocument,
  AssetStatus,
  DocumentKind,
  Task,
  TaskStatus,
} from "@/types/api";

const CONDITION_TONE: Record<AssetCondition, "moss" | "sand" | "rust"> = {
  new: "moss",
  good: "moss",
  fair: "sand",
  poor: "rust",
  needs_replacement: "rust",
};

const STATUS_TONE: Record<AssetStatus, "moss" | "sand" | "rust" | "ghost"> = {
  active: "moss",
  in_repair: "sand",
  decommissioned: "ghost",
  disposed: "rust",
};

const TASK_STATUS_TONE: Record<TaskStatus, "moss" | "sky" | "ghost" | "rust" | "sand"> = {
  completed: "moss",
  in_progress: "sky",
  pending: "ghost",
  scheduled: "ghost",
  skipped: "rust",
  cancelled: "rust",
  overdue: "rust",
};

const KIND_ICON: Record<DocumentKind, string> = {
  manual: "\u{1F4D6}",
  warranty: "\u{1F6E1}\uFE0F",
  invoice: "\u{1F9FE}",
  receipt: "\u{1F9FE}",
  photo: "\u{1F4F7}",
  certificate: "\u{1F4DC}",
  contract: "\u{1F4DD}",
  permit: "\u{1F4CB}",
  insurance: "\u{1F3E6}",
  other: "\u{1F4C4}",
};

function fmtDate(iso: string | null): string {
  if (!iso) return "\u2014";
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function fmtCents(cents: number | null, currency: string | null): string {
  if (cents == null) return "\u2014";
  return (cents / 100).toFixed(2) + " " + (currency ?? "EUR");
}

function dueTone(iso: string | null): "moss" | "sand" | "rust" | "ghost" {
  if (!iso) return "ghost";
  const diff = new Date(iso).getTime() - Date.now();
  if (diff < 0) return "rust";
  if (diff < 14 * 86_400_000) return "sand";
  return "moss";
}

type Tab = "overview" | "actions" | "documents" | "history";

export default function AssetDetailPage() {
  const { aid = "" } = useParams<{ aid: string }>();
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const queryClient = useQueryClient();

  const detailQ = useQuery({
    queryKey: qk.asset(aid),
    queryFn: () => fetchJson<AssetDetailPayload>("/api/v1/assets/" + aid),
    enabled: aid !== "",
  });

  const completeMut = useMutation({
    mutationFn: (actionId: string) =>
      fetchJson<AssetAction>("/api/v1/assets/" + aid + "/actions/" + actionId + "/complete", {
        method: "POST",
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: qk.asset(aid) });
      void queryClient.invalidateQueries({ queryKey: qk.assets() });
    },
  });

  if (detailQ.isPending) {
    return <DeskPage title="Asset"><Loading /></DeskPage>;
  }
  if (!detailQ.data) {
    return <DeskPage title="Asset">Failed to load.</DeskPage>;
  }

  const { asset, asset_type, property, actions, documents, linked_tasks } = detailQ.data;
  const subText = property.name + (asset.area ? " / " + asset.area : "");

  const sortedActions = [...actions].sort((a, b) => {
    if (!a.next_due_on) return 1;
    if (!b.next_due_on) return -1;
    return a.next_due_on.localeCompare(b.next_due_on);
  });

  return (
    <DeskPage
      title={asset.name}
      sub={subText}
      actions={<button className="btn btn--ghost">Edit</button>}
    >
      <nav className="tabs tabs--h">
        {(["overview", "actions", "documents", "history"] as const).map((t) => (
          <a
            key={t}
            className={"tab-link" + (activeTab === t ? " tab-link--active" : "")}
            onClick={() => setActiveTab(t)}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </a>
        ))}
      </nav>

      {activeTab === "overview" && (
        <OverviewTab
          asset={asset}
          assetType={asset_type}
          sortedActions={sortedActions}
        />
      )}

      {activeTab === "actions" && (
        <ActionsTab
          actions={sortedActions}
          completeMut={completeMut}
        />
      )}

      {activeTab === "documents" && <DocumentsTab documents={documents} />}

      {activeTab === "history" && <HistoryTab tasks={linked_tasks} />}
    </DeskPage>
  );
}

function OverviewTab({
  asset,
  assetType,
  sortedActions,
}: {
  asset: AssetDetailPayload["asset"];
  assetType: AssetDetailPayload["asset_type"];
  sortedActions: AssetAction[];
}) {
  const upcoming = sortedActions.slice(0, 3);

  return (
    <section className="grid grid--split">
      <div className="panel">
        <header className="panel__head"><h2>Details</h2></header>
        <dl className="asset-kv">
          <dt>Type</dt>
          <dd>
            {assetType ? (
              <span className="asset-type-line">
                <AssetIcon name={assetType.icon_name} />
                {assetType.name}
              </span>
            ) : (
              "\u2014"
            )}
          </dd>
          <dt>Make</dt>
          <dd>{asset.make ?? "\u2014"}</dd>
          <dt>Model</dt>
          <dd>{asset.model ?? "\u2014"}</dd>
          <dt>Serial</dt>
          <dd className="mono">{asset.serial_number ?? "\u2014"}</dd>
          <dt>Condition</dt>
          <dd><Chip tone={CONDITION_TONE[asset.condition]} size="sm">{asset.condition.replace("_", " ")}</Chip></dd>
          <dt>Status</dt>
          <dd><Chip tone={STATUS_TONE[asset.status]} size="sm">{asset.status.replace("_", " ")}</Chip></dd>
          <dt>Installed</dt>
          <dd>{fmtDate(asset.installed_on)}</dd>
          <dt>Purchased</dt>
          <dd>{fmtDate(asset.purchased_on)}</dd>
          <dt>Purchase price</dt>
          <dd>{fmtCents(asset.purchase_price_cents, asset.purchase_currency)}</dd>
          <dt>Vendor</dt>
          <dd>{asset.purchase_vendor ?? "\u2014"}</dd>
          <dt>Warranty expires</dt>
          <dd>{fmtDate(asset.warranty_expires_on)}</dd>
          <dt>Expected lifespan</dt>
          <dd>{asset.expected_lifespan_years != null ? asset.expected_lifespan_years + " years" : "\u2014"}</dd>
          <dt>Guest visible</dt>
          <dd>{asset.guest_visible ? "yes" : "no"}</dd>
          <dt>QR token</dt>
          <dd className="mono">{asset.qr_token}</dd>
        </dl>
      </div>

      <div>
        <div className="panel">
          <header className="panel__head"><h2>Next maintenance</h2></header>
          {upcoming.length === 0 ? (
            <p className="muted">No scheduled actions.</p>
          ) : (
            upcoming.map((ac) => (
              <div key={ac.id} className="action-row">
                <span className="action-row__label">{ac.label}</span>
                <Chip tone={dueTone(ac.next_due_on)} size="sm">{fmtDate(ac.next_due_on)}</Chip>
                {ac.interval_days != null && (
                  <span className="action-row__interval">every {ac.interval_days}d</span>
                )}
              </div>
            ))
          )}
        </div>

        <div className="qr-stub">QR</div>
      </div>
    </section>
  );
}

function ActionsTab({
  actions,
  completeMut,
}: {
  actions: AssetAction[];
  completeMut: ReturnType<typeof useMutation<AssetAction, Error, string>>;
}) {
  return (
    <div className="panel">
      <header className="panel__head"><h2>Maintenance actions</h2></header>
      {actions.length === 0 ? (
        <p className="muted">No actions configured.</p>
      ) : (
        actions.map((ac) => (
          <div key={ac.id} className="action-row">
            <span className="action-row__label">
              <strong>{ac.label}</strong>
              {ac.description && <span className="muted"> {ac.description}</span>}
            </span>
            {ac.interval_days != null && (
              <span className="action-row__interval">every {ac.interval_days}d</span>
            )}
            <span className="muted mono">{fmtDate(ac.last_performed_at)}</span>
            <Chip tone={dueTone(ac.next_due_on)} size="sm">{fmtDate(ac.next_due_on)}</Chip>
            <button
              className="btn btn--sm btn--moss"
              onClick={() => completeMut.mutate(ac.id)}
              disabled={completeMut.isPending}
            >
              Mark done
            </button>
          </div>
        ))
      )}
    </div>
  );
}

function DocumentsTab({ documents }: { documents: AssetDocument[] }) {
  return (
    <div className="panel">
      <header className="panel__head"><h2>Documents</h2></header>
      {documents.length === 0 ? (
        <p className="muted">No documents attached.</p>
      ) : (
        documents.map((doc) => (
          <div key={doc.id} className="doc-row">
            <span className="doc-thumb">{KIND_ICON[doc.kind]}</span>
            <span><strong>{doc.title}</strong></span>
            <Chip tone="ghost" size="sm">{doc.kind}</Chip>
            <span className="mono muted">{doc.size_kb} KB</span>
            <span className="muted">{fmtDate(doc.expires_on)}</span>
          </div>
        ))
      )}
    </div>
  );
}

function HistoryTab({ tasks }: { tasks: Task[] }) {
  return (
    <div className="panel">
      <header className="panel__head"><h2>Linked tasks</h2></header>
      {tasks.length === 0 ? (
        <p className="muted">No linked tasks.</p>
      ) : (
        <ul className="kb-list">
          {tasks.map((t) => (
            <li key={t.id} className="kb-item">
              <div className="kb-item__main">
                <div className="kb-item__head">
                  <span className="kb-item__title">{t.title}</span>
                  <Chip tone={TASK_STATUS_TONE[t.status]} size="sm">{t.status}</Chip>
                </div>
                <div className="kb-item__meta">
                  <span className="mono muted">{fmtDate(t.scheduled_start)}</span>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
