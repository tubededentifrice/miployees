// PLACEHOLDER — real impl lands in cd-knp1. DO NOT USE FOR PRODUCTION DECISIONS.
//
// No-op provider; the real impl (cd-knp1) subscribes to `/events` via
// `lib/sse.startEventStream`.
import type { ReactNode } from "react";

export function SseProvider({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
