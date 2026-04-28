// Single fetch wrapper for the SPA. Every network call goes through
// `fetchJson` so CSRF, credentials, workspace-slug URL rewriting,
// bearer-token injection, and error shape stay consistent. No ad-hoc
// `fetch()` calls in components.
//
// Spec refs: docs/specs/14-web-frontend.md §"Data layer" (every
// request built as `/w/${workspaceSlug}/api/v1/...`) and
// docs/specs/12-rest-api.md §"Errors" (RFC 7807 `problem+json`
// envelope we decode into `ApiError`).

const CSRF_COOKIE = "crewday_csrf";

// --- Pluggable providers -----------------------------------------------------
//
// `api.ts` is used in components, hooks, and plain functions; importing React
// context here would couple it to React. Instead we expose two pluggable
// getters that `WorkspaceContext` (and, later, the auth store from cd-kc7u)
// populate on mount. This keeps the wrapper framework-agnostic and testable
// without a React tree.

type Getter<T> = () => T;
type UnauthorizedHandler = (status: number, path: string) => void;

let workspaceSlugGetter: Getter<string | null> = () => null;
let authTokenGetter: Getter<string | null> = () => null;
let onUnauthorizedHandler: UnauthorizedHandler | null = null;

/**
 * Wire the active workspace-slug source. `WorkspaceProvider` calls this
 * exactly once on mount so URL building reads the current slug lazily —
 * a workspace switch is reflected on the very next request without a
 * full reload.
 */
export function registerWorkspaceSlugGetter(getter: Getter<string | null>): void {
  workspaceSlugGetter = getter;
}

export function getActiveWorkspaceSlug(): string | null {
  return workspaceSlugGetter();
}

/**
 * Wire the optional auth-token source. The passkey login / PAT flow
 * (cd-kc7u) registers this; until it ships the getter returns `null`
 * and `fetchJson` relies on the `__Host-crewday_session` cookie for
 * browser auth (`credentials: "same-origin"` below).
 */
export function registerAuthTokenGetter(getter: Getter<string | null>): void {
  authTokenGetter = getter;
}

/**
 * Wire a single 401 callback. The auth module (cd-kc7u) registers this
 * on mount: any 401 response from `fetchJson` invokes the handler
 * **and** still throws `ApiError` so TanStack Query / individual call
 * sites can also react. Centralising the redirect here means a stale
 * session detected mid-render or mid-mutation leads to one consistent
 * "kicked back to /login" experience instead of every screen handling
 * 401 itself.
 *
 * Pass `null` to clear the registration (used by tests on teardown).
 */
export function registerOnUnauthorized(handler: UnauthorizedHandler | null): void {
  onUnauthorizedHandler = handler;
}

/**
 * Test-only reset so unit tests don't leak state through the module-
 * level getters. Never call from product code.
 */
export function __resetApiProvidersForTests(): void {
  workspaceSlugGetter = () => null;
  authTokenGetter = () => null;
  onUnauthorizedHandler = null;
}

// --- URL building ------------------------------------------------------------

/**
 * Rewrite `/api/v1/...` paths to `/w/<slug>/api/v1/...` when a workspace
 * is active. Paths already scoped (`/w/...`, `/admin/...`) or absolute
 * URLs pass through untouched, so the admin shell and the bare-host
 * surfaces (signup, login, workspace picker) continue to work.
 *
 * Exported for the unit tests; product code calls `fetchJson` directly.
 */
export function resolveApiPath(path: string, slug: string | null = workspaceSlugGetter()): string {
  // Absolute URLs bypass workspace rewriting — some surfaces (dev
  // tools, third-party OAuth callbacks) need to hit non-relative endpoints.
  if (/^https?:\/\//i.test(path)) return path;
  // Admin surface has no slug; workspace-prefixed paths are already final.
  if (path.startsWith("/w/") || path.startsWith("/admin/")) return path;
  // Only rewrite tenant API paths; non-API relative paths (e.g.
  // /theme/set, /switch/manager) keep their bare shape.
  if (slug && path.startsWith("/api/v1/")) return `/w/${slug}${path}`;
  return path;
}

function readCookie(name: string): string | null {
  const target = name + "=";
  for (const chunk of document.cookie.split(";")) {
    const c = chunk.trim();
    if (c.startsWith(target)) {
      const raw = c.slice(target.length);
      // A malformed percent-encoded cookie (`%zz`) would crash every
      // non-GET request — surface the raw value so at worst the CSRF
      // header mismatches and the server rejects the request, rather
      // than the whole fetch wrapper throwing.
      try {
        return decodeURIComponent(raw);
      } catch {
        return raw;
      }
    }
  }
  return null;
}

// --- Error shape -------------------------------------------------------------

/**
 * Minimal shape of the RFC 7807 `problem+json` body the server emits
 * (see `app/api/errors.py`). Fields are optional because we also
 * surface non-JSON error bodies through `ApiError`.
 */
export interface ProblemDetail {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  instance?: string;
  errors?: ReadonlyArray<{ loc?: readonly (string | number)[]; msg?: string; type?: string }>;
  // Approval pipeline extension — see spec §11.
  approval_request_id?: string;
  expires_at?: string;
  // Any other extension fields the server attached.
  [key: string]: unknown;
}

/**
 * Thrown for every non-2xx response. Mirrors the CLI's shape so
 * downstream toast / banner components can switch on `error.type`
 * without parsing the body a second time.
 *
 * `body` carries the parsed JSON (if the response was JSON) or the
 * raw text (if it wasn't); `problem` is the same body narrowed to
 * the `ProblemDetail` shape when JSON parsed successfully.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly problem: ProblemDetail | null;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.problem = isProblemDetail(body) ? body : null;
  }

  /** Short `type` key from the RFC 7807 body (e.g. `"validation"`). */
  get type(): string | null {
    const raw = this.problem?.type;
    if (typeof raw !== "string") return null;
    // Spec §12: the full `type` URI has a `crewday.dev/errors/` prefix;
    // strip it so callers compare on the short name. Unknown URIs pass
    // through untouched.
    const m = raw.match(/\/errors\/([^/]+)$/);
    return m?.[1] ?? raw;
  }

  /** RFC 7807 `title` (short human summary). */
  get title(): string | null {
    return this.problem?.title ?? null;
  }

  /** RFC 7807 `detail` (longer human line). */
  get detail(): string | null {
    return this.problem?.detail ?? null;
  }

  /** RFC 7807 `errors[]` extension (field-level validation). */
  get fieldErrors(): ReadonlyArray<{ loc?: readonly (string | number)[]; msg?: string; type?: string }> {
    return this.problem?.errors ?? [];
  }
}

function isProblemDetail(body: unknown): body is ProblemDetail {
  return typeof body === "object" && body !== null && !Array.isArray(body);
}

// --- Request options ---------------------------------------------------------

type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface FetchOpts {
  method?: HttpMethod;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

/**
 * Perform a JSON request against the crewday API.
 *
 * - URL is rewritten through `resolveApiPath` so bare `/api/v1/...`
 *   paths become `/w/<slug>/api/v1/...` when a workspace is active.
 * - Cookies ride along (`credentials: "same-origin"`) so browser auth
 *   via `__Host-crewday_session` (spec §15) works unchanged.
 * - When the auth-token getter resolves to a non-null string, an
 *   `Authorization: Bearer <token>` header is added (spec §03
 *   "Personal access tokens / Usage"). Cookie and bearer coexist —
 *   bearer wins server-side.
 * - CSRF token (spec §15) is picked from the `crewday_csrf` cookie
 *   and echoed as `X-CSRF` for every non-GET.
 * - Non-2xx responses throw `ApiError` with the parsed RFC 7807 body.
 */
export async function fetchJson<T>(path: string, opts: FetchOpts = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const url = resolveApiPath(path);

  const headers: Record<string, string> = { Accept: "application/json", ...(opts.headers ?? {}) };
  const init: RequestInit = {
    method,
    credentials: "same-origin",
    headers,
    signal: opts.signal,
  };

  if (opts.body !== undefined) {
    if (opts.body instanceof FormData) {
      // Let the browser set the multipart boundary on Content-Type.
      init.body = opts.body;
    } else {
      // Only default to JSON when the caller hasn't supplied their own
      // Content-Type (e.g. a rare `text/plain` ping).
      if (!headers["Content-Type"]) headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
  }

  if (method !== "GET") {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers["X-CSRF"] = csrf;
  }

  const token = authTokenGetter();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(url, init);
  const text = await res.text();
  const body: unknown = text ? safeParse(text) : null;
  if (!res.ok) {
    const message = pickMessage(body, res.statusText, res.status);
    if (res.status === 401 && onUnauthorizedHandler) {
      // Fire-and-forget: the handler clears the auth store + navigates
      // to /login. Any throw inside the handler is intentionally
      // swallowed (logged) because it must not mask the underlying
      // 401 the caller is about to see.
      try {
        onUnauthorizedHandler(res.status, url);
      } catch (err) {
        // eslint-disable-next-line no-console -- last-resort visibility for an auth-handler bug.
        console.error("onUnauthorized handler threw", err);
      }
    }
    throw new ApiError(message, res.status, body);
  }
  return body as T;
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function pickMessage(body: unknown, statusText: string, status: number): string {
  if (isProblemDetail(body)) {
    if (typeof body.detail === "string" && body.detail) return body.detail;
    if (typeof body.title === "string" && body.title) return body.title;
  }
  if (typeof body === "string" && body) return body;
  return statusText || `HTTP ${status}`;
}
