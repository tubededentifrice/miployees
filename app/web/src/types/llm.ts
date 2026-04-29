// crewday — JSON API types: LLM providers, models, provider-model
// graph, capabilities, assignments, prompt templates, per-workspace
// usage envelope, and agent preferences / agent docs metadata.

export interface ModelAssignment {
  capability: string;
  description: string;
  provider: string;
  model_id: string;
  enabled: boolean;
  daily_budget_usd: number;
  spent_24h_usd: number;
  calls_24h: number;
}

export interface LLMCall {
  at: string;
  capability: string;
  model_id: string;
  input_tokens: number;
  output_tokens: number;
  cost_cents: number;
  latency_ms: number;
  status: "ok" | "error" | "redacted_block";
  // §11 "Cost tracking" — chain metadata; nullable on legacy rows.
  assignment_id?: string | null;
  provider_model_id?: string | null;
  prompt_template_id?: string | null;
  prompt_version?: number | null;
  fallback_attempts?: number;
  raw_response_available?: boolean;
}

// §11 — provider / model / provider-model graph shapes.

export type LlmProviderType = "openrouter" | "openai_compatible" | "fake";
export type LlmApiKeyStatus = "present" | "missing" | "rotating";
export type LlmPriceSource = "openrouter" | "manual" | "";
export type LlmPriceSourceOverride = "" | "none" | "openrouter";
export type LlmReasoningEffort = "" | "low" | "medium" | "high";

export interface LlmProvider {
  id: string;
  name: string;
  provider_type: LlmProviderType;
  endpoint: string;
  api_key_ref: string | null;
  api_key_status: LlmApiKeyStatus;
  default_model: string | null;
  requests_per_minute: number;
  timeout_s: number;
  priority: number;
  is_enabled: boolean;
  provider_model_count: number;
}

export interface LlmModel {
  id: string;
  canonical_name: string;
  display_name: string;
  vendor: string;
  capabilities: string[];
  context_window: number | null;
  max_output_tokens: number | null;
  price_source: LlmPriceSource;
  price_source_model_id: string | null;
  is_active: boolean;
  notes: string | null;
  provider_model_count: number;
}

export interface LlmProviderModel {
  id: string;
  provider_id: string;
  model_id: string;
  api_model_id: string;
  input_cost_per_million: number;
  output_cost_per_million: number;
  max_tokens_override: number | null;
  temperature_override: number | null;
  supports_system_prompt: boolean;
  supports_temperature: boolean;
  reasoning_effort: LlmReasoningEffort;
  price_source_override: LlmPriceSourceOverride;
  price_last_synced_at: string | null;
  is_enabled: boolean;
}

export interface LlmAssignment {
  id: string;
  capability: string;
  description: string;
  priority: number;
  provider_model_id: string;
  max_tokens: number | null;
  temperature: number | null;
  extra_api_params: Record<string, unknown>;
  required_capabilities: string[];
  is_enabled: boolean;
  last_used_at: string | null;
  spend_usd_30d: number;
  calls_30d: number;
}

export interface LlmCapabilityEntry {
  key: string;
  description: string;
  required_capabilities: string[];
}

export interface LlmCapabilityInheritance {
  capability: string;
  inherits_from: string;
}

export interface LlmAssignmentIssue {
  assignment_id: string;
  capability: string;
  missing_capabilities: string[];
}

export interface LlmPromptTemplate {
  id: string;
  capability: string;
  name: string;
  version: number;
  is_active: boolean;
  is_customised: boolean;
  default_hash: string;
  updated_at: string;
  revisions_count: number;
  preview: string;
}

export interface LlmGraphPayload {
  providers: LlmProvider[];
  models: LlmModel[];
  provider_models: LlmProviderModel[];
  capabilities: LlmCapabilityEntry[];
  inheritance: LlmCapabilityInheritance[];
  assignments: LlmAssignment[];
  assignment_issues: LlmAssignmentIssue[];
  totals: {
    spend_usd_30d: number;
    calls_30d: number;
    provider_count: number;
    model_count: number;
    capability_count: number;
    unassigned_capabilities: string[];
  };
}

export interface LlmSyncPricingResult {
  started_at: string;
  deltas: {
    provider_model_id: string;
    api_model_id: string;
    input_before: number;
    input_after: number;
    output_before: number;
    output_after: number;
    status: "updated" | "unchanged" | "pinned" | "error";
  }[];
  updated: number;
  skipped: number;
  errors: number;
}

// §11 — Workspace usage budget (manager-visible shape).
// Deliberately percent-only: no dollars, no tokens, no reset date.
// Dollars live on the LLM settings page for the operator audience;
// workers and managers only see the envelope usage here.
export interface WorkspaceUsage {
  percent: number;
  paused: boolean;
  window_label: string;
}

// §11 — Agent preferences. Free-form Markdown stacked into the LLM
// system prompt; three layers (workspace / property / user).
export type AgentPreferenceScope = "workspace" | "property" | "user";

export interface AgentPreference {
  scope_kind: AgentPreferenceScope;
  scope_id: string;
  body_md: string;
  token_count: number;
  updated_by_user_id: string | null;
  updated_at: string | null;
  writable: boolean;
  soft_cap: number;
  hard_cap: number;
  blocked_actions: string[];
  default_approval_mode: "bypass" | "auto" | "strict";
}

export interface AgentPreferenceRevision {
  revision_number: number;
  body_md: string;
  saved_by_user_id: string;
  saved_at: string;
  save_note: string | null;
}

export interface AgentPreferenceRevisionsPayload {
  scope_kind: AgentPreferenceScope;
  scope_id: string;
  revisions: AgentPreferenceRevision[];
}
