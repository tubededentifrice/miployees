// Static (mock) sign-in surface. The form is decorative — passkey
// registration / magic-link issuance are owned by the real auth flow,
// not the mocks app.

export default function LoginPage() {
  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crewday</span>
          </div>
          <h1 className="login__headline">Sign in with your passkey</h1>
          <p className="login__sub">No passwords, ever. Tap once to unlock the house.</p>
          <button className="btn btn--moss btn--lg login__primary" type="button">🔑 Use passkey</button>
          <div className="login__divider"><span>or</span></div>
          <button className="btn btn--ghost btn--lg" type="button">Email me a magic link</button>
          <a href="/recover" className="login__recover">I lost my device</a>
        </div>
        <p className="login__footnote">
          First time here? Your manager will email you an enrollment link.{" "}
          <a href="/enroll/demo-abc123" className="link">See what enrollment looks like →</a>
        </p>
      </main>
    </div>
  );
}
