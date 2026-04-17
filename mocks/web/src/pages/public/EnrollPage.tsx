import { useParams } from "react-router-dom";

// Static (mock) enrollment surface. The token is decorative — it's
// echoed back in the footnote to mirror what the real flow shows after
// the link in the welcome email is opened.

export default function EnrollPage() {
  const { token = "" } = useParams<{ token: string }>();

  return (
    <div className="surface surface--login">
      <main className="login">
        <div className="login__card login__card--wide">
          <div className="login__brand">
            <span className="desk__logo" aria-hidden="true">◈</span>
            <span className="desk__wordmark">crewday</span>
          </div>
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
                  🔑 Register this device
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
          <p className="login__footnote muted">
            Enrollment link: <code className="inline-code">/enroll/{token}</code> — valid once,
            expires in 24 hours.
          </p>
        </div>
      </main>
    </div>
  );
}
