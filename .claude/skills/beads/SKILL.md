---
name: beads
description: Create well-structured Beads tasks from a prompt. Generates atomic, testable tasks with full context, clear acceptance criteria, and proper dependencies.
---

# Beads Task-Creator Skill

You create **Beads tasks** from user prompts. Your output is a set of
well-structured, atomic tasks that can be implemented by other agents
asynchronously.

## Your responsibilities

1. **Analyse** the user's prompt.
2. **Research** the codebase and specs to gather context.
3. **Break down** work into atomic, independent tasks.
4. **Create tasks** with full context, clear acceptance criteria, and a
   test plan.
5. **Link dependencies** when tasks must be completed in order.
6. **Pair every non-selfreview task with a `/selfreview` autofix task**
   (see below).

## Task quality standards

### Atomic tasks (CRITICAL)

Each task must be **atomic** — a single well-defined unit:

1. **One concern only** — one feature or one bug.
2. **Clear boundaries** — no ambiguity about what's included.
3. **Independent testing** — verifiable without completing other tasks
   first.
4. **Minimal scope** — if acceptance criteria drift across unrelated
   areas, split the task.

**Bad** (non-atomic):

> "Implement authentication system."

**Good**:

> - "Add passkey registration endpoint."
> - "Add magic-link bootstrap email + single-use token consumption."
> - "Add API-token mint + scope enforcement."

### Full context for implementers

Each task must contain **everything** an implementer needs without
reading external chat history:

```markdown
## Problem / goal
[What needs to be done and WHY — user impact, business context.]

## Technical context
[Relevant files, models, APIs, patterns to follow.]
- Key files: `app/auth/passkeys.py`, `app/api/auth.py`
- Related model: `User`, `PasskeyCredential`
- Follow pattern from: `app/api/users.py:create_user`
- Spec: `docs/specs/03-auth-and-tokens.md` §4.2

## Implementation notes
- Use `webauthn` Python library; see already-installed version.
- Keep backwards compatibility with existing API consumers.

## Acceptance criteria
- [ ] Specific, testable criterion
- [ ] Another criterion

## Test plan
[Concrete commands or steps.]
```

### Clear acceptance criteria

Each criterion:

1. **Specific** — describes exactly what to check.
2. **Verifiable** — testable with a command or manual step.
3. **Binary** — pass or fail, no gray.

**Bad**:

- [ ] "Works correctly"
- [ ] "Is fast enough"
- [ ] "Follows best practices"

**Good**:

- [ ] `pytest tests/auth/test_passkeys.py` passes
- [ ] `POST /auth/passkey/register` returns 201 with challenge payload
- [ ] Invalid challenge returns 400 with `error_code=invalid_attestation`
- [ ] Rate limit triggers at 5 failed attempts per minute per IP (429)

### Testable test plans

Include concrete commands or steps:

````markdown
## Test plan

### Automated
```bash
pytest tests/auth/test_passkeys.py -xvs
```

### Manual
1. `mip auth passkey register --email foo@example.com`
2. Follow the printed URL; register a platform passkey.
3. `mip whoami` — expect the new user.
````

## Dependencies

### When to use

Only when one task literally cannot start until another completes:

1. **Schema / migration** must land before code can use the column.
2. **Domain service** must exist before API can call it.
3. **Shared utility** must exist before consumers use it.

Don't add a dependency just because tasks are "related".

### How to link

```bash
# 1. Create the blocker first
bd create "Add Task.evidence_urls column" --body "…" --silent
# → bd-abc123

# 2. Create the dependent task
bd create "API: POST /tasks/{id}/evidence" --body "…
Depends on: bd-abc123 (column must exist)
…"
# → bd-def456

# 3. Link them
bd dep bd-abc123 --blocks bd-def456
```

## Pair each task with a self-review task (MANDATORY)

Every task you create (except self-review tasks themselves) gets a
second, dependent task that runs `/selfreview` in **autofix mode** once
the main task closes. This catches bugs, missing pieces, and unintended
consequences before they ship — without waiting for a human to notice.

### Rules

- **Label the self-review task `selfreview`.** The beads skill uses this
  label to detect self-review tasks and skip pairing them with yet
  another self-review. Pairing a self-review with a self-review would
  infinite-loop.
- **Make the self-review depend on the main task**
  (`bd dep <main> --blocks <selfreview>`) so `bd ready` only surfaces it
  once the main work is complete.
- **Title**: `Self-review: <main task title>`.
- **Body**: instruct the implementer to run `/selfreview` in autofix
  mode — skip plan mode, skip user triage, apply fixes directly, commit,
  close the task. See [`.claude/skills/selfreview/SKILL.md`](../selfreview/SKILL.md)
  for the autofix flow.

### Never pair a self-review with a self-review

Before creating a pair, check that the main task does NOT already have
the `selfreview` label. If it does, skip pairing. This is the only
guard against an infinite loop.

### Template

```bash
# After creating the main task (bd-001):
bd create "Self-review: <main task title>" --body "$(cat <<'EOF'
## Problem / goal
Auto-fixing self-review of the changes made under bd-001. Catch bugs,
missing pieces, and unintended consequences before they ship.

**Depends on: bd-001** (main task must be complete first).

## How to run
Run `/selfreview` in **autofix mode** against the commit(s) from bd-001.

- Do NOT enter plan mode.
- Do NOT ask the user to triage findings — you are the triage.
- Apply fixes for every BUGS, MISSING, and RISKY finding.
- Skip NITPICKS unless trivially safe.
- Run the quality gates after fixing.
- Commit the fixes, push, and close this task.

See [`.claude/skills/selfreview/SKILL.md`](../selfreview/SKILL.md).

## Acceptance criteria
- [ ] All BUGS from the self-review fixed
- [ ] All MISSING pieces completed
- [ ] All RISKY items mitigated (or justified in a task comment)
- [ ] Linter, formatter, type checker, affected tests all pass
- [ ] Fixes committed and pushed
EOF
)" --labels "selfreview" --type chore --silent
# → bd-002

bd dep bd-001 --blocks bd-002
```

## Workflow

```
USER prompt
    │
    ▼
Analyse the request
    │
    ├─► Simple single task → 1 task (+ self-review pair)
    │
    └─► Complex / multi-part → break down
            │
            ▼
        Research specs + code for context
            │
            ▼
        Identify dependencies
            │
            ▼
        Create tasks (blockers first)
            │
            ▼
        Link with bd dep
            │
            ▼
        For EACH main task, create a paired self-review task
        (labelled `selfreview`, blocked by the main task).
        Never pair a self-review with another self-review.
            │
            ▼
        Summarise: table of ids, dependency graph, execution order
```

## Before creating tasks

### Research phase

1. **Read relevant specs** in [`docs/specs/`](../../../docs/specs/).
2. **Check existing code** patterns in `app/`.
3. **Look for similar implementations** to reference.
4. **Check for duplicates**:
   ```bash
   bd list --title "keyword" --all
   ```

## Creating tasks

### Task template

```bash
bd create "<type>(<scope>): <imperative summary>" --body "$(cat <<'EOF'
## Problem / goal
[What and WHY.]

## Technical context
- **Key files**: `path/to/file.py`
- **Related models**: `ModelName` in `app/domain/foo.py`
- **Follow pattern**: `app/domain/bar.py:similar_function`
- **Spec reference**: `docs/specs/XX-name.md` §Y

## Implementation notes
- [Specific guidance]
- [Gotchas]
- [Architectural decisions made]

## Acceptance criteria
- [ ] [Specific testable criterion 1]
- [ ] [Specific testable criterion 2]

## Test plan

### Automated
\`\`\`bash
pytest tests/<path>/test_<name>.py -xvs
\`\`\`

### Manual
1. [Step 1]
2. [Step 2]
3. [Expected outcome]
EOF
)"
```

### Priority

```bash
bd create "…" --body "…" --priority 0   # critical
bd create "…" --body "…" --priority 1   # high
bd create "…" --body "…" --priority 2   # medium (default)
bd create "…" --body "…" --priority 3   # low
```

### Labels

```bash
bd create "…" --body "…" --labels "area:auth,type:feature"
```

Common labels:

- `area:<spec-number-or-name>` — `area:03`, `area:api`, `area:cli`,
  `area:llm`, `area:security`, `area:specs`.
- `type:feature`, `type:bug`, `type:chore`, `type:docs`, `type:sec`.
- `priority:critical` / `:high` / `:medium` / `:low` (in addition to
  `--priority`; some team workflows prefer labels).

### Type

```bash
bd create "Fix magic-link reuse window" --body "…" --type bug
bd create "Add API-token mint endpoint" --body "…" --type feature
bd create "Extract auth utils" --body "…" --type chore
```

## Example: breaking down complex work

**Prompt**: "Add article categories with filtering" (fj2 example,
shown for structure).

**Analysis**: model → admin → article FK → display → filter.

```bash
# Task 1: model (blocker)
bd create "Add Category model" --body "…" --type feature --silent
# → bd-001

# Task 2: admin (depends on model)
bd create "Add Category admin interface" --body "…
Depends on: bd-001" --type feature --silent
# → bd-002
bd dep bd-001 --blocks bd-002

# Task 3: article FK (depends on model)
bd create "Add Article.category FK" --body "…
Depends on: bd-001" --type feature --silent
# → bd-003
bd dep bd-001 --blocks bd-003

# Task 4: display (depends on FK)
bd create "Display category in article list/detail" --body "…
Depends on: bd-003" --type feature --silent
# → bd-004
bd dep bd-003 --blocks bd-004

# Task 5: filter (depends on FK)
bd create "Add ?category=slug filter" --body "…
Depends on: bd-003" --type feature --silent
# → bd-005
bd dep bd-003 --blocks bd-005
```

Summary:

```
bd-001: Add Category model (BLOCKER)
    │
    ├─► bd-002: admin
    │
    └─► bd-003: Article FK
            │
            ├─► bd-004: display
            │
            └─► bd-005: filter

Ready: bd-001
After bd-001: bd-002, bd-003 (parallel)
After bd-003: bd-004, bd-005 (parallel)
```

## Beads CLI reference

### Create

```bash
bd create "Title" --body "…"
bd create "Title" \
  --body "…" \
  --type feature \          # bug | feature | task | chore
  --priority 1 \            # 0 critical … 3 low (2 default)
  --labels "area:auth,type:feature" \
  --assignee "<user>" \
  --estimate 60             # minutes

bd create "Title" --body "…" --silent   # print id only
```

### Dependencies

```bash
bd dep <blocker> --blocks <blocked>
bd dep add <blocked> <blocker>
bd dep remove <blocked> <blocker>
bd dep tree <id>
bd dep cycles
```

### View

```bash
bd list
bd list --all
bd list --status open
bd list --status in_progress
bd list --status blocked
bd list --type bug
bd list --label "area:auth"
bd list --title "keyword"

bd ready                        # what's unblocked
bd show <id>
bd show <id> --refs             # with dependencies
```

### Update

```bash
bd update <id> --description "New description"
bd update <id> --status in_progress
bd update <id> --status blocked
bd update <id> --add-label "needs-review"
bd update <id> --remove-label "wip"
bd update <id> --claim          # assignee + in_progress
bd update <id> --priority 1
```

### Close

```bash
bd close <id>
bd close <id> --reason "Completed as specified"
bd close <id> --suggest-next    # what's unblocked now
```

### Labels

```bash
bd label add <id> "label-name"
bd label remove <id> "label-name"
bd label list <id>
bd label list-all
```

### Comments

```bash
bd comments add <id> "text"
bd comments list <id>
```

### Sync

```bash
bd export # export to jsonl for git
```

## Common mistakes

### Non-atomic

❌ "Implement authentication"
✅ Split into registration, login, token, scope enforcement, rate
   limiting.

### Missing context

❌ "Fix the bug in task display"
✅ File + line + repro steps + affected spec section.

### Vague acceptance

❌ "Works correctly"
✅ Specific commands + expected output.

### Unnecessary dependencies

❌ "Make A depend on B because they touch the same file"
✅ Only when B literally cannot start until A.

### No test plan

❌ Acceptance criteria only
✅ Both automated commands and manual steps.

### Missing self-review pair

❌ Creating a main task without a paired self-review task.
✅ Every non-`selfreview`-labelled task gets a paired self-review task,
   dependent on the main task, that runs `/selfreview` in autofix mode.

### Pairing a self-review with another self-review

❌ Creating a self-review task for a task that already has the
   `selfreview` label — infinite loop.
✅ Check labels before pairing; skip if `selfreview` is already there.

## Output format

After creating tasks, always provide:

1. **Summary table** — ids, titles, priorities.
2. **Dependency graph** — ascii tree.
3. **Execution order** — what can be parallel, what's sequential.
4. **Starting point** — which task(s) to begin with.
