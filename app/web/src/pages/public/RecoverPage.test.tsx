// crewday — RecoverPage component test.
//
// What this covers:
//   1. Happy path — fill in the email, submit, server returns 202 →
//      form disappears, "check your email" confirmation replaces it.
//      The request body carries ONLY the email (no break-glass code)
//      when the step-up checkbox is off. Focus lands on the "Check
//      your email" heading so keyboard / screen-reader users aren't
//      stranded on the unmounted submit button.
//   2. Step-up branch — checking "I'm a manager or owner" reveals the
//      break-glass input; a subsequent submit sends both the email
//      and the break-glass code.
//   3. 429 rate-limit — server returns 429 → a danger notice surfaces
//      the "slow down" copy; the form stays visible so the user can
//      retry after a minute; the confirmation view is NOT shown.
//   4. 500 server error — server returns 500 → the generic "couldn't
//      send the link" fallback surfaces; the form is still
//      interactive so the user can retry. Mirrors LoginPage's
//      non-429 error parity.
//   5. Empty email guard — `fireEvent.submit` bypasses the browser's
//      native `required` validation (jsdom honours it for synthesised
//      `submit` events, but belt-and-braces on the component seam is
//      cheap). An empty/whitespace email must NOT fire the mutation.
//   6. Concurrency guard — a synchronous double-submit (two submits
//      in the same tick, before React commits `pending`) must
//      coalesce into a single request. Mirrors LoginPage's cd-4z54
//      fix so a keyboard-Enter-spam doesn't burn two throttle slots.
//
// What this does NOT cover (and why):
//   - The Playwright pixel-diff against the mock — cd-gids's e2e
//     acceptance run by the Director.
//   - The actual magic-link-click / enrollment ceremony — that lands
//     on `/recover/enroll` and is covered by separate tests.
//   - The "sent_if_exists" audit trail — server concern; the UI's
//     job is only to always render the generic confirmation on 2xx,
//     which is exactly what test #1 asserts.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactElement } from "react";
import RecoverPage from "./RecoverPage";
import { __resetApiProvidersForTests } from "@/lib/api";

// ── Test harness ──────────────────────────────────────────────────

interface FakeResponse {
  status: number;
  body?: unknown;
}

/**
 * Scripted `fetch`. Mirrors the shape used in `LoginPage.test.tsx`:
 * one FIFO queue per URL suffix so a multi-request test can assert
 * on order without fighting a shared `responses[]`.
 */
function installFetch(scripted: Record<string, FakeResponse[]>): {
  calls: Array<{ url: string; init: RequestInit }>;
  restore: () => void;
} {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const original = globalThis.fetch;
  const queues: Record<string, FakeResponse[]> = {};
  for (const [k, v] of Object.entries(scripted)) queues[k] = [...v];
  const spy = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const resolved = typeof url === "string" ? url : url.toString();
    calls.push({ url: resolved, init: init ?? {} });
    const suffix = Object.keys(queues).find((s) => resolved.endsWith(s));
    if (!suffix) throw new Error(`Unscripted fetch: ${resolved}`);
    const next = queues[suffix]!.shift();
    if (!next) throw new Error(`No more responses for: ${resolved}`);
    const ok = next.status >= 200 && next.status < 300;
    const text = next.body === undefined ? "" : JSON.stringify(next.body);
    return {
      ok,
      status: next.status,
      statusText: ok ? "OK" : "Error",
      text: async () => text,
    } as unknown as Response;
  });
  (globalThis as { fetch: typeof fetch }).fetch = spy as unknown as typeof fetch;
  return {
    calls,
    restore: () => {
      (globalThis as { fetch: typeof fetch }).fetch = original;
    },
  };
}

function Harness(): ReactElement {
  // Fresh QueryClient per render so one test's retry state doesn't
  // leak into the next. `retry: false` keeps 429 from climbing the
  // exponential-backoff ladder while we're asserting.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={qc}>
      <RecoverPage />
    </QueryClientProvider>
  );
}

async function flush(): Promise<void> {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

beforeEach(() => {
  __resetApiProvidersForTests();
});

afterEach(() => {
  cleanup();
  __resetApiProvidersForTests();
  vi.unstubAllGlobals();
});

// ── Tests ─────────────────────────────────────────────────────────

describe("<RecoverPage> — happy path", () => {
  it("submits the email and shows the 'check your email' confirmation on 202", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/recover/passkey/request": [
        { status: 202, body: { status: "accepted" } },
      ],
    });

    try {
      render(<Harness />);

      // Headline + form render from the idle state.
      expect(screen.getByText("Lost your device?")).toBeInTheDocument();
      const emailInput = screen.getByTestId("recover-email") as HTMLInputElement;
      fireEvent.change(emailInput, { target: { value: "maria@example.com" } });

      await act(async () => {
        fireEvent.submit(emailInput.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      // Exactly one call to the real endpoint; body carries email only.
      expect(calls).toHaveLength(1);
      expect(calls[0]!.url.endsWith("/api/v1/recover/passkey/request")).toBe(true);
      expect(calls[0]!.init.method).toBe("POST");
      const sent = JSON.parse(calls[0]!.init.body as string) as Record<string, unknown>;
      expect(sent).toEqual({ email: "maria@example.com" });

      // The form is replaced by the generic confirmation — the
      // "sent_if_exists" contract means this renders for ANY 2xx,
      // even if the email doesn't match an account.
      const sentPanel = screen.getByTestId("recover-sent");
      expect(sentPanel).toBeInTheDocument();
      // `role="status"` + `aria-live="polite"` so assistive tech is
      // notified that the form was replaced rather than just vanished.
      expect(sentPanel.getAttribute("role")).toBe("status");
      expect(sentPanel.getAttribute("aria-live")).toBe("polite");
      const heading = screen.getByText("Check your email");
      expect(heading).toBeInTheDocument();
      // Focus moves to the confirmation heading — otherwise keyboard
      // focus is stranded on the unmounted submit button.
      expect(document.activeElement).toBe(heading);
      // Form inputs are no longer in the DOM.
      expect(screen.queryByTestId("recover-email")).toBeNull();
      expect(screen.queryByTestId("recover-submit")).toBeNull();
      // "Back to sign in" link survives the state swap.
      expect(screen.getByText("← Back to sign in")).toBeInTheDocument();
    } finally {
      restore();
    }
  });
});

describe("<RecoverPage> — step-up branch", () => {
  it("reveals the break-glass code field and submits it alongside the email", async () => {
    const { calls, restore } = installFetch({
      "/api/v1/recover/passkey/request": [
        { status: 202, body: { status: "accepted" } },
      ],
    });

    try {
      render(<Harness />);

      // Break-glass field is hidden until the step-up toggle flips.
      expect(screen.queryByTestId("recover-code")).toBeNull();

      const toggle = screen.getByTestId("recover-stepup-toggle") as HTMLInputElement;
      fireEvent.click(toggle);
      expect(toggle.checked).toBe(true);

      // The input now renders with the mock's `recovery-code` class.
      const code = screen.getByTestId("recover-code") as HTMLInputElement;
      expect(code.className).toContain("recovery-code");

      const email = screen.getByTestId("recover-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "elodie@example.com" } });
      fireEvent.change(code, { target: { value: "ABCDE12345" } });

      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      const sent = JSON.parse(calls[0]!.init.body as string) as Record<string, unknown>;
      expect(sent).toEqual({
        email: "elodie@example.com",
        break_glass_code: "ABCDE12345",
      });
      // Step-up submissions still land on the same confirmation so
      // the enumeration guard applies uniformly.
      expect(screen.getByTestId("recover-sent")).toBeInTheDocument();
    } finally {
      restore();
    }
  });
});

describe("<RecoverPage> — error branches", () => {
  it("surfaces the rate-limit notice on 429 and leaves the form visible", async () => {
    const { restore } = installFetch({
      "/api/v1/recover/passkey/request": [
        {
          status: 429,
          body: { type: "rate_limited", title: "Slow down", detail: "Too many requests." },
        },
      ],
    });

    try {
      render(<Harness />);

      const email = screen.getByTestId("recover-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "spammer@example.com" } });

      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      const notice = screen.getByTestId("recover-error");
      expect(notice.textContent).toContain("Too many recovery requests");
      expect(notice.className).toContain("login__notice--danger");

      // Form is still visible — the user can retry after the window.
      // The confirmation view must NOT render on error.
      expect(screen.queryByTestId("recover-sent")).toBeNull();
      const submit = screen.getByTestId("recover-submit") as HTMLButtonElement;
      expect(submit.disabled).toBe(false);
    } finally {
      restore();
    }
  });

  it("surfaces the generic fallback notice on 500 so the user can retry", async () => {
    // Non-429 errors take the `messageFor` fallback branch — without
    // this test, dropping the `ApiError` import or mis-wiring the
    // mutation's `onError` could silently ship a blank card. Mirrors
    // LoginPage's "we couldn't sign you in" parity.
    const { restore } = installFetch({
      "/api/v1/recover/passkey/request": [
        {
          status: 500,
          body: { type: "internal_error", title: "Boom", detail: "Unexpected failure." },
        },
      ],
    });

    try {
      render(<Harness />);

      const email = screen.getByTestId("recover-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });

      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      const notice = screen.getByTestId("recover-error");
      expect(notice.textContent).toContain("We couldn't send the recovery link");
      // The server's `detail` must NOT be surfaced verbatim — the
      // enumeration guard demands a stable UI regardless of what the
      // server body leaks.
      expect(notice.textContent).not.toContain("Unexpected failure");
      // Form remains interactive.
      expect(screen.queryByTestId("recover-sent")).toBeNull();
      const submit = screen.getByTestId("recover-submit") as HTMLButtonElement;
      expect(submit.disabled).toBe(false);
    } finally {
      restore();
    }
  });
});

describe("<RecoverPage> — input guards", () => {
  it("does not fire a request when the email is empty or whitespace", async () => {
    // jsdom honours `required` for user-initiated submits, but
    // `fireEvent.submit` on the form element bypasses that gate. The
    // component's `!trimmedEmail` guard is the belt-and-braces
    // defence; this test pins that behaviour so a future refactor
    // that leans entirely on HTML5 validation doesn't silently send
    // `{email: ""}` to the throttle budget.
    const { calls, restore } = installFetch({
      // Scripted with an empty queue — if the component ever fires,
      // the spy will throw "No more responses for: …" and fail.
      "/api/v1/recover/passkey/request": [],
    });

    try {
      render(<Harness />);

      const email = screen.getByTestId("recover-email") as HTMLInputElement;
      // First: empty email.
      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      expect(calls).toHaveLength(0);

      // Second: whitespace-only email — the trim guard must still
      // reject it.
      fireEvent.change(email, { target: { value: "   " } });
      await act(async () => {
        fireEvent.submit(email.closest("form")!);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();
      expect(calls).toHaveLength(0);

      // Form still in the idle view — no confirmation, no error.
      expect(screen.queryByTestId("recover-sent")).toBeNull();
      expect(screen.queryByTestId("recover-error")).toBeNull();
    } finally {
      restore();
    }
  });
});

describe("<RecoverPage> — concurrency guard", () => {
  it("coalesces a synchronous double-submit into a single request", async () => {
    // `disabled={pending}` only takes effect after React commits the
    // pending state. A synchronous pair of submits (keyboard
    // Enter-spam, double form submission) in the same tick can both
    // pass `mutation.isPending === false` and enqueue two POSTs.
    // The inflight-ref guard is what stops them — mirrors
    // LoginPage's cd-4z54 fix.
    //
    // Only ONE response is scripted. A second POST would throw
    // "No more responses for: …" and fail the test, which is
    // exactly the regression we want to catch.
    const { calls, restore } = installFetch({
      "/api/v1/recover/passkey/request": [
        { status: 202, body: { status: "accepted" } },
      ],
    });

    try {
      render(<Harness />);

      const email = screen.getByTestId("recover-email") as HTMLInputElement;
      fireEvent.change(email, { target: { value: "maria@example.com" } });

      await act(async () => {
        const form = email.closest("form")!;
        // Two synchronous submits in the same tick — before React
        // commits the pending state. The ref must coalesce them.
        fireEvent.submit(form);
        fireEvent.submit(form);
        await new Promise((r) => setTimeout(r, 0));
      });
      await flush();

      const postCalls = calls.filter((c) =>
        c.url.endsWith("/api/v1/recover/passkey/request"),
      );
      expect(postCalls.length).toBe(1);
      // Confirmation still renders — the second submit was dropped,
      // not swallowed by an error.
      expect(screen.getByTestId("recover-sent")).toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
