// crewday — JSON API types: workspace settings tree, setting
// catalog entries, and resolved-setting envelopes per entity.

export interface WorkspaceSettings {
  meta: {
    name: string;
    timezone: string;
    currency: string;
    country: string;
    default_locale: string;
  };
  defaults: Record<string, unknown>;
  policy: {
    approvals: { always_gated: string[]; configurable: string[] };
    danger_zone: string[];
  };
}

export interface SettingDefinition {
  key: string;
  label: string;
  type: "enum" | "int" | "bool";
  catalog_default: unknown;
  enum_values: string[] | null;
  override_scope: string;
  description: string;
  spec: string;
}

export interface ResolvedSetting {
  value: unknown;
  source: "workspace" | "property" | "employee" | "task" | "catalog";
}

export interface ResolvedSettingsPayload {
  entity_kind: string;
  entity_id: string;
  settings: Record<string, ResolvedSetting>;
}

export interface EntitySettingsPayload {
  overrides: Record<string, unknown>;
  resolved: Record<string, ResolvedSetting>;
}
