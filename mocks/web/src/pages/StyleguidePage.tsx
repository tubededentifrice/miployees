// Visual regression baseline — if anything in tokens.css / globals.css
// drifts, the pixel diff on /styleguide is the fastest signal.
// Keep this page static and self-contained.

export default function StyleguidePage() {
  return (
    <main className="styleguide">
      <header className="styleguide__hero">
        <span className="styleguide__eyebrow">crew.day design system</span>
        <h1 className="styleguide__title">Paper, moss, and a little grit.</h1>
        <p className="styleguide__lede">
          A warm, editorial operations product for households. Fraunces sets the voice;
          Inter Tight carries the weight; moss green says "go."
        </p>
      </header>

      <section className="styleguide__section">
        <h2>Palette</h2>
        <div className="swatch-grid">
          <div className="swatch swatch--paper"><span>Paper</span><code>#FAF7F2</code></div>
          <div className="swatch swatch--ink"><span>Ink</span><code>#1F1A14</code></div>
          <div className="swatch swatch--moss"><span>Moss</span><code>#3F6E3B</code></div>
          <div className="swatch swatch--rust"><span>Rust</span><code>#B04A27</code></div>
          <div className="swatch swatch--sand"><span>Sand</span><code>#D9A441</code></div>
          <div className="swatch swatch--sky"><span>Sky</span><code>#4F7CA8</code></div>
          <div className="swatch swatch--night"><span>Night</span><code>#F4EFE6</code></div>
        </div>
      </section>

      <section className="styleguide__section">
        <h2>Typography</h2>
        <div className="type-row">
          <p className="type-sample type-sample--display">Villa Sud — tomorrow's turnover</p>
          <p className="type-caption">Fraunces · display · 600</p>
        </div>
        <div className="type-row">
          <p className="type-sample">
            The cook sees what to prepare tomorrow. The driver sees the airport run.
            The head of house sees everything.
          </p>
          <p className="type-caption">Inter Tight · body · 400</p>
        </div>
        <div className="type-row">
          <p className="type-sample type-sample--mono">task_id=t-2 · scheduled=10:30 · est=25min</p>
          <p className="type-caption">JetBrains Mono · dev-facing</p>
        </div>
      </section>

      <section className="styleguide__section">
        <h2>Chips &amp; dots</h2>
        <div className="demo-row">
          <span className="chip chip--moss">moss</span>
          <span className="chip chip--rust">rust</span>
          <span className="chip chip--sand">sand</span>
          <span className="chip chip--sky">sky</span>
          <span className="chip chip--ghost">ghost</span>
          <span className="chip chip--sm chip--moss">small</span>
          <span className="dot dot--moss" />
          <span className="dot dot--rust" />
          <span className="dot dot--sand" />
        </div>
      </section>

      <section className="styleguide__section">
        <h2>Buttons</h2>
        <div className="demo-row">
          <button className="btn btn--moss">Primary</button>
          <button className="btn btn--ghost">Ghost</button>
          <button className="btn btn--rust">Destructive</button>
          <button className="btn btn--moss btn--lg">Large primary</button>
          <button className="btn btn--sm btn--ghost">Small ghost</button>
        </div>
      </section>

      <section className="styleguide__section">
        <h2>Button groups</h2>
        <p className="styleguide__note">
          Use <code>.btn-group</code> for any row of buttons. Pair with a
          layout modifier (<code>--end</code>, <code>--split</code>,
          <code>--stack</code>) and add <code>.btn--block</code> to any
          button that should fill its track with a 44px tap target.
        </p>

        <div className="styleguide__demo-track">
          <h3>Default (inline, left)</h3>
          <div className="btn-group">
            <button className="btn btn--moss">Save</button>
            <button className="btn btn--ghost">Cancel</button>
          </div>

          <h3>Modal footer (<code>--end</code>)</h3>
          <div className="btn-group btn-group--end">
            <button className="btn btn--ghost">Cancel</button>
            <button className="btn btn--moss">Confirm</button>
          </div>

          <h3>Two-up, equal width (<code>--split</code> + <code>--block</code>)</h3>
          <p className="styleguide__note">
            Ideal for mobile dialog actions. Each child flexes to an equal
            share; <code>.btn--block</code> lifts the tap target to 44px.
          </p>
          <div className="btn-group btn-group--split">
            <button className="btn btn--ghost btn--block">Adjust this day</button>
            <button className="btn btn--ghost btn--block">Request leave</button>
          </div>

          <h3>Vertical stack (<code>--stack</code>)</h3>
          <div className="btn-group btn-group--stack">
            <button className="btn btn--moss btn--block">Continue</button>
            <button className="btn btn--ghost btn--block">Use a different account</button>
            <button className="btn btn--ghost btn--block">Back</button>
          </div>

          <h3>Standalone full-width (<code>.btn--block</code>)</h3>
          <button className="btn btn--moss btn--block">Sign in</button>
        </div>
      </section>

      <section className="styleguide__section">
        <h2>Task card</h2>
        <div className="demo-row demo-row--stack">
          <a className="task-card task-card--now" href="#">
            <div className="task-card__head">
              <span className="chip chip--moss">Villa Sud</span>
              <span className="chip chip--rust">High priority</span>
              <span className="chip chip--sand chip--sm">📷 photo required</span>
              <span className="task-card__when">10:30 · 25 min</span>
            </div>
            <h3 className="task-card__title">Change linen — master bedroom</h3>
            <div className="task-card__meta">Master bedroom</div>
            <div className="task-card__progress">
              <span className="progress-bar"><span style={{ width: "33%" }} /></span>
              <span className="progress-label">1/3 steps</span>
            </div>
            <div className="task-card__cta">Complete with photo →</div>
          </a>
        </div>
      </section>

      <section className="styleguide__section">
        <h2>Motion principles</h2>
        <ul className="kb-list">
          <li className="kb-item">
            <div className="kb-item__main"><strong>Enter</strong> — 150ms fade &amp; rise 4px.</div>
          </li>
          <li className="kb-item">
            <div className="kb-item__main"><strong>Tick</strong> — scale-to-checkmark, spring.</div>
          </li>
          <li className="kb-item">
            <div className="kb-item__main">
              <strong>Respect</strong> <code className="inline-code">prefers-reduced-motion</code> — reduce to opacity only.
            </div>
          </li>
        </ul>
      </section>
    </main>
  );
}
