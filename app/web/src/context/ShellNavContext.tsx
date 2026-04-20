// PLACEHOLDER — real impl lands in cd-knp1. DO NOT USE FOR PRODUCTION DECISIONS.
//
// Shared shell-nav handle the page header reads to decide whether to
// render a hamburger. Shape matches `mocks/web/src/context/ShellNavContext.tsx`.
import { createContext, useContext, type ReactNode } from "react";

export interface ShellNavCtxValue {
  hasDrawer: boolean;
  isOpen: boolean;
  toggle: () => void;
}

const Ctx = createContext<ShellNavCtxValue | null>(null);

interface ProviderProps extends ShellNavCtxValue {
  children: ReactNode;
}

export function ShellNavProvider({
  hasDrawer,
  isOpen,
  toggle,
  children,
}: ProviderProps) {
  return (
    <Ctx.Provider value={{ hasDrawer, isOpen, toggle }}>{children}</Ctx.Provider>
  );
}

export function useShellNav(): ShellNavCtxValue | null {
  return useContext(Ctx);
}
