// PLACEHOLDER — real impl lands in cd-k69n. DO NOT USE FOR PRODUCTION
// DECISIONS.
//
// Shared sidebar skeleton. Surface matches
// `mocks/web/src/components/SideNav.tsx` so the layouts that import it
// compile; the real implementation ships brand row, active-link state,
// collapsed rail, and footer card.
import type { ReactNode } from "react";

export interface SideNavLinkItem {
  type: "link";
  to: string;
  label: string;
  icon?: ReactNode;
  matchPrefix?: string | string[];
  phoneHidden?: boolean;
}

export interface SideNavSectionItem {
  type: "section";
  label: string;
  phoneHidden?: boolean;
}

export type SideNavItem = SideNavLinkItem | SideNavSectionItem;

interface SideNavFooter {
  initials: string;
  avatarUrl?: string | null;
  name: string;
  role: string;
}

interface SideNavProps {
  items: SideNavItem[];
  footer?: SideNavFooter;
  action?: ReactNode;
  ariaLabel?: string;
  onLinkClick?: () => void;
  collapsed?: boolean;
  onToggleCollapsed?: () => void;
  brand?: string;
}

export default function SideNav(_props: SideNavProps) {
  // Placeholder: no chrome yet. Keeping the real return shape lets
  // layouts mount us without visual regressions beyond "the nav is
  // empty", which the shell CSS tolerates while the real component
  // arrives.
  return <aside className="desk__nav" aria-label="Main navigation" />;
}
