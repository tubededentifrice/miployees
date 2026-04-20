import { createContext, useContext, type ReactNode } from "react";

// Shared "is there a drawer, and how do I toggle it" handle that the
// page header reads to decide whether to render a hamburger in its
// leading slot. ManagerLayout and AdminLayout mount this; the
// worker shell does not (its nav lives in the bottom tabs).
//
// `hasDrawer` is authoritative: a layout may have a sidebar at
// desktop widths but no mobile drawer (workers), and we only want
// the hamburger in the second case. Keeping the flag explicit
// avoids the alternative of reading a media query at render time.
export interface ShellNavCtxValue {
  hasDrawer: boolean;
  isOpen: boolean;
  toggle: () => void;
}

const Ctx = createContext<ShellNavCtxValue | null>(null);

interface ProviderProps extends ShellNavCtxValue {
  children: ReactNode;
}

export function ShellNavProvider({ hasDrawer, isOpen, toggle, children }: ProviderProps) {
  return (
    <Ctx.Provider value={{ hasDrawer, isOpen, toggle }}>{children}</Ctx.Provider>
  );
}

export function useShellNav(): ShellNavCtxValue | null {
  return useContext(Ctx);
}
