// crewday — production `/recover` surface.
//
// Self-service lost-device recovery (§03 "Self-service lost-device
// recovery"). The user lost every passkey and needs a magic link to
// enrol a fresh one on a new device. Workers, clients, and guests
// enter their email and nothing else; managers and owners-group
// members additionally enter an unused break-glass code — the
// step-up branch (§03 "Entry point").
//
// Visual contract mirrors `mocks/web/src/pages/public/RecoverPage.tsx`
// verbatim: every semantic class (`login__card`, `field`,
// `field--inline`, `recovery-code`, `login__recover`, …) is preserved
// so the mock's CSS applies unchanged. The only additions beyond the
// mock are (a) a confirmation view that replaces the form after a
// successful submit, (b) a `login__notice` for rate-limit / unexpected
// errors, and (c) a `pending` state on the submit button. The mock
// has none of these because it is visual-only.
//
// Server contract (`POST /api/v1/recover/passkey/request`):
//   - 202 { status: "accepted" } on both hit and miss — the
//     enumeration guard means we cannot discriminate. The UI ALWAYS
//     swaps to the "check your email" confirmation on 2xx and MUST
//     NOT expose whether the email is known.
//   - 429 — rate limited; surface a friendly "slow down" notice so
//     the user doesn't retry immediately.
//   - Other non-2xx — generic "couldn't send the link" notice; the
//     user can retry.
//
// The break-glass code is kept in local component state and not yet
// wired to the request body — the current server schema
// (`RecoveryRequestBody`) takes `email` only. When the backend adds
// `break_glass_code` (§03), the payload below extends without a UI
// change. The step-up state machine is preserved exactly so the
// visual diff against the mock stays empty.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactElement,
  type RefObject,
} from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError, fetchJson } from "@/lib/api";

interface RecoverRequestBody {
  email: string;
  // Forward-compat: the server will accept this once the step-up
  // validation lands. Serialised only when non-empty to avoid sending
  // an empty string that the current schema would reject.
  break_glass_code?: string;
}

interface RecoverRequestResponse {
  status: string;
}

type FormState =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "sent" }
  | { kind: "error"; message: string };

export default function RecoverPage() {
  const [stepUp, setStepUp] = useState(false);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [form, setForm] = useState<FormState>({ kind: "idle" });
  // Concurrency guard. `disabled={pending}` only blocks the NEXT click
  // after React commits the pending state, so a synchronous burst
  // (double-click, Enter held down, a scripted Playwright submit
  // followed immediately by a keyboard press) can enqueue two
  // mutations before `onMutate` runs. Without this ref the server
  // sees two `/recover/passkey/request` POSTs — burning two attempts
  // against the per-IP throttle budget and writing two
  // `audit.recovery.requested` rows for a single user intent. Mirrors
  // LoginPage's fix for cd-4z54.
  const inflightRef = useRef(false);
  // Focus pivot for the "sent" confirmation. When the form is
  // replaced, keyboard focus is stranded on the now-removed submit
  // button. We move it to the confirmation heading so screen readers
  // announce the new view and keyboard users regain an anchor.
  const sentHeadingRef = useRef<HTMLHeadingElement | null>(null);

  const mutation = useMutation<RecoverRequestResponse, Error, RecoverRequestBody>({
    mutationFn: (body) =>
      fetchJson<RecoverRequestResponse>("/api/v1/recover/passkey/request", {
        method: "POST",
        body,
      }),
    onMutate: () => {
      setForm({ kind: "pending" });
    },
    onSuccess: () => {
      // Enumeration guard: we swap to the "check your email"
      // confirmation on ANY 2xx response. The server's audit log
      // discriminates hit from miss; the UI never does.
      setForm({ kind: "sent" });
      inflightRef.current = false;
    },
    onError: (err) => {
      setForm({ kind: "error", message: messageFor(err) });
      inflightRef.current = false;
    },
  });

  const onSubmit = useCallback(
    (e: FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      // Ref check before `isPending` — the mutation's pending flag
      // only flips on the microtask after `mutate()` returns, so two
      // synchronous submits in the same tick can both pass
      // `mutation.isPending === false`. The ref flips synchronously
      // here and is cleared from `onSuccess` / `onError`.
      if (inflightRef.current) return;
      if (mutation.isPending) return;
      const trimmedEmail = email.trim();
      if (!trimmedEmail) return;
      inflightRef.current = true;
      const body: RecoverRequestBody = { email: trimmedEmail };
      const trimmedCode = code.trim();
      if (stepUp && trimmedCode) {
        body.break_glass_code = trimmedCode;
      }
      mutation.mutate(body);
    },
    [mutation, email, code, stepUp],
  );

  const pending = form.kind === "pending";

  // Move focus to the confirmation heading once the form is replaced
  // by the success view. Keyboard focus would otherwise be stranded
  // on the unmounted submit button; screen readers re-announce the
  // new heading when it receives focus.
  useEffect(() => {
    if (form.kind === "sent") {
      sentHeadingRef.current?.focus();
    }
  }, [form.kind]);

  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>
          {form.kind === "sent" ? (
            <RecoverSentConfirmation headingRef={sentHeadingRef} />
          ) : (
            <>
              <h1 className="login__headline">Lost your device?</h1>
              <p className="login__sub">
                We'll email you a one-time link that lets you register a new passkey. Your old
                passkeys are revoked when the new one is saved.
              </p>
              {form.kind === "error" && (
                <p
                  className="login__notice login__notice--danger"
                  role="alert"
                  data-testid="recover-error"
                >
                  {form.message}
                </p>
              )}
              <form className="form" onSubmit={onSubmit}>
                <label className="field">
                  <span>Your email</span>
                  <input
                    type="email"
                    placeholder="you@example.com"
                    autoComplete="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    data-testid="recover-email"
                  />
                </label>

                <label className="field field--inline">
                  <input
                    type="checkbox"
                    checked={stepUp}
                    onChange={(e) => setStepUp(e.target.checked)}
                    data-testid="recover-stepup-toggle"
                  />
                  <span>I'm a manager or owner (I have a break-glass code)</span>
                </label>

                {stepUp ? (
                  <label className="field">
                    <span>Break-glass code</span>
                    <input
                      className="recovery-code"
                      placeholder="XXXXXXXXXX"
                      autoComplete="one-time-code"
                      required
                      value={code}
                      onChange={(e) => setCode(e.target.value)}
                      data-testid="recover-code"
                    />
                  </label>
                ) : null}

                <button
                  type="submit"
                  className="btn btn--moss btn--lg"
                  disabled={pending}
                  aria-busy={pending}
                  data-testid="recover-submit"
                >
                  {pending ? "Sending recovery link…" : "Send recovery link"}
                </button>
              </form>
              <p className="login__footnote muted">
                Links expire after one use or 15 minutes, whichever comes first. If nothing arrives,
                your workspace may have disabled self-service recovery — ask a manager to re-issue
                your link.
              </p>
            </>
          )}
          <a href="/login" className="login__recover">← Back to sign in</a>
        </div>
      </main>
    </div>
  );
}

// ── Internals ─────────────────────────────────────────────────────

/**
 * Generic "check your email" confirmation shown on any 2xx response
 * from `/recover/passkey/request`. The copy deliberately does NOT
 * confirm that the email matched an account — the server replies
 * 202 on both hit and miss so the UI cannot leak the discriminator.
 *
 * `role="status"` + `aria-live="polite"` announces the swap to
 * screen readers — without it, assistive tech sees the form vanish
 * with no replacement context. `tabIndex={-1}` on the heading makes
 * it programmatically focusable so the parent effect can move
 * keyboard focus off the unmounted submit button and onto a stable
 * landmark.
 */
function RecoverSentConfirmation({
  headingRef,
}: {
  headingRef: RefObject<HTMLHeadingElement | null>;
}): ReactElement {
  return (
    <div data-testid="recover-sent" role="status" aria-live="polite">
      <h1 className="login__headline" ref={headingRef} tabIndex={-1}>
        Check your email
      </h1>
      <p className="login__sub">
        If that address matches an account, we've sent a one-time link that lets you register a
        new passkey. The link expires after 15 minutes or one use, whichever comes first.
      </p>
      <p className="login__footnote muted">
        Nothing in your inbox? Check spam, wait a minute, then try again. Repeated requests may be
        rate-limited.
      </p>
    </div>
  );
}

/**
 * Translate a mutation error into a short, user-facing line.
 *
 * 429 gets a dedicated "slow down" message so the user understands
 * why the next click won't help; everything else collapses to a
 * generic "try again" line. We deliberately do NOT surface server
 * `detail` verbatim — the endpoint's enumeration guard means we want
 * a stable UI regardless of whether the server leaked more context.
 */
function messageFor(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 429) {
      return "Too many recovery requests. Wait a minute and try again.";
    }
  }
  return "We couldn't send the recovery link. Try again in a moment.";
}
