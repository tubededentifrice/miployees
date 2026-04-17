// Static (mock) recovery surface. The real flow is manager-initiated
// (`crewday admin recover`) — this page only shows what the operator
// would see after they receive the one-time code.

export default function RecoverPage() {
  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crewday</span>
          </div>
          <h1 className="login__headline">Lost your device?</h1>
          <p className="login__sub">
            Ask your manager to run <code className="inline-code">crewday admin recover</code>{" "}
            and send you a one-time recovery code. Then enter it below.
          </p>
          <form className="form" onSubmit={(e) => e.preventDefault()}>
            <label className="field">
              <span>Your email</span>
              <input type="email" placeholder="you@example.com" required />
            </label>
            <label className="field">
              <span>Recovery code</span>
              <input className="recovery-code" placeholder="XXXX-XXXX-XXXX" required />
            </label>
            <button type="button" className="btn btn--moss btn--lg">
              Verify &amp; enroll new passkey
            </button>
          </form>
          <p className="login__footnote muted">
            Codes expire after one use or 15 minutes, whichever comes first.
            The manager has to kick off recovery from the host — there's no self-service
            path, by design. See §03 of the spec.
          </p>
          <a href="/login" className="login__recover">← Back to sign in</a>
        </div>
      </main>
    </div>
  );
}
