import { useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { KeyRound } from "lucide-react";

// Static (mock) click-to-accept invitation surface (§03
// "Additional users (invite → click-to-accept)"). One URL,
// two rendered states:
//   • ?state=new (default) — the invitee has no users row yet,
//     so the flow chains straight into the passkey-registration
//     ceremony. Grants activate on `webauthn/finish_registration`.
//   • ?state=existing — the email matched a users row. The user
//     signs in with their existing passkey (not re-registered)
//     and sees an Acceptance card itemising the grants being
//     added. Pending rows only activate on the explicit Accept.
// The `state` query param is a demo convenience; the real app
// reads the invite record server-side.

export default function AcceptPage() {
  const { token = "" } = useParams<{ token: string }>();
  const [params] = useSearchParams();
  const existing = params.get("state") === "existing";
  const [accepted, setAccepted] = useState(false);

  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card login__card--wide">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crew.day</span>
          </div>

          {existing ? (
            <>
              <h1 className="login__headline">You've been invited to more surfaces</h1>
              <p className="login__sub">
                Élodie is adding you to two more properties on your existing crew.day
                account. Nothing changes until you accept below.
              </p>

              <section className="panel panel--inset">
                <header className="panel__head"><h2>What will change</h2></header>
                <ul className="settings-list">
                  <li>
                    <strong>Housekeeper</strong> at <em>Villa Sud</em>
                    <span className="muted"> — task assignments, bookings, expenses</span>
                  </li>
                  <li>
                    <strong>Housekeeper</strong> at <em>Apt 3B</em>
                    <span className="muted"> — task assignments, bookings, expenses</span>
                  </li>
                </ul>
                <p className="muted">
                  No passkey re-registration. No break-glass regeneration. Your other
                  workspaces are untouched.
                </p>
              </section>

              {accepted ? (
                <p className="empty-state empty-state--quiet">
                  Accepted — the new scopes appear in your workspace switcher.
                </p>
              ) : (
                <div className="form__actions">
                  <button
                    type="button"
                    className="btn btn--moss btn--lg"
                    onClick={() => setAccepted(true)}
                  >
                    Accept
                  </button>
                  <button type="button" className="btn btn--ghost btn--lg">Not now</button>
                </div>
              )}
            </>
          ) : (
            <>
              <h1 className="login__headline">Welcome to the household, Maria</h1>
              <p className="login__sub">
                Élodie has added you as <strong>Housekeeper</strong> at Villa Sud and Apt 3B.
              </p>

              <ol className="enroll-steps">
                <li className="enroll-step enroll-step--done">
                  <span className="enroll-step__num">1</span>
                  <div>
                    <strong>Confirm it's you</strong>
                    <p>
                      This link was sent to <code className="inline-code">maria@example.com</code>.
                      If that isn't you, close this page.
                    </p>
                  </div>
                </li>
                <li className="enroll-step enroll-step--active">
                  <span className="enroll-step__num">2</span>
                  <div>
                    <strong>Register a passkey</strong>
                    <p>
                      Your phone, Face ID, fingerprint — whatever your device already uses to unlock
                      itself. No password to remember.
                    </p>
                    <button type="button" className="btn btn--moss btn--lg">
                      <KeyRound size={18} strokeWidth={1.8} aria-hidden="true" /> Register this device
                    </button>
                  </div>
                </li>
                <li className="enroll-step">
                  <span className="enroll-step__num">3</span>
                  <div>
                    <strong>Install the app shortcut</strong>
                    <p>
                      After signing in, tap "Add to home screen". The app works offline for today's
                      tasks.
                    </p>
                  </div>
                </li>
              </ol>
            </>
          )}

          <p className="login__footnote muted">
            Invite link: <code className="inline-code">/accept/{token}</code> — valid once,
            expires in 24 hours.
          </p>
        </div>
      </main>
    </div>
  );
}
