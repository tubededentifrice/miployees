// crewday — singleton auth store.
//
// Why a hand-rolled store instead of Zustand or Redux:
// - The dependency budget for the SPA explicitly excludes a state
//   library (see app/web/package.json). React context drives every
//   other cross-cutting concern (theme, role, workspace).
// - Auth, however, is read from non-React seams (`api.ts` 401
//   handler, the passkey ceremony helpers) so it can't live behind a
//   provider that needs `useContext`. A module-scoped store + tiny
//   subscription protocol matches what `react-router` and TanStack
//   Query do for the same reason.
//
// The store carries:
//
// - `user` — the authenticated identity (from `GET /api/v1/auth/me`).
//   Null while we're loading, after logout, or before the first
//   probe. The presence of a user is the source of truth for
//   "logged in"; we never derive it from cookie presence (we can't —
//   `__Host-crewday_session` is `HttpOnly`).
// - `token` — an optional bearer token for non-cookie auth (PAT,
//   delegated token, or future OAuth/native bridge). `api.ts`'s
//   `registerAuthTokenGetter` reads this on every fetch. The default
//   browser flow leaves it `null` and rides the session cookie.
// - `status` — three-state load gate (`'loading' | 'authenticated' |
//   'unauthenticated'`). Components branch on this to decide
//   between spinner / content / `<Navigate>`. Starts at `'loading'`
//   so the very first render of `<RequireAuth>` defers to the
//   `/auth/me` probe instead of bouncing straight to `/login`.
//
// Token persistence: `sessionStorage` only (cleared when the tab
// closes), keyed under `crewday_auth_token`. We deliberately avoid
// `localStorage` because:
//   1. The cookie does the long-lived persistence work for the
//      default browser flow.
//   2. PAT-style tokens that *do* go through this seam are usually
//      short-lived "give the dev tools an hour" affairs — surviving
//      a tab close is a footgun, not a feature.

import type { AuthMe } from "./types";

export interface AuthState {
  status: "loading" | "authenticated" | "unauthenticated";
  user: AuthMe | null;
  token: string | null;
}

const TOKEN_STORAGE_KEY = "crewday_auth_token";

const INITIAL_STATE: AuthState = {
  status: "loading",
  user: null,
  token: null,
};

type Listener = (state: AuthState) => void;

let state: AuthState = readInitialState();
const listeners = new Set<Listener>();

function readInitialState(): AuthState {
  // `sessionStorage` is unavailable in some embedded WebViews and
  // (notably) inside SSR-style harnesses. Treat any access exception
  // as "no persisted token" and start clean — the user can log in
  // again, vs. crashing the entire shell on an unrelated `localStorage
  // is null` quirk.
  const token = readPersistedToken();
  return {
    status: "loading",
    user: null,
    token,
  };
}

function readPersistedToken(): string | null {
  try {
    if (typeof sessionStorage === "undefined") return null;
    const raw = sessionStorage.getItem(TOKEN_STORAGE_KEY);
    return raw && raw.length > 0 ? raw : null;
  } catch {
    return null;
  }
}

function persistToken(token: string | null): void {
  try {
    if (typeof sessionStorage === "undefined") return;
    if (token === null) sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    else sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
  } catch {
    // Storage may be wedged (Safari private mode, quota exceeded).
    // The in-memory `state.token` is still authoritative for the
    // current tab — persistence is a nice-to-have for page reloads.
  }
}

function emit(): void {
  // Iterate over a snapshot so a listener that unsubscribes mid-emit
  // doesn't break the iteration order. Cheap because the listener
  // count is bounded by the number of `useAuth()` callers + the
  // 401 handler + the api-token getter (small constant).
  for (const l of [...listeners]) {
    l(state);
  }
}

/** Read the current auth state synchronously. Avoid in components — use `useAuth()`. */
export function getAuthState(): AuthState {
  return state;
}

/** Subscribe to state changes. Returns an unsubscribe function. */
export function subscribeAuth(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Replace the current state. Internal; product code goes through the
 * named action helpers below so the transitions are auditable from
 * one file.
 */
function setState(patch: Partial<AuthState>): void {
  const next: AuthState = { ...state, ...patch };
  // Skip the emit when nothing meaningful changed — keeps React
  // re-renders quiet when, say, a 401 fires mid-logout and the store
  // is already `unauthenticated`.
  if (
    next.status === state.status
    && next.user === state.user
    && next.token === state.token
  ) {
    return;
  }
  state = next;
  emit();
}

// ── Action helpers ────────────────────────────────────────────────

/**
 * Mark the store as actively probing auth state — used by the
 * initial `/auth/me` request and by `refreshAuth()`. Does not clear
 * the existing user, so a re-probe doesn't blank out the chrome
 * mid-flight.
 */
export function setLoading(): void {
  setState({ status: "loading" });
}

/** Apply a successful login or refresh. */
export function setAuthenticated(user: AuthMe, token: string | null = state.token): void {
  if (token !== state.token) persistToken(token);
  setState({ status: "authenticated", user, token });
}

/** Apply an explicit logout or a 401 kick. */
export function setUnauthenticated(): void {
  if (state.token !== null) persistToken(null);
  setState({ status: "unauthenticated", user: null, token: null });
}

/**
 * Set the bearer token without changing the authentication status —
 * used by PAT-style flows that mint a token outside the passkey
 * ceremony. The next request will pick up the new value via
 * `registerAuthTokenGetter`.
 */
export function setAuthToken(token: string | null): void {
  if (token === state.token) return;
  persistToken(token);
  setState({ token });
}

// ── Test seam ─────────────────────────────────────────────────────

/**
 * Reset the store to its initial shape. Vitest tests call this in
 * `beforeEach` so module-scoped state never leaks between cases.
 * Never call from product code.
 */
export function __resetAuthStoreForTests(): void {
  // Drop subscribers so a stale listener from a previous test (e.g. a
  // mounted component that wasn't unmounted) can't observe the reset
  // and re-render in the next test's render tree.
  listeners.clear();
  try {
    if (typeof sessionStorage !== "undefined") {
      sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    // Same fallback posture as `persistToken` — storage may be
    // wedged; the in-memory reset below still applies.
  }
  state = { ...INITIAL_STATE };
}
