---
name: selfreview
description: Skeptical review of your own recent changes. Enter plan mode, find what you missed, report findings, then fix.
---

# Self-Review Skill

Deep, skeptical review of your own recent changes. Find what you missed
*before* handing off.

## Trigger

Run after completing any non-trivial coding or spec-editing task. The
goal is to catch bugs, missing edge cases, and unintended consequences
before the work ships.

## Workflow

```
1. GATHER CHANGES (git diff, git log)
   ↓
2. ENTER PLAN MODE (read-only)
   ↓
3. DEEP REVIEW (systematic, adversarial)
   ↓
4. REPORT FINDINGS
   ↓
5. ASK USER what to fix
   ↓
6. FIX (exit plan mode, apply fixes)
```

## Phase 1: Gather changes

```bash
git diff --stat main...HEAD          # scope
git diff main...HEAD                 # full diff
git diff                             # unstaged
git diff --cached                    # staged
git log --oneline main...HEAD        # commit history = intent
```

If there are no commits beyond `main`, diff against the working tree:

```bash
git diff HEAD
git diff --cached
```

**Capture intent**. Read commit messages and the linked Beads task
(`bd show <id>`) to understand what the change was *supposed* to do.
You review against intent, not against generic "could be better"
observations.

## Phase 2: Enter plan mode

Call `EnterPlanMode` to switch to read-only analysis. All investigation
happens here before proposing any change.

## Phase 3: Deep review

Be **adversarial**. Assume the code has bugs. Try to break it.

### 3a. Correctness

For each changed file:

- **Does it do what was intended?** Compare against commit message and
  Beads task.
- **Off-by-one** — loop bounds, slicing, pagination ranges.
- **`None` paths** — what if the queryset is empty? The field is NULL?
- **Type mismatches** — especially in template context or API responses.
- **Logic** — trace through mentally with edge-case inputs.

### 3b. Missing pieces

For each change, ask:

- **Tests?** New code paths without coverage.
- **Migrations?** Model changes without a corresponding Alembic
  revision. See `/new-migration`.
- **Translations?** Miployees defers i18n, but new user-facing strings
  must route through the seam described in
  [`docs/specs/18-i18n.md`](../../../docs/specs/18-i18n.md).
- **Error handling?** What if the network call fails, the file is
  missing, the external API times out?
- **Auth / scopes?** New endpoint without the right
  `require_role`/`require_scope`?
- **Redirects?** URL changes without a redirect from the old path.
- **OpenAPI?** Endpoint change without `/update-openapi`?
- **CLI parity?** New REST capability that should also be in the CLI
  per [`docs/specs/13-cli.md`](../../../docs/specs/13-cli.md)?

### 3c. Unintended consequences

- **Existing tests** — will any break? Check fixtures / factories.
- **Other callers** — did a rename break something downstream?
- **Templates** — is this template also included elsewhere with
  different context?
- **CSS / HTMX** — does a DOM change break an event handler or HTMX
  target?
- **Background jobs / schedulers** — are they expecting the old
  interface?
- **API consumers** — does this break the contract? Agents rely on a
  stable REST surface.
- **Privacy** — new `mark_safe`, new log line, new response field,
  unredacted prompt?
- **Performance** — new N+1, missing `selectinload`?

### 3d. Consistency

- **Style** — new code matches surrounding patterns?
- **Naming** — consistent with existing conventions?
- **Imports** — unused imports added, needed imports missing?

### 3e. FastAPI / SQLAlchemy specifics

- **Pydantic models** — request/response models use correct types;
  `model_config` (v2) set where needed.
- **Dependencies** — `Depends(...)` chain correct; auth applied at the
  right layer.
- **Session scope** — no leaked sessions, no transactions open across
  awaits.
- **Async correctness** — no blocking I/O in async handlers.
- **Alembic** — migration autogenerates cleanly; downgrade works.

### 3f. Spec alignment

- **Spec consistency** — did the change drift from the relevant spec?
  If yes, was the spec updated or explicitly flagged for an
  `/audit-spec` pass?

## Phase 4: Report findings

Organise by severity. Every finding needs a specific `file:line`.

```markdown
## Self-review findings

### BUGS (will cause errors or wrong behaviour)

1. **[file:line] Brief title**
   - What's wrong: …
   - How to trigger: …
   - Fix: …

### MISSING (incomplete implementation)

1. **[file:line] Brief title**
   - What's missing: …
   - Why it matters: …
   - Fix: …

### RISKY (might cause problems under specific conditions)

1. **[file:line] Brief title**
   - The risk: …
   - When it triggers: …
   - Mitigation: …

### NITPICKS (minor, optional)

1. **[file:line] Brief title**
   - Issue: …
   - Suggestion: …
```

**Rules:**

- Every finding has a specific `file:line`.
- Every finding explains *how* it would manifest.
- Do NOT pad with generic advice.
- If nothing is wrong, say so. An empty report is better than invented
  issues.

## Phase 5: Ask the user

Use `AskUserQuestion` to triage:

```yaml
questions:
  - question: "Found {N} issues in self-review. How should I proceed?"
    header: "Self-review"
    multiSelect: false
    options:
      - label: "Fix all (Recommended)"
        description: "Fix {bugs} bugs, {missing} missing pieces, and {risky} risky items now."
      - label: "Fix bugs only"
        description: "Fix the {bugs} bugs. Skip missing pieces and risks."
      - label: "Show details first"
        description: "Walk through each finding before deciding."
      - label: "Skip all"
        description: "Accept the code as-is."
```

If the user wants details, walk through findings one at a time.

## Phase 6: Fix

Exit plan mode and apply the agreed fixes. Afterwards:

```bash
./scripts/lint.sh
./scripts/format.sh --check
./scripts/typecheck.sh
pytest <affected paths> -x -q
```

When green, present the fixes for the user to review.

## Tips

- **Be harsh on yourself.** The point is to catch what you missed, not
  to confirm everything is fine.
- **Trace data flow end-to-end.** Follow input from request →
  dependency → service → domain → DB → response.
- **Check the negative path.** What happens when validation fails? When
  the object doesn't exist? When the user has the wrong scope?
- **Read surrounding code.** A change at line 50 can break something
  at line 200 in the same file, or in a module that imports from here.
- **Think about concurrency.** Two agents hitting the same endpoint,
  two scheduler ticks processing the same recurring task.
- **Run the tests in your head first.** Predict which will fail and
  why before running them.
