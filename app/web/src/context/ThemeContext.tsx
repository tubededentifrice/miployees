// PLACEHOLDER — real impl lands in cd-knp1. DO NOT USE FOR PRODUCTION DECISIONS.
//
// Minimal stand-in for `context/ThemeContext` that exposes
// `{ theme, resolved, setTheme, toggle }` so `PreviewShell` and the
// rest of the shell chrome compile. The real impl mirrors the
// `crewday_theme` cookie and syncs `data-theme` on document.
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { ResolvedTheme, Theme } from "@/types/api";

interface ThemeCtx {
  theme: Theme;
  resolved: ResolvedTheme;
  setTheme: (t: Theme) => void;
  toggle: () => void;
}

const Ctx = createContext<ThemeCtx | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>("light");
  const resolved: ResolvedTheme = theme === "dark" ? "dark" : "light";
  const toggle = useCallback(() => {
    setTheme((t) => (t === "light" ? "dark" : t === "dark" ? "system" : "light"));
  }, []);
  const value = useMemo(
    () => ({ theme, resolved, setTheme, toggle }),
    [theme, resolved, toggle],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useTheme must be used inside <ThemeProvider>");
  return v;
}
