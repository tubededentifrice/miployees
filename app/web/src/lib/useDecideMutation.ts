import { useEffect, useRef } from "react";
import { useMutation, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { fetchJson, resolveApiPath } from "@/lib/api";
import {
  enqueueMutation,
  isBrowserOnline,
  subscribeOfflineQueueReplay,
  type QueuedMutation,
} from "@/lib/offlineQueue";
import { qk } from "@/lib/queryKeys";

// Approve / reject / reimburse mutations across manager desks share the
// same optimistic flow: cancel in-flight queries, snapshot the cache,
// apply a local edit, POST, restore on error, invalidate on settle.
// Only the local edit differs (remove, update-status, split-list). The
// caller passes that as `applyOptimistic`; the rest is factored here so
// changes to invalidation or error handling ripple to all desks.

export function useDecideMutation<TQueryData, TDecision extends string>({
  queryKey,
  endpoint,
  applyOptimistic,
  alsoInvalidate = [qk.dashboard()],
}: {
  queryKey: QueryKey;
  endpoint: (id: string, decision: TDecision) => string;
  applyOptimistic: (prev: TQueryData, id: string, decision: TDecision) => TQueryData;
  alsoInvalidate?: QueryKey[];
}) {
  const qc = useQueryClient();
  const alsoInvalidateRef = useRef(alsoInvalidate);

  useEffect(() => {
    alsoInvalidateRef.current = alsoInvalidate;
  }, [alsoInvalidate]);

  useEffect(() => subscribeOfflineQueueReplay((entry) => {
    if (entry.kind !== "decision") return;
    qc.invalidateQueries({ queryKey });
    for (const k of alsoInvalidateRef.current) qc.invalidateQueries({ queryKey: k });
  }), [qc, queryKey]);

  return useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: TDecision }) => {
      const path = endpoint(id, decision);
      if (!isBrowserOnline()) {
        const scopedPath = resolveApiPath(path);
        const queueId = `decision:${scopedPath}`;
        return enqueueMutation({
          kind: "decision",
          method: "POST",
          path,
          id: queueId,
          idempotencyKey: queueId,
        });
      }
      return fetchJson(path, { method: "POST" });
    },
    onMutate: async ({ id, decision }) => {
      await qc.cancelQueries({ queryKey });
      const prev = qc.getQueryData<TQueryData>(queryKey);
      if (prev !== undefined) {
        qc.setQueryData<TQueryData>(queryKey, applyOptimistic(prev, id, decision));
      }
      return { prev };
    },
    onError: (_err, _vars, ctx) => {
      if (ctx?.prev !== undefined) qc.setQueryData(queryKey, ctx.prev);
    },
    onSettled: (data) => {
      if (isQueuedMutation(data)) return;
      qc.invalidateQueries({ queryKey });
      for (const k of alsoInvalidate) qc.invalidateQueries({ queryKey: k });
    },
  });
}

function isQueuedMutation(value: unknown): value is QueuedMutation {
  return typeof value === "object"
    && value !== null
    && "storageKey" in value
    && "idempotencyKey" in value
    && "nextAttemptAt" in value;
}
