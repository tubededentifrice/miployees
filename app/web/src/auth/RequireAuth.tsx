import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./useAuth";
import { sanitizeNext } from "./onUnauthorized";

// §14 "Auth" — route guard that defers child rendering until the
// auth store has resolved. Three terminal states:
//
//   - `loading`        → render the holding pattern (no redirect yet).
//   - `unauthenticated`→ `<Navigate to="/login?next=...">`.
//   - `authenticated`  → `<Outlet />` (children mount).
//
// Public routes (login, recover, accept invite, guest, signup) are
// **not** wrapped with this component in `App.tsx`; they live in
// their own `<Route element={<PublicLayout />}>` branch and render
// regardless of session state. Centralising the whitelist here would
// duplicate the router config — better to gate at the route level.
//
// The `next` query parameter survives the bounce: a deep-link to
// `/property/abc?tab=tasks` becomes `/login?next=%2Fproperty%2Fabc%3Ftab%3Dtasks`,
// and the LoginPage replays it on success. The encoded value goes
// through `sanitizeNext()` so protocol-ish / off-origin inputs are
// dropped before the bounce — a defence-in-depth guard that matches
// the central 401 handler's posture.

export function RequireAuth({ children }: { children?: React.ReactNode }) {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    // Minimal hold pattern. The styles live in globals.css under
    // `.auth-hold` so any future redesign (skeleton, spinner, animated
    // wordmark) is one CSS edit away — the component contract stays
    // "render *something* without flashing /login or the chrome".
    return (
      <div className="auth-hold" role="status" aria-live="polite" aria-busy="true">
        <span className="auth-hold__label">Checking your session…</span>
      </div>
    );
  }

  if (status === "unauthenticated") {
    const here = location.pathname + location.search + location.hash;
    const safeHere = sanitizeNext(here);
    const target = safeHere ? `/login?next=${encodeURIComponent(safeHere)}` : "/login";
    return <Navigate to={target} replace />;
  }

  // Authenticated. Two integration shapes are supported:
  //
  //   <Route element={<RequireAuth />}>...children routes...</Route>   → Outlet
  //   <RequireAuth><MyComponent/></RequireAuth>                         → children
  //
  // The router-level form is what `App.tsx` uses; the props form is
  // there for ad-hoc protected widgets that don't sit on a route.
  return <>{children ?? <Outlet />}</>;
}

export default RequireAuth;
