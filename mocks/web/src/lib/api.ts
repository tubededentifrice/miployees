// Single fetch wrapper for the SPA. All network calls go through here
// so CSRF, credentials, and error shape stay consistent. No ad-hoc
// fetch() in components.

const CSRF_COOKIE = "crewday_csrf";

// Resolve a backend-bound absolute path against Vite's ``base``. When
// the SPA is served standalone at the origin root (``/``), the
// ``BASE_URL`` is ``/`` and paths pass through unchanged. When mounted
// under ``/mocks/`` in the dev-stack compose topology, every backend
// path gets the ``/mocks`` prefix so the sibling Vite proxy can route
// it to ``mocks-api`` (its ``/mocks/api`` entry strips the prefix
// before forwarding). External URLs (``http*``) and empty paths are
// returned as-is; non-slash-leading paths also pass through to leave
// relative URLs (``foo/bar``) alone.
export function withBase(path: string): string {
  if (!path || !path.startsWith("/")) return path;
  const base = import.meta.env.BASE_URL.replace(/\/$/, "");
  return base ? `${base}${path}` : path;
}

function readCookie(name: string): string | null {
  const target = name + "=";
  for (const chunk of document.cookie.split(";")) {
    const c = chunk.trim();
    if (c.startsWith(target)) return decodeURIComponent(c.slice(target.length));
  }
  return null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

type FetchOpts = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
};

export async function fetchJson<T>(path: string, opts: FetchOpts = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
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
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
  }
  if (method !== "GET") {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers["X-CSRF"] = csrf;
  }

  const res = await fetch(withBase(path), init);
  const text = await res.text();
  const body: unknown = text ? safeParse(text) : null;
  if (!res.ok) {
    const msg = (body as { detail?: string } | null)?.detail ?? res.statusText;
    throw new ApiError(msg || `HTTP ${res.status}`, res.status, body);
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
