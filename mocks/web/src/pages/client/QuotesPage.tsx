import { useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import DeskPage from "@/components/DeskPage";
import { Chip, Loading } from "@/components/common";
import { formatMoney } from "@/lib/money";
import type { Me, Property, Quote, WorkOrder } from "@/types/api";

// §22 — quotes the client may accept or reject. Acceptance is
// unconditionally approval-gated, so the production system would
// route the click through the approval UI; the mock applies it
// in-memory and emits the corresponding SSE event for parity.
export default function ClientQuotesPage() {
  const queryClient = useQueryClient();
  const meQ = useQuery({ queryKey: qk.me(), queryFn: () => fetchJson<Me>("/api/v1/me") });
  const orgIds = useMemo(() => meQ.data?.client_binding_org_ids ?? [], [meQ.data]);
  const woQs = useQuery({
    queryKey: qk.workOrders(orgIds.join(",")),
    queryFn: async () => {
      const groups = await Promise.all(
        orgIds.map((oid) => fetchJson<WorkOrder[]>("/api/v1/work_orders?client_org_id=" + oid)),
      );
      return groups.flat();
    },
    enabled: orgIds.length > 0,
  });
  const propsQ = useQuery({
    queryKey: qk.properties(),
    queryFn: () => fetchJson<Property[]>("/api/v1/properties"),
  });

  const woIds = (woQs.data ?? []).map((w) => w.id);
  const quotesQ = useQuery({
    queryKey: ["quotes_for_client", woIds.join(",")] as const,
    queryFn: async () => {
      const groups = await Promise.all(
        woIds.map((id) => fetchJson<Quote[]>("/api/v1/quotes?work_order_id=" + id)),
      );
      return groups.flat();
    },
    enabled: woIds.length > 0,
  });

  // §22 client surface: quote acceptance is approval-gated in
  // production; the mock applies it immediately so the chip flips
  // and we can demo end-to-end. The work-order query is invalidated
  // because acceptance also flips the parent work_order state.
  const decide = useMutation({
    mutationFn: (vars: { quote_id: string; decision: "accept" | "reject" }) =>
      fetchJson<Quote>("/api/v1/quotes/" + vars.quote_id + "/" + vars.decision, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["quotes_for_client"] });
      void queryClient.invalidateQueries({ queryKey: qk.workOrders() });
    },
  });

  if (meQ.isPending) return <DeskPage title="Quotes"><Loading /></DeskPage>;

  const propsById = new Map((propsQ.data ?? []).map((p) => [p.id, p]));
  const woById = new Map((woQs.data ?? []).map((w) => [w.id, w]));
  const quotes = quotesQ.data ?? [];

  return (
    <DeskPage
      title="Quotes"
      sub="Work orders awaiting your decision (§22 client surface)."
    >
      {quotes.length === 0 ? (
        <div className="panel">
          <p className="muted">No open quotes.</p>
        </div>
      ) : (
        <div className="panel">
          <table className="table">
            <thead>
              <tr><th>Property</th><th>Work order</th><th>Total</th><th>Status</th><th>Decided</th><th></th></tr>
            </thead>
            <tbody>
              {quotes.map((q) => {
                const wo = woById.get(q.work_order_id);
                const prop = wo ? propsById.get(wo.property_id) : undefined;
                return (
                  <tr key={q.id}>
                    <td>{prop?.name ?? wo?.property_id ?? "—"}</td>
                    <td>
                      <strong>{wo?.title ?? q.work_order_id}</strong>
                    </td>
                    <td className="table__mono">{formatMoney(q.total_cents, q.currency)}</td>
                    <td>
                      <Chip
                        size="sm"
                        tone={q.status === "accepted" ? "moss" : q.status === "rejected" ? "rust" : "sky"}
                      >
                        {q.status}
                      </Chip>
                    </td>
                    <td className="table__mono muted">{q.decided_at ? new Date(q.decided_at).toLocaleDateString() : "—"}</td>
                    <td>
                      {q.status === "submitted" && (
                        <div className="row-actions">
                          <button
                            type="button"
                            className="btn btn--ghost btn--sm"
                            disabled={decide.isPending}
                            onClick={() => decide.mutate({ quote_id: q.id, decision: "reject" })}
                          >
                            Reject
                          </button>
                          <button
                            type="button"
                            className="btn btn--moss btn--sm"
                            disabled={decide.isPending}
                            onClick={() => decide.mutate({ quote_id: q.id, decision: "accept" })}
                          >
                            Accept
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </DeskPage>
  );
}
