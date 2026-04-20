// PLACEHOLDER — real impl lands in cd-y4g5. DO NOT USE FOR PRODUCTION
// DECISIONS.
//
// No-op EventSource bootstrap. The production impl opens `/events` and
// routes SSE messages to TanStack Query invalidations; see
// `mocks/web/src/lib/sse.ts` for the full dispatcher.
import type { QueryClient } from "@tanstack/react-query";

export function startEventStream(_client: QueryClient): () => void {
  return () => undefined;
}
