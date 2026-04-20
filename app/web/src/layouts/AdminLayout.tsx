import { useCallback, useEffect, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ActivitySquare,
  BookOpen,
  Building2,
  Gauge,
  MessageSquareMore,
  ScrollText,
  Settings,
  Sparkles,
  Users,
} from "lucide-react";
import AgentSidebar from "@/components/AgentSidebar";
import SideNav, { type SideNavItem } from "@/components/SideNav";
import { ShellNavProvider } from "@/context/ShellNavContext";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import {
  initialAgentCollapsed,
  initialNavCollapsed,
  persistNavCollapsed,
} from "@/lib/preferences";
import type { AdminMe, Me } from "@/types/api";

// AdminLayout — bare-host /admin/* shell (§14 "Admin shell").
//
// Mirrors ManagerLayout structurally (same .desk grid, same
// AgentSidebar sibling-of-Outlet pattern so chat state survives
// route changes) but swaps the nav for deployment-level entries
// and the agent for the admin-side agent (role="admin", §11).
//
// Access: the caller must pass GET /admin/api/v1/me. A 404
// renders the "ask your operator" card; the component doesn't
// try to be clever about redirects (the guard is a render-time
// concern, not a routing concern — matches §14's "polite card,
// not /login").

const ICON_SIZE = 16;
const ICON_STROKE = 1.75;
const NAV_ICON = (Icon: typeof Gauge) => (
  <Icon size={ICON_SIZE} strokeWidth={ICON_STROKE} />
);

const NAV_ITEMS: SideNavItem[] = [
  { type: "section", label: "OPERATE" },
  { type: "link", to: "/admin/dashboard", label: "Dashboard", icon: NAV_ICON(Gauge) },
  { type: "link", to: "/admin/workspaces", matchPrefix: "/admin/workspaces", label: "Workspaces", icon: NAV_ICON(Building2) },
  { type: "section", label: "USAGE" },
  { type: "link", to: "/admin/llm", label: "LLM & agents", icon: NAV_ICON(Sparkles) },
  { type: "link", to: "/admin/agent-docs", label: "Agent docs", icon: NAV_ICON(BookOpen) },
  { type: "link", to: "/admin/chat-gateway", label: "Chat gateway", icon: NAV_ICON(MessageSquareMore) },
  { type: "link", to: "/admin/usage", label: "Usage", icon: NAV_ICON(ActivitySquare) },
  { type: "section", label: "ADMIN" },
  { type: "link", to: "/admin/admins", label: "Admins", icon: NAV_ICON(Users) },
  { type: "link", to: "/admin/settings", label: "Settings", icon: NAV_ICON(Settings) },
  { type: "link", to: "/admin/audit", label: "Audit log", icon: NAV_ICON(ScrollText) },
];

export default function AdminLayout() {
  const navigate = useNavigate();
  const collapsed = initialAgentCollapsed();
  const { pathname } = useLocation();
  const [navOpen, setNavOpen] = useState(false);
  const [navCollapsed, setNavCollapsed] = useState<boolean>(() => initialNavCollapsed());
  const toggleNavCollapsed = useCallback(() => {
    setNavCollapsed((c) => {
      const next = !c;
      persistNavCollapsed(next ? "collapsed" : "open");
      return next;
    });
  }, []);

  const meQ = useQuery({
    queryKey: qk.me(),
    queryFn: () => fetchJson<Me>("/api/v1/me"),
  });
  const adminMeQ = useQuery({
    queryKey: qk.adminMe(),
    queryFn: () => fetchJson<AdminMe>("/admin/api/v1/me"),
    retry: false,
  });

  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setNavOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen]);

  const denied = adminMeQ.isError || meQ.data?.is_deployment_admin === false;
  const hasAccess = adminMeQ.isSuccess && meQ.data?.is_deployment_admin === true;

  if (denied) {
    return (
      <div className="desk desk--admin-denied">
        <section className="desk__main">
          <div className="admin-denied">
            <h1>Administration</h1>
            <p>
              You don't have access to the deployment admin surface.
              Ask your operator to grant you admin rights, or head back
              to your workspace.
            </p>
            <button
              type="button"
              className="btn btn--moss"
              onClick={() => navigate("/")}
            >
              Back to workspace
            </button>
          </div>
        </section>
      </div>
    );
  }

  if (!hasAccess) {
    // Still resolving identity — render a minimal chrome without
    // mounting the outlet, so child pages don't fire admin queries
    // before we know whether the caller is authorised. Avoids a burst
    // of 404s in the console for visitors who aren't admins.
    return (
      <div className="desk desk--admin">
        <section className="desk__main" aria-busy="true">
          <div className="empty-state empty-state--quiet">Checking access…</div>
        </section>
      </div>
    );
  }

  const toggleNav = useCallback(() => setNavOpen((v) => !v), []);

  return (
    <ShellNavProvider hasDrawer={true} isOpen={navOpen} toggle={toggleNav}>
      <div
        className="desk desk--admin"
        data-agent-collapsed={collapsed ? "true" : "false"}
        data-nav-collapsed={navCollapsed ? "true" : "false"}
        data-nav-open={navOpen ? "true" : "false"}
      >
        {navOpen && (
          <div
            className="desk__scrim"
            onClick={() => setNavOpen(false)}
            role="presentation"
            aria-hidden="true"
          />
        )}

        <SideNav
          items={NAV_ITEMS}
          collapsed={navCollapsed}
          onToggleCollapsed={toggleNavCollapsed}
          footer={{
            initials: (adminMeQ.data?.display_name ?? "Admin")
              .split(" ")
              .map((w) => w[0])
              .join("")
              .slice(0, 2)
              .toUpperCase(),
            name: adminMeQ.data?.display_name ?? "Deployment admin",
            role: adminMeQ.data?.is_owner ? "Deployment owner" : "Deployment admin",
          }}
          action={
            <button
              type="button"
              className="btn btn--ghost admin-backlink"
              onClick={() => navigate("/")}
            >
              ← Back to workspaces
            </button>
          }
        />

        <section className="desk__main">
          <Outlet />
        </section>

        {/* Sibling of <Outlet />. Do not nest. */}
        <AgentSidebar role="admin" />
      </div>
    </ShellNavProvider>
  );
}
