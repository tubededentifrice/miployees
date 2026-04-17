// Cookie-backed preferences. Reads are synchronous from document.cookie
// so they can hydrate initial state without waiting for /api/v1/me;
// writes go to FastAPI (which re-sets the cookie) and we update our
// local mirror optimistically so layout doesn't flash.

import type { Role, Theme } from "@/types/api";

const ROLE_COOKIE = "crewday_role";
const THEME_COOKIE = "crewday_theme";
const AGENT_COLLAPSED_COOKIE = "crewday_agent_collapsed";

function readCookie(name: string): string | null {
  const target = name + "=";
  for (const chunk of document.cookie.split(";")) {
    const c = chunk.trim();
    if (c.startsWith(target)) return decodeURIComponent(c.slice(target.length));
  }
  return null;
}

export function readRoleCookie(): Role {
  const r = readCookie(ROLE_COOKIE);
  return r === "manager" ? "manager" : "employee";
}

export function readThemeCookie(): Theme {
  const t = readCookie(THEME_COOKIE);
  return t === "dark" ? "dark" : "light";
}

export function readAgentCollapsedCookie(): boolean {
  return readCookie(AGENT_COLLAPSED_COOKIE) === "1";
}

// Fire-and-forget writers. The server is authoritative; we optimistic
// -mirror so the next paint reflects the choice.
export function persistRole(role: Role): void {
  fetch("/switch/" + role, { method: "GET", credentials: "same-origin", keepalive: true })
    .catch(() => { /* preferences are best-effort */ });
}

export function persistTheme(): void {
  fetch("/theme/toggle", { method: "POST", credentials: "same-origin", keepalive: true })
    .catch(() => { /* best-effort */ });
}

export function persistAgentCollapsed(state: "open" | "collapsed"): void {
  const url = "/agent/sidebar/" + state;
  let delivered = false;
  if (navigator.sendBeacon) {
    try {
      delivered = navigator.sendBeacon(url, new Blob([], { type: "text/plain" }));
    } catch {
      /* fall through */
    }
  }
  if (!delivered) {
    fetch(url, { method: "POST", credentials: "same-origin", keepalive: true })
      .catch(() => { /* best-effort */ });
  }
}
