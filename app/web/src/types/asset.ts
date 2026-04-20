// crewday — JSON API types: assets, asset actions, asset documents,
// file extractions, knowledge-base hits, agent docs, and the asset
// detail payload.

import type { Property } from "./property";
import type { Task } from "./task";

export type AssetCondition = "new" | "good" | "fair" | "poor" | "needs_replacement";
export type AssetStatus = "active" | "in_repair" | "decommissioned" | "disposed";
export type AssetCategory = "climate" | "appliance" | "plumbing" | "pool" | "heating" | "outdoor" | "safety" | "security" | "vehicle" | "other";
export type DocumentKind = "manual" | "warranty" | "invoice" | "receipt" | "photo" | "certificate" | "contract" | "permit" | "insurance" | "other";

export interface AssetType {
  id: string;
  key: string;
  name: string;
  category: AssetCategory;
  icon_name: string;
  default_actions: {
    key: string;
    label: string;
    interval_days?: number;
    estimated_duration_minutes?: number;
  }[];
  default_lifespan_years: number | null;
}

export interface Asset {
  id: string;
  property_id: string;
  asset_type_id: string | null;
  name: string;
  area: string | null;
  condition: AssetCondition;
  status: AssetStatus;
  make: string | null;
  model: string | null;
  serial_number: string | null;
  installed_on: string | null;
  purchased_on: string | null;
  purchase_price_cents: number | null;
  purchase_currency: string | null;
  purchase_vendor: string | null;
  warranty_expires_on: string | null;
  expected_lifespan_years: number | null;
  guest_visible: boolean;
  guest_instructions: string | null;
  notes: string | null;
  qr_token: string;
}

export interface AssetAction {
  id: string;
  asset_id: string;
  key: string | null;
  label: string;
  interval_days: number | null;
  last_performed_at: string | null;
  next_due_on: string | null;
  linked_task_id: string | null;
  linked_schedule_id: string | null;
  description: string | null;
  estimated_duration_minutes: number | null;
}

export type FileExtractionStatus =
  | "pending"
  | "extracting"
  | "succeeded"
  | "failed"
  | "unsupported"
  | "empty";

export type FileExtractor =
  | "pypdf"
  | "pdfminer"
  | "python_docx"
  | "openpyxl"
  | "tesseract"
  | "llm_vision"
  | "passthrough";

export interface AssetDocument {
  id: string;
  asset_id: string | null;
  property_id: string;
  kind: DocumentKind;
  title: string;
  filename: string;
  size_kb: number;
  uploaded_at: string;
  expires_on: string | null;
  amount_cents: number | null;
  amount_currency: string | null;
  extraction_status: FileExtractionStatus;
  extracted_at: string | null;
}

export interface DocumentExtraction {
  document_id: string;
  status: FileExtractionStatus;
  extractor: FileExtractor | null;
  body_preview: string;
  page_count: number;
  token_count: number;
  has_secret_marker: boolean;
  last_error: string | null;
  extracted_at: string | null;
}

export interface KbHit {
  kind: "instruction" | "document";
  id: string;
  title: string;
  snippet: string;
  score: number;
  why: string;
}

export interface KbSearchResponse {
  results: KbHit[];
  total: number;
}

export interface KbDocPayload {
  kind: "instruction" | "document";
  id: string;
  title?: string;
  body?: string;
  page?: number;
  page_count?: number;
  more_pages?: boolean;
  source_ref?: Record<string, string | null>;
  extraction_status?: FileExtractionStatus;
  hint?: string;
}

export interface AgentDocSummary {
  slug: string;
  title: string;
  summary: string;
  roles: string[];
  updated_at: string;
}

export interface AgentDoc extends AgentDocSummary {
  body_md: string;
  capabilities: string[];
  version: number;
  is_customised: boolean;
  default_hash: string;
}

export interface AssetDetailPayload {
  asset: Asset;
  asset_type: AssetType | null;
  property: Property;
  actions: AssetAction[];
  documents: AssetDocument[];
  linked_tasks: Task[];
}
