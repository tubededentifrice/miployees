import { Outlet } from "react-router-dom";
import { useEffect, useMemo, useRef } from "react";
import { Building2, ArrowRight } from "lucide-react";
import { useAuth } from "./useAuth";
import { useWorkspace } from "@/context/WorkspaceContext";

// §14 "Workspace selector" — when the caller is authenticated but
// hasn't picked a workspace yet (no `crewday_workspace` cookie set
// on this device, or the cookie was for a workspace they no longer
// belong to), block the protected tree behind a chooser.
//
// Three branches:
//
//   1. Single workspace → adopt it silently. The user never sees the
//      chooser; spec §14 explicitly says "users with exactly one
//      workspace skip this page". We do the adoption here rather
//      than at /select-workspace so a fresh user landing on /today
//      via deep-link doesn't bounce through an extra screen.
//   2. Multiple workspaces → render the chooser as a modal-style
//      surface above the protected tree. Selecting one writes the
//      cookie via `setWorkspaceId`; the protected tree mounts on
//      the next render.
//   3. Zero workspaces → render the "no access yet" empty state. The
//      user is logged in but has no live grants — usually a brand-
//      new account whose first invite hasn't been redeemed. They
//      can sign out to switch identity.
//
// Public routes (login, recover, accept) are **not** wrapped with
// this component — they don't need a workspace. The router places
// `<WorkspaceGate>` inside the protected branch only.

export function WorkspaceGate({ children }: { children?: React.ReactNode }) {
  const { user, logout } = useAuth();
  const { workspaceId, setWorkspaceId } = useWorkspace();
  // Focused on mount so keyboard users (and screen-reader users on a
  // JAWS / NVDA "forms mode" switch) land inside the dialog rather
  // than in the page chrome beneath. We target the first pickable
  // button (or the sign-out in the empty state) — the dialog itself
  // stays non-tabbable so Tab / Shift+Tab move through the picks.
  const firstActionRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (workspaceId !== null) return;
    firstActionRef.current?.focus({ preventScroll: true });
  }, [workspaceId, user?.available_workspaces?.length]);

  const available = useMemo(
    () => user?.available_workspaces ?? [],
    [user?.available_workspaces],
  );

  const onlySlug = useMemo(() => {
    if (available.length !== 1) return null;
    const w = available[0];
    return w ? slugFor(w.workspace.id, w.workspace.name) : null;
  }, [available]);

  // Auto-adopt for single-workspace users. Runs as an effect so the
  // store update happens outside render (avoids the
  // "setState-during-render" warning) but before the protected tree
  // commits — `setWorkspaceId` triggers a synchronous re-render via
  // the `WorkspaceContext`, and the next pass sees `workspaceId !== null`.
  useEffect(() => {
    if (workspaceId !== null) return;
    if (!onlySlug) return;
    setWorkspaceId(onlySlug);
  }, [workspaceId, onlySlug, setWorkspaceId]);

  // Server already picked a workspace for this session (cookie was
  // set by the login handler) — surface it without forcing the user
  // through the chooser. The auth-me probe carries
  // `current_workspace_id` exactly so this no-op adoption can happen
  // without a follow-up call.
  useEffect(() => {
    if (workspaceId !== null) return;
    if (!user?.current_workspace_id) return;
    if (available.length === 0) return;
    setWorkspaceId(user.current_workspace_id);
  }, [workspaceId, user?.current_workspace_id, available.length, setWorkspaceId]);

  if (workspaceId !== null) return <>{children ?? <Outlet />}</>;

  // From here we know `workspaceId === null`. Render the chooser
  // (or the empty state) instead of the protected tree.

  if (available.length === 0) {
    return (
      <div className="auth-gate" role="dialog" aria-modal="true" aria-labelledby="auth-gate-title">
        <div className="auth-gate__panel">
          <h1 id="auth-gate-title" className="auth-gate__title">No workspaces yet</h1>
          <p className="auth-gate__sub">
            You're signed in as <strong>{user?.display_name ?? user?.email ?? "this account"}</strong>,
            but you don't have access to any workspaces. Ask your manager to send you an invite,
            or open the link they already sent.
          </p>
          <div className="auth-gate__actions">
            <button
              ref={firstActionRef}
              type="button"
              className="btn"
              onClick={() => { void logout(); }}
            >
              Sign out
            </button>
          </div>
        </div>
      </div>
    );
  }

  // 2+ workspaces: pick one. Hold-pattern style matches `<RequireAuth>`'s
  // loading state so the transition between the two doesn't flash.
  return (
    <div className="auth-gate" role="dialog" aria-modal="true" aria-labelledby="auth-gate-title">
      <div className="auth-gate__panel">
        <h1 id="auth-gate-title" className="auth-gate__title">Pick a workspace</h1>
        <p className="auth-gate__sub">
          You have access to {available.length} workspaces. Choose one to continue.
        </p>
        <ul className="auth-gate__list" role="list">
          {available.map((w, idx) => {
            const slug = slugFor(w.workspace.id, w.workspace.name);
            return (
              <li key={w.workspace.id} className="auth-gate__item">
                <button
                  ref={idx === 0 ? firstActionRef : undefined}
                  type="button"
                  className="auth-gate__pick"
                  onClick={() => setWorkspaceId(slug)}
                >
                  <span className="auth-gate__pick-icon" aria-hidden="true">
                    <Building2 size={18} strokeWidth={1.6} />
                  </span>
                  <span className="auth-gate__pick-body">
                    <span className="auth-gate__pick-name">{w.workspace.name}</span>
                    {w.grant_role && (
                      <span className="auth-gate__pick-role">{labelForRole(w.grant_role)}</span>
                    )}
                  </span>
                  <span className="auth-gate__pick-chev" aria-hidden="true">
                    <ArrowRight size={16} strokeWidth={1.6} />
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
        <div className="auth-gate__actions">
          <button type="button" className="btn" onClick={() => { void logout(); }}>
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}

const ROLE_LABELS: Record<string, string> = {
  manager: "Manager",
  worker: "Worker",
  client: "Client",
  guest: "Guest",
  admin: "Admin",
};

function labelForRole(role: string): string {
  return ROLE_LABELS[role] ?? role;
}

/**
 * Resolve the URL-safe slug for a workspace. The /me payload carries
 * the workspace `id` (ULID) and `name` (display) but not the slug
 * directly — the API layer derives the slug from the name on
 * persistence. For the picker we use the id (always URL-safe) so
 * the navigation contract is unambiguous; the next `/auth/me` call
 * after the workspace is adopted will hand back the canonical slug
 * via the workspace cookie.
 *
 * Exported for `WorkspaceGate.test.tsx`.
 */
export function slugFor(id: string, _name: string): string {
  return id;
}

export default WorkspaceGate;
