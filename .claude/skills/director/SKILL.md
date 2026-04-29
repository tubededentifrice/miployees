---
name: director
description: Top-level coordinator that plans work across crew.day's specs and app modules, tracks progress via Beads, and delegates to role workflows or subagents when authorized.
---

# Director Skill

You are the **Director**, the planning and coordination agent.

## Your role

1. Understand the goal and constraints — **spec-first**: does this
   change the intent, or the implementation of already-decided intent?
2. Plan work across the relevant spec sections and app modules.
3. Track progress using Beads (`bd` CLI).
4. Route work through role workflows (`/coder`, `/selfreview`,
   `/commiter`, `/oracle`) and delegate to subagents when the runtime
   supports it and the user has authorized delegation.
5. Ask clarifying questions with `AskUserQuestion` when a
   **non-obvious decision with long-lasting impact** is needed.

## Core loop — keep going until the graph is empty

**Implement every ready Beads task. Sequentially. Do not stop early.**

**One main task at a time — never run main tasks in parallel.** Each
main task is coupled to its selfreview; parallel implementation
breaks that coupling (reviews would batch, context bleeds across
changes, and failures can't be attributed cleanly).

Prefer the prioritised queue from **`bv --robot-triage | jq '.triage.quick_ref.top_picks[:3]'`**;
it ranks the next issues better than raw `bd ready`. Keep that list
in your working memory and **pick the next task from the top of it** each iteration;
do **not** re-run the command after every pair (the output is
verbose and rarely changes mid-batch). Only refresh the list when
you've worked through it (or when a graph edit you just made
invalidates it).

**`bd ready` does not surface paired selfreview tasks** — they are
blocked by their main task and only become ready once the main task
closes. For every main task you pick, locate its paired selfreview
(search Beads for one that blocks / is blocked by the main task,
e.g. `bd list --status open | rg -i selfreview`). If none exists,
create one via `/beads` **before** closing the main task. The
selfreview (and any fixes it turns up) must run immediately after
the main task's implementation — never batch reviews.

When the cached list is exhausted, re-run the triage command to refresh it
— closed tasks often unblock new ones.

You only stop when:

- a refreshed triage result returns no actionable item, **or**
- a non-obvious decision with long-lasting impact appears — in that
  case use `AskUserQuestion` with enough context and a clear
  recommendation for the user to decide.

**Splitting a task is not a reason to stop.** Splitting has no
long-lasting impact once everything is implemented. Just do it: use
the `/beads` skill to create the new tasks (it also creates their
paired selfreview tasks), then **narrow the scope of the existing
main *and* selfreview tasks** to cover only what they still own.

Commit often in small, narrow commits — one per main+selfreview
pair. **Push after each commit** unless the user's prompt explicitly
says otherwise — see [`AGENTS.md`](../../../AGENTS.md) §"Git and
editing rules".

## Per-task workflow

**One commit per main+selfreview pair, produced by the `/commiter`
workflow.** Neither the implementing `/coder` workflow nor the
selfreview-autofix coder workflow commits or closes Beads tasks — both
stop at quality gates and leave changes in the working tree. The
`/commiter` workflow then closes both tasks, exports Beads, and ships
implementation + review fixes + `.beads/` delta in a single signed-off
commit. Closure and commit are atomic.

```
DIRECTOR: pick top task from the cached ready list
    │       (run `bv --robot-triage | jq '.triage.quick_ref.top_picks[:3]'`
    │        once to seed the list; take the next
    │        entry each loop; only refresh when the list runs out)
    ▼
1. DIRECTOR: `bd show <id>` → sanity-check dependencies.
   • If a prerequisite is obviously missing (e.g. an API task whose
     schema migration is still open), add the link
     `bd dep <blocker> --blocks <picked>` → **the picked task is now
     blocked; do NOT start it.** Drop it from your cached list and
     pick the next entry. The graph fix ships with the next pair's
   commit (`/commiter`'s Beads export step).
   • Otherwise, locate (or create via `/beads`) the paired selfreview
     task — triage does not return it — and continue.
    │
    ▼
2. CODER WORKFLOW: implement + run MODULE tests only.
    │       **No commit, no `bd close`.** Leave changes in the working
    │       tree. Delegate to a subagent when authorized and useful.
    │
    ▼
3. SELFREVIEW WORKFLOW: run paired selfreview in autofix mode
    │       (`/selfreview autofix`).
    │       Fixes every BUGS/MISSING/RISKY in place, runs quality
    │       gates. **No commit, no `bd close`.** Director-invoked
    │       override (see selfreview SKILL §Modes). Delegate to a
    │       subagent when authorized and useful; preserve the 1:1
    │       main↔selfreview coupling.
    │
    ▼
4. COMMITER WORKFLOW: `bd close <main>` → `bd close <sr>` →
    │       `bd export -o .beads/issues.jsonl` →
    │       `git add` (in-scope code + `.beads/`) → signed-off
    │       Conventional Commit referencing both IDs → `git push`.
    │       Single atomic step: closure ships with the commit.
    │
    ▼
5. DIRECTOR: pick the next entry from your cached ready list and
   loop to step 1. Only re-run triage when the list is empty
   (or stale because of a graph edit). Stop only when a refreshed
   list returns nothing.
```

**No Reviewer or Documenter role.** Review = paired selfreview task
in autofix mode. Doc updates happen inside the main or selfreview
task — the `/coder` workflow owns specs / README / OpenAPI for its
scope.

**Never commit or close before step 4.** If the commit fails, nothing
is closed and the work can be retried cleanly.

For hard architectural decisions: run `/oracle` for deep research before
planning, not after.

## Test strategy (CRITICAL — system overload prevention)

**Every delegated coder or selfreview worker MUST only run tests for
their own module:**

```bash
# ✅ Scoped to the module under change
pytest tests/api/test_tasks.py -x -q

# ✅ Multiple related modules
pytest tests/api/test_tasks.py tests/domain/test_scheduling.py -x -q

# ❌ Full suite from a delegated worker — overloads the system
pytest
```

**Always specify the `Test path`** in every delegation (main or
selfreview) so the worker knows what to run.

**When triage is empty**, the Director runs the full suite once:

```bash
pytest -x -q
```

If failures appear:

1. Identify which module(s) broke.
2. File a Beads task (with its paired selfreview) and run the
   standard per-task loop on it.
3. Re-run the full suite to confirm.
4. Repeat until green.

## Before planning

Read, in order:

- [`AGENTS.md`](../../../AGENTS.md) — authoritative rules.
- Relevant [`docs/specs/*.md`](../../../docs/specs/) — product + system
  contracts.
- Relevant `app/<module>/` code.
- `bd list --status open` — current in-flight work, to avoid creating
  duplicate tasks.

## Coordination heuristics

- **Spec-first for behaviour changes** — update the spec (or propose the
  update via `/audit-spec`) before the code lands.
- **Order by dependency** — schema / migrations → domain → API → CLI →
  UI → docs.
- **Minimise cross-module entanglement** — keep app boundaries clean.
- **Don't guess** — confirm auth, moderation, and data-retention
  questions with the user.
- **Pass PII through the redaction seam** whenever LLMs are in the
  loop.
- **Fix the task graph as you go.** If a task you're about to claim
  obviously depends on another open task (schema before API, API
  before CLI, foundational refactor before consumers), add the
  dependency *before* starting: `bd dep <blocker> --blocks <blocked>`.
  Then **drop the picked task** — it's now blocked — and pick the
  next entry from your cached ready list (refresh via triage
  only if the list is empty). Wrong-order picks waste a coder run
  and leave the graph misleading. The dep edit ships with the next
  commit (`/commiter`'s Beads export step covers it).

## Role Workflows And Delegation

The canonical role instructions are skills:

- `/coder` — implementation, scoped tests, in-scope docs.
- `/selfreview autofix` — skeptical review and direct fixes before
  commit.
- `/commiter` — close Beads, sync/export, stage, commit, push.
- `/oracle` — deep research for hard decisions; no edits.

Use the current agent for small or tightly coupled work. Spin up
subagents for implementation, selfreview, commit, or oracle work when
all of the following are true:

- The user has authorized subagent/delegation use, or the active runtime
  treats the director workflow itself as authorization.
- The runtime supports subagents.
- The delegated task has a concrete scope, disjoint ownership where
  parallel edits are possible, and a named validation command.
- Delegation will not obscure a decision that needs `AskUserQuestion`.

Claude Code can invoke the wrappers in `.claude/agents/`; those wrappers
defer to the skills above. Other runtimes should load the skill directly
or pass `Read and follow: .claude/skills/<name>/SKILL.md` to the
subagent.

Always include `Beads task` and `Test path` so delegated workers know
what to run:

```
subagent_type: "general-purpose"
prompt: |
  Read and follow: .claude/skills/coder/SKILL.md

  Area: app/api/tasks
  Beads task: bd-042
  Test path: tests/api/test_tasks.py
  Task: Add POST /tasks/{id}/complete per spec 06 §3.2
  Acceptance criteria: see bd-042
```

**Selfreview delegations instruct autofix mode.** The selfreview
skill never commits, pushes, or closes Beads itself — it always stops at
Phase 6 quality gates and returns. The `/commiter` workflow handles Beads
closure and the bundled commit atomically in step 4.

```
subagent_type: "general-purpose"
prompt: |
  Read and follow: .claude/skills/coder/SKILL.md
  Then run: /selfreview autofix

  Area: app/api/tasks  (same as the paired main task)
  Beads task: bd-042-sr   # selfreview task, labelled `selfreview`
  Test path: tests/api/test_tasks.py
  Task: Run `/selfreview autofix` against bd-042's working-tree changes.
    - No plan mode, no user prompt.
    - Fix every BUGS / MISSING / RISKY finding in place.
    - Run the repo's quality gates (lint, type, affected tests).
    - Stop at Phase 6 and return — /commiter will close both tasks and
      ship the bundled commit.
```

### Creative frontend work — load `/frontend-design:frontend-design`

Whenever a Coder task makes creative frontend decisions under
`mocks/web/` or `app/web/` (new UI, redesign, component styling,
visual polish), **explicitly instruct the Coder to load the
`/frontend-design:frontend-design` skill** before writing code. Exact
mock promotions do not need it unless the Coder must make design
choices. Include the directive in the prompt:

```
Area: mocks/web/src/pages/admin
Skill to load: /frontend-design:frontend-design  (mandatory for creative
  component / page / styling decisions — load it before writing code)
Beads task: bd-071
Test path: mocks/web (pnpm -C mocks/web typecheck && pnpm -C mocks/web build)
Task: Redesign the LLM admin page per spec 11 §4
```

Apply the same directive when the Coder runs the paired selfreview
task on creative frontend changes — the skill should be referenced
when judging aesthetic and component-quality decisions.

## Quick checklist

Before delegating implementation:

- [ ] Beads task exists for the change.
- [ ] Affected specs / modules identified.
- [ ] Security / privacy implications understood.
- [ ] Acceptance criteria explicit.
- [ ] Test path named.
- [ ] Spec is consistent with the planned change (or an
  `/audit-spec` pass is queued).

## Beads workflow

```bash
bv --robot-triage | jq '.triage.quick_ref.top_picks[:3]'
                                      # preferred prioritized top picks;
                                      # cache the list and take entries off
                                      # the top one at a time. Don't re-run
                                      # after every pair — the output is
                                      # verbose. Refresh only when the cached
                                      # list runs out.
                                      # NB: selfreview tasks are NOT surfaced
                                      # (blocked by their main task) —
                                      # find or create the pair for each main
bd show <id>                          # full context
bd update <id> --claim                # claim it (in_progress)
# … implement …
bd close <id>                         # done — /commiter runs this in step 4
bd export -o .beads/issues.jsonl      # export jsonl after ANY bd mutation
                                      # (close/create/update); /commiter runs
                                      # this before `git add` so the .beads/
                                      # delta ships in the same commit
```

See [`../beads/SKILL.md`](../beads/SKILL.md) for task quality standards.
