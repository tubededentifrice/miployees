# Public site specs

Marketing surface for crew.day — the landing pages at the root
`crew.day` host, the scenario-driven demo picker, and the
agent-clustered suggestion box linked from the app.

> **This directory is separate from `docs/specs/`.** Those are the
> **app** specs — the product itself. The files here govern only the
> **site**: a different audience, a different deploy, a different
> tech stack. Any agent editing the app should treat the site as
> out of scope, and vice versa. Where a site rule overlaps with an
> app rule, the app rule wins; the site extends, it never overrides.

## What's in scope for the site

- Landing pages at `crew.day` — hero, features, pricing placeholder,
  "who is this for" scenario picker, legal, changelog.
- The **embedded demo picker** on the landing page: a two-axis
  (persona × intent) selector that resolves to a §24 demo URL and
  either mounts it as an iframe or plays a pre-recorded video.
- The **suggestion box** at `crew.day/suggest`: unauthenticated
  submission form + public board of agent-clustered ideas. The app
  links here via `CREWDAY_FEEDBACK_URL`.
- The **clustering RPC** contract — the site calls the app; the
  app owns the LLM side (budget, redaction, audit).
- Deployment, security, abuse posture for the site alone.

## What's out of scope

- The product at `app.crew.day` — see `../specs/`.
- The demo server at `demo.crew.day` — see `../specs/24-demo-mode.md`.
  The site consumes the iframe contract documented there; it does
  not redefine it.
- Any stored workspace data, user sessions, or authenticated flows.
  The site has no users — only visitors.

## Files

| # | Spec | Covers |
|---|------|--------|
| 00 | [Overview](00-overview.md) | Vision, audiences, surfaces, tech stack, repo layout, principles |
| 01 | [Landing and demo embed](01-landing-and-demo-embed.md) | Page map, scenario picker, iframe vs video policy, copy + i18n |
| 02 | [Suggestion box](02-suggestion-box.md) | Form, data model, public board, moderation, PII, rate limits |
| 03 | [App integration](03-app-integration.md) | Clustering RPC, `CREWDAY_FEEDBACK_URL`, versioning |
| 04 | [Deployment and security](04-deployment-and-security.md) | Independent deploy, DNS, CSP, abuse controls, privacy |
| 05 | [Roadmap](05-roadmap.md) | Phased delivery |

## Cross-references into the app specs

- §00 [overview](../specs/00-overview.md) — personas the marketing
  copy targets
- §11 [LLM and agents](../specs/11-llm-and-agents.md) — the plumbing
  the clustering RPC reuses on the app side
- §15 [security and privacy](../specs/15-security-privacy.md) — the
  baseline the site extends (separate origin, narrower surface)
- §18 [i18n](../specs/18-i18n.md) — the deferred-i18n seam the site
  adopts wholesale
- §24 [demo mode](../specs/24-demo-mode.md) — the iframe contract
  the picker produces URLs for

## Editing conventions

- Keep the `NN-name.md` prefix stable; never rename a spec file
  without asking.
- Concrete, named actors and triggers — no "the system handles".
- ASCII unless the file already uses non-ASCII or the content is
  user-facing copy.
- No TODOs in spec prose — file a Beads task instead.
- Any change here that would invalidate an app spec rule is a
  conflict: update the app spec first, then the site spec.
