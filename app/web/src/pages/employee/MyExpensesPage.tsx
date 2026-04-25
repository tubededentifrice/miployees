import { useCallback, useState } from "react";
import PageHeader from "@/components/PageHeader";
import type { ExpenseScanResult } from "@/types/api";
import OwedToYou from "./expenses/OwedToYou";
import ReceiptScanPanel from "./expenses/ReceiptScanPanel";
import RecentExpenses from "./expenses/RecentExpenses";
import SubmitExpenseForm from "./expenses/SubmitExpenseForm";

// Worker's expense surface (§09). Orchestrator only — owns the
// `ScanPhase` state machine and the post-scan handoff between the
// receipt picker and the review form. Every panel below it is a
// self-contained piece (own queries, own mutations, own state) so the
// orchestrator stays readable and the panels are individually
// reviewable.
//
// State machine, mock-verbatim:
//   upload → processing → review → submitted → upload (auto)
// "+ New expense" on the page header jumps straight from `upload` to
// `review` with `scan = null` (manual entry). "Back" from `review`
// returns to `upload`.

type ScanPhase = "upload" | "processing" | "review" | "submitted";

const SUBMITTED_AUTO_DISMISS_MS = 1500;

export default function MyExpensesPage() {
  const [phase, setPhase] = useState<ScanPhase>("upload");
  const [scan, setScan] = useState<ExpenseScanResult | null>(null);

  const handleScanStarted = useCallback(() => {
    setPhase("processing");
  }, []);

  const handleScanResult = useCallback((result: ExpenseScanResult) => {
    setScan(result);
    setPhase("review");
  }, []);

  const handleScanFailed = useCallback(() => {
    // Revert to `upload` so the panel re-renders its picker — the
    // panel surfaces the failure message itself via local state, so
    // the parent only owns the phase transition.
    setScan(null);
    setPhase("upload");
  }, []);

  const handleManualEntry = useCallback(() => {
    setScan(null);
    setPhase("review");
  }, []);

  const handleBack = useCallback(() => {
    setScan(null);
    setPhase("upload");
  }, []);

  const handleSubmitted = useCallback(() => {
    setPhase("submitted");
    // Auto-rewind to the picker so the worker can scan the next
    // receipt without an extra tap. The 1.5 s pause matches the mock
    // — long enough to register the success banner, short enough not
    // to feel sticky.
    setTimeout(() => {
      setScan(null);
      setPhase("upload");
    }, SUBMITTED_AUTO_DISMISS_MS);
  }, []);

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
        <ReceiptScanPanel
          phase={phase}
          onScanResult={handleScanResult}
          onScanStarted={handleScanStarted}
          onScanFailed={handleScanFailed}
        />
        {phase === "review" && (
          <SubmitExpenseForm
            initialScan={scan}
            onSubmitted={handleSubmitted}
            onBack={handleBack}
          />
        )}
        {phase === "submitted" && (
          <div className="done-banner">Expense submitted</div>
        )}
      </section>

      <OwedToYou />
      <RecentExpenses />
    </>
  );
}
