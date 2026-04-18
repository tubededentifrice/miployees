import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import AgentApprovalModePanel from "@/components/AgentApprovalModePanel";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import { Chip, Loading } from "@/components/common";
import type {
  Employee,
  Property,
  SettingDefinition,
  WorkspaceSettings,
} from "@/types/api";

const NAMESPACE_LABELS: Record<string, string> = {
  evidence: "Evidence",
  time: "Time tracking",
  pay: "Pay",
  retention: "Retention",
  scheduling: "Scheduling",
  tasks: "Tasks",
};

function groupByNamespace(
  defaults: Record<string, unknown>,
  catalog: SettingDefinition[],
): Record<string, { def: SettingDefinition; value: unknown }[]> {
  const groups: Record<string, { def: SettingDefinition; value: unknown }[]> = {};
  for (const def of catalog) {
    const ns = def.key.split(".")[0] ?? "other";
    const bucket = groups[ns] ?? (groups[ns] = []);
    bucket.push({ def, value: defaults[def.key] ?? def.catalog_default });
  }
  return groups;
}

function formatValue(value: unknown): string {
  if (value === true) return "yes";
  if (value === false) return "no";
  if (value === null || value === undefined) return "—";
  return String(value);
}

function OverrideSummary({ properties, employees }: { properties: Property[]; employees: Employee[] }) {
  const propsWithOverrides = properties.filter((p) => Object.keys(p.settings_override).length > 0);
  const empsWithOverrides = employees.filter((e) => Object.keys(e.settings_override).length > 0);

  if (propsWithOverrides.length === 0 && empsWithOverrides.length === 0) {
    return (
      <div className="panel">
        <header className="panel__head"><h2>Override summary</h2></header>
        <p className="muted">No properties or employees have settings overrides.</p>
      </div>
    );
  }

  return (
    <div className="panel">
      <header className="panel__head"><h2>Override summary</h2></header>
      <p className="muted">Properties and employees that override workspace defaults.</p>
      {propsWithOverrides.length > 0 && (
        <>
          <h3 className="section-title section-title--sm">Properties</h3>
          <ul className="settings-list">
            {propsWithOverrides.map((p) => (
              <li key={p.id}>
                <Link to={`/property/${p.id}`} className="link">
                  <strong>{p.name}</strong>
                </Link>{" "}
                <Chip tone={p.color} size="sm">
                  {Object.keys(p.settings_override).length} override{Object.keys(p.settings_override).length !== 1 ? "s" : ""}
                </Chip>
                <span className="muted"> — {Object.keys(p.settings_override).join(", ")}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {empsWithOverrides.length > 0 && (
        <>
          <h3 className="section-title section-title--sm">Employees</h3>
          <ul className="settings-list">
            {empsWithOverrides.map((e) => (
              <li key={e.id}>
                <Link to={`/employee/${e.id}`} className="link">
                  <strong>{e.name}</strong>
                </Link>{" "}
                <Chip tone="sky" size="sm">
                  {Object.keys(e.settings_override).length} override{Object.keys(e.settings_override).length !== 1 ? "s" : ""}
                </Chip>
                <span className="muted"> — {Object.keys(e.settings_override).join(", ")}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export default function SettingsPage() {
  const settingsQ = useQuery({
    queryKey: qk.settings(),
    queryFn: () => fetchJson<WorkspaceSettings>("/api/v1/settings"),
  });
  const catalogQ = useQuery({
    queryKey: qk.settingsCatalog(),
    queryFn: () => fetchJson<SettingDefinition[]>("/api/v1/settings/catalog"),
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });
  const empsQ = useQuery({
    queryKey: qk.employees(),
    queryFn: () => fetchJson<Employee[]>("/api/v1/employees"),
  });
  const sub = "Workspace-wide configuration. Settings cascade from workspace to property to employee to task.";

  if (settingsQ.isPending || catalogQ.isPending || propsQ.isPending || empsQ.isPending) {
    return <DeskPage title="Settings" sub={sub}><Loading /></DeskPage>;
  }
  if (!settingsQ.data || !catalogQ.data || !propsQ.data || !empsQ.data) {
    return <DeskPage title="Settings" sub={sub}>Failed to load.</DeskPage>;
  }

  const ws = settingsQ.data;
  const catalog = catalogQ.data;
  const grouped = groupByNamespace(ws.defaults, catalog);

  return (
    <DeskPage title="Settings" sub={sub}>
      {/* Personal (your account) — agent approval mode is a per-user setting (§11). */}
      <AgentApprovalModePanel variant="desktop" />

      {/* §11 — Agent preferences (workspace layer). Soft guidance stacked
          into every composition-capability system prompt. */}
      <AgentPreferencesPanel
        scope="workspace"
        title="Agent preferences — Workspace"
        subtitle="Stacked broadest-first with property and user preferences into every agent turn. CLAUDE.md-style free-form guidance; not a substitute for the structured settings cascade below."
      />

      {/* Your personal layer also lives on this page for managers — workers
          reach their own blob from the phone '/me' screen. */}
      <AgentPreferencesPanel
        scope="user"
        title="Agent preferences — You"
        subtitle="Private to you. Nobody else — not even an owner — can read or edit this text. Your chat agent sees it on every turn."
      />


      {/* Workspace identity */}
      <section className="panel">
        <header className="panel__head"><h2>Workspace</h2></header>
        <dl className="settings-kv">
          <dt>Name</dt><dd>{ws.meta.name}</dd>
          <dt>Timezone</dt><dd className="mono">{ws.meta.timezone}</dd>
          <dt>Currency</dt><dd className="mono">{ws.meta.currency}</dd>
          <dt>Country</dt><dd className="mono">{ws.meta.country}</dd>
          <dt>Locale</dt><dd className="mono">{ws.meta.default_locale}</dd>
        </dl>
      </section>

      {/* Workspace defaults grouped by namespace */}
      <section className="grid grid--split">
        {Object.entries(grouped).map(([ns, items]) => (
          <div key={ns} className="panel">
            <header className="panel__head">
              <h2>{NAMESPACE_LABELS[ns] ?? ns}</h2>
            </header>
            <dl className="settings-kv">
              {items.map(({ def, value }) => (
                <div key={def.key}>
                  <dt title={def.description}>{def.label}</dt>
                  <dd>
                    {def.type === "enum" ? (
                      <Chip tone="sky" size="sm">{formatValue(value)}</Chip>
                    ) : (
                      <span className="mono">{formatValue(value)}</span>
                    )}
                    <span className="muted setting-scope">
                      {def.override_scope}
                    </span>
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </section>

      <section className="panel">
        <header className="panel__head"><h2>External chat adapters</h2></header>
        <p className="muted">
          The architecture keeps a deferred seam for WhatsApp and Telegram
          adapters (§23), but they are not enabled in this preview or in
          shipped v1. Settings here intentionally stay focused on the
          in-app web chat surfaces.
        </p>
      </section>

      {/* Override summary */}
      <OverrideSummary properties={propsQ.data} employees={empsQ.data} />

      {/* Policy + Danger zone */}
      <div className="panel">
        <header className="panel__head"><h2>Agent approvals</h2></header>
        <p className="muted">Actions that require your manual approval before an agent can execute them.</p>

        <h3 className="section-title section-title--sm">Always gated (cannot be disabled)</h3>
        <ul className="settings-list">
          {ws.policy.approvals.always_gated.map((a) => (
            <li key={a}><code className="inline-code">{a}</code></li>
          ))}
        </ul>

        <h3 className="section-title section-title--sm">Configurable</h3>
        <ul className="settings-list">
          {ws.policy.approvals.configurable.map((a) => (
            <li key={a}>
              <code className="inline-code">{a}</code>{" "}
              <Chip tone="moss" size="sm">gated</Chip>
            </li>
          ))}
        </ul>
      </div>

      <div className="panel panel--danger">
        <header className="panel__head"><h2>Danger zone</h2></header>
        <p className="muted">Host-CLI-only. No HTTP surface, no agent path.</p>
        <ul className="danger-list">
          {ws.policy.danger_zone.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
      </div>
    </DeskPage>
  );
}
