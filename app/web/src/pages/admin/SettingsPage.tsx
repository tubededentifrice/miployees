import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Checkbox, Chip, Loading } from "@/components/common";
import type {
  AdminDeploymentSetting,
  AdminDeploymentSettingsResponse,
  AdminMe,
  AdminSignupSettings,
  JsonValue,
} from "@/types/api";

type SettingDraft = string | number | boolean;
type ParsedSetting =
  | { ok: true; value: string | number | boolean | JsonValue }
  | { ok: false; message: string };

function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function displayDate(value: string): string {
  if (!value) return "default";
  return new Date(value).toLocaleString();
}

function parseSettingDraft(row: AdminDeploymentSetting, draft: SettingDraft): ParsedSetting {
  if (row.kind !== "json") return { ok: true, value: draft };
  if (typeof draft !== "string") return { ok: false, message: "JSON draft must be text." };
  try {
    return { ok: true, value: JSON.parse(draft) as JsonValue };
  } catch {
    return { ok: false, message: "Fix invalid JSON before saving." };
  }
}

function settingInputValue(row: AdminDeploymentSetting, drafts: Record<string, SettingDraft>): SettingDraft {
  if (Object.prototype.hasOwnProperty.call(drafts, row.key)) return drafts[row.key]!;
  if (row.kind === "json") return prettyJson(row.value);
  if (row.kind === "int" && typeof row.value === "number") return row.value;
  if (row.kind === "bool" && typeof row.value === "boolean") return row.value;
  return String(row.value);
}

function signupValue<K extends keyof AdminSignupSettings>(
  source: AdminSignupSettings,
  draft: Partial<AdminSignupSettings>,
  key: K,
): AdminSignupSettings[K] {
  if (Object.prototype.hasOwnProperty.call(draft, key)) {
    return draft[key] as AdminSignupSettings[K];
  }
  return source[key];
}

export default function AdminSettingsPage() {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: qk.adminMe(),
    queryFn: () => fetchJson<AdminMe>("/admin/api/v1/me"),
  });
  const q = useQuery({
    queryKey: qk.adminSettings(),
    queryFn: () => fetchJson<AdminDeploymentSettingsResponse>("/admin/api/v1/settings"),
  });
  const update = useMutation({
    mutationFn: ({ key, value }: { key: string; value: string | number | boolean | JsonValue }) =>
      fetchJson<AdminDeploymentSetting>(`/admin/api/v1/settings/${key}`, {
        method: "PUT",
        body: { value },
      }),
    onMutate: async ({ key, value }) => {
      await qc.cancelQueries({ queryKey: qk.adminSettings() });
      const previous = qc.getQueryData<AdminDeploymentSettingsResponse>(qk.adminSettings());
      qc.setQueryData<AdminDeploymentSettingsResponse>(qk.adminSettings(), (current) => {
        if (!current) return current;
        return {
          settings: current.settings.map((row) =>
            row.key === key ? { ...row, value } : row,
          ),
        };
      });
      return { previous };
    },
    onError: (_err, _vars, context) => {
      if (context?.previous) qc.setQueryData(qk.adminSettings(), context.previous);
    },
    onSuccess: (saved) => {
      qc.setQueryData<AdminDeploymentSettingsResponse>(qk.adminSettings(), (current) => {
        if (!current) return { settings: [saved] };
        return {
          settings: current.settings.map((row) =>
            row.key === saved.key ? saved : row,
          ),
        };
      });
    },
    onSettled: () => qc.invalidateQueries({ queryKey: qk.adminSettings() }),
  });

  const signupQ = useQuery({
    queryKey: qk.adminSignup(),
    queryFn: () => fetchJson<AdminSignupSettings>("/admin/api/v1/signup/settings"),
  });
  const signupUpdate = useMutation<
    AdminSignupSettings,
    Error,
    Partial<AdminSignupSettings>,
    { previous: AdminSignupSettings | undefined }
  >({
    mutationFn: (patch: Partial<AdminSignupSettings>) =>
      fetchJson<AdminSignupSettings>("/admin/api/v1/signup/settings", {
        method: "PUT",
        body: patch,
      }),
    onMutate: async (patch) => {
      await qc.cancelQueries({ queryKey: qk.adminSignup() });
      const previous = qc.getQueryData<AdminSignupSettings>(qk.adminSignup());
      qc.setQueryData<AdminSignupSettings>(qk.adminSignup(), (current) =>
        current ? { ...current, ...patch } : current,
      );
      return { previous };
    },
    onError: (_err, _patch, context) => {
      if (context?.previous) qc.setQueryData(qk.adminSignup(), context.previous);
    },
    onSuccess: (saved) => {
      qc.setQueryData(qk.adminSignup(), saved);
      setSignupDraft({});
      setThrottleDraft(null);
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: qk.adminSignup() });
      qc.invalidateQueries({ queryKey: qk.adminSettings() });
    },
  });

  const [drafts, setDrafts] = useState<Record<string, SettingDraft>>({});
  const [signupDraft, setSignupDraft] = useState<Partial<AdminSignupSettings>>({});
  const [throttleDraft, setThrottleDraft] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [signupError, setSignupError] = useState<string | null>(null);

  const sub =
    "Deployment-scope settings: self-serve signup policy, the capability registry read-out, and the raw key/value store. Root-only keys require deployment owner rights.";

  if (q.isPending || me.isPending || signupQ.isPending) {
    return <DeskPage title="Settings" sub={sub}><Loading /></DeskPage>;
  }
  if (!q.data || !me.data || !signupQ.data) {
    return <DeskPage title="Settings" sub={sub}>Failed to load.</DeskPage>;
  }

  const isOwner = me.data.is_owner;
  const rows = q.data.settings;
  const setDraft = (key: string, value: SettingDraft) => {
    setSaveError(null);
    setDrafts((current) => ({ ...current, [key]: value }));
  };
  const parsedDraft = (row: AdminDeploymentSetting): ParsedSetting =>
    parseSettingDraft(row, settingInputValue(row, drafts));
  const dirtyRows = rows.filter((row) => {
    const locked = row.root_only && !isOwner;
    if (locked || !Object.prototype.hasOwnProperty.call(drafts, row.key)) return false;
    const parsed = parsedDraft(row);
    return !parsed.ok || !sameValue(parsed.value, row.value);
  });
  const invalidJsonCount = dirtyRows.filter((row) => !parsedDraft(row).ok).length;
  const dirtyCount = dirtyRows.filter((row) => parsedDraft(row).ok).length;

  const saveAll = async () => {
    if (dirtyCount === 0 || invalidJsonCount > 0) return;
    setSaveError(null);
    const clearedKeys = new Set<string>();
    try {
      for (const row of dirtyRows) {
        const parsed = parsedDraft(row);
        if (parsed.ok) {
          try {
            await update.mutateAsync({ key: row.key, value: parsed.value });
          } finally {
            clearedKeys.add(row.key);
          }
        }
      }
    } catch {
      setSaveError("Could not save changes.");
    } finally {
      setDrafts((current) => {
        if (clearedKeys.size === 0) return current;
        const next = { ...current };
        for (const key of clearedKeys) delete next[key];
        return next;
      });
    }
  };

  const resetAll = () => {
    setSaveError(null);
    setDrafts({});
  };

  const s = signupQ.data;
  const signupThrottleText = throttleDraft ?? prettyJson(s.signup_throttle_overrides);
  let signupThrottleParsed: Record<string, number> | null = null;
  let signupThrottleInvalid = false;
  try {
    const parsed = JSON.parse(signupThrottleText) as unknown;
    signupThrottleInvalid =
      typeof parsed !== "object" ||
      parsed === null ||
      Array.isArray(parsed) ||
      Object.values(parsed).some((value) => typeof value !== "number" || !Number.isInteger(value) || value < 0);
    if (!signupThrottleInvalid) signupThrottleParsed = parsed as Record<string, number>;
  } catch {
    signupThrottleInvalid = true;
  }
  const setSignup = <K extends keyof AdminSignupSettings>(key: K, value: AdminSignupSettings[K]) => {
    setSignupError(null);
    setSignupDraft((current) => ({ ...current, [key]: value }));
  };
  const signupPatch: Partial<AdminSignupSettings> = {};
  const signupEnabled = signupValue(s, signupDraft, "signup_enabled");
  const signupDisposableDomainsPath = signupValue(s, signupDraft, "signup_disposable_domains_path");
  if (signupEnabled !== s.signup_enabled) {
    signupPatch.signup_enabled = signupEnabled;
  }
  if (signupDisposableDomainsPath !== s.signup_disposable_domains_path) {
    signupPatch.signup_disposable_domains_path = signupDisposableDomainsPath;
  }
  if (
    throttleDraft !== null &&
    signupThrottleParsed !== null &&
    !sameValue(signupThrottleParsed, s.signup_throttle_overrides)
  ) {
    signupPatch.signup_throttle_overrides = signupThrottleParsed;
  }
  const signupHasDraft = Object.keys(signupDraft).length > 0 || throttleDraft !== null;
  const signupDirty = Object.keys(signupPatch).length > 0;
  const saveSignup = () => {
    if (!signupDirty || signupThrottleInvalid) return;
    setSignupError(null);
    signupUpdate.mutate(signupPatch, {
      onError: () => setSignupError("Could not save signup settings."),
    });
  };
  const resetSignup = () => {
    setSignupError(null);
    setSignupDraft({});
    setThrottleDraft(null);
  };
  const capabilities = Object.entries(me.data.capabilities).sort(([a], [b]) => a.localeCompare(b));

  return (
    <DeskPage title="Settings" sub={sub}>
      <div className="panel" id="signup">
        <header className="panel__head">
          <h2>Visitor signup</h2>
          <Chip tone={signupValue(s, signupDraft, "signup_enabled") ? "moss" : "ghost"} size="sm">
            {signupValue(s, signupDraft, "signup_enabled") ? "enabled" : "closed"}
          </Chip>
        </header>
        <p className="muted">
          Self-serve signup controls for this deployment (§03). Flip enabled, tighten
          throttles, point at the disposable-domain blocklist.
        </p>
        <div className="form-grid form-grid--two">
          <div className="form-row">
            <span className="form-label">Enabled</span>
            <Checkbox
              checked={signupValue(s, signupDraft, "signup_enabled")}
              onChange={(e) => setSignup("signup_enabled", e.target.checked)}
              label={
                signupValue(s, signupDraft, "signup_enabled")
                  ? "Anyone can create a workspace via /signup."
                  : "/signup/start returns 404; /signup renders a 'closed' page."
              }
            />
          </div>
          <label className="form-row">
            <span className="form-label">Disposable domains path</span>
            <input
              type="text"
              className="input input--inline"
              value={signupValue(s, signupDraft, "signup_disposable_domains_path")}
              onChange={(e) => setSignup("signup_disposable_domains_path", e.target.value)}
            />
          </label>
          <label className="form-row">
            <span className="form-label">Throttle overrides</span>
            <textarea
              className="input input--inline"
              rows={5}
              value={signupThrottleText}
              onChange={(e) => {
                setSignupError(null);
                setThrottleDraft(e.target.value);
              }}
            />
            {signupThrottleInvalid && (
              <span className="table__sub muted">Use a JSON object with non-negative integer values.</span>
            )}
          </label>
        </div>
        <footer className="panel__foot">
          <span className="muted">
            {signupError ?? "Signup settings are stored as deployment setting rows."}
          </span>
          <div className="inline-actions">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={!signupHasDraft || signupUpdate.isPending}
              onClick={resetSignup}
            >
              Discard
            </button>
            <button
              type="button"
              className="btn btn--moss"
              disabled={!signupDirty || signupThrottleInvalid || signupUpdate.isPending}
              onClick={saveSignup}
            >
              Save
            </button>
          </div>
        </footer>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Deployment settings</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Key</th>
              <th>Value</th>
              <th>Last edit</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const locked = row.root_only && !isOwner;
              const parsed = parsedDraft(row);
              return (
                <tr key={row.key}>
                  <td>
                    <code className="inline-code">{row.key}</code>
                    {row.root_only && (
                      <>
                        {" "}
                        <Chip tone="rust" size="sm">owners-only</Chip>
                      </>
                    )}
                    <div className="table__sub muted">{row.description}</div>
                  </td>
                  <td>
                    {row.kind === "bool" ? (
                      <Checkbox
                        checked={Boolean(settingInputValue(row, drafts))}
                        disabled={locked}
                        onChange={(e) => setDraft(row.key, e.target.checked)}
                      />
                    ) : row.kind === "int" ? (
                      <input
                        type="number"
                        className="input input--inline"
                        value={String(settingInputValue(row, drafts))}
                        disabled={locked}
                        onChange={(e) => setDraft(row.key, Number(e.target.value))}
                      />
                    ) : row.kind === "json" ? (
                      <>
                        <textarea
                          className="input input--inline"
                          rows={5}
                          value={String(settingInputValue(row, drafts))}
                          disabled={locked}
                          onChange={(e) => setDraft(row.key, e.target.value)}
                        />
                        {!parsed.ok && (
                          <div className="table__sub muted">{parsed.message}</div>
                        )}
                      </>
                    ) : (
                      <input
                        type="text"
                        className="input input--inline"
                        value={String(settingInputValue(row, drafts))}
                        disabled={locked}
                        onChange={(e) => setDraft(row.key, e.target.value)}
                      />
                    )}
                  </td>
                  <td className="mono muted">
                    {displayDate(row.updated_at)}
                    <div className="table__sub">{row.updated_by || "registry default"}</div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <footer className="panel__foot">
          <span className="muted">
            {saveError
              ? saveError
              : invalidJsonCount > 0
              ? "Fix invalid JSON before saving."
              : dirtyCount === 0
                ? "No pending changes."
                : `${dirtyCount} pending change${dirtyCount === 1 ? "" : "s"}.`}
          </span>
          <div className="inline-actions">
            <button
              type="button"
              className="btn btn--ghost"
              disabled={dirtyRows.length === 0 || update.isPending}
              onClick={resetAll}
            >
              Discard
            </button>
            <button
              type="button"
              className="btn btn--moss"
              disabled={dirtyCount === 0 || invalidJsonCount > 0 || update.isPending}
              onClick={() => { void saveAll(); }}
            >
              {update.isPending ? "Saving..." : "Save changes"}
            </button>
          </div>
        </footer>
      </div>

      <div className="panel">
        <header className="panel__head"><h2>Capability registry</h2></header>
        <table className="table table--roomy">
          <thead>
            <tr>
              <th>Capability</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {capabilities.map(([key, enabled]) => (
              <tr key={key}>
                <td><code className="inline-code">{key}</code></td>
                <td>
                  <Chip tone={enabled ? "moss" : "ghost"} size="sm">
                    {enabled ? "enabled" : "disabled"}
                  </Chip>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </DeskPage>
  );
}
