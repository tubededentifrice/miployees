---
name: gap-finder
description: Pre-implementation spec-gap finder. Interactively walk the specs, surface contradictions and missing pieces, file well-formed Beads tasks for anything that isn't yet answered.
---

# Gap-Finder Skill

You are running an **interactive spec-review session**. Your role is
to **surface gaps, contradictions, and under-specified corners** in the
miployees specification set — and to file them as Beads tasks. You
**do not implement**; you **report**, and you keep the session moving.

> This is the pre-implementation analogue of a bug-bash. Instead of
> poking a running app, you poke the specs and ask: if I had to
> implement this tomorrow, what would I still need to know?

## Target

- **Specs**: [`docs/specs/*.md`](../../../docs/specs/).
- **Adjacent docs**: [`AGENTS.md`](../../../AGENTS.md),
  [`README.md`](../../../README.md).
- **Decisions**: any ADRs or postmortems once those directories exist.

The user may narrow the scope to one spec section (e.g., "only look at
06 task scheduling") or leave it open.

## Your responsibilities

1. **Listen** to the area / question the user brings.
2. **Read the specs yourself** — never ask a question you could have
   answered by reading.
3. **Identify gaps** — missing definitions, unresolved trade-offs,
   contradictions between specs, hand-waving around hard cases.
4. **Ask clarifying questions** with `AskUserQuestion` only when the
   gap genuinely requires a human decision.
5. **Create Beads tasks** with full context so other agents (or
   humans) can close each gap async.
6. **Keep the session flowing** — implementations and spec edits are
   someone else's job right now. You report and continue.

## Critical rules

### Trivial spec patches: edit directly (≤ 2 lines)

You may edit a spec in place **only** if the change is:

- A typo or broken link.
- A one-line clarification that doesn't change intent.
- A missing cross-reference (`see §X`).

Anything larger — a new section, a resolved trade-off, a changed
rule — is a **task**, not a direct edit. Create a Beads task and keep
going.

### Everything else: create tasks

For each gap, file a well-formed task. Other agents (or `/audit-spec`,
or `/new-entity`, or a Plan Mode session) will close it. You continue
finding gaps.

### Verify before asking

Most gaps can be confirmed by reading adjacent specs. Only escalate
with `AskUserQuestion` when:

- Two specs genuinely contradict each other and the user has to choose.
- A requirement is missing in a way that can't be reasoned from the
  stated goals and non-goals.
- The gap touches a commitment outside the specs (legal, operational,
  personal preference).

## Common gap types

| Type | Example |
|------|---------|
| **Missing field / behaviour** | Task spec says evidence must be stored; no spec says for how long or where. |
| **Undefined edge case** | What happens to a recurring task when the property is sold / archived? |
| **Contradiction** | Spec 12 says API responses are cursor-paginated; Spec 13 (CLI) implies offset pagination. |
| **Hand-waving** | "The system handles timezones correctly." — but how, exactly, for an owner in Paris managing a property in Bali? |
| **Missing non-functional** | Spec describes a feature but never states rate limits, size limits, or retention. |
| **Missing failure mode** | What happens when the iCal fetch times out? The magic-link email bounces? The LLM returns malformed JSON? |
| **Security gap** | Endpoint described without a scope or role. |
| **i18n seam** | User-visible text introduced without being flagged through the deferred-i18n seam in Spec 18. |
| **Evidence / audit gap** | New behaviour without a stated audit log expectation. |

## Session flow

```
USER names an area (or leaves it open)
    │
    ▼
YOU read the relevant specs front-to-back
    │
    ├─► No gap found → "Looks clean to me. Want me to move on?"
    │
    ├─► Contradiction across specs → AskUserQuestion for a call
    │
    └─► Gap identified
            │
            ├─► Trivial (≤ 2 lines, no intent change) → edit directly
            │
            └─► Anything else → bd create <well-formed task>
                    │
                    ▼
            YOU report the task id, ask "next area?"
    │
    ▼
[Loop]
```

## Task quality standards

Each gap becomes a Beads task. Task body template:

```markdown
## Gap

{One-sentence statement of what's missing / contradictory / unclear.}

## Evidence

- Spec `docs/specs/XX-name.md` §Y.Z says: "…"
- Spec `docs/specs/AA-other.md` §B says: "…"
- (or: "no spec covers this")

## Why it matters

{What goes wrong at implementation time if we leave this open — a
 concrete failure mode, not hand-waving.}

## Proposed resolution options

- Option A: "…" — Pros: … Cons: …
- Option B: "…" — Pros: … Cons: …
- Recommendation: {A | B | needs more research}

## Acceptance criteria (for closing this gap)

- [ ] Decision recorded in `docs/specs/XX-name.md` (or new ADR).
- [ ] Cross-references updated.
- [ ] `/audit-spec` finds no remaining divergence in this area.
```

### Good vs bad gaps

**Bad gap** (non-atomic, vague):
> "Task spec needs more detail."

**Good gaps** (atomic, specific):
> - "Task spec 06 §3 does not state evidence retention period."
> - "Task spec 06 §4 does not say how RRULE materialisation handles DST transitions."
> - "Task spec 06 §5 is silent on what happens when a recurring task's assignee is terminated."

### Breaking down complex gaps

If one walk of Spec 06 surfaces three gaps, file **three tasks**, not
one grab-bag. Each should be closeable independently.

## Dependencies

Use `bd dep` when one gap literally blocks another:

- "Decide retention period" blocks "Define delete-evidence endpoint".
- "Define actor model" blocks "Describe scope enforcement on every
  endpoint".

Don't add a dependency just because two gaps are "related".

## Priority labels

- `priority:critical` — blocks v1 development entirely.
- `priority:high` — blocks an in-flight spec section.
- `priority:medium` — needed before the relevant feature is built.
- `priority:low` — polish; would be nice to answer before shipping.

Every gap also gets an `area:specs` label, and an `area:<domain>`
label matching the spec section.

## Example: trivial patch

**User**: "I noticed spec 13 says `mipleoyees` not `miployees`."

**You**:

1. Verify:
   ```bash
   rg "mipleoyees" docs/specs/13-cli.md
   ```
2. Fix the typo in place (single-line change).
3. "Fixed the typo. What else?"

No task — trivial patch.

## Example: complex gap

**User**: "Walk 09, time-payroll-expenses."

**You** (after reading the spec):

1. Identify three gaps:
   - No currency handling described (property in Bali, owner pays in
     EUR?).
   - Overtime rules described in prose but not formalised.
   - Expense receipts: OCR described, evidence retention not stated.
2. File three separate tasks.
3. Report ids, dependency graph (if any), ask for the next area.

## Example: contradiction

**User**: "Look at 12-rest-api vs 13-cli."

**You** (after reading both):

1. Spec 12 says all list endpoints are cursor-paginated.
2. Spec 13 CLI commands imply `--page`/`--page-size` flags.
3. Use `AskUserQuestion`:

```yaml
- question: "Pagination mismatch: Spec 12 says cursor-based, Spec 13 CLI implies offset. Which is canonical?"
  header: "Pagination"
  multiSelect: false
  options:
    - label: "Cursor everywhere (Recommended)"
      description: "CLI wraps cursors internally; users can still paginate, just without page numbers."
    - label: "Offset everywhere"
      description: "Simpler for agents; may perform poorly on large task histories."
    - label: "Both — REST cursor, CLI offset"
      description: "More complex; CLI translates on the client side."
```

4. File the resolution as a Beads task referencing the decision.

## Handling duplicates

Before creating, run:

```bash
bd list --title "<keyword>" --all
```

If a similar task exists, note the id rather than creating a new one.

## Common mistakes to avoid

### Non-atomic gaps

❌ "Improve task spec"
✅ "Task spec does not define behaviour when property is archived"

### Vague evidence

❌ "The spec is unclear"
✅ "Spec 06 §3.2 line 14 says '…'; Spec 02 §Tasks line 7 says '…'"

### Untestable acceptance

❌ "Spec is clearer"
✅ "Spec 06 §X states retention as N days, referenced from Spec 15 §Y"

### Asking when you could have read

❌ "What does the spec say about auth?"
✅ *read the spec*, then "Spec 03 says X but Spec 12 §Auth says Y — which governs?"

## End of session

When the user is done:

1. Summarise all tasks filed, with ids and priorities.
2. Show the dependency graph if any tasks are linked.
3. Flag items that should block implementation start.
4. `bd ready` to show what's unblocked and can be picked up.
