// Helper around `GET /api/v1/expenses` (the cd-t6y2 list endpoint).
//
// The server returns the cursor-paginated envelope from spec §12 "Time,
// payroll, expenses":
//
//     { data: ExpenseClaimPayload[], next_cursor: string | null, has_more: boolean }
//
// Every SPA call previously read the response as a bare `Expense[]`,
// so the cast silently succeeded but the page hit the empty-list /
// "Failed to load." branch. This helper centralises the unwrap (and a
// future payload→`Expense` shape transform) so individual call sites
// just request a list and get back the same `Expense[]` they always
// did, but with the envelope honoured at the boundary.
//
// The shape returned by the helper today is identical to the API
// payload — the SPA's `Expense` type was migrated to mirror the
// `ExpenseClaimPayload` schema as part of cd-75cv. If the wire shape
// and the SPA shape ever diverge again, the per-row mapping should
// land in `mapExpenseClaimPayload` below so all callers pick it up.
//
// References:
// - `app/api/v1/expenses.py:498` — `list_expense_claims_route` and
//   `ExpenseClaimListResponse`.
// - `docs/specs/12-rest-api.md` §"Time, payroll, expenses".

import { fetchJson } from "@/lib/api";
import type { Expense, ExpenseStatus } from "@/types/expense";

/**
 * Cursor-paginated list response shape emitted by every `/api/v1/`
 * endpoint that uses `app.api.pagination` (spec §12).
 *
 * Generic over the row type so a future helper for `/expenses/pending`
 * (`ExpenseClaimPendingListResponse`) can reuse the same envelope
 * without redeclaring its own.
 */
export interface ListEnvelope<T> {
  data: T[];
  next_cursor: string | null;
  has_more: boolean;
}

/**
 * Wire shape of a single row returned by `GET /api/v1/expenses` /
 * `GET /api/v1/expenses/{id}` / `POST /api/v1/expenses` (the
 * `ExpenseClaimPayload` Pydantic model in `app/api/v1/expenses.py`).
 *
 * Kept distinct from `Expense` so the *transport* shape stays exact
 * even if the SPA's domain type ever extends it with derived fields
 * (e.g. a client-side "claimant_name" decoration once a roster
 * endpoint exists).
 */
export interface ExpenseClaimPayload {
  id: string;
  workspace_id: string;
  work_engagement_id: string;
  vendor: string;
  /** ISO-8601 UTC. */
  purchased_at: string;
  currency: string;
  total_amount_cents: number;
  category: string;
  property_id: string | null;
  note_md: string;
  state: ExpenseStatus;
  /** ISO-8601 UTC; null while the claim is still in `draft`. */
  submitted_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note_md: string | null;
  /** ISO-8601 UTC. */
  created_at: string;
  /** ISO-8601 UTC; non-null when the row is soft-deleted. */
  deleted_at: string | null;
  attachments: ExpenseAttachmentPayload[];
}

export interface ExpenseAttachmentPayload {
  id: string;
  claim_id: string;
  blob_hash: string;
  kind: string;
  pages: number | null;
  created_at: string;
}

/**
 * Project a single wire row into the SPA's `Expense` shape.
 *
 * Today the `Expense` interface mirrors `ExpenseClaimPayload` 1:1, so
 * the body is a structural copy. The function exists for two reasons:
 *
 * 1. **Forward compatibility.** When the SPA grows derived fields
 *    (e.g. a `claimant_name` decoration once a roster endpoint
 *    exists), this is the one place to wire them up — every consumer
 *    routes through `fetchExpenseClaims` / `mapExpenseClaimPayload`.
 * 2. **Explicit boundary.** Returning a freshly built object instead
 *    of casting the raw payload makes type-system narrowing precise:
 *    `state` lands as a real `ExpenseStatus` literal rather than a
 *    `string` the consumer has to re-narrow.
 */
export function mapExpenseClaimPayload(payload: ExpenseClaimPayload): Expense {
  return {
    id: payload.id,
    workspace_id: payload.workspace_id,
    work_engagement_id: payload.work_engagement_id,
    vendor: payload.vendor,
    purchased_at: payload.purchased_at,
    currency: payload.currency,
    total_amount_cents: payload.total_amount_cents,
    category: payload.category,
    property_id: payload.property_id,
    note_md: payload.note_md,
    state: payload.state,
    submitted_at: payload.submitted_at,
    decided_by: payload.decided_by,
    decided_at: payload.decided_at,
    decision_note_md: payload.decision_note_md,
    created_at: payload.created_at,
    deleted_at: payload.deleted_at,
    attachments: payload.attachments.map((a) => ({
      id: a.id,
      claim_id: a.claim_id,
      blob_hash: a.blob_hash,
      kind: a.kind,
      pages: a.pages,
      created_at: a.created_at,
    })),
  };
}

/**
 * Build the query string for `GET /api/v1/expenses`.
 *
 * Server-side query params (per `list_expense_claims_route`):
 * - `user_id` — list someone else's claims (requires `expenses.approve`).
 * - `state` — narrow by lifecycle state.
 * - `cursor` / `limit` — pagination.
 *
 * Returns an empty string for the no-param case so the helper can be
 * concatenated unconditionally without producing a stray `?`.
 */
function buildListQuery(opts: ListExpenseClaimsOptions): string {
  const params = new URLSearchParams();
  if (opts.userId !== undefined) params.set("user_id", opts.userId);
  if (opts.state !== undefined) params.set("state", opts.state);
  if (opts.cursor !== undefined) params.set("cursor", opts.cursor);
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export interface ListExpenseClaimsOptions {
  /**
   * Target a different user's claims. Omit (the default) to read the
   * caller's own claims. Cross-user reads require `expenses.approve`
   * server-side; the SPA surfaces the 403 envelope unchanged.
   */
  userId?: string;
  /** Lifecycle filter — see `ExpenseStatus`. */
  state?: ExpenseStatus;
  /** Opaque cursor from a previous page's `next_cursor`. */
  cursor?: string;
  /** Page size. Server caps it (default + max in `app.api.pagination`). */
  limit?: number;
}

/**
 * Fetch one page of expense claims from `GET /api/v1/expenses`.
 *
 * Returns the full envelope so the caller can drive pagination
 * (`next_cursor` + `has_more`). Use `fetchAllExpenseClaims` for the
 * common "load every page into one array" need.
 */
export async function fetchExpenseClaimsPage(
  opts: ListExpenseClaimsOptions = {},
): Promise<ListEnvelope<Expense>> {
  const url = `/api/v1/expenses${buildListQuery(opts)}`;
  const envelope = await fetchJson<ListEnvelope<ExpenseClaimPayload>>(url);
  return {
    data: envelope.data.map(mapExpenseClaimPayload),
    next_cursor: envelope.next_cursor,
    has_more: envelope.has_more,
  };
}

/**
 * Fetch every page of `GET /api/v1/expenses` and return the flattened
 * `Expense[]`.
 *
 * The manager approvals desk and the worker "Recent expenses" panel
 * render against the full list (filtered client-side by status), so a
 * single-page fetch would silently drop older claims past the page
 * boundary. We follow `next_cursor` until the server signals the
 * walk is complete.
 *
 * Bounded by `MAX_PAGES` so a server bug or infinite cursor loop
 * cannot wedge a tab. The cap is generous (50 pages × default limit
 * = thousands of claims); a workspace that has more is well past the
 * UI's "scrollable list" threshold and should be paginating in the
 * UI itself, not auto-loading every page.
 */
const MAX_PAGES = 50;

export async function fetchAllExpenseClaims(
  opts: ListExpenseClaimsOptions = {},
): Promise<Expense[]> {
  const out: Expense[] = [];
  let cursor: string | undefined = opts.cursor;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const envelope = await fetchExpenseClaimsPage({ ...opts, cursor });
    out.push(...envelope.data);
    if (!envelope.has_more || envelope.next_cursor === null) return out;
    cursor = envelope.next_cursor;
  }
  // Hit the cap — surface the page count we drained instead of
  // silently truncating. The manager desk explicitly wants every
  // claim to filter on, so a partial read is wrong.
  throw new Error(
    `fetchAllExpenseClaims: exceeded ${MAX_PAGES} pages while walking /api/v1/expenses`,
  );
}
