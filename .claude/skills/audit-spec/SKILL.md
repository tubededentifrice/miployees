---
name: audit-spec
description: Systematically review specs vs code (in either direction). Update specs that have drifted, and create Beads tasks for code that doesn't match intended behaviour.
---

# Audit-Spec Skill

Systematically compare specification documents against the actual
implementation. Update specs that have drifted from reality, create
Beads tasks for code that doesn't match the intended specification.

> This skill matches the `/audit-spec` trigger in
> [`AGENTS.md`](../../../AGENTS.md). Run it after any feature that adds
> or removes behaviour.

## Key principles

1. **Specs describe intent** — what the system *should* do.
2. **Code describes reality** — what the system *actually* does.
3. **Divergence requires a decision**:
   - Code is correct and spec is outdated → **update the spec**.
   - Spec is correct and code is wrong → **create a Beads task**.
   - Unclear → **ask the user** with `AskUserQuestion`.

Crewday is spec-first: the default when in doubt is to update the
code to match the spec. But the spec can still lag — especially after
incremental implementation learnings — and reality eventually wins.

## Workflow

```
1. LOAD CONTEXT
   - Read AGENTS.md
   - List all spec files under docs/specs/
   ↓
2. REVIEW EACH SPEC (parallel subagents)
   - Compare spec vs implementation
   - Identify divergences
   - Write findings to ./reports/
   ↓
3. COLLECT FINDINGS
   - Aggregate reports
   - Group by type: spec-updates vs code-changes
   ↓
4. TRIAGE WITH USER
   - For each divergence, decide: update spec or fix code?
   - AskUserQuestion for unclear cases and batch confirmation
   ↓
5. APPLY SPEC UPDATES
   - Edit spec files directly
   - Commit spec changes
   ↓
6. CREATE BEADS TASKS
   - Atomic, testable tasks for code changes
   - Proper dependencies between related tasks
   ↓
7. SUMMARY
```

## Phase 1: Load context

```bash
ls -1 docs/specs/*.md
```

The specs, as of this skill's writing:

| Spec | Domain |
|------|--------|
| `00-overview.md` | Vision, personas, goals, non-goals |
| `01-architecture.md` | Stack, components, repo layout |
| `02-domain-model.md` | Entities, ERD, ID strategy |
| `03-auth-and-tokens.md` | Passkeys, magic links, API tokens |
| `04-properties-and-stays.md` | Properties, areas, iCal, guests |
| `05-employees-and-roles.md` | Staff model, roles, capabilities |
| `06-tasks-and-scheduling.md` | Task model, RRULE, evidence |
| `07-instructions-kb.md` | Global / house / room SOPs |
| `08-inventory.md` | Supplies, linens, reorder |
| `09-time-payroll-expenses.md` | Clock-in, pay rules, expense claims |
| `10-messaging-notifications.md` | Comments, issues, email, webhooks |
| `11-llm-and-agents.md` | OpenRouter, model assignment, audit |
| `12-rest-api.md` | OpenAPI surface, conventions |
| `13-cli.md` | `crewday` CLI |
| `14-web-frontend.md` | HTMX, PWA, offline, a11y |
| `15-security-privacy.md` | Threat model, secrets, GDPR |
| `16-deployment-operations.md` | Packaging, backups, observability |
| `17-testing-quality.md` | Test strategy, CI gates |
| `18-i18n.md` | Deferred locales, seam design |
| `19-roadmap.md` | Phased delivery plan |
| `20-glossary.md` | Terms used across the spec |

Before launching subagents, skim `bd list --status open` — a planned
change may already explain a divergence.

## Phase 2: Review each spec (parallel subagents)

Launch subagents in **parallel**, grouped by domain.

### Subagent groups

```yaml
Group 1 — Core domain:
  specs: [02-domain-model.md, 05-employees-and-roles.md,
          06-tasks-and-scheduling.md]
  code:  [app/domain/, app/db/migrations/]

Group 2 — Properties & stays:
  specs: [04-properties-and-stays.md, 08-inventory.md]
  code:  [app/domain/properties/, app/domain/inventory/]

Group 3 — Auth & security:
  specs: [03-auth-and-tokens.md, 15-security-privacy.md]
  code:  [app/auth/, app/security/]

Group 4 — Surfaces:
  specs: [12-rest-api.md, 13-cli.md, 14-web-frontend.md]
  code:  [app/api/, app/cli/, app/web/, templates/]

Group 5 — LLM & agents:
  specs: [07-instructions-kb.md, 11-llm-and-agents.md]
  code:  [app/llm/, app/agents/]

Group 6 — Infra & quality:
  specs: [01-architecture.md, 16-deployment-operations.md,
          17-testing-quality.md]
  code:  [config/, tests/, docker/, docker-compose*.yml]
```

### Subagent prompt template

```
subagent_type: "Explore"
prompt: |
  # Spec review: {Group name}

  ## Objective
  Compare specifications against the implementation. Find ALL
  divergences.

  ## Specs
  {list}

  ## Code paths
  {list}

  ## Output
  Write findings to: ./reports/audit-spec-{group-name}.md

  For each divergence:

  1. **Divergence type**:
     - SPEC_OUTDATED: code is correct, spec needs update
     - CODE_WRONG: spec is correct, code needs fix
     - UNCLEAR: needs user decision

  2. **Document each finding**:

     ### [TYPE] Brief title

     **Spec location**: docs/specs/XX-name.md, line N
     **Spec says**: "exact quote"

     **Code location**: app/foo/bar.py:123
     **Code does**: "actual behaviour"

     **Recommended action**: …

     **Impact**: [LOW | MEDIUM | HIGH | CRITICAL]
     - Privacy / security?
     - Public API contract?
     - User-visible?

  ## Checklist
  - [ ] Read each spec fully
  - [ ] Check all code paths named by the spec
  - [ ] Verify model fields, URL patterns, API responses
  - [ ] Features in spec but not in code
  - [ ] Features in code but not in spec
```

## Phase 3: Collect findings

Aggregate into `./reports/audit-spec-SUMMARY.md`:

```markdown
# Audit-spec summary — {date}

## Overview
| Group | SPEC_OUTDATED | CODE_WRONG | UNCLEAR | Total |
|-------|---------------|------------|---------|-------|
| …     | …             | …          | …       | …     |

## Spec updates needed (SPEC_OUTDATED)
1. …

## Code fixes needed (CODE_WRONG)
1. …

## Needs decision (UNCLEAR)
1. …
```

## Phase 4: Triage with the user

For **each divergence**, use `AskUserQuestion`. Always provide:

1. **What the spec says** (exact quote).
2. **What the code does** (exact behaviour).
3. **Impact of each option**.
4. **Your recommendation, with reasoning**.
5. **Dependencies** on other findings.

### Unclear items — ask per item

```yaml
- question: "Spec says '{spec}' but code does '{code}'. Which is correct?"
  header: "{short title}"
  multiSelect: false
  options:
    - label: "Update spec (Recommended)"
      description: "Code is correct; spec is outdated."
    - label: "Fix code"
      description: "Spec is correct; create a Beads task."
    - label: "Skip"
      description: "Accept the discrepancy for now."
```

### SPEC_OUTDATED — batch confirm

```yaml
- question: "Found {N} outdated spec sections. Update them?"
  header: "Spec updates"
  options:
    - label: "Yes, update all (Recommended)"
    - label: "Review individually"
    - label: "Skip"
```

### CODE_WRONG — batch confirm

```yaml
- question: "Found {N} code divergences. Create Beads tasks?"
  header: "Code tasks"
  options:
    - label: "Yes, create tasks (Recommended)"
    - label: "Review individually"
    - label: "Skip"
```

## Phase 5: Apply spec updates

1. Read the current spec.
2. Edit **only the divergent sections** — preserve structure.
3. Commit:

```bash
git add docs/specs/
git commit -s -m "docs(specs): update specs to match implementation

Identified by /audit-spec on $(date +%Y-%m-%d).

Sections:
- docs/specs/06-tasks-and-scheduling.md §3.2 — document evidence upload limits
- docs/specs/12-rest-api.md §Pagination — clarify cursor semantics"
```

## Phase 6: Create Beads tasks

For confirmed code fixes, create atomic, testable tasks. See
[`../beads/SKILL.md`](../beads/SKILL.md) for the full standard. Each
task MUST be atomic, testable, contextualised, and dependency-aware.

### Task template

```bash
bd create "fix(<area>): <brief description>" --body "$(cat <<'EOF'
## Context

Spec `docs/specs/XX-name.md` (line N) says:
> "exact quote"

But the implementation does: {actual behaviour}.

## Spec reference
- **Spec**: docs/specs/XX-name.md §Y.Z
- **Requirement**: {paraphrase}

## Current behaviour
- **Location**: app/foo/bar.py:123
- **Behaviour**: {what it does}
- **Impact**: {who / what is affected}

## Expected behaviour
{what it should do}

## Test plan
\`\`\`bash
pytest tests/foo/test_bar.py::test_specific_behaviour -xvs
\`\`\`

## Acceptance criteria
- [ ] {specific criterion}
- [ ] Spec requirement met
- [ ] No regression elsewhere

## Dependencies
{if any, list bd-<id>s}
EOF
)"
```

### Dependencies

When creating multiple related tasks:

1. **Create blockers first** (schema / migrations).
2. **Link dependents** with `bd dep <blocker> --blocks <blocked>`.
3. **Visualise the graph** in your final summary.

## Phase 7: Summary

```markdown
# Audit-spec complete

## Spec updates applied
| Spec | Section | Change |
|------|---------|--------|
| 04-properties-and-stays.md | §iCal | documented two-way sync limits |

Commit: <sha>

## Beads tasks created
| id | Title | Priority | Deps |
|----|-------|----------|------|
| bd-001 | fix(api): … | high | — |
| bd-002 | fix(cli): … | medium | bd-001 |

## Items skipped
…

## Next steps
1. `bd ready` — ordered work queue.
2. Start with blockers.
3. Each task has a test plan in its body.
```

## Quality checklist

- [ ] Every spec reviewed (directly or via subagent).
- [ ] Every divergence has a decision.
- [ ] User was asked about every UNCLEAR item.
- [ ] Spec updates committed.
- [ ] Beads tasks are atomic and testable.
- [ ] Dependencies linked.
- [ ] Summary delivered.

## Common divergence types

### Model fields

Spec says model has field X, code doesn't — check rename, move to
related model, or descoped feature.

### URL patterns

Spec `/tasks/{id}/complete`, code `/tasks/{id}/complete/` — seemingly
trivial but may break agent clients. Ask.

### API responses

Response shape, pagination, error envelope — any shift is a contract
break for agent consumers.

### Auth / scopes

Spec says manager-only, code allows any staff — security implication.
Always ask.

### Feature flags

Spec documents the feature, code has it behind a flag — flag should
be documented or removed.

## Tips

- **Read specs thoroughly.** Skimming causes false positives.
- **Check migrations.** Model divergences often show up there.
- **Search for `TODO`.** They often reference spec gaps.
- **Run it when in doubt.** Don't assume — reproduce.
- **Be conservative.** Ask when unclear.
- **Atomic tasks.** One fix per task, even if tedious.
