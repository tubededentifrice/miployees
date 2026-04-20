// crewday — central 401 callback factory.
//
// `useAuthBootstrap()` (in `useAuth.ts`) wires this into `api.ts`'s
// `registerOnUnauthorized` seam. It lives in its own file so unit
// tests can exercise the handler in isolation — without mounting a
// router tree, a query-client, or the React store.
//
// The factory shape (rather than a bare callback) keeps the handler
// pure: it receives its dependencies (navigate, queryClient, store
// reset) at registration time, so mocking them in a test is a
// one-line `createOnUnauthorized({ navigate: spy, ... })`.

import type { QueryClient } from "@tanstack/react-query";
import { setUnauthenticated } from "./authStore";

export interface OnUnauthorizedDeps {
  navigate: (to: string, options?: { replace?: boolean }) => void;
  queryClient: QueryClient;
  /**
   * Resolve the user's current location for the `?next=` round-trip.
   * Defaults to reading `window.location` — overridable so tests can
   * pass a fixed value without poking jsdom's history.
   */
  getCurrentLocation?: () => string;
}

/**
 * Path predicate: the registered handler MUST NOT redirect when the
 * 401 came from an endpoint where 401 is the expected protocol-level
 * answer (the auth-me probe, the login finish call, the logout call,
 * any anonymous passkey-login / recover / magic / signup endpoint).
 *
 * IMPORTANT — only *anonymous* auth endpoints are suppressed. The
 * `authenticated-user` passkey endpoints (`/auth/passkey/register/*`)
 * and the `email/*` change-of-address flows DO need the redirect
 * when a session expires mid-ceremony: they target a logged-in user,
 * so a 401 from them is the "kicked to /login" case. A broader
 * `/auth/passkey/` match would silently swallow the redirect and
 * leave the user staring at a non-responsive form.
 *
 * Exported for the unit tests so a regression that adds a path to
 * the redirect set is caught at the predicate level rather than
 * inside the live router.
 */
export function isAuthEndpoint(path: string): boolean {
  // Strip the query/fragment so comparisons stay pure-path; the server
  // never cares about them for the "is this endpoint anonymous" check,
  // and `window.location.search`-aware builders occasionally append
  // telemetry params that would break a naive `endsWith`.
  const bare = path.split(/[?#]/, 1)[0] ?? path;
  if (bare.endsWith("/api/v1/auth/me")) return true;
  if (bare.endsWith("/api/v1/auth/logout")) return true;
  // Passkey login ceremony only — NOT the register flow (authenticated).
  if (bare.includes("/api/v1/auth/passkey/login/")) return true;
  // Signup passkey ceremony is mounted under /auth/passkey/signup —
  // anonymous, so suppress the redirect (the user hasn't signed in yet).
  if (bare.includes("/api/v1/auth/passkey/signup/")) return true;
  // All recover-* endpoints are anonymous (they exist precisely because
  // the user cannot sign in), magic-link send/consume is anonymous, and
  // every /signup/ surface is pre-auth.
  if (bare.includes("/api/v1/auth/recover/")) return true;
  if (bare.includes("/api/v1/auth/magic/")) return true;
  if (bare.includes("/api/v1/signup/")) return true;
  // Invite introspection / accept is anonymous (the token is the
  // credential); the existing-user branch that lands on /accept is a
  // page route, not an API call.
  if (/\/api\/v1\/invites\/[^/]+(?:\/accept)?$/.test(bare)) return true;
  return false;
}

/**
 * Restrict the `next` round-trip to a same-origin, path-shaped value.
 *
 * Why: a `next` that survives into the LoginPage is eventually handed
 * to `<Navigate to={next}>`, and an attacker-crafted URL (for example
 * a phishing link that lands on `/login?next=https://evil.example/`)
 * could drive the post-login redirect off-origin. We emit `next` only
 * when it looks like an in-app path — leading `/`, not `//` (a
 * protocol-relative URL), no `javascript:` / `data:` / absolute scheme.
 * Anything else is dropped so the user lands on `/` after login.
 *
 * Exported so `<RequireAuth>` and any future caller that assembles a
 * `/login?next=...` URL routes through the same sanitiser — keeping
 * the contract uniform across the two emission points.
 */
export function sanitizeNext(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const trimmed = value.trim();
  if (trimmed === "") return null;
  // Same-origin, path-shaped. `//foo` is a protocol-relative URL that
  // browsers resolve against the current scheme — treat it as unsafe.
  if (!trimmed.startsWith("/")) return null;
  if (trimmed.startsWith("//")) return null;
  // Backslash-at-start is a known same-origin-bypass trick in some
  // browsers (`/\/evil.example` collapses to `//evil.example`).
  if (trimmed.startsWith("/\\")) return null;
  // Opaque schemes occasionally sneak in via double-encoding. A second
  // guard on common URI schemes in case a caller hand-concatenates.
  if (/^\/\s*(?:javascript|data|vbscript|file|about):/i.test(trimmed)) return null;
  // Landing on /login with a self-referential next is a no-op; avoid
  // the pointless round-trip (and the potential for nested `next`s).
  if (trimmed === "/login" || trimmed.startsWith("/login?") || trimmed.startsWith("/login#")) {
    return null;
  }
  return trimmed;
}

/**
 * Build the 401 callback for `registerOnUnauthorized()`. The hook
 * (`useAuthBootstrap`) inlines the equivalent logic for now; this
 * factory is exported so future refactors (e.g. a non-React entry
 * point used by a service worker) can compose the same behaviour
 * without re-declaring the path predicate.
 */
export function createOnUnauthorized(deps: OnUnauthorizedDeps): (status: number, path: string) => void {
  const here = deps.getCurrentLocation
    ?? (() => window.location.pathname + window.location.search + window.location.hash);
  return (_status, path) => {
    if (isAuthEndpoint(path)) return;
    setUnauthenticated();
    deps.queryClient.clear();
    const safeNext = sanitizeNext(here());
    const next = safeNext ? `?next=${encodeURIComponent(safeNext)}` : "";
    deps.navigate(`/login${next}`, { replace: true });
  };
}
