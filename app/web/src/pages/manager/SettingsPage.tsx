import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import AgentApprovalModePanel from "@/components/AgentApprovalModePanel";
import AgentPreferencesPanel from "@/components/AgentPreferencesPanel";
import { Chip, Loading, ProgressBar } from "@/components/common";
import type {
  Employee,
  Property,
  SettingDefinition,
  WorkspaceSettings,
  WorkspaceUsage,
} from "@/types/api";

const NAMESPACE_LABELS: Record<string, string> = {
  evidence: "Evidence",
  time: "Time tracking",
  pay: "Pay",
  retention: "Retention",
  scheduling: "Scheduling",
  tasks: "Tasks",
  auth: "Authentication",
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

function draftFromValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  return "";
}

function parseDraft(def: SettingDefinition, draft: string): unknown {
  if (def.type === "bool") return draft === "true";
  if (def.type === "int") return Number(draft);
  return draft;
}

function SettingEditor({
  def,
  value,
}: {
  def: SettingDefinition;
  value: unknown;
}) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState(draftFromValue(value));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDraft(draftFromValue(value));
  }, [value]);

  const save = useMutation({
    mutationFn: (next: unknown) =>
      fetchJson<WorkspaceSettings>("/api/v1/settings", {
        method: "PATCH",
        body: { [def.key]: next },
      }),
    onSuccess: (next) => {
      qc.setQueryData(qk.settings(), next);
      setError(null);
    },
    onError: () => setError("Save failed."),
  });

  const reset = useMutation({
    mutationFn: () =>
      fetchJson<WorkspaceSettings>("/api/v1/settings", {
        method: "PATCH",
        body: { [def.key]: null },
      }),
    onSuccess: (next) => {
      qc.setQueryData(qk.settings(), next);
      setError(null);
    },
    onError: () => setError("Reset failed."),
  });

  const current = draftFromValue(value);
  const changed = draft !== current;
  const saving = save.isPending || reset.isPending;
  const isDefault = value === def.catalog_default;
  const invalid = def.type === "int" && (!Number.isInteger(Number(draft)) || draft.trim() === "");

  return (
    <div className="settings-editor">
      <dt title={def.description}>{def.label}</dt>
      <dd>
        <form
          className="settings-editor__form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!invalid) save.mutate(parseDraft(def, draft));
          }}
        >
          <div className="settings-editor__control">
            {def.type === "bool" ? (
              <select
                aria-label={def.label}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                disabled={saving}
              >
                <option value="true">yes</option>
                <option value="false">no</option>
              </select>
            ) : def.type === "enum" ? (
              <select
                aria-label={def.label}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                disabled={saving}
              >
                {(def.enum_values ?? []).map((option) => (
                  <option key={option} value={option}>{option}</option>
                ))}
              </select>
            ) : (
              <input
                aria-label={def.label}
                type="number"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                disabled={saving}
              />
            )}
            <span className="muted setting-scope">{def.override_scope}</span>
          </div>
          <div className="settings-editor__actions">
            <button
              className="btn btn--moss btn--sm"
              type="submit"
              disabled={!changed || invalid || saving}
            >
              {save.isPending ? "Saving…" : "Save"}
            </button>
            <button
              className="btn btn--ghost btn--sm"
              type="button"
              disabled={isDefault || saving}
              onClick={() => reset.mutate()}
            >
              Default
            </button>
          </div>
          {error ? <p className="settings-editor__error">{error}</p> : null}
        </form>
      </dd>
    </div>
  );
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
  const usageQ = useQuery({
    queryKey: qk.workspaceUsage(),
    queryFn: () => fetchJson<WorkspaceUsage>("/api/v1/workspace/usage"),
  });
  const sub = "Workspace-wide configuration. Settings cascade from workspace to property to employee to task.";

  if (
    settingsQ.isPending ||
    catalogQ.isPending ||
    propsQ.isPending ||
    empsQ.isPending ||
    usageQ.isPending
  ) {
    return <DeskPage title="Settings" sub={sub}><Loading /></DeskPage>;
  }
  if (!settingsQ.data || !catalogQ.data || !propsQ.data || !empsQ.data || !usageQ.data) {
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

      {/* §11 — Workspace usage budget. Manager-visible shape is
          percent-only by design: no dollars, no tokens, no reset date.
          Dollars live on /settings/llm for the operator audience. The
          cap itself is adjusted via `crewday admin budget set-cap`; there
          is no HTTP surface to raise it. */}
      <section className="panel agent-usage">
        <header className="panel__head">
          <div className="agent-usage__heading">
            <h2>Agent usage</h2>
            <span className="muted agent-usage__window">{usageQ.data.window_label}</span>
          </div>
        </header>
        <div className="agent-usage__row">
          <div className="agent-usage__value">
            {usageQ.data.paused ? (
              <Chip tone="rust" size="sm">Paused</Chip>
            ) : (
              <span className="agent-usage__pct">{usageQ.data.percent}%</span>
            )}
          </div>
          <div className="agent-usage__bar">
            <ProgressBar value={usageQ.data.percent} />
          </div>
        </div>
        {usageQ.data.paused ? (
          <p className="muted">
            Agents are paused until older activity ages out of the window.
          </p>
        ) : null}
      </section>

      {/* Workspace defaults grouped by namespace */}
      <section className="grid grid--split">
        {Object.entries(grouped).map(([ns, items]) => (
          <div key={ns} className="panel">
            <header className="panel__head">
              <h2>{NAMESPACE_LABELS[ns] ?? ns}</h2>
            </header>
            <dl className="settings-kv settings-kv--editable">
              {items.map(({ def, value }) => (
                <SettingEditor key={def.key} def={def} value={value} />
              ))}
            </dl>
          </div>
        ))}
      </section>

      <section className="panel">
        <header className="panel__head">
          <h2>Chat gateway</h2>
          <Chip tone="ghost" size="sm">using deployment default</Chip>
        </header>
        <p className="muted">
          WhatsApp runs on the deployment-default Meta account — every workspace on this
          deployment shares one phone number. That's what your workers link when they pair their
          phone on <Link to="/me" className="link">/me → Chat channels</Link>.
        </p>
        <p className="muted">
          No per-user preferences live here: a linked WhatsApp means agent reach-out is on for
          that worker, unlinked means off. Whatever a worker could do via the CLI, the chat agent
          can do on their behalf — never more.
        </p>
        <dl className="settings-kv">
          <dt>Provider</dt>
          <dd>Deployment-default WhatsApp (<span className="mono">offapp_whatsapp</span>)</dd>
          <dt>Workspace override</dt>
          <dd className="muted">None. Opt in below to bring your own Meta account.</dd>
        </dl>
        <div className="chat-gateway-panel__footer">
          <button type="button" className="btn btn--ghost btn--sm" disabled>
            Use a dedicated Meta account for this workspace
          </button>
          <p className="muted chat-gateway-panel__hint">
            Overriding the default makes this workspace Meta-verify and own its own WhatsApp
            Business number — useful for branded communication or stricter isolation.
          </p>
        </div>
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
