import { useCallback, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchJson } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { formatMoney } from "@/lib/money";
import { fmtDate } from "@/lib/dates";
import { Search } from "lucide-react";
import { Chip, Loading } from "@/components/common";
import PageHeader from "@/components/PageHeader";
import AutoGrowTextarea from "@/components/AutoGrowTextarea";
import type {
  Expense,
  ExpenseCategory,
  ExpenseScanResult,
  ExpenseStatus,
  PendingReimbursement,
} from "@/types/api";

type ScanPhase = "upload" | "processing" | "review" | "submitted";

const STATUS_TONE: Record<ExpenseStatus, "moss" | "rust" | "sand" | "sky" | "ghost"> = {
  draft: "ghost",
  submitted: "sand",
  approved: "moss",
  rejected: "rust",
  reimbursed: "sky",
};

const CATEGORIES: { value: ExpenseCategory; label: string }[] = [
  { value: "supplies", label: "Supplies" },
  { value: "fuel", label: "Fuel" },
  { value: "food", label: "Food" },
  { value: "transport", label: "Transport" },
  { value: "maintenance", label: "Maintenance" },
  { value: "other", label: "Other" },
];

function confidenceClass(c: number | null): string {
  if (c === null || c >= 0.9) return "";
  if (c >= 0.6) return "field--warn";
  return "";
}

export default function MyExpensesPage() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<ScanPhase>("upload");
  const [isScanned, setIsScanned] = useState(false);

  // Form fields
  const [merchant, setMerchant] = useState("");
  const [amount, setAmount] = useState("");
  const [currency, setCurrency] = useState("EUR");
  const [category, setCategory] = useState<ExpenseCategory>("other");
  const [note, setNote] = useState("");

  // Confidence tracking (null = manual entry)
  const [conf, setConf] = useState<Record<string, number | null>>({});

  // Agent question
  const [agentQuestion, setAgentQuestion] = useState<string | null>(null);
  const [agentReply, setAgentReply] = useState("");
  const [questionDismissed, setQuestionDismissed] = useState(false);

  const q = useQuery({
    queryKey: qk.expenses("mine"),
    queryFn: () => fetchJson<Expense[]>("/api/v1/expenses?mine=true"),
  });

  // §09 "Amount owed to the employee" — destination-currency total of
  // approved-but-not-yet-reimbursed claims. Refreshes alongside the
  // expenses list so the worker sees the number update the moment a
  // claim is approved.
  const pending = useQuery({
    queryKey: qk.expensesPendingReimbursement("me"),
    queryFn: () =>
      fetchJson<PendingReimbursement>(
        "/api/v1/expenses/pending_reimbursement?user_id=me",
      ),
  });

  const resetForm = useCallback(() => {
    setMerchant("");
    setAmount("");
    setCurrency("EUR");
    setCategory("other");
    setNote("");
    setConf({});
    setAgentQuestion(null);
    setAgentReply("");
    setQuestionDismissed(false);
    setIsScanned(false);
  }, []);

  const handleScanResult = useCallback((result: ExpenseScanResult) => {
    // Fill fields where confidence >= 0.6; leave blank below that
    const fillIf = <T,>(f: { value: T; confidence: number }, threshold = 0.6): T | null =>
      f.confidence >= threshold ? f.value : null;

    const v = fillIf(result.vendor);
    if (v) setMerchant(v);
    else setMerchant("");

    const cents = fillIf(result.total_amount_cents);
    if (cents !== null) setAmount((cents / 100).toFixed(2));
    else setAmount("");

    const cur = fillIf(result.currency);
    setCurrency(cur ?? "EUR");

    const cat = fillIf(result.category);
    setCategory(cat ?? "other");

    const n = fillIf(result.note_md);
    setNote(n ?? "");

    setConf({
      merchant: result.vendor.confidence,
      amount: result.total_amount_cents.confidence,
      currency: result.currency.confidence,
      category: result.category.confidence,
      note: result.note_md.confidence,
    });

    setAgentQuestion(result.agent_question);
    setQuestionDismissed(false);
    setAgentReply("");
    setIsScanned(true);
  }, []);

  const handleFileSelect = useCallback(async () => {
    setPhase("processing");
    const minWait = new Promise((r) => setTimeout(r, 1500));
    const scan = fetchJson<ExpenseScanResult>("/api/v1/expenses/scan", { method: "POST" });
    const [result] = await Promise.all([scan, minWait]);
    handleScanResult(result);
    setPhase("review");
  }, [handleScanResult]);

  const handleManualEntry = useCallback(() => {
    resetForm();
    setPhase("review");
  }, [resetForm]);

  const create = useMutation({
    mutationFn: (payload: {
      merchant: string;
      amount: string;
      currency: string;
      category: string;
      note: string;
      ocr_confidence: number | null;
    }) =>
      fetchJson<Expense>("/api/v1/expenses", { method: "POST", body: payload }),
    onSuccess: () => {
      setPhase("submitted");
      qc.invalidateQueries({ queryKey: qk.expenses("mine") });
      setTimeout(() => {
        resetForm();
        setPhase("upload");
      }, 1500);
    },
  });

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      // If there's an unanswered agent question reply, fold it into the note
      let finalNote = note;
      if (agentReply.trim() && !questionDismissed) {
        finalNote = note ? `${note}\n\nReply: ${agentReply.trim()}` : agentReply.trim();
      }
      // Compute min confidence across all fields for ocr_confidence
      const confValues = Object.values(conf).filter((c): c is number => c !== null);
      const ocrConf = confValues.length > 0 ? Math.min(...confValues) : null;
      create.mutate({
        merchant,
        amount,
        currency,
        category,
        note: finalNote,
        ocr_confidence: ocrConf,
      });
    },
    [merchant, amount, currency, category, note, conf, agentReply, questionDismissed, create],
  );

  const dismissQuestion = useCallback(() => {
    if (agentReply.trim()) {
      setNote((prev) => (prev ? `${prev}\n\nReply: ${agentReply.trim()}` : agentReply.trim()));
    }
    setQuestionDismissed(true);
  }, [agentReply]);

  const showQuestion = agentQuestion && !questionDismissed;

  return (
    <>
      <PageHeader
        title="My expenses"
        sub="Scan a receipt or add one by hand — approved claims land with your next payslip."
        actions={
          phase === "upload" ? (
            <button
              type="button"
              className="btn btn--moss"
              onClick={handleManualEntry}
            >
              + New expense
            </button>
          ) : undefined
        }
      />
      <section className="phone__section">
        {/* ── Upload phase ──────────────────────────────── */}
        {phase === "upload" && (
          <>
            <h2 className="section-title">Submit an expense</h2>
            <label className="evidence__picker" tabIndex={0}>
              <input
                ref={fileRef}
                type="file"
                accept="image/*"
                capture="environment"
                onChange={handleFileSelect}
              />
              <span className="evidence__picker-cta">Scan a receipt or screenshot</span>
              <span className="evidence__picker-sub">
                Photo, payment confirmation, or bank transfer
              </span>
            </label>
          </>
        )}

        {/* ── Processing phase ─────────────────────────── */}
        {phase === "processing" && (
          <div className="empty-state">
            <span className="empty-state__glyph" aria-hidden="true">
              <Search size={28} strokeWidth={1.8} />
            </span>
            Reading your receipt...
          </div>
        )}

        {/* ── Review phase ─────────────────────────────── */}
        {phase === "review" && (
          <>
            <h2 className="section-title">
              {isScanned ? "Review scanned expense" : "New expense"}
            </h2>

            {showQuestion && (
              <div className="chat-log chat-log--inline">
                <div className="chat-msg chat-msg--agent">
                  <span className="chat-msg__body">{agentQuestion}</span>
                </div>
                <div className="comment__compose">
                  <AutoGrowTextarea
                    placeholder="Your reply..."
                    value={agentReply}
                    onChange={(e) => setAgentReply(e.target.value)}
                  />
                  <button
                    type="button"
                    className="btn btn--sm btn--moss"
                    onClick={dismissQuestion}
                  >
                    Reply
                  </button>
                </div>
              </div>
            )}

            <form className="form" onSubmit={handleSubmit}>
              <label className={`field ${confidenceClass(conf.merchant ?? null)}`}>
                <span>
                  Merchant
                  {isScanned && conf.merchant != null && conf.merchant >= 0.6 && conf.merchant < 0.9 && (
                    <Chip tone="sand" size="sm">{Math.round(conf.merchant * 100)}%</Chip>
                  )}
                </span>
                <input
                  name="merchant"
                  placeholder="e.g. Carrefour"
                  required
                  value={merchant}
                  onChange={(e) => setMerchant(e.target.value)}
                />
              </label>

              <div className="form__row">
                <label
                  className={`field field--grow ${confidenceClass(conf.amount ?? null)}`}
                >
                  <span>
                    Amount
                    {isScanned && conf.amount != null && conf.amount >= 0.6 && conf.amount < 0.9 && (
                      <Chip tone="sand" size="sm">{Math.round(conf.amount * 100)}%</Chip>
                    )}
                  </span>
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
                <label className="field field--currency">
                  <span>Currency</span>
                  <input
                    name="currency"
                    value={currency}
                    onChange={(e) => setCurrency(e.target.value)}
                  />
                </label>
              </div>

              <div className={`field ${confidenceClass(conf.category ?? null)}`}>
                <span>
                  Category
                  {isScanned && conf.category != null && conf.category >= 0.6 && conf.category < 0.9 && (
                    <Chip tone="sand" size="sm">{Math.round(conf.category * 100)}%</Chip>
                  )}
                </span>
                <div className="chip-group">
                  {CATEGORIES.map((c) => (
                    <label key={c.value} className="chip-radio">
                      <input
                        type="radio"
                        name="category"
                        value={c.value}
                        checked={category === c.value}
                        onChange={() => setCategory(c.value)}
                      />
                      <span>{c.label}</span>
                    </label>
                  ))}
                </div>
              </div>

              <label className={`field ${confidenceClass(conf.note ?? null)}`}>
                <span>
                  Note
                  {isScanned && conf.note != null && conf.note >= 0.6 && conf.note < 0.9 && (
                    <Chip tone="sand" size="sm">{Math.round(conf.note * 100)}%</Chip>
                  )}
                </span>
                <AutoGrowTextarea
                  name="note"
                  placeholder="What it was for"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
              </label>

              <div className="form__row">
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => {
                    resetForm();
                    setPhase("upload");
                  }}
                >
                  Back
                </button>
                <button type="submit" className="btn btn--moss" disabled={create.isPending}>
                  Submit expense
                </button>
              </div>
            </form>
          </>
        )}

        {/* ── Submitted phase ──────────────────────────── */}
        {phase === "submitted" && (
          <div className="done-banner">Expense submitted</div>
        )}
      </section>

      {/* ── Pending reimbursement (§09 "Amount owed") ─── */}
      {pending.data && pending.data.totals_by_currency.length > 0 && (
        <section className="phone__section">
          <h2 className="section-title">Owed to you</h2>
          <p className="muted">
            Approved, paid out with your next payslip. Each amount is
            shown in the currency of the account where it will land.
          </p>
          <ul className="reimbursement-totals">
            {pending.data.totals_by_currency.map((t) => (
              <li key={t.currency} className="reimbursement-totals__row">
                <span className="reimbursement-totals__amount">
                  {formatMoney(t.amount_cents, t.currency)}
                </span>
                <span className="reimbursement-totals__ccy">{t.currency}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* ── Recent expenses list (always visible) ────── */}
      <section className="phone__section">
        <h2 className="section-title">My recent expenses</h2>
        {q.isPending ? (
          <Loading />
        ) : q.isError || !q.data ? (
          <p className="muted">Failed to load.</p>
        ) : (
          <ul className="expense-list">
            {q.data.map((x) => {
              const converted =
                x.owed_currency &&
                x.owed_amount_cents != null &&
                x.owed_currency !== x.currency;
              return (
                <li key={x.id} className="expense-row">
                  <div className="expense-row__main">
                    <strong>{x.merchant}</strong>
                    <span className="expense-row__note">{x.note}</span>
                    <span className="expense-row__time">{fmtDate(x.submitted_at)}</span>
                  </div>
                  <div className="expense-row__side">
                    <span className="expense-row__amount">
                      {formatMoney(x.amount_cents, x.currency)}
                    </span>
                    {converted && (
                      <span className="expense-row__owed" title={
                        `Snapped at approval: 1 ${x.currency} = ${x.owed_exchange_rate} ${x.owed_currency} (${x.owed_rate_source})`
                      }>
                        = {formatMoney(x.owed_amount_cents!, x.owed_currency!)}
                      </span>
                    )}
                    <span className={"chip chip--sm chip--" + STATUS_TONE[x.status]}>
                      {x.status}
                    </span>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </>
  );
}
