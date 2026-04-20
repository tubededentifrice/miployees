import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocation, useNavigationType } from "react-router-dom";

// Tracks how many in-app navigations the user has stacked since the
// page first loaded. The header back button uses this to choose
// between "navigate(-1)" (real back) and the static parent map
// (cold-load deep links). Without it, every /task/:id back button
// goes to /today regardless of where the user came from — see
// `routeParents.ts`.
//
// Counts:
//   PUSH    → +1 (Link click, navigate(to))
//   POP     → -1 (browser back/forward, navigate(-1))
//   REPLACE → no-op (Navigate replace, navigate(to, {replace: true}))
//
// The very first location event is always POP (initial mount); we
// skip it via the `initialised` ref so depth starts at 0.
interface NavHistoryValue {
  canGoBack: boolean;
}

const Ctx = createContext<NavHistoryValue>({ canGoBack: false });

export function NavHistoryProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navType = useNavigationType();
  const [depth, setDepth] = useState(0);
  const initialised = useRef(false);

  useEffect(() => {
    if (!initialised.current) {
      initialised.current = true;
      return;
    }
    if (navType === "PUSH") {
      setDepth((d) => d + 1);
    } else if (navType === "POP") {
      setDepth((d) => Math.max(0, d - 1));
    }
  }, [location.key, navType]);

  return <Ctx.Provider value={{ canGoBack: depth > 0 }}>{children}</Ctx.Provider>;
}

export function useNavHistory(): NavHistoryValue {
  return useContext(Ctx);
}
