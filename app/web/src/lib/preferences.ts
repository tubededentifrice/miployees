// PLACEHOLDER — real impl lands in cd-knp1 (cookie readers/writers ride
// with ThemeContext + RoleContext + WorkspaceContext wiring). DO NOT USE
// FOR PRODUCTION DECISIONS.
//
// Cookie-backed preferences shim. Every reader returns a sane default
// and every writer is a no-op; the production impl mirrors
// `mocks/web/src/lib/preferences.ts` (cookie reads, sendBeacon writes).
import type { Role, Theme } from "@/types/api";

export function readRoleCookie(): Role {
  return "manager";
}

export function readWorkspaceCookie(): string | null {
  return null;
}

export function readThemeCookie(): Theme {
  return "system";
}

export function readAgentCollapsedCookie(): boolean | null {
  return null;
}

export function initialAgentCollapsed(): boolean {
  return true;
}

export function readNavCollapsedCookie(): boolean | null {
  return null;
}

export function initialNavCollapsed(): boolean {
  return false;
}

export function persistRole(_role: Role): void {
  /* placeholder */
}

export function persistTheme(_theme: Theme): void {
  /* placeholder */
}

export function persistWorkspace(_workspaceId: string): void {
  /* placeholder */
}

export function persistAgentCollapsed(_state: "open" | "collapsed"): void {
  /* placeholder */
}

export function persistNavCollapsed(_state: "open" | "collapsed"): void {
  /* placeholder */
}
