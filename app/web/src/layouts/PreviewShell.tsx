import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Monitor, Moon, Sun } from "lucide-react";
import { useRole } from "@/context/RoleContext";
import { useTheme } from "@/context/ThemeContext";
import { useBannerHeightVar } from "@/lib/useBannerHeightVar";

// PreviewShell is the outermost layout: grain, sticky preview banner,
// then the routed layout inside <Outlet />. Grain is mounted once at
// tree root (not per-page) so navigation doesn't flicker.
export default function PreviewShell() {
  const { role, setRole } = useRole();
  const { theme, resolved, toggle } = useTheme();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  useBannerHeightVar();

  // Pages that don't render role-specific content: pill clicks should
  // still navigate (so they have a visible effect), but neither pill
  // should display as active while the user is here. Public auth flows
  // are role-agnostic AND should keep the user in place.
  const roleNeutral =
    pathname === "/styleguide" ||
    pathname === "/login" ||
    pathname === "/recover" ||
    pathname.startsWith("/accept/") ||
    pathname.startsWith("/guest/");
  const stayOnRoleSwitch =
    pathname === "/login" ||
    pathname === "/recover" ||
    pathname.startsWith("/accept/") ||
    pathname.startsWith("/guest/");

  const switchRole = (r: typeof role) => {
    setRole(r);
    if (!stayOnRoleSwitch) {
      const next =
        r === "employee" ? "/today"
        : r === "client" ? "/portfolio"
        : "/dashboard";
      navigate(next);
    }
  };

  return (
    <div className="surface" data-role={role} data-theme={resolved}>
      <img src="/grain.svg" alt="" aria-hidden="true" className="grain" />

      <div className="preview-banner">
        <span className="preview-banner__badge">PREVIEW</span>
        <span className="preview-banner__note">Interactive mocks · no real data</span>
        <nav className="preview-banner__switch" aria-label="Preview controls">
          <button
            type="button"
            className={"pill" + (!roleNeutral && role === "employee" ? " pill--active" : "")}
            onClick={() => switchRole("employee")}
          >
            Employee
          </button>
          <button
            type="button"
            className={"pill" + (!roleNeutral && role === "manager" ? " pill--active" : "")}
            onClick={() => switchRole("manager")}
          >
            Manager
          </button>
          <button
            type="button"
            className={"pill" + (!roleNeutral && role === "client" ? " pill--active" : "")}
            onClick={() => switchRole("client")}
          >
            Client
          </button>
          <button
            type="button"
            className="pill pill--ghost preview-banner__theme"
            aria-label={"Theme: " + theme + " (click to cycle)"}
            title={"Theme: " + theme}
            onClick={toggle}
          >
            {theme === "light" ? (
              <Sun size={14} aria-hidden="true" />
            ) : theme === "dark" ? (
              <Moon size={14} aria-hidden="true" />
            ) : (
              <Monitor size={14} aria-hidden="true" />
            )}
          </button>
          <Link to="/styleguide" className="pill pill--ghost">§ styleguide</Link>
        </nav>
      </div>

      <Outlet />
    </div>
  );
}
