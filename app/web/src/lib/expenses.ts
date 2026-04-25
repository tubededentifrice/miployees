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
 * - `mine` — explicit "caller's own claims only" form; pins
 *   `user_id=<caller>` and skips the manager-cap branch so a worker
 *   without `expenses.approve` is never 403'd. Mutually exclusive
 *   with `user_id` — combining them surfaces 422
 *   `mine_user_id_conflict`.
 * - `user_id` — list someone else's claims (requires `expenses.approve`).
 * - `state` — narrow by lifecycle state.
 * - `cursor` / `limit` — pagination.
 *
 * Returns an empty string for the no-param case so the helper can be
 * concatenated unconditionally without producing a stray `?`.
 */
function buildListQuery(opts: ListExpenseClaimsOptions): string {
  const params = new URLSearchParams();
  if (opts.mine === true) params.set("mine", "true");
  if (opts.userId !== undefined) params.set("user_id", opts.userId);
  if (opts.state !== undefined) params.set("state", opts.state);
  if (opts.cursor !== undefined) params.set("cursor", opts.cursor);
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export interface ListExpenseClaimsOptions {
  /**
   * Explicit "my own claims only" filter. When `true`, the server
   * pins the listing to the caller and skips the `expenses.approve`
   * gate — the worker-side recent-expenses panel uses this so it
   * never trips the manager-cap branch. Mutually exclusive with
   * `userId`; combining them yields 422 `mine_user_id_conflict`.
   *
   * Omitting both still defaults to "caller's own claims" on the
   * server (per `app.domain.expenses.claims.list_for_user`), but
   * passing `mine: true` makes the intent explicit and survives
   * future server-side default changes.
   */
  mine?: boolean;
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

// ---------------------------------------------------------------------------
// Create
// ---------------------------------------------------------------------------

/**
 * Wire shape of `POST /api/v1/expenses` — 1:1 with the server's
 * `ExpenseClaimCreate` Pydantic model
 * (`app/domain/expenses/claims.py:417`).
 *
 * Required at the boundary except `property_id` (optional pin) and
 * `note_md` (defaults to `""`). `purchased_at` MUST be a timezone-
 * aware ISO-8601 string — the DTO rejects naive timestamps.
 */
export interface ExpenseClaimCreatePayload {
  work_engagement_id: string;
  vendor: string;
  /** ISO-8601 with timezone (`Z` or `±HH:MM`); naive strings 422. */
  purchased_at: string;
  currency: string;
  /** Strictly positive integer — the server CHECK is `> 0`. */
  total_amount_cents: number;
  category: string;
  property_id?: string;
  note_md?: string;
}

/**
 * Inputs the worker form collects, before currency / cents / ISO
 * normalisation. Kept separate from the wire type so the projection
 * step in `buildExpenseClaimCreatePayload` is the sole place that
 * encodes the conversion rules.
 */
export interface ExpenseClaimFormInput {
  work_engagement_id: string;
  vendor: string;
  /** `YYYY-MM-DD` from a native date picker (browser locale). */
  purchased_on: string;
  /** Decimal string from the amount input, e.g. "12.50". */
  amount: string;
  currency: string;
  category: string;
  /** Empty string means "unset"; the wire field is then omitted. */
  property_id: string;
  note_md: string;
}

/**
 * Project a worker form snapshot into the `ExpenseClaimCreate` body
 * shape.
 *
 * - `vendor` is trimmed; an empty trim throws so the caller surfaces
 *   the validation locally rather than forwarding a 422.
 * - `purchased_on` (the date-picker's `YYYY-MM-DD`) is interpreted as
 *   the worker's *local-noon* and converted to a UTC-`Z` ISO string.
 *   The server rejects naive timestamps, so the trailing `Z` is the
 *   contract; anchoring on local-noon (rather than local-midnight)
 *   keeps the receipt's calendar date stable when the row is read
 *   back via `Date#toLocaleDateString` from any timezone within
 *   ±12 h of the worker's, and avoids the DST-edge case where
 *   local-midnight crosses into the previous calendar day on the
 *   server.
 * - `amount` is parsed as a decimal and multiplied by 100; rounding
 *   is `Math.round` so a `0.005` rounding artefact never silently
 *   shaves a cent off the worker. Non-positive / non-finite amounts
 *   throw — the server's `gt=0` would otherwise fire 422.
 * - `property_id` is omitted when blank ("no property pin"); the
 *   server treats omission and `null` identically.
 * - `note_md` is forwarded verbatim (empty string is fine — the
 *   server defaults to `""` and we keep the column NOT NULL contract
 *   honest by sending what the worker typed).
 */
export function buildExpenseClaimCreatePayload(
  input: ExpenseClaimFormInput,
): ExpenseClaimCreatePayload {
  const vendor = input.vendor.trim();
  if (!vendor) {
    throw new Error("vendor is required");
  }
  const eng = input.work_engagement_id.trim();
  if (!eng) {
    throw new Error("work_engagement_id is required");
  }
  const purchased_at = isoFromDateInput(input.purchased_on);
  const total_amount_cents = centsFromAmountInput(input.amount);
  const currency = input.currency.trim().toUpperCase();
  if (currency.length !== 3) {
    throw new Error("currency must be a 3-letter ISO code");
  }

  const payload: ExpenseClaimCreatePayload = {
    work_engagement_id: eng,
    vendor,
    purchased_at,
    currency,
    total_amount_cents,
    category: input.category,
    note_md: input.note_md,
  };
  const propertyId = input.property_id.trim();
  if (propertyId) {
    payload.property_id = propertyId;
  }
  return payload;
}

/**
 * Convert a date picker's `YYYY-MM-DD` value into the `Z`-suffixed
 * ISO string the server expects.
 *
 * The picker collects a calendar date with no time-of-day; the wire
 * needs an aware datetime. We anchor on **local-noon** of the picked
 * date so:
 *
 * 1. Round-tripping through `new Date(iso).toLocaleDateString(...)`
 *    in the worker's timezone always lands on the same calendar
 *    date, even at DST boundaries (local-midnight is on the edge
 *    and can flip to the previous day; local-noon is well inside).
 * 2. Any other-timezone viewer within ±12 h of the worker's wall
 *    clock — i.e. the entire useful tz band for a single workspace
 *    — also reads back the same calendar date.
 *
 * `new Date(year, month-1, day, 12)` constructs the local-noon
 * `Date`; `.toISOString()` then serialises in UTC so the server
 * sees the canonical aware form.
 */
function isoFromDateInput(value: string): string {
  const m = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) {
    throw new Error(`purchased_on must be YYYY-MM-DD; got ${value}`);
  }
  const year = Number(m[1]);
  const month = Number(m[2]);
  const day = Number(m[3]);
  const local = new Date(year, month - 1, day, 12, 0, 0, 0);
  if (Number.isNaN(local.getTime())) {
    throw new Error(`purchased_on is not a valid date: ${value}`);
  }
  return local.toISOString();
}

/**
 * Parse the decimal-string amount input into a strictly-positive
 * integer cent count. Anything non-finite or `<= 0` throws — the
 * server's `total_amount_cents > 0` check would otherwise fire 422
 * and the worker would lose context on which field was wrong.
 */
function centsFromAmountInput(value: string): number {
  const trimmed = value.trim();
  if (!trimmed) {
    throw new Error("amount is required");
  }
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) {
    throw new Error(`amount is not a number: ${value}`);
  }
  const cents = Math.round(parsed * 100);
  if (cents <= 0) {
    throw new Error("amount must be strictly positive");
  }
  return cents;
}

// ---------------------------------------------------------------------------
// Active engagement lookup
// ---------------------------------------------------------------------------

/**
 * Wire shape of one `/api/v1/work_engagements` row (the
 * `WorkEngagementResponse` Pydantic model). Only the fields the SPA
 * uses are pinned here — adding more later doesn't require a wire-
 * shape change in this module.
 */
interface WorkEngagementRow {
  id: string;
  user_id: string;
  workspace_id: string;
  archived_on: string | null;
}

interface WorkEngagementListEnvelope {
  data: WorkEngagementRow[];
  next_cursor: string | null;
  has_more: boolean;
}

/**
 * Resolve the caller's active engagement in the current workspace, or
 * `null` when none exists.
 *
 * Used by the worker expense-submit flow — `POST /expenses` requires
 * a `work_engagement_id` bound to the caller, and the SPA has no
 * other place to source it (the `/me` payload deliberately omits
 * engagement state so a user with multiple workspaces doesn't have
 * to pay the join cost on every page load).
 *
 * Picks the first non-archived engagement returned by
 * `GET /work_engagements?user_id=<me>&active=true`. The schema's
 * partial UNIQUE on `(user_id, workspace_id) WHERE archived_on IS
 * NULL` (§02) guarantees at most one such row per worker per
 * workspace, so "first" is also "the only one". Returning `null`
 * (rather than throwing) lets the caller render a friendly "no
 * active engagement" message instead of an opaque error.
 */
export async function fetchActiveEngagementId(
  userId: string,
): Promise<string | null> {
  const params = new URLSearchParams({ user_id: userId, active: "true" });
  const env = await fetchJson<WorkEngagementListEnvelope>(
    `/api/v1/work_engagements?${params.toString()}`,
  );
  for (const row of env.data) {
    if (row.archived_on === null) return row.id;
  }
  return null;
}
