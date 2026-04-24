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

## Modes

- **Interactive (default)** — enter plan mode, report findings, ask
  the user how to triage, then fix.
- **Autofix** — fix every BUGS/MISSING/RISKY directly. No plan mode,
  no user prompt. Triggered by `/selfreview autofix` (or `--autofix`),
  by claiming a Beads task labelled `selfreview`, or by an explicit
  hands-off request.

Autofix has two sub-modes for the final phase:

- **Standalone (default)** — also closes the Beads task, commits,
  and pushes (Phase 7).
- **Director-invoked** — stops at quality gates (Phase 6). The
  caller's prompt will say *"do not commit, do not `bd close`"*; the
  director's `commiter` subagent closes both tasks and ships the
  bundled commit atomically. **Skip Phase 7 entirely** and return.

Autofix never creates another selfreview task for its own findings —
that would infinite-loop the beads pairing rule.

## Workflow (interactive mode)

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

## Workflow (autofix mode)

Same numbering as interactive — autofix skips phase 2 (plan mode) and
phase 5 (ask user). Phase 7 runs **only in standalone autofix**;
director-invoked autofix stops after phase 6.

```
1. GATHER CHANGES         (git diff, git log, bd show <main-task>)
   ↓
2. —  skipped in autofix — no plan mode
   ↓
3. DEEP REVIEW            (systematic, adversarial — same as interactive)
   ↓
4. WRITE FINDINGS         (organised BUGS/MISSING/RISKY/NITPICKS with file:line)
   ↓
5. —  skipped in autofix — do not ask the user
   ↓
6. FIX + QUALITY GATES    (every BUGS/MISSING/RISKY; then run repo's lint/format/type/tests)
   ↓
7. CLOSE + COMMIT + PUSH  (STANDALONE ONLY — skip when director-invoked)
```

**Empty findings are a valid outcome.** Phase 6 still runs the quality
gates (they must be green). Standalone autofix still proceeds to
Phase 7 (the underlying main-task work isn't on `main` yet and the
selfreview Beads task must be closed). Director-invoked autofix
returns after Phase 6 — the commiter handles closure and commit.

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

## Phase 2: Enter plan mode (interactive mode only)

> **Autofix mode: skip this phase entirely.** Go straight to Phase 3.

Call `EnterPlanMode` to switch to read-only analysis. All investigation
happens here before proposing any change.

**Do NOT repeat the original implementation plan in your new plan.** The
selfreview plan is a review checklist and findings report — not a copy of
what was just built. You may reference the original plan inline
(e.g. "the migration added in step 3 of the original plan is missing a
downgrade") when it helps the next implementer understand *what* you're
referring to, but never reproduce it wholesale.

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
- **Translations?** crew.day defers i18n, but new user-facing strings
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

## Phase 5: Ask the user (interactive mode only)

> **Autofix mode: skip this phase entirely.** Fix every BUGS / MISSING /
> RISKY directly and move on to quality gates (Phase 6).

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

**Interactive mode**: exit plan mode and apply the agreed fixes.

**Autofix mode**: apply fixes for every BUGS, MISSING, and RISKY
finding. Skip NITPICKS unless trivially safe. Do not ask the user.
If findings is empty, there is nothing to fix here — proceed to the
quality gates and commit the underlying main-task work anyway.

### Quality gates (both modes)

Run the repo's actual gates. Do **not** assume `./scripts/*.sh` —
those don't exist in every repo. Discover the right commands from
the repo:

- `Makefile` targets (common pattern: `make lint`, `make fmt`,
  `make type`, `make test`) — usually the simplest entrypoint.
- `pyproject.toml` / `package.json` scripts for the underlying tools.
- `AGENTS.md` / `CLAUDE.md` "quality gates" section.

Typical Python stack (adapt to this repo):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy <module>
uv run pytest <affected paths> -x -q
```

Typical JS/TS stack:

```bash
pnpm -C <web-dir> lint
pnpm -C <web-dir> typecheck
pnpm -C <web-dir> test
```

When green, move on: interactive mode hands the fixes to the user
for review; autofix mode continues to Phase 7.

## Phase 7: Close Beads, commit, push, verify (standalone autofix only)

> **Skip this phase entirely when invoked by the director.** The
> director's prompt will instruct *"do not commit, do not `bd close`"*.
> Stop after Phase 6 quality gates and return — the `commiter` subagent
> will close both tasks and ship the bundled commit atomically.

**Order matters.** `bd close` + `bd sync` must run *before* `git add`
so the `.beads/*.jsonl` delta ships inside the same commit as the
code. Don't push before committing. Don't commit before closing.

```bash
# 7.1 — Close the selfreview Beads task
bd close <selfreview-task-id> --reason "Autofix self-review complete"

# 7.2 — Optional: add a comment with the findings summary
bd comments add <selfreview-task-id> "Autofix complete. Findings: <summary>."

# 7.3 — Sync .beads/*.jsonl to the worktree
bd sync

# 7.4 — Stage the in-scope work PLUS .beads/
git add <in-scope paths under the main task> .beads

# 7.5 — Commit (Conventional Commits, signed-off, HEREDOC)
git commit -s -m "$(cat <<'EOF'
<type>(<scope>): <subject> (<main-task-id>, <selfreview-task-id>)

<1-4 lines: what landed, notable self-review findings or "no findings".>

Refs: docs/specs/<section>.md §"<section title>"
EOF
)"

# 7.6 — Push to origin
git push

# If the push is rejected as non-fast-forward:
git pull --rebase origin main
git push
```

### Rules for the commit

- **Conventional Commits** type (`feat`, `fix`, `chore`, `docs`,
  `refactor`, `test`, `sec`, `perf`, `build`, `ci`) + scope + short
  subject.
- **Signed-off**: `git commit -s` (never skip).
- **HEREDOC** for multi-line messages so the body survives shell
  quoting.
- Reference both Beads task IDs (main + selfreview) in the title so
  `git log | grep <id>` finds the landing commit.
- Stage only in-scope files plus `.beads/` — never `git add -A` /
  `git add .`.
- Never `--amend`, never `--no-verify`, never force-push.

### 7.7 — Verify

```bash
git log --oneline -3              # confirm the commit landed
bd show <selfreview-task-id>      # confirm CLOSED
git status                        # confirm clean worktree
```

Report all three outputs back to the caller.

Do NOT create a new self-review task for the fixes you just applied —
that would infinite-loop with the beads skill pairing rule. The autofix
commit ships as-is.

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
