import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import { Avatar, Chip, Loading } from "@/components/common";
import type {
  Asset,
  AssetDocument,
  Employee,
  EntitySettingsPayload,
  Instruction,
  InventoryItem,
  Property,
  PropertyClosure,
  SettingDefinition,
  Stay,
  Task,
  TaskStatus,
} from "@/types/api";

interface PropertyDetail {
  property: Property;
  property_tasks: Task[];
  stays: Stay[];
  inventory: InventoryItem[];
  instructions: Instruction[];
  closures: PropertyClosure[];
  assets: Asset[];
  asset_documents: AssetDocument[];
}

const STATUS_TONE: Record<TaskStatus, "moss" | "sky" | "ghost" | "rust"> = {
  completed: "moss",
  in_progress: "sky",
  pending: "ghost",
  scheduled: "ghost",
  skipped: "rust",
  cancelled: "rust",
  overdue: "rust",
};

function fmtDayMon(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
}

function fmtDayMonTime(iso: string): string {
  const d = new Date(iso);
  const date = d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return date + " · " + time;
}

function formatValue(value: unknown): string {
  if (value === true) return "yes";
  if (value === false) return "no";
  if (value === null || value === undefined) return "--";
  return String(value);
}

function SettingsOverridePanel({
  overrides,
  resolved,
  catalog,
}: {
  overrides: Record<string, unknown>;
  resolved: Record<string, { value: unknown; source: string }>;
  catalog: SettingDefinition[];
}) {
  const propertyScoped = catalog.filter((d) => d.override_scope.includes("P"));

  return (
    <div className="panel">
      <header className="panel__head"><h2>Settings overrides</h2></header>
      <p className="muted">
        Property-scoped settings. Overridden values take precedence over workspace defaults.
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Setting</th>
            <th>Effective value</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody>
          {propertyScoped.map((def) => {
            const hasOverride = def.key in overrides;
            const res = resolved[def.key];
            return (
              <tr key={def.key}>
                <td title={def.description}>
                  <code className="inline-code">{def.key}</code>
                  <span className="muted setting-label">{def.label}</span>
                </td>
                <td>
                  {hasOverride ? (
                    <strong>{formatValue(res?.value)}</strong>
                  ) : (
                    <span className="muted">{formatValue(res?.value)}</span>
                  )}
                </td>
                <td>
                  {hasOverride ? (
                    <Chip tone="moss" size="sm">overridden</Chip>
                  ) : (
                    <span className="muted">inherited ({res?.source ?? "catalog"})</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

type Tab = "overview" | "assets" | "settings";

export default function PropertyDetailPage() {
  const { pid = "" } = useParams<{ pid: string }>();
  const [activeTab, setActiveTab] = useState<Tab>("overview");

  const detailQ = useQuery({
    queryKey: qk.property(pid),
    queryFn: () => fetchJson<PropertyDetail>("/api/v1/properties/" + pid),
    enabled: pid !== "",
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  const settingsQ = useQuery({
    queryKey: qk.propertySettings(pid),
    queryFn: () => fetchJson<EntitySettingsPayload>("/api/v1/properties/" + pid + "/settings"),
    enabled: pid !== "" && activeTab === "settings",
  });
  const catalogQ = useQuery({
    queryKey: qk.settingsCatalog(),
    queryFn: () => fetchJson<SettingDefinition[]>("/api/v1/settings/catalog"),
    enabled: activeTab === "settings",
  });

  if (detailQ.isPending || empsQ.isPending) {
    return <DeskPage title="Property"><Loading /></DeskPage>;
  }
  if (!detailQ.data || !empsQ.data) {
    return <DeskPage title="Property">Failed to load.</DeskPage>;
  }

  const { property, property_tasks, stays, assets, asset_documents: _asset_documents } = detailQ.data;
  void _asset_documents;
  const empsById = new Map(empsQ.data.map((e) => [e.id, e]));

  return (
    <DeskPage
      title={property.name}
      sub={property.city + " · " + property.timezone}
      actions={
        <>
          <button className="btn btn--ghost">New task</button>
          <button className="btn btn--moss">Edit property</button>
        </>
      }
    >
      <nav className="tabs tabs--h">
        <a
          className={"tab-link" + (activeTab === "overview" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("overview")}
        >
          Overview
        </a>
        <a className="tab-link">Areas</a>
        <a className="tab-link">Stays</a>
        <a
          className={"tab-link" + (activeTab === "assets" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("assets")}
        >
          Assets
        </a>
        <a className="tab-link">Instructions</a>
        <a className="tab-link">Closures</a>
        <a
          className={"tab-link" + (activeTab === "settings" ? " tab-link--active" : "")}
          onClick={() => setActiveTab("settings")}
        >
          Settings
        </a>
      </nav>

      {activeTab === "overview" && (
        <section className="grid grid--split">
          <div className="panel">
            <header className="panel__head"><h2>Upcoming stays</h2></header>
            <table className="table">
              <thead>
                <tr>
                  <th>Guest</th><th>Source</th><th>In</th><th>Out</th><th>Guests</th>
                </tr>
              </thead>
              <tbody>
                {stays.map((s) => (
                  <tr key={s.id}>
                    <td><strong>{s.guest_name}</strong></td>
                    <td>{s.source}</td>
                    <td className="table__mono">{fmtDayMon(s.check_in)}</td>
                    <td className="table__mono">{fmtDayMon(s.check_out)}</td>
                    <td>{s.guest_name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="panel">
            <header className="panel__head"><h2>Tasks for this property</h2></header>
            <ul className="task-list task-list--desk">
              {property_tasks.map((t) => {
                const emp = empsById.get(t.assignee_id);
                return (
                  <li key={t.id} className="task-row">
                    <span className="task-row__time table__mono">
                      {fmtDayMonTime(t.scheduled_start)}
                    </span>
                    <span className="task-row__title">
                      <strong>{t.title}</strong>
                      <span className="task-row__area">{t.area}</span>
                    </span>
                    <span className="task-row__assignee">
                      {emp && (
                        <>
                          <Avatar initials={emp.avatar_initials} size="xs" />{" "}
                          {emp.name.split(" ")[0]}
                        </>
                      )}
                    </span>
                    <Chip tone={STATUS_TONE[t.status]} size="sm">{t.status}</Chip>
                  </li>
                );
              })}
            </ul>
          </div>
        </section>
      )}

      {activeTab === "settings" && (
        <>
          {(settingsQ.isPending || catalogQ.isPending) ? (
            <Loading />
          ) : settingsQ.data && catalogQ.data ? (
            <SettingsOverridePanel
              overrides={settingsQ.data.overrides}
              resolved={settingsQ.data.resolved}
              catalog={catalogQ.data}
            />
          ) : (
            <p>Failed to load settings.</p>
          )}
        </>
      )}

      {activeTab === "assets" && (
        <div className="panel">
          <header className="panel__head">
            <h2>Assets</h2>
            <span className="muted mono">{assets.length} tracked</span>
          </header>
          {assets.length === 0 ? (
            <p className="muted">No assets tracked for this property.</p>
          ) : (
            <table className="table">
              <thead>
                <tr><th>Asset</th><th>Area</th><th>Condition</th><th>Status</th></tr>
              </thead>
              <tbody>
                {assets.map((a) => (
                  <tr key={a.id}>
                    <td><strong>{a.name}</strong>{a.make && <span className="table__sub"> {a.make} {a.model}</span>}</td>
                    <td>{a.area ?? "\u2014"}</td>
                    <td><Chip tone={a.condition === "fair" ? "sand" : (a.condition === "poor" || a.condition === "needs_replacement") ? "rust" : "moss"} size="sm">{a.condition}</Chip></td>
                    <td><Chip tone={a.status === "active" ? "moss" : a.status === "in_repair" ? "sand" : "rust"} size="sm">{a.status}</Chip></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      <AgentPreferencesPanel
        scope="property"
        scopeId={property.id}
        title={"Agent preferences — " + property.name}
        subtitle="Sits between workspace and user preferences when the agent discusses this property. Soft guidance only — hard rules belong in the settings cascade above."
      />
    </DeskPage>
  );
}
