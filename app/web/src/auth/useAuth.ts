// crewday — `useAuth()` hook + auth-store wiring.
//
// Wraps the module-scoped `authStore` in a React-friendly contract:
//
// - `useAuth()` subscribes to store changes via `useSyncExternalStore`,
//   so a 401 fired from anywhere in the app re-renders every consumer
//   with `status: 'unauthenticated'`.
// - `useAuthBootstrap()` is the one-shot effect that registers the
//   token getter + 401 handler with `api.ts`, then probes
//   `/api/v1/auth/me` to discover whether the cookie is still valid.
//   `<AuthProvider>` mounts it once at the app root.
// - Login / logout helpers run the passkey ceremony (or the explicit
//   logout endpoint) and update the store.
//
// SSE teardown on logout is handled by the SseContext effect: it
// subscribes to the auth store directly and tears the live
// `EventSource` down whenever `status !== 'authenticated'`. We do
// not poke `EventSource` from this module — that would couple two
// unrelated lifecycles.

import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  ApiError,
  fetchJson,
  registerAuthTokenGetter,
  registerOnUnauthorized,
} from "@/lib/api";
import {
  __resetAuthStoreForTests as _resetForTests,
  getAuthState,
  setAuthenticated,
  setLoading,
  setUnauthenticated,
  subscribeAuth,
  type AuthState,
} from "./authStore";
import {
  PasskeyCancelledError,
  PasskeyUnsupportedError,
  runPasskeyLoginCeremony,
} from "./passkey";
import { createOnUnauthorized } from "./onUnauthorized";
import type { AuthMe, PasskeyLoginFinish } from "./types";

export type { AuthState } from "./authStore";

export interface UseAuthApi extends AuthState {
  /** True iff the store is currently in the `authenticated` state. */
  isAuthenticated: boolean;
  /** Drive the passkey ceremony end-to-end. Throws on cancellation / failure. */
  loginWithPasskey: () => Promise<PasskeyLoginFinish>;
  /** Tear down the session (server logout + local store). Idempotent. */
  logout: () => Promise<void>;
  /** Re-probe `/auth/me`; useful after a workspace invite redemption. */
  refresh: () => Promise<void>;
}

// `useSyncExternalStore` requires a stable snapshot reference between
// renders or React 19 will warn about an infinite render loop. The
// store already returns the same object until something changes, so
// `getAuthState` itself is the snapshot getter.
function subscribe(listener: () => void): () => void {
  return subscribeAuth(listener);
}

/**
 * Read the current auth state from any component. Re-renders the
 * caller on every store change. Side-effect-free — does not probe
 * `/auth/me`; mount `<AuthProvider>` (or call `useAuthBootstrap()`
 * yourself) to drive the initial probe.
 */
export function useAuth(): UseAuthApi {
  const state = useSyncExternalStore(subscribe, getAuthState, getAuthState);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const loginWithPasskey = useCallback(async (): Promise<PasskeyLoginFinish> => {
    setLoading();
    try {
      const result = await runPasskeyLoginCeremony({ mediation: "required" });
      // Cookie is now set by the server response; pull the fresh /me
      // envelope so the store's `user` is populated before any
      // protected route renders. A 401 here would be a server bug
      // (login just succeeded), so we surface it.
      const me = await fetchJson<AuthMe>("/api/v1/auth/me");
      setAuthenticated(me);
      // Drop every cached query — the previous user's data must not
      // bleed into the next session's TanStack cache.
      queryClient.clear();
      return result;
    } catch (err) {
      // Any failure leaves us in `unauthenticated` so the LoginPage
      // can render its inline error and re-arm the button.
      setUnauthenticated();
      throw err;
    }
  }, [queryClient]);

  const logout = useCallback(async (): Promise<void> => {
    // Best-effort server tear-down. A 401 (cookie already gone) is
    // success-by-other-means; any other error is logged and swallowed
    // so the user always lands on `/login`, not stuck on a half-torn-
    // down session.
    try {
      await fetchJson("/api/v1/auth/logout", { method: "POST", body: {} });
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 401) {
        // eslint-disable-next-line no-console -- visibility for an unexpected logout failure.
        console.warn("logout endpoint failed; clearing local state anyway", err);
      }
    }
    setUnauthenticated();
    queryClient.clear();
    navigate("/login", { replace: true });
  }, [navigate, queryClient]);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading();
    try {
      const me = await fetchJson<AuthMe>("/api/v1/auth/me");
      setAuthenticated(me);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setUnauthenticated();
        return;
      }
      // Non-401 (network blip, 500): fall back to `unauthenticated`
      // so the user is funnelled to /login. The LoginPage will
      // surface the underlying error if the issue persists.
      setUnauthenticated();
      throw err;
    }
  }, []);

  return {
    ...state,
    isAuthenticated: state.status === "authenticated",
    loginWithPasskey,
    logout,
    refresh,
  };
}

/**
 * One-shot effect: registers the token-getter + 401 handler, then
 * probes `/auth/me`. Mounts under `<AuthProvider>` at the app root.
 *
 * Re-running the bootstrap (e.g. across React 19 strict-mode
 * double-mount) is safe — `registerAuthTokenGetter` is idempotent
 * and the `/auth/me` probe is cheap. We still guard with a `ref` so
 * the probe doesn't fire twice when nothing has changed.
 */
export function useAuthBootstrap(): void {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const probedRef = useRef(false);

  useEffect(() => {
    // Token getter — `api.ts` reads this on every fetch.
    registerAuthTokenGetter(() => getAuthState().token);

    // Centralised 401 handler. The factory in `onUnauthorized.ts`
    // owns the path filter (so the initial `/auth/me` probe doesn't
    // bounce the user before the probe has had a chance to set the
    // `unauthenticated` state itself) and the navigate / clear /
    // setUnauthenticated sequence. Wired through a factory so the
    // unit test can exercise it without a router tree.
    registerOnUnauthorized(
      createOnUnauthorized({ navigate: (to, opts) => navigate(to, opts), queryClient }),
    );

    if (probedRef.current) return;
    probedRef.current = true;

    void (async () => {
      try {
        const me = await fetchJson<AuthMe>("/api/v1/auth/me");
        setAuthenticated(me);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          setUnauthenticated();
          return;
        }
        // Unexpected probe failure (network, 500). Treat as
        // unauthenticated so the user can at least see /login;
        // the LoginPage will retry on submit.
        setUnauthenticated();
      }
    })();

    return () => {
      // Leave the registrations in place — `<AuthProvider>` mounts
      // once at the root and unmount happens only at full app
      // tear-down. Clearing here would leave `api.ts` with a stale
      // `null` token-getter for any in-flight request.
    };
  }, [navigate, queryClient]);
}

// ── Test seam ─────────────────────────────────────────────────────

/** Re-export so tests can import a single symbol. Never call from product code. */
export const __resetAuthStoreForTests = _resetForTests;

// Re-export the typed errors so the LoginPage doesn't have to import
// `passkey.ts` directly.
export { PasskeyCancelledError, PasskeyUnsupportedError };
