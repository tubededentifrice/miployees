// PLACEHOLDER — real impl lands in cd-qdsl. DO NOT USE FOR PRODUCTION
// DECISIONS.
//
// Returns a QueryClient with minimal defaults; the production impl
// mirrors `mocks/web/src/lib/queryClient.ts` (staleTime, 4xx-skip retry).
import { QueryClient } from "@tanstack/react-query";

export function makeQueryClient(): QueryClient {
  return new QueryClient();
}
