// PLACEHOLDER — real impl lands in cd-qdsl. DO NOT USE FOR PRODUCTION
// DECISIONS.
//
// Central query-key factory used by TanStack Query consumers. Only the
// keys referenced by the current layouts ship here; the full catalog
// mirrors `mocks/web/src/lib/queryKeys.ts` and will arrive with the
// page/component ports.

export const qk = {
  me: () => ["me"] as const,
  bookings: () => ["bookings"] as const,
  adminMe: () => ["admin", "me"] as const,
} as const;
