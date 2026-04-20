// PLACEHOLDER — real impl lands in cd-knp1. DO NOT USE FOR PRODUCTION DECISIONS.
//
// Provides a `canGoBack` flag so the (future) page header can decide
// between `navigate(-1)` and a static parent map. The real impl tracks
// in-app navigation depth — see `mocks/web/src/context/NavHistoryContext.tsx`.
import { createContext, useContext, type ReactNode } from "react";

interface NavHistoryValue {
  canGoBack: boolean;
}

const Ctx = createContext<NavHistoryValue>({ canGoBack: false });

export function NavHistoryProvider({ children }: { children: ReactNode }) {
  return <Ctx.Provider value={{ canGoBack: false }}>{children}</Ctx.Provider>;
}

export function useNavHistory(): NavHistoryValue {
  return useContext(Ctx);
}
