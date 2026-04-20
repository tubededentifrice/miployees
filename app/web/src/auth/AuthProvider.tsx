import { type ReactNode } from "react";
import { useAuthBootstrap } from "./useAuth";

// Thin wrapper that runs the one-shot `useAuthBootstrap()` effect.
// Mounted once at the app root (between `<BrowserRouter>` and the
// other providers) so the auth store + 401 handler are wired before
// any protected route mounts.
//
// We deliberately do *not* render a loading spinner here — the
// initial probe is fast (one `/auth/me` call) and any UI flash would
// land outside the route shell, where it has nowhere to live. The
// `<RequireAuth>` guard handles the `'loading'` state at the route
// boundary instead.
export function AuthProvider({ children }: { children: ReactNode }) {
  useAuthBootstrap();
  return <>{children}</>;
}
