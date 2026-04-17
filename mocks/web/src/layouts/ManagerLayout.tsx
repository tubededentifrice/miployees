import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import AgentSidebar from "@/components/AgentSidebar";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { readAgentCollapsedCookie } from "@/lib/preferences";
import type { Me } from "@/types/api";

// ManagerLayout mounts AgentSidebar as a SIBLING of <Outlet />.
// React Router remounts only the outlet subtree on navigation, so the
// sidebar's chat log scroll position, composer draft, and cached log
// survive route changes. Do NOT wrap the outlet in the sidebar's
// parent (that would couple them), and do NOT put a `key` prop on the
// layout route (that would force a full remount).
export default function ManagerLayout() {
  const { data } = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const collapsed = readAgentCollapsedCookie();

  return (
    <div className="desk" data-agent-collapsed={collapsed ? "true" : "false"}>
      <aside className="desk__nav" aria-label="Main navigation">
        <div className="desk__brand">
          <span className="desk__logo" aria-hidden="true">◈</span>
          <span className="desk__wordmark">miployees</span>
        </div>
        <nav className="desk__nav-group">
          <NavItem to="/dashboard">Dashboard</NavItem>
          <div className="nav-section">OPERATE</div>
          <NavItem to="/properties" matchPrefix="/propert">Properties</NavItem>
          <NavItem to="/stays">Stays</NavItem>
          <NavItem to="/employees" matchPrefix="/employee">Employees</NavItem>
          <NavItem to="/templates">Templates</NavItem>
          <NavItem to="/schedules">Schedules</NavItem>
          <NavItem to="/instructions" matchPrefix="/instructions">Instructions</NavItem>
          <NavItem to="/inventory">Inventory</NavItem>
          <div className="nav-section">ASSETS</div>
          <NavItem to="/assets" matchPrefix="/asset">Assets</NavItem>
          <NavItem to="/asset_types">Catalog</NavItem>
          <NavItem to="/documents">Documents</NavItem>
          <div className="nav-section">DECIDE</div>
          <NavItem to="/approvals">Approvals</NavItem>
          <NavItem to="/leaves">Leaves</NavItem>
          <NavItem to="/expenses">Expenses</NavItem>
          <NavItem to="/pay">Pay</NavItem>
          <div className="nav-section">ADMIN</div>
          <NavItem to="/permissions">Permissions</NavItem>
          <NavItem to="/audit">Audit log</NavItem>
          <NavItem to="/webhooks">Webhooks</NavItem>
          <NavItem to="/llm">LLM &amp; agents</NavItem>
          <NavItem to="/settings">Settings</NavItem>
        </nav>
        <div className="desk__me">
          <div className="avatar avatar--md">EB</div>
          <div>
            <div className="desk__me-name">{data?.manager_name ?? "Élodie Bernard"}</div>
            <div className="desk__me-role">Manager</div>
          </div>
        </div>
      </aside>

      <section className="desk__main">
        <Outlet />
      </section>

      {/* Sibling of <Outlet />. Do not nest. */}
      <AgentSidebar />
    </div>
  );
}

function NavItem({
  to,
  matchPrefix,
  children,
}: {
  to: string;
  matchPrefix?: string;
  children: React.ReactNode;
}) {
  const { pathname } = useLocation();
  const active = matchPrefix ? pathname.startsWith(matchPrefix) : pathname === to;
  return (
    <NavLink
      to={to}
      className={"nav-link" + (active ? " nav-link--active" : "")}
    >
      {children}
    </NavLink>
  );
}
