---
name: specs
description: Interactive spec + mock co-evolution. Discuss a change with the user via AskUserQuestion, stress-test edge cases, then update specs (docs/specs/) and mocks (mocks/) in lockstep. Use this while there is no production app code yet.
---

# Specs Skill

You are running an **interactive design session**. The product has
specs (`docs/specs/`) and a Vite + React mock app (`mocks/`) — but no
production code yet. The specs and the mocks are the *entire* source
of truth, and this skill is the canonical way to evolve them together.

Your job: **talk with the user, stress-test the idea against real
edge cases, and only then edit the specs and the mocks** so they
stay consistent. No Beads deferral, no "we'll figure it out later" —
close the loop in this session.

> This is **not** `/gap-finder` (which reports gaps as Beads tasks and
> never edits) and **not** `/audit-spec` (which reconciles specs with
> *existing* code). Use `/specs` when the user wants to change the
> design itself. If the area is already built, prefer `/audit-spec`.

## Non-negotiables

1. **Ask, don't guess.** Any non-obvious decision goes through
   `AskUserQuestion`. Batch related questions into one prompt — don't
   ping twice per round.
2. **Specs are the source of truth; mocks mirror them.** If a change
   lands in the mock UI but not the spec, you've created drift. Every
   mock edit has a matching spec edit in the same turn (or a reasoned
   note saying why not — e.g., the mock is stubbing a detail the spec
   intentionally leaves open).
3. **Think about the edges before you type.** Before proposing any
   change, write down — to yourself, then to the user if non-trivial —
   the failure modes, boundaries, and interactions you can think of.
   See "Edge-case checklist" below.
4. **No hand-waving.** "The system handles X correctly" is not a
   spec. Name the rule, the actor, the timing, and the failure mode.
5. **Small, reversible edits.** One concern per pass. If the user
   raises three things at once, handle them as three mini-loops (read
   → propose → ask → apply), not one giant patch.

## When to run this

Triggered by `/specs` in AGENTS.md. Good fits:

- Adding or changing a rule in `docs/specs/*.md`.
- Introducing a new entity, role, or flow and wiring it into the
  React mock under `mocks/web/src/` plus the mock API in
  `mocks/app/`.
- Reconciling two specs that disagree (e.g., §12 REST says X, §14 Web
  says Y).
- Pinning down an edge case the current spec leaves ambiguous.

Not a fit:

- Pure typo / broken link → edit directly, no skill needed.
- Real production code exists in the area → use `/audit-spec`.
- You want to *find* gaps, not close them → use `/gap-finder`.

## Session flow

```
1. UNDERSTAND the ask (re-read the user, don't paraphrase sloppily)
   │
   ▼
2. LOAD CONTEXT
   - Read every spec section the change touches, front to back
   - Read the matching mock pieces (React pages/components, mock API
     handlers, mock_data.py)
   - Note the glossary (docs/specs/20-glossary.md) terms in play
   │
   ▼
3. GENERATE EDGE CASES
   - Walk the Edge-case checklist below
   - Write the non-obvious ones down before asking the user
   │
   ▼
4. PROPOSE + ASK (AskUserQuestion)
   - Present options with concrete pros/cons + a recommendation
   - Batch related questions; one prompt, multiple options where it
     helps
   - If something is already answered by an adjacent spec, say so —
     don't waste a question on it
   │
   ▼
5. DECIDE
   - Record the answers. If the user picks "other / discuss", iterate
   - Lock the decision and summarise it back in one sentence before
     you edit anything
   │
   ▼
6. APPLY (specs + mocks in lockstep)
   - Edit the spec sections (surgical, preserve structure)
   - Update the React mock + mock API + mock_data seed so the UI
     reflects the new rule
   - Run the mock app's type-check / build when the change is
     non-trivial
   │
   ▼
7. REVIEW + HANDOFF
   - Summarise: what changed in specs, what changed in mocks, what is
     deliberately left open, and what the user should eyeball
   - Offer the next loop ("anything else?") — don't close the session
     for them
```

Loop steps 3–7 until the user is done.

## Edge-case checklist

Before you open `AskUserQuestion`, walk these explicitly. Most design
bugs are one of these.

| Category | Prompt for yourself |
|----------|---------------------|
| **Actor & scope** | Who can do this? Owner-manager, staff, contractor, client, system, LLM agent? What scope on the API token? (§03, §05) |
| **Lifecycle** | What happens when the subject is created, edited, archived, deleted, restored, or reassigned mid-flight? |
| **Empty / zero** | What does the UI show when the list is empty, the count is 0, the input is optional and absent? |
| **Many / bulk** | What happens at 1, 10, 1 000, 100 000? Pagination (§12)? Bulk actions? |
| **Concurrency** | Two owner-managers edit at once. Two clients book the same slot. The LLM writes while the human edits. |
| **Time & timezone** | Property-local time vs UTC (§ "Time is UTC at rest, local for display" in AGENTS.md). DST. Past/future/now boundaries. Recurrence and RRULE. |
| **Money & currency** | Multi-currency? Rounding? Who owns the conversion? Tax? (§09, §22) |
| **Offline / partial failure** | iCal fetch times out. Email bounces. SSE drops. LLM returns malformed JSON. Client is offline. |
| **Privacy / PII** | Does this field belong in LLM context? Exported to a CSV? Visible to a contractor? (§15, §11) |
| **Auth & abuse** | Magic-link replay, API-token leak, passkey rotation. Rate limits. Role escalation paths. |
| **Audit / evidence** | Is this a loggable event? Who sees the log? For how long? |
| **i18n seam** | New user-visible strings — go through the deferred-i18n seam in §18, don't hardcode copy. |
| **Glossary drift** | New term? Add it to §20. Reused term? Check the existing definition. |
| **Cross-surface** | Same rule on REST (§12), CLI (§13), and Web (§14)? Specs often agree on the happy path and disagree on the edges. |
| **Migration** | Existing mock data needs updating? Seed file (`mocks/app/mock_data.py`) stays consistent? |

You do not have to *ask* the user about every row — most you can
answer yourself from adjacent specs. Ask when the answer genuinely
requires judgment.

## Asking well

`AskUserQuestion` is your main tool. Quality bar:

- **One question per decision.** Don't smuggle two choices into one
  prompt.
- **Batch independent decisions into one call** (multiple questions
  in the same tool use) rather than a serial drip.
- **Every option names the consequence**, not just the label. "Use
  cursor pagination (Recommended) — simple for agents, but CLI users
  lose page numbers" beats "Use cursor pagination".
- **Always include a recommendation.** You have read the specs; the
  user expects a staff-engineer opinion.
- **Offer an "other / discuss" option** when the space isn't
  obviously two-or-three-way.
- **Never ask what you could have read.** If §03 already says
  passkeys are mandatory for owner-managers, don't ask.

Example shape:

```yaml
- question: "Clients can see staff first names on work orders. Should they also see last names?"
  header: "Client visibility"
  multiSelect: false
  options:
    - label: "First name only (Recommended)"
      description: "Keeps staff PII minimal; matches §15 data-minimisation stance."
    - label: "Full name"
      description: "Simpler UI; leaks last names to every client by default."
    - label: "Owner-manager choice per staff member"
      description: "Adds a per-staff toggle; more surface, more spec, more UI."
    - label: "Discuss further"
      description: "None of the above feel right — let's talk it through."
```

## Applying the change

### Specs (`docs/specs/`)

- Edit surgically. Keep section numbering, headings, and prose style.
- If you add a new rule, add it in the narratively correct section —
  not in a "Notes" graveyard at the bottom.
- New term? Update `docs/specs/20-glossary.md` in the same pass.
- Cross-reference with `§` links (e.g., "see §05 Roles").
- Keep the §NN-name.md prefix stable; never rename a spec file
  without asking.

### Mocks (`mocks/`)

- **Data model**: update `mocks/app/mock_data.py` so seeds reflect
  the new rule. Do not leave stale fixtures that contradict the
  spec.
- **API**: update `mocks/app/main.py` routes / response shapes so
  the mock server returns what the spec now promises.
- **UI**: update the React code under `mocks/web/src/`.
  - Pages under `pages/`, shared parts under `components/`,
    types under `types/`, styling under `styles/`.
  - Follow the project's **semantic CSS** rule — class names like
    `work-order-card`, never utility classes like `mt-4` (see
    AGENTS.md §"Application-specific notes").
  - Cross-client coherence goes through the SSE → TanStack Query
    invalidation path (§14). If you add a new resource, invalidate
    its query key on the matching `/events` message.
- **Build check**: after a non-trivial change, from `mocks/web/`
  run the type-check + build that the package script exposes
  (check `package.json` scripts — typically `npm run build` or
  `npm run typecheck`). A red build is a blocker.

### Keeping specs and mocks in lockstep

If you edit a spec rule, the corresponding mock must change in the
same session. If you edit a mock, the spec must change too (or you
must justify why the mock is intentionally freer than the spec —
e.g., the spec says "at least one method"; the mock picks one).

## Reviewing your own work (before handoff)

Before ending the session:

- [ ] Every decision the user made is reflected in *both* specs and
      mocks.
- [ ] No new user-visible string bypasses the i18n seam.
- [ ] No new role / scope silently granted to an existing actor.
- [ ] Glossary updated if a new term was introduced.
- [ ] Cross-references from other specs still make sense.
- [ ] Mock seed data is internally consistent (no orphan foreign
      keys, no dangling ids).
- [ ] Type-check / build passes on the React mock if touched.
- [ ] A one-paragraph summary has been written for the user.

If the change is significant, `/selfreview` afterwards — same as
any feature.

## Example loop

**User**: "Can a contractor reassign a work order to another
contractor, or only the owner-manager?"

**You** (before asking):

1. Read §22 (clients-and-vendors) and §05 (roles) on contractor
   capabilities.
2. Note §22 says contractors accept or decline; silent on
   reassignment.
3. Walk the edge-case checklist:
   - Actor: contractor, owner-manager, client — who is authorised?
   - Audit: any reassignment is an event; log it for the
     owner-manager.
   - Money: does payout follow the new assignee?
   - Concurrency: two accept clicks race.
4. Two real questions remain — the rest you can answer yourself.

**You** (`AskUserQuestion`):

```yaml
- question: "Who can reassign an accepted work order?"
  header: "Reassignment"
  options:
    - label: "Only owner-manager (Recommended)"
      description: "Matches §05 'owner-manager owns assignment'. Contractors can only decline; owner then reassigns."
    - label: "Accepting contractor can forward to a peer"
      description: "Faster in practice; adds a trust chain and a new spec rule about the peer network."
    - label: "Discuss further"

- question: "When a work order is reassigned, who gets paid for work already done?"
  header: "Payout on reassignment"
  options:
    - label: "Pro-rata by evidence timestamps (Recommended)"
      description: "Aligns with §09 evidence-based payroll."
    - label: "Original contractor forfeits"
      description: "Simpler; unfair in partial-completion cases."
    - label: "Discuss further"
```

**After** the user answers, you:

- Edit `docs/specs/22-clients-and-vendors.md` — add the reassignment
  rule, the payout rule, and the audit-log event.
- Edit `docs/specs/05-employees-and-roles.md` — tighten the
  contractor capability if needed.
- Edit `mocks/app/main.py` — add a `POST
  /work-orders/{id}/reassign` handler scoped to owner-manager.
- Edit `mocks/app/mock_data.py` — seed one reassigned order so the
  UI has something to render.
- Edit `mocks/web/src/pages/WorkOrders/` — add the reassign action
  (owner-manager only), the status pill, the audit-trail row.
- Run the mock build; fix any type errors.
- Summarise in a short paragraph; offer the next loop.

## Tone and output

- Plain text summaries, matching AGENTS.md §"Presenting your work".
- Reference files as `docs/specs/22-clients-and-vendors.md:123`.
- No "I will now…" narration. State what you read, what you propose,
  ask, apply, report. Short.

## Common mistakes to avoid

- **Editing mocks without touching specs**, or vice versa. Drift
  starts here.
- **Asking twelve questions in a row**. Batch, or answer yourself.
- **Skipping the edge-case walk**. The checklist exists because the
  user will find the edge case you didn't.
- **"The system" as the actor.** Every rule has a named actor and a
  named trigger.
- **Inventing new roles or scopes** without reflecting them in §03
  and §05.
- **Hardcoding strings** in new React mock code — use the i18n seam.
- **Leaving `TODO` in the spec.** If it's a TODO, it's a Beads task,
  not a spec line.
