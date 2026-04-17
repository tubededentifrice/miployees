import { NavLink, useLocation } from "react-router-dom";

// Shared sidebar used by both ManagerLayout (`.desk__nav` inside
// `.desk`) and EmployeeLayout (`.desk__nav` inside `.phone`, revealed
// at >=720px; the phone-mode bottom tab bar takes over below).
//
// The visual system — brand row, section labels, nav-link padding,
// hover / active colours, bottom "me" card — lives entirely in CSS
// against these class names (`.desk__brand`, `.desk__nav-group`,
// `.nav-section`, `.nav-link`, `.desk__me`). Callers pass items +
// footer; the component renders the chrome.

export interface SideNavLinkItem {
  type: "link";
  to: string;
  label: string;
  matchPrefix?: string;
}

export interface SideNavSectionItem {
  type: "section";
  label: string;
}

export type SideNavItem = SideNavLinkItem | SideNavSectionItem;

interface SideNavFooter {
  initials: string;
  name: string;
  role: string;
}

interface SideNavProps {
  items: SideNavItem[];
  footer?: SideNavFooter;
  action?: React.ReactNode;
  ariaLabel?: string;
  onLinkClick?: () => void;
}

export default function SideNav({
  items,
  footer,
  action,
  ariaLabel = "Main navigation",
  onLinkClick,
}: SideNavProps) {
  return (
    <aside className="desk__nav" aria-label={ariaLabel}>
      <div className="desk__brand">
        <span className="desk__logo" aria-hidden="true">◈</span>
        <span className="desk__wordmark">crewday</span>
      </div>
      <nav className="desk__nav-group">
        {items.map((item, i) =>
          item.type === "section" ? (
            <div key={"s-" + i} className="nav-section">{item.label}</div>
          ) : (
            <NavItem
              key={item.to}
              to={item.to}
              matchPrefix={item.matchPrefix}
              onClick={onLinkClick}
            >
              {item.label}
            </NavItem>
          ),
        )}
      </nav>
      {action && <div className="desk__nav-action">{action}</div>}
      {footer && (
        <div className="desk__me">
          <div className="avatar avatar--md">{footer.initials}</div>
          <div>
            <div className="desk__me-name">{footer.name}</div>
            <div className="desk__me-role">{footer.role}</div>
          </div>
        </div>
      )}
    </aside>
  );
}

interface NavItemProps {
  to: string;
  matchPrefix?: string;
  children: React.ReactNode;
  onClick?: () => void;
}

function NavItem({ to, matchPrefix, children, onClick }: NavItemProps) {
  const { pathname } = useLocation();
  const active = matchPrefix ? pathname.startsWith(matchPrefix) : pathname === to;
  return (
    <NavLink
      to={to}
      onClick={onClick}
      className={"nav-link" + (active ? " nav-link--active" : "")}
    >
      {children}
    </NavLink>
  );
}
