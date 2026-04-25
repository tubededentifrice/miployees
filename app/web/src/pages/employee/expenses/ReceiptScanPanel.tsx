import { useCallback, useEffect, useState } from "react";
import type { ChangeEvent } from "react";
import { Search } from "lucide-react";
import { ApiError, fetchJson } from "@/lib/api";
import type { ExpenseScanResult } from "@/types/api";

// Phase machine matches the mock verbatim: `upload` shows the receipt
// picker, `processing` swaps to a spinner with a deliberate 1.5 s
// minimum so the OCR feels considered (not a flash of "did anything
// happen?"), then the parent transitions to `review`.
//
// The panel only owns the file-picker DOM and the request lifecycle —
// the parent handles `phase` so the upload pane and the review form
// can share a single state machine without prop-drilling.
//
// Wire contract (spec §12 §expenses):
//   POST /api/v1/expenses/scan, multipart/form-data, field `image`.
//   Server allow-list: image/jpeg, image/png, image/webp, image/heic,
//   application/pdf (≤ 10 MB). Errors arrive as RFC 7807 with the
//   short `type` keys mapped below — we surface a plain-English line
//   per code rather than dumping the raw `detail`, since the worker
//   shouldn't have to read "blob_mime_not_allowed" to understand
//   "we can't read this format yet". `extraction_*` codes from the
//   LLM side (timeout, rate-limited, provider error, parse error,
//   invariant) are folded into a single retry message — the worker's
//   action ("try again in a moment, or add it by hand") is identical
//   regardless of which provider hiccup landed.

// Mirrors the server's `_SCAN_ALLOWED_MIME` allow-list verbatim
// (`app/api/v1/expenses.py`). Kept as a module-level constant so a
// drift on the server triggers a corresponding edit here, not a
// silent client/server mismatch where the picker accepts a file the
// server then rejects with 422.
const ACCEPTED_MIMES: ReadonlySet<string> = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
  "image/heic",
  "application/pdf",
]);

// String list for the <input accept="…"> attribute. Native pickers
// honour this as a hint (they may still surface "all files" on some
// platforms); we re-validate after selection regardless.
const ACCEPT_ATTR = [...ACCEPTED_MIMES].join(",");

// Spinner-floor so the OCR feels considered even on cache hits. Runs
// on the failure path too so a fast 422 doesn't flash the picker
// state in and out.
const MIN_SPINNER_MS = 1500;

interface Props {
  /**
   * Drives which slot renders. The panel itself is a no-op for
   * `review` and `submitted` (the parent's other panels take over),
   * but we keep the prop intentionally narrow so the parent can pass
   * the full union without massaging it.
   */
  phase: "upload" | "processing" | "review" | "submitted";
  /**
   * Called once the OCR call resolves with a parsed result. The parent
   * folds the result into the review form's initial state and flips to
   * the `review` phase.
   */
  onScanResult: (result: ExpenseScanResult) => void;
  /** Flips to `processing` the instant the user picks a file. */
  onScanStarted: () => void;
  /**
   * Called when the scan fails. The parent reverts to `upload` so the
   * panel can re-render its picker with an inline error notice.
   * Optional so older call sites still compile during the staged
   * rollout, but every production caller wires this — without it the
   * spinner would stay visible after a 422.
   */
  onScanFailed?: () => void;
}

export default function ReceiptScanPanel({
  phase,
  onScanResult,
  onScanStarted,
  onScanFailed,
}: Props) {
  const [error, setError] = useState<string | null>(null);

  // Drop the inline error the moment the worker leaves the picker —
  // otherwise a failed scan, then "+ New expense" → "Back", would
  // show a stale error against an unrelated fresh picker. We only
  // clear on the transition *out* of `upload`; the in-flight failure
  // path itself keeps the error visible because the parent flips
  // from `processing` straight back to `upload` (where the notice
  // belongs).
  useEffect(() => {
    if (phase !== "upload" && phase !== "processing") {
      setError(null);
    }
  }, [phase]);

  const handleFileSelect = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const input = event.target;
      const file = input.files?.[0] ?? null;
      // Reset the input value so picking the same file twice in a row
      // still fires `change` (browsers debounce identical selections).
      input.value = "";
      if (!file) return;

      if (!ACCEPTED_MIMES.has(file.type)) {
        // Some platforms hand us a blank `type` for HEIC; rather than
        // guess from the extension we surface the same notice and
        // let the worker re-pick — the server's allow-list is the
        // source of truth.
        setError(
          "We can't read that format yet — try a JPEG, PNG, WebP, HEIC, or PDF.",
        );
        return;
      }

      setError(null);
      onScanStarted();

      const form = new FormData();
      form.append("image", file);

      // The minimum-wait promise keeps the spinner visible long
      // enough to register as "we read your receipt", even when the
      // OCR call returns in <100 ms (e.g. when the LLM cache hits).
      // We wait alongside both success and failure so a fast 422
      // doesn't pop the picker in/out.
      const minWait = new Promise<void>((r) => setTimeout(r, MIN_SPINNER_MS));
      try {
        const scan = fetchJson<ExpenseScanResult>("/api/v1/expenses/scan", {
          method: "POST",
          body: form,
        });
        const [result] = await Promise.all([scan, minWait]);
        onScanResult(result);
      } catch (err) {
        await minWait;
        setError(messageForScanError(err));
        onScanFailed?.();
      }
    },
    [onScanResult, onScanStarted, onScanFailed],
  );

  if (phase === "upload") {
    return (
      <>
        <h2 className="section-title">Submit an expense</h2>
        {error && (
          <p className="evidence__error" role="alert">
            {error}
          </p>
        )}
        <label className="evidence__picker" tabIndex={0}>
          <input
            type="file"
            accept={ACCEPT_ATTR}
            capture="environment"
            onChange={handleFileSelect}
          />
          <span className="evidence__picker-cta">
            Scan a receipt or screenshot
          </span>
          <span className="evidence__picker-sub">
            Photo, payment confirmation, or bank transfer
          </span>
        </label>
      </>
    );
  }

  if (phase === "processing") {
    return (
      <div className="empty-state">
        <span className="empty-state__glyph" aria-hidden="true">
          <Search size={28} strokeWidth={1.8} />
        </span>
        Reading your receipt...
      </div>
    );
  }

  return null;
}

/**
 * Map a thrown error from `fetchJson` to a plain-English line for
 * the worker. The server's RFC 7807 `type` is the discriminator; we
 * fall back to the response detail / generic message so a brand-new
 * code still surfaces *something* readable rather than "undefined".
 *
 * Exported for unit tests only — the panel is the sole production
 * caller.
 */
export function messageForScanError(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.type) {
      case "scan_not_configured":
        return "Receipt scanning isn't enabled here yet — add the expense by hand for now.";
      case "blob_mime_not_allowed":
        return "We can't read that format yet — try a JPEG, PNG, WebP, HEIC, or PDF.";
      case "blob_too_large":
        return "That file is too large — keep it under 10 MB.";
      case "blob_empty":
        return "That file looked empty. Try the picker again.";
      case "extraction_timeout":
      case "extraction_rate_limited":
      case "extraction_provider_error":
      case "extraction_parse_error":
      case "extraction_invariant":
        return "Our reader is having a moment — try again in a few seconds, or add it by hand.";
      default:
        // Unknown short-name: prefer the server's human line over a
        // generic fallback so a brand-new error still tells the
        // worker something actionable.
        return (
          err.detail ??
          err.title ??
          "We couldn't read that receipt. Try again, or add it by hand."
        );
    }
  }
  return "We couldn't read that receipt. Try again, or add it by hand.";
}
