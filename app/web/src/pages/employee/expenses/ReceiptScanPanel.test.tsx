// Component-level coverage for the worker's receipt-scan picker.
//
// What this file pins:
//   1. Happy path — a valid file lands a multipart POST on
//      `/api/v1/expenses/scan` with the correct `image` field, and
//      the parsed `ExpenseScanResult` is forwarded to `onScanResult`.
//   2. Client-side mime check — a non-receipt mime never reaches
//      `fetch` and surfaces an inline notice.
//   3. Server error mapping — every documented short-name from
//      §12 §expenses (`blob_too_large`, `blob_empty`,
//      `blob_mime_not_allowed`, `scan_not_configured`, the three
//      `extraction_*` flavours) projects to its plain-English line
//      and `onScanFailed` runs so the parent reverts the phase.
//   4. Unknown error type — a server code we don't recognise still
//      surfaces a readable line (server `detail` first, then
//      generic).
//
// We patch `MIN_SPINNER_MS` indirectly by using fake timers around
// the panel — `waitFor` cannot poll under fake timers, so the test
// drives advancement explicitly via `act` + `runAllTimersAsync`,
// and asserts state synchronously once the promise chain has
// drained.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import {
  __resetApiProvidersForTests,
  registerWorkspaceSlugGetter,
} from "@/lib/api";
import ReceiptScanPanel, { messageForScanError } from "./ReceiptScanPanel";
import { ApiError } from "@/lib/api";

interface ScriptedResponse {
  status?: number;
  body: unknown;
}

interface FetchCall {
  url: string;
  init: RequestInit;
}

function installFetch(scripted: ScriptedResponse[]): {
  calls: FetchCall[];
  restore: () => void;
} {
  const calls: FetchCall[] = [];
  const original = globalThis.fetch;
  const queue = [...scripted];
  const spy = vi.fn(
    async (url: string | URL | Request, init?: RequestInit) => {
      const resolved = typeof url === "string" ? url : url.toString();
      calls.push({ url: resolved, init: init ?? {} });
      const next = queue.shift();
      if (!next) {
        throw new Error(`Unscripted fetch: ${resolved}`);
      }
      const status = next.status ?? 200;
      const ok = status >= 200 && status < 300;
      const text = JSON.stringify(next.body);
      return {
        ok,
        status,
        statusText: ok ? "OK" : "Error",
        text: async () => text,
      } as unknown as Response;
    },
  );
  (globalThis as { fetch: typeof fetch }).fetch =
    spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function highConfidenceScanBody(): unknown {
  return {
    vendor: { value: "Carrefour", confidence: 0.95 },
    purchased_at: { value: "2026-04-20T00:00:00Z", confidence: 0.92 },
    currency: { value: "EUR", confidence: 0.99 },
    total_amount_cents: { value: 1234, confidence: 0.97 },
    category: { value: "supplies", confidence: 0.88 },
    note_md: { value: "", confidence: 0.5 },
    agent_question: null,
  };
}

function pickerInput(): HTMLInputElement {
  // The picker label wraps a hidden <input type="file">. Querying by
  // role would skip it (hidden), so we reach in via the label.
  const label = screen.getByText("Scan a receipt or screenshot")
    .parentElement as HTMLElement;
  const input = label.querySelector(
    "input[type=file]",
  ) as HTMLInputElement | null;
  if (!input) throw new Error("file input not found");
  return input;
}

function selectFile(file: File): void {
  const input = pickerInput();
  // jsdom's <input type=file> needs a manual `files` setter.
  Object.defineProperty(input, "files", {
    value: [file],
    configurable: true,
  });
  fireEvent.change(input);
}

/**
 * Drain the spinner-floor timer + any pending micro-tasks. Used
 * instead of `waitFor` because `vi.useFakeTimers()` halts the
 * polling library used by `waitFor`. Calling under `act` keeps
 * React's batching honest.
 */
async function drainSpinnerFloor(): Promise<void> {
  await act(async () => {
    await vi.runAllTimersAsync();
  });
}

beforeEach(() => {
  __resetApiProvidersForTests();
  registerWorkspaceSlugGetter(() => "acme");
  document.cookie = "crewday_csrf=; path=/; max-age=0";
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  vi.useRealTimers();
});

describe("ReceiptScanPanel — happy path", () => {
  it("posts the receipt as multipart/form-data and forwards the parsed result", async () => {
    const env = installFetch([{ body: highConfidenceScanBody() }]);
    try {
      const onScanResult = vi.fn();
      const onScanStarted = vi.fn();
      const onScanFailed = vi.fn();

      render(
        <ReceiptScanPanel
          phase="upload"
          onScanResult={onScanResult}
          onScanStarted={onScanStarted}
          onScanFailed={onScanFailed}
        />,
      );

      const file = new File(["receipt-bytes"], "receipt.jpg", {
        type: "image/jpeg",
      });
      selectFile(file);

      // `onScanStarted` fires synchronously off the change event,
      // before the spinner-floor promise resolves.
      expect(onScanStarted).toHaveBeenCalledTimes(1);

      await drainSpinnerFloor();

      // The fetch landed once, with the right URL + multipart body.
      expect(env.calls).toHaveLength(1);
      const call = env.calls[0]!;
      expect(call.init.method).toBe("POST");
      expect(call.url).toContain("/w/acme/api/v1/expenses/scan");
      const body = call.init.body;
      // `fetchJson` passes FormData straight through; the browser
      // attaches the multipart boundary itself (Content-Type unset).
      expect(body).toBeInstanceOf(FormData);
      const fd = body as FormData;
      const sentImage = fd.get("image");
      expect(sentImage).toBeInstanceOf(File);
      expect((sentImage as File).name).toBe("receipt.jpg");
      expect((sentImage as File).type).toBe("image/jpeg");
      const headers = call.init.headers as Record<string, string>;
      expect(headers["Content-Type"]).toBeUndefined();
      expect(headers["Accept"]).toBe("application/json");

      expect(onScanResult).toHaveBeenCalledTimes(1);
      expect(onScanFailed).not.toHaveBeenCalled();
      const forwarded = onScanResult.mock.calls[0]![0];
      expect(forwarded.vendor.value).toBe("Carrefour");
      expect(forwarded.total_amount_cents.value).toBe(1234);
    } finally {
      env.restore();
    }
  });
});

describe("ReceiptScanPanel — client-side validation", () => {
  it("rejects a non-receipt mime without hitting the server", async () => {
    const env = installFetch([]);
    try {
      const onScanStarted = vi.fn();
      const onScanFailed = vi.fn();

      render(
        <ReceiptScanPanel
          phase="upload"
          onScanResult={vi.fn()}
          onScanStarted={onScanStarted}
          onScanFailed={onScanFailed}
        />,
      );

      const file = new File(["…"], "evil.exe", {
        type: "application/x-msdownload",
      });
      selectFile(file);

      // No phase transition — we never even called the server.
      expect(onScanStarted).not.toHaveBeenCalled();
      expect(onScanFailed).not.toHaveBeenCalled();
      expect(env.calls).toHaveLength(0);

      // The notice mounts synchronously off the change handler.
      const notice = screen.getByRole("alert");
      expect(notice).toHaveTextContent(
        /can't read that format yet — try a JPEG, PNG, WebP, HEIC, or PDF/i,
      );
    } finally {
      env.restore();
    }
  });

  it("accepts every documented mime in the server allow-list", async () => {
    // One scripted response per mime; we don't care about the body —
    // we only assert each pick reaches `fetch` exactly once and the
    // panel hands the parsed result back via `onScanResult`.
    const mimes = [
      ["image/jpeg", "r.jpg"],
      ["image/png", "r.png"],
      ["image/webp", "r.webp"],
      ["image/heic", "r.heic"],
      ["application/pdf", "r.pdf"],
    ] as const;
    const env = installFetch(
      mimes.map(() => ({ body: highConfidenceScanBody() })),
    );
    try {
      for (const [mime, name] of mimes) {
        const onScanResult = vi.fn();
        const { unmount } = render(
          <ReceiptScanPanel
            phase="upload"
            onScanResult={onScanResult}
            onScanStarted={vi.fn()}
            onScanFailed={vi.fn()}
          />,
        );
        selectFile(new File(["…"], name, { type: mime }));
        await drainSpinnerFloor();
        expect(onScanResult).toHaveBeenCalledTimes(1);
        unmount();
      }
      expect(env.calls).toHaveLength(mimes.length);
      for (const call of env.calls) {
        expect(call.init.method).toBe("POST");
        expect(call.url).toContain("/api/v1/expenses/scan");
        expect(call.init.body).toBeInstanceOf(FormData);
      }
    } finally {
      env.restore();
    }
  });
});

describe("ReceiptScanPanel — server error mapping", () => {
  // Drive each server error through the component and assert (a) the
  // parent flips back via `onScanFailed`, and (b) the inline notice
  // says the right plain-English line.
  const cases: ReadonlyArray<{
    label: string;
    status: number;
    body: { type: string; title: string; detail?: string };
    expectMessage: RegExp;
  }> = [
    {
      label: "scan_not_configured (503) → manual-entry hint",
      status: 503,
      body: {
        type: "https://crewday.dev/errors/scan_not_configured",
        title: "Scan not configured",
        detail: "settings.llm_ocr_model is unset",
      },
      expectMessage: /scanning isn't enabled here yet/i,
    },
    {
      label: "blob_too_large (422) → 10 MB hint",
      status: 422,
      body: {
        type: "https://crewday.dev/errors/blob_too_large",
        title: "File too large",
      },
      expectMessage: /too large.*under 10 MB/i,
    },
    {
      label: "blob_empty (422) → re-pick",
      status: 422,
      body: {
        type: "https://crewday.dev/errors/blob_empty",
        title: "Empty upload",
      },
      expectMessage: /looked empty/i,
    },
    {
      label: "blob_mime_not_allowed (422) → format hint",
      status: 422,
      body: {
        type: "https://crewday.dev/errors/blob_mime_not_allowed",
        title: "Mime not allowed",
      },
      expectMessage: /can't read that format yet/i,
    },
    {
      label: "extraction_timeout (504) → retry-or-manual",
      status: 504,
      body: {
        type: "https://crewday.dev/errors/extraction_timeout",
        title: "Extraction timed out",
      },
      expectMessage: /reader is having a moment/i,
    },
    {
      label: "extraction_rate_limited (503) → retry-or-manual",
      status: 503,
      body: {
        type: "https://crewday.dev/errors/extraction_rate_limited",
        title: "Rate limited",
      },
      expectMessage: /reader is having a moment/i,
    },
    {
      label: "extraction_provider_error (503) → retry-or-manual",
      status: 503,
      body: {
        type: "https://crewday.dev/errors/extraction_provider_error",
        title: "Provider error",
      },
      expectMessage: /reader is having a moment/i,
    },
    {
      label: "extraction_parse_error (422) → retry-or-manual",
      status: 422,
      body: {
        type: "https://crewday.dev/errors/extraction_parse_error",
        title: "Parse error",
        detail: "model returned malformed JSON",
      },
      expectMessage: /reader is having a moment/i,
    },
    {
      label: "extraction_invariant (500) → retry-or-manual",
      status: 500,
      body: {
        type: "https://crewday.dev/errors/extraction_invariant",
        title: "Internal extraction invariant",
      },
      expectMessage: /reader is having a moment/i,
    },
  ];

  for (const tc of cases) {
    it(`surfaces the right line for ${tc.label}`, async () => {
      const env = installFetch([{ status: tc.status, body: tc.body }]);
      try {
        const onScanFailed = vi.fn();
        const onScanResult = vi.fn();
        // The panel is the unit-under-test — we leave `phase` pinned
        // at `upload` so the inline notice it sets locally on a
        // failure stays mounted (in production the parent's
        // `onScanFailed` reverts an in-flight `processing` back to
        // `upload`; under unit, the panel renders the notice as
        // long as `phase === "upload"`).
        render(
          <ReceiptScanPanel
            phase="upload"
            onScanResult={onScanResult}
            onScanStarted={vi.fn()}
            onScanFailed={onScanFailed}
          />,
        );

        selectFile(
          new File(["…"], "receipt.jpg", { type: "image/jpeg" }),
        );

        await drainSpinnerFloor();

        const notice = screen.getByRole("alert");
        expect(notice).toHaveTextContent(tc.expectMessage);
        expect(onScanFailed).toHaveBeenCalledTimes(1);
        expect(onScanResult).not.toHaveBeenCalled();
      } finally {
        env.restore();
      }
    });
  }
});

describe("ReceiptScanPanel — notice lifecycle", () => {
  it("clears the inline error after the parent leaves the picker (manual entry → back)", async () => {
    // Reproduces a stale-notice path: scan fails → "+ New expense"
    // (parent flips to `review`) → "Back" (parent flips to `upload`).
    // Without the phase-transition reset the old failure notice
    // would still be visible against an unrelated fresh picker.
    const env = installFetch([
      {
        status: 422,
        body: {
          type: "https://crewday.dev/errors/blob_too_large",
          title: "File too large",
        },
      },
    ]);
    try {
      const onScanFailed = vi.fn();
      const { rerender } = render(
        <ReceiptScanPanel
          phase="upload"
          onScanResult={vi.fn()}
          onScanStarted={vi.fn()}
          onScanFailed={onScanFailed}
        />,
      );

      selectFile(new File(["…"], "huge.jpg", { type: "image/jpeg" }));
      await drainSpinnerFloor();
      expect(screen.getByRole("alert")).toHaveTextContent(/too large/i);

      // Parent transitions to `review` (manual entry) → notice gone.
      rerender(
        <ReceiptScanPanel
          phase="review"
          onScanResult={vi.fn()}
          onScanStarted={vi.fn()}
          onScanFailed={onScanFailed}
        />,
      );
      expect(screen.queryByRole("alert")).toBeNull();

      // Parent transitions back to `upload` (Back button) → still
      // gone, picker is rendered fresh.
      rerender(
        <ReceiptScanPanel
          phase="upload"
          onScanResult={vi.fn()}
          onScanStarted={vi.fn()}
          onScanFailed={onScanFailed}
        />,
      );
      expect(screen.queryByRole("alert")).toBeNull();
      expect(
        screen.getByText("Scan a receipt or screenshot"),
      ).toBeInTheDocument();
    } finally {
      env.restore();
    }
  });
});

describe("messageForScanError — fallbacks", () => {
  it("falls back to the server detail when the type is unknown", () => {
    const err = new ApiError("something", 422, {
      type: "https://crewday.dev/errors/some_new_thing",
      title: "Brand-new error",
      detail: "A specific human line from the server.",
    });
    expect(messageForScanError(err)).toBe(
      "A specific human line from the server.",
    );
  });

  it("falls back to the title when no detail is present", () => {
    const err = new ApiError("something", 422, {
      type: "https://crewday.dev/errors/some_new_thing",
      title: "Brand-new error",
    });
    expect(messageForScanError(err)).toBe("Brand-new error");
  });

  it("returns a generic line for non-ApiError throws", () => {
    expect(messageForScanError(new Error("boom"))).toMatch(
      /couldn't read that receipt/i,
    );
    expect(messageForScanError("not even an Error")).toMatch(
      /couldn't read that receipt/i,
    );
  });
});
