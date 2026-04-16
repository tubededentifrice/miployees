import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import { fmtDate } from "@/lib/dates";
import { Loading } from "@/components/common";
import type { Expense, ExpenseStatus } from "@/types/api";

const STATUS_TONE: Record<ExpenseStatus, "moss" | "rust" | "sand" | "sky"> = {
  pending: "sand",
  approved: "moss",
  rejected: "rust",
  reimbursed: "sky",
};

export default function MyExpensesPage() {
  const qc = useQueryClient();
  const [merchant, setMerchant] = useState("");
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");

  const q = useQuery({
    queryKey: qk.expenses("mine"),
    queryFn: () => fetchJson<Expense[]>("/api/v1/expenses?mine=true"),
  });

  const create = useMutation({
    mutationFn: (payload: { merchant: string; amount: string; note: string }) =>
      fetchJson<Expense>("/api/v1/expenses", { method: "POST", body: payload }),
    onSuccess: () => {
      setMerchant("");
      setAmount("");
      setNote("");
      qc.invalidateQueries({ queryKey: qk.expenses("mine") });
    },
  });

  return (
    <>
      <section className="phone__section">
        <h2 className="section-title">Submit an expense</h2>
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate({ merchant, amount, note });
          }}
        >
          <label className="field">
            <span>Merchant</span>
            <input
              name="merchant"
              placeholder="e.g. Carrefour"
              required
              value={merchant}
              onChange={(e) => setMerchant(e.target.value)}
            />
          </label>
          <label className="field field--inline">
            <span>Amount</span>
            <input
              name="amount"
              type="number"
              step="0.01"
              placeholder="0.00"
              required
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
            />
          </label>
          <label className="field">
            <span>Note</span>
            <textarea
              name="note"
              rows={2}
              placeholder="What it was for"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
          <div className="form__row">
            <button type="button" className="btn btn--ghost">📷 Attach receipt</button>
            <button type="submit" className="btn btn--moss">Submit</button>
          </div>
        </form>
      </section>

      <section className="phone__section">
        <h2 className="section-title">My recent expenses</h2>
        {q.isPending ? (
          <Loading />
        ) : q.isError || !q.data ? (
          <p className="muted">Failed to load.</p>
        ) : (
          <ul className="expense-list">
            {q.data.map((x) => (
              <li key={x.id} className="expense-row">
                <div className="expense-row__main">
                  <strong>{x.merchant}</strong>
                  <span className="expense-row__note">{x.note}</span>
                  <span className="expense-row__time">{fmtDate(x.submitted_at)}</span>
                </div>
                <div className="expense-row__side">
                  <span className="expense-row__amount">{formatMoney(x.amount_cents, x.currency)}</span>
                  <span className={"chip chip--sm chip--" + STATUS_TONE[x.status]}>{x.status}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
