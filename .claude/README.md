# Claude Code configuration for crew.day

This directory holds agent-development configuration for anyone operating on
the crew.day codebase with an AI coding tool (Claude Code, Codex, Cursor,
OpenClaw, etc.).

> Authoritative rules live in the top-level [`AGENTS.md`](../AGENTS.md).
> The files in this directory are the *how* — standards, playbooks, and
> specialised agent roles — that support those rules.

## Status

crew.day is **pre-implementation**. The repo currently contains specs only
(see [`docs/specs/`](../docs/specs/)). The agents and skills here are sized
for that reality: the most useful ones right now are `audit-spec`,
`selfreview`, `director`, `gap-finder`, and `security-check` — all of which
operate primarily on specifications. More will land as code does.

## Directory structure

```
.claude/
├── README.md              # This file
├── agents/                # Specialised agent roles
│   ├── oracle.md          # Deep research / hard decisions (slow, expensive)
│   ├── coder.md           # Implementation (code + docs in scope)
│   └── commiter.md        # Stage, commit, push
├── skills/                # Reusable playbooks loaded per task
│   ├── audit-spec/        # Spec ↔ code drift audit (matches `/audit-spec` trigger)
│   ├── selfreview/        # Skeptical review of your own recent changes
│   ├── director/          # Top-level planning across specs / apps
│   ├── security-check/    # Red-team pass on a feature or spec
│   └── gap-finder/        # Pre-impl: find holes and contradictions in specs
└── commands/
    └── ai-slop.md         # Slash command: strip AI-generated noise from a branch
```

## Skills vs. agents

- **Skills** describe *how* to do something — a playbook loaded for a
  particular kind of task.
- **Agents** describe *who* does something — a role with its own constraints
  and output format, usually invoked by the Director.

Most day-to-day work goes through skills. Agents are used when a task is
large enough that it helps to separate implementation from review, or when a
decision is hard enough to warrant a dedicated research role.

## Typical workflow

```
DIRECTOR (plan) → CODER (implement + docs) → /selfreview (paired task)
                                                     │
                                                     ▼
                                             COMMITER (commit + push)
```

See [`.claude/skills/director/SKILL.md`](skills/director/SKILL.md) for
the full per-task loop.

For hard problems, any agent may invoke **ORACLE** for deep research.
