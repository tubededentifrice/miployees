// Static (mock) sign-in surface. Passkey is the only credential
// (§03 Principles). Magic links never issue a session on their own —
// users who have lost every passkey device recover via /recover
// (§03 "Self-service lost-device recovery").

import { KeyRound } from "lucide-react";

export default function LoginPage() {
  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>
          <h1 className="login__headline">Sign in with your passkey</h1>
          <p className="login__sub">No passwords, ever. Tap once to unlock the house.</p>
          <button className="btn btn--moss btn--lg login__primary" type="button">
            <KeyRound size={18} strokeWidth={1.8} aria-hidden="true" /> Use passkey
          </button>
          <a href="/recover" className="login__recover">Lost your device? Recover access →</a>
        </div>
        <p className="login__footnote">
          First time here? Open the invite link your manager sent.{" "}
          <a href="/accept/demo-abc123" className="link">See what accepting an invite looks like →</a>
        </p>
      </main>
    </div>
  );
}
