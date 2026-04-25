// Phone vs desktop split for `/schedule` (§14 "Schedule view"). Mirrors
// the `(min-width: 720px)` breakpoint used by `.schedule--phone` /
// `.schedule--desktop` in CSS so the per-variant layout lines up with
// what `useIsPhone` reports. Both variants run the same bidirectional
// infinite query; only the per-week rendering differs.

import { useEffect, useState } from "react";

export function useIsPhone(): boolean {
  const query = "(max-width: 719px)";
  const [isPhone, setIsPhone] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia(query).matches;
  });
  useEffect(() => {
    const mq = window.matchMedia(query);
    const handler = (e: MediaQueryListEvent): void => setIsPhone(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return isPhone;
}
