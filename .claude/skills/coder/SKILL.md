---
name: coder
description: Implementation workflow for crew.day coding tasks. Use when implementing a scoped code, test, or documentation change, especially when delegated by the director workflow.
---

# Coder Skill

You are running the **Coder** workflow for crew.day.

## Role

1. Read the relevant spec before changing code.
2. Read existing code and search for matching helpers or patterns.
3. Implement the requested scope and nothing unrelated.
4. Update in-scope docs/specs/OpenAPI when behavior changes.
5. Run the narrow quality gates for the touched area.
6. Report what changed, what passed, and any remaining caveats.

Follow root [`AGENTS.md`](../../../AGENTS.md) for repository-wide rules:
shared worktree safety, AskUserQuestion triggers, type safety, privacy,
frontend constraints, Beads usage, and git restrictions.

## Required Inputs

Every delegated coder task should include:

- `Area`: files, module, or spec section.
- `Beads task`: issue id, or `none`.
- `Task`: concrete implementation request.
- `Acceptance criteria`: what proves the work is complete.
- `Test path`: the narrow test or validation command to run.

## Workflow

### 1. Understand Scope

- Read `bd show <id>` when a Beads task is provided.
- Read the relevant `docs/specs/*.md`; spec is the source of truth.
- If the request contradicts a spec, stop and ask unless the task is
  explicitly to change the spec.
- For creative frontend work under `mocks/web/` or `app/web/`
  (new UI, redesign, component styling, visual polish), load
  `/frontend-design:frontend-design` before editing UI code. Exact
  mock promotions do not need it unless you make design choices.

### 2. Search Before Writing

Use `rg` / `fd` to find existing patterns before adding helpers,
schemas, endpoints, components, or tests. Prefer reuse or small
extension over duplication.

### 3. Implement

- Keep changes inside the requested area.
- Type public signatures.
- Avoid `Any`, `cast`, `# type: ignore`, bare `except:`, and silent
  `except Exception: pass`.
- Preserve behavior unless the task explicitly changes it.
- Route PII through the model-client redaction layer before any upstream
  LLM call.
- Do not commit, push, or close Beads tasks from this workflow.

### 4. Test

Run the narrow gates for the touched module. Prefer existing wrappers
when present:

```bash
./scripts/lint.sh
./scripts/format.sh --check
./scripts/typecheck.sh
pytest <test path> -x -q
```

For delegated coder work, run only the named module tests; the director
or final caller owns broader suite selection.

### 5. Response Format

```text
Completed: <summary>
Beads task: <id or "none">

Changes:
- <path>: <what changed>

Validation:
- <command>: passing

Specs / docs:
- <path or "none needed">

Notes:
- <caveats or follow-ups>
```
