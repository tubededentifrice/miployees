import { useEffect, useSyncExternalStore, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useWorkspace } from "@/context/WorkspaceContext";
import { getAuthState, subscribeAuth } from "@/auth";

// §14 "SSE-driven invalidation" — one `EventSource('/w/${slug}/events')`
// per active workspace. Re-established on workspace switch (and on
// transport drops, via exponential backoff). When no workspace is
// picked yet (pre-/me, or the user hasn't chosen one), we fall back
// to `/events` so the server can still push workspace-agnostic
// events (e.g. onboarding, admin) before the SPA knows its tenant.
//
// The transport is only opened while the user is authenticated. A
// logout flips `useAuth().isAuthenticated` to `false`, which tears
// down this effect and closes the underlying `EventSource` — keeping
// the §"Logout clears storage + closes SSE" acceptance criterion
// honest without `SseContext` knowing about cookies or storage.
//
// Message dispatch (query invalidation, setQueryData fan-out) is
// the responsibility of `lib/sse` and lands with cd-y4g5. This
// provider only owns the lifecycle of the transport: connect,
// reconnect with backoff, reset backoff on successful open, and
// tear down on unmount, slug switch, or sign-out.

// Backoff: 1s, 2s, 4s, 8s, capped at 30s. The cap keeps a stuck
// server from hammering the browser while still recovering quickly
// once it comes back; the reset-on-open makes the next drop start
// from 1s again.
const BACKOFF_INITIAL_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;

function sseUrl(slug: string | null): string {
  return slug ? `/w/${slug}/events` : "/events";
}

export function SseProvider({ children }: { children: ReactNode }) {
  // `qc` is wired here so cd-y4g5 can attach the dispatcher without
  // having to re-plumb the provider. It is intentionally unused
  // today — the stream is opened for its side effect of reconciling
  // the server's push queue with the client's TanStack cache once
  // the dispatcher lands.
  const qc = useQueryClient();
  const { workspaceId } = useWorkspace();
  // Read directly from the auth store rather than `useAuth()` so this
  // provider doesn't drag a `useNavigate()` dependency into trees
  // that legitimately mount it without a `<Router>` (the unit tests
  // for the SSE lifecycle, for one).
  const status = useSyncExternalStore(
    subscribeAuth,
    () => getAuthState().status,
    () => getAuthState().status,
  );

  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    // Only open the stream once auth is positively resolved. On the
    // unauthenticated leg the user is bouncing between /login and the
    // protected tree; opening a stream the server will refuse anyway
    // is wasted reconnect chatter (and would leak a transport across
    // a logout). The `'loading'` leg also defers — the bootstrap
    // probe usually settles in a single tick.
    if (status !== "authenticated") return;

    let es: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let backoff = BACKOFF_INITIAL_MS;
    let closed = false;

    const connect = (): void => {
      if (closed) return;
      es = new EventSource(sseUrl(workspaceId), { withCredentials: true });
      es.onopen = () => {
        // Successful open — reset the backoff for the next drop.
        backoff = BACKOFF_INITIAL_MS;
      };
      es.onerror = () => {
        // The browser opens `readyState === 2` (CLOSED) on a hard
        // failure and `1` (OPEN) on transient errors it will retry
        // on its own. Only re-arm our backoff ladder on a hard close;
        // letting the native retry handle transients avoids stacking
        // two reconnects racing each other.
        if (!es || es.readyState !== EventSource.CLOSED) return;
        es.close();
        es = null;
        if (closed) return;
        reconnectTimer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
      };
    };

    connect();

    return () => {
      closed = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (es) es.close();
    };
  }, [qc, workspaceId, status]);

  return <>{children}</>;
}
