# AGENTS.md

Working rules for coding agents (Claude Code, Codex, Cursor, Hermes,
OpenClaw, etc.) operating on this repository. Adapted from the patterns
used by [`micasa-dev/micasa`](https://github.com/micasa-dev/micasa); where
the two disagree, this file wins.

> If you are an **LLM agent operating the running system** (taking actions
> on behalf of the household manager), this file is **not for you** — see
> [`docs/specs/11-llm-and-agents.md`](docs/specs/11-llm-and-agents.md)
> and [`docs/specs/13-cli.md`](docs/specs/13-cli.md) instead. This file
> is for agents writing code in the repo.

## Environments

- **Dev**: <https://dev.crewday.app> is the dev version of the app,
  served by the mocks container running locally on this host. It is
  exposed through Pangolin + Traefik with badger auth (same wiring as
  `../fj2`) and bound locally to `127.0.0.1:8100` (FastAPI mocks) and
  `127.0.0.1:5173` (Vite HMR). Prefer the public URL end-to-end
  (auth, cookies, CSP all match prod shape); use the loopback ports
  for quick `curl` / Playwright runs from this host.
- **Production**: not yet deployed — there is no prod app code in
  this repo yet, only specs and mocks. See `docs/specs/19-roadmap.md`.
- **Bring the dev stack up**: `docker compose -f mocks/docker-compose.yml up -d --build`.
  Never bind to the public interface; see `docs/specs/16`.

## Ask first

- **Use `AskUserQuestion` for any non-obvious decision.** When in doubt,
  ask. Batch related questions so you are not pinging the user every
  thirty seconds, but do not silently guess at ambiguous requirements —
  especially in auth, privacy, payroll, and anything touching PII.
- **Use `AskUserQuestion` before any irreversible operation.** Never
  delete, purge, force-push, or overwrite production data or committed
  work without explicit user approval. Destructive git and destructive
  DB operations are confirmed per-invocation, not once-per-session.
- **Shared codebase, shared worktree.** Multiple agents may be working
  concurrently. Before `git checkout`, `git stash`, `git clean`, or any
  reset, run `git status` and understand what's there. Never discard
  changes you did not make. If you see unexpected edits mid-task, stop
  and ask.

## Session bootstrap

At the start of every session:

1. Run `/resume-work` (or equivalent) to read the latest git log, open PRs
   and issues, uncommitted changes, and active worktrees.
2. Read the codebase map at `.claude/codebase/*.md`. These files summarize
   package layout, key types, and patterns so you do not re-explore from
   scratch. They carry a `<!-- verified: YYYY-MM-DD -->` marker. If older
   than 30 days, spot-check the documented paths and update the file.
3. Read the relevant spec under `docs/specs/`. **The spec is the source of
   truth; the code follows.** If code and spec diverge, default to
   updating the code — unless the divergence was an explicit decision
   recorded in a postmortem, ADR, or spec revision.
4. If the `bd` CLI is available (`command -v bd` succeeds), skim
   `bd ready` — Beads is the task queue (see §"Issue tracking with
   Beads" below). If a task covers what you are about to do, claim it
   (`bd update <id> --claim`) rather than starting fresh. If `bd` is
   not installed in your environment, skip this step and proceed from
   specs and `git log`; leave a note for a Beads-equipped agent if
   you notice something worth tracking. Do **not** block session
   start on Beads availability.

## Autonomy and persistence

- Default to **delivering working code**, not a plan. If a detail is
  missing, make a reasonable assumption, state it, and proceed.
- Operate as a staff engineer: gather context, plan, implement, test,
  refine. Persist to a complete, verified outcome within the turn when
  feasible.
- Bias to action; do not end a turn with clarifying questions unless you
  are truly blocked.
- Stop if you catch yourself looping — re-reading or re-editing the same
  files without progress. End the turn with a concise summary.

## Partner in thought

The user expects pushback, not compliance. Flag before acting when:

- The change is materially larger than the user seems to expect
  (touches far more files, forces a migration, breaks callers).
- You see unintended consequences (perf, security, PII, cross-module
  coupling, spec drift).
- The request contradicts a spec, a recent decision, or itself.
- A simpler or cheaper alternative exists.

Say what you'd do instead and why, in one or two lines. Do not silently
"fix" a request you disagree with.

## Keep this file fresh

Treat `CLAUDE.md`, `.claude/skills/`, and `.claude/agents/` as living
instructions. When you hit one of these, update them in the same turn:

- An instruction was wrong, stale, or missing and cost you a retry.
- A skill's procedure failed or produced the wrong shape of output.
- You discovered a convention (or a trap) the next agent will also hit.
- The user corrected you on something that will recur.

Prefer editing the existing file over adding new ones; keep wording
concise. Mention the update in your wrap-up so the user sees it.

## Code quality bar

- Correctness and clarity over speed. No speculative refactors, no
  symptom-only patches — fix root causes.
- Follow existing conventions (naming, formatting, package layout, test
  patterns). If you must diverge, say why in the PR.
- Preserve behavior unless the task is explicitly about changing it. When
  behavior does change, gate it (feature flag or explicit release note)
  and add tests.
- Tight error handling. No bare `except:`, no silent `except Exception:
  pass`. Errors propagate or are logged explicitly.
- Type safety: the codebase is fully type-annotated. `mypy --strict`
  passes. Avoid `Any` and `# type: ignore`.
- DRY with judgment — search for existing helpers before writing a new
  one; but three similar lines is not yet a helper.

## Editing constraints

- Default to ASCII. Only introduce non-ASCII when the file already uses
  it or there is a clear reason (user-facing content, examples).
- Rare, concise comments — only where the **why** is non-obvious.
- You may land in a dirty worktree.
    - **Never revert** edits you did not make; they are the user's.
    - If the changes are in files you are touching, work with them.
    - If unrelated, ignore them.
    - If you see unexpected changes mid-task, **stop immediately** and
      ask.
- No destructive git (`reset --hard`, `checkout --`, `clean -fd`, branch
  deletion) without explicit approval.
- No `--amend` unless explicitly requested.
- No revert commits on unpushed work — use `git reset HEAD~1` instead.
- Never force-push to `main`.

## Tooling conventions

- **No `&&`** in tool calls. Run commands as separate tool calls; run
  independent ones in parallel.
- **JSON with `jq`**, not Python. Use `gh`'s `--jq` for GitHub payloads.
- **Modern CLI tools**: `rg` over `grep`, `fd` over `find`, `sd` over
  `sed`.
- **Read dependencies from the local env**: Python packages under the
  active venv; do not curl GitHub.
- **Never `cd` out of the worktree root** — always use absolute paths.

## Skill triggers (repo slash-commands)

These are executed as skills when working on this repo. Full procedures
live in the skill files themselves.

| Skill | When |
|-------|------|
| `/create-pr` | Every PR body, rebase merges, description upkeep |
| `/specs` | Interactive spec + mock co-evolution while there's no prod code — see `.claude/skills/specs/` |
| `/audit-spec` | After any feature adding or removing behavior — see `.claude/skills/audit-spec/` |
| `/selfreview` | Skeptical pass on your own changes before handoff — `.claude/skills/selfreview/` |
| `/security-check` | Red-team pass on a feature or spec — `.claude/skills/security-check/` |
| `/gap-finder` | Pre-implementation walk of a spec section, filing Beads tasks for gaps — `.claude/skills/gap-finder/` |
| `/director` | Top-level planning across specs / modules — `.claude/skills/director/` |
| `/beads` | Create well-formed Beads tasks from a prompt — `.claude/skills/beads/` |
| `/ai-slop` | Strip AI-generated noise from a branch before it ships — `.claude/commands/ai-slop.md` |
| `/update-openapi` | After any change under `app/api/` |
| `/bump-deps` | Periodic dependency bump (uv + Python + JS tooling) |
| `/fix-osv-finding` | Every OSV finding is a blocker |
| `/pre-commit-check` | Before committing — lint, type, unit tests |
| `/new-entity` | Adding a new domain entity (see checklist) |
| `/new-migration` | Every Alembic migration (see checklist + backfill rules) |
| `/record-demo` | After any UI change (tape + GIF committed) |

## Specialised agents

For larger changes, split the work across the agents in
[`.claude/agents/`](.claude/agents/):

| Agent | Role |
|-------|------|
| `director` (skill) | Plans, tracks via Beads, delegates |
| `coder` | Implements within a narrow scope; runs only its module's tests |
| `reviewer` | Returns `APPROVED` or `CHANGES_REQUIRED`; runs only its module's tests |
| `documenter` | Updates specs, READMEs, codebase maps, OpenAPI |
| `commiter` | Stages, signs off, commits, pushes — nothing else |
| `oracle` | Deep research for hard decisions; no edits, just advice |

The default flow is
`director → coder → reviewer → documenter → commiter → /selfreview`,
with `oracle` pulled in when a decision is genuinely hard. See
[`.claude/README.md`](.claude/README.md) for details.

Every plan MUST end with this final step:

> **After implementation is committed**, run `/selfreview` to catch bugs, missing pieces, and unintended consequences. Fix any issues found before pushing.

This applies to all plans, regardless of scope. The self-review enters
its own plan mode — this is intentional.

## Issue tracking with Beads

Crewday uses **Beads** (`bd` CLI) as its task queue. Non-trivial
work — anything bigger than a typo, a one-line clarification, or an
obvious same-file fix — should have a Beads issue so follow-ups don't
get lost between sessions. Day-to-day tweaks can skip it.

**Installing `bd`** is out of scope for this repo. If `bd` is not on
your `PATH`, install it through your environment's normal package
channel (system package manager, Homebrew, `uv tool install`, or
whatever Beads publishes). Do **not** `curl … | bash` or pull binaries
ad-hoc — see §"Tooling conventions". Until `bd` is available, every
Beads instruction in this file is optional: fall back to specs and
`git log`, and leave a note for a Beads-equipped agent to pick up
anything you noticed.

```bash
bd ready                              # what's unblocked right now
bd show <id>                          # full task context
bd update <id> --claim                # claim it
# … do the work …
bd close <id>                         # done
bd sync                               # export jsonl → git
```

- **Create issues** for anything you discover but won't do this turn —
  don't leak follow-ups into commit messages only.
- **Keep tasks atomic** — one concern per task (see
  [`.claude/skills/beads/SKILL.md`](.claude/skills/beads/SKILL.md)).
- **Link dependencies** with `bd dep <blocker> --blocks <blocked>` only
  when one task literally cannot start before another.
- **Commit the jsonl** — after any `bd` change, run `bd sync` and
  include the `.beads/` updates in the same commit as the code
  change. The `commiter` agent does this automatically.
- **Close what you finished** — if you claimed an issue, `bd close
  <id>` it before handing off, so `bd ready` stays honest for the next
  agent.

Push after every commit. Run `git push` directly; only reach for
`git pull --rebase` if the push is rejected as non-fast-forward. An
unconditional `pull --rebase` can collide with another agent's
in-progress work in the shared worktree (it can refuse, or rewrite
their staged state on resume) and force you into a stash/unstash
dance you didn't need. Never force-push, never push to `main` without
an explicit user instruction (the default is a branch + PR).

## Presenting your work

Plain text to the user. CLI handles styling.

- Lead with the change and where it lives, not "Summary:".
- Inline code for paths and identifiers. Reference files as
  `app/domain/tasks.py:42`.
- Flat bullets. Short **bold** headings when grouping.
- No nested bullets.
- Do not dump files — reference paths and summarize diffs.

## Application-specific notes

- **Python is required.** All server code is Python 3.12+.
- **SQLite is the default store.** Code must also work on Postgres 15+ —
  CI runs both. Use only portable SQL or SQLAlchemy idioms.
- **React is the interaction model.** The mocks — and, shortly, the
  production frontend — are a Vite + React + TypeScript strict SPA
  served by FastAPI from `mocks/web/dist`. Data goes through TanStack
  Query with optimistic mutations; cross-client coherence is SSE-driven
  (one `EventSource('/events')` feeds `queryClient.invalidateQueries`).
  No Alpine, no Vue, no Tailwind, no HTMX. See `docs/specs/14`.
- **Semantic CSS classes only in HTML.** Keep the markup decoupled from
  the presentation layer. Name classes after the thing they represent
  (`task-card`, `shift-timeline`, `payroll-summary`), never after how it
  looks (`mt-4`, `text-red`, `flex-row`). No utility/atomic classes
  (Tailwind-style), no inline `style=""`, no presentational attributes
  (`bgcolor`, `align`). Styling lives in CSS keyed off semantic class
  names; reuse an existing class before inventing a new one, and promote
  variants via modifiers (`task-card--overdue`) rather than ad-hoc
  wrappers. If a one-off style is genuinely unavoidable, justify it in
  the PR.
- **Do not bind to the public interface.** See `docs/specs/16`.
  Development and production bind to `127.0.0.1` or the `tailscale0`
  interface only. A misbound port is a blocker bug.
- **No PII to upstream LLMs without explicit opt-in.** The model client
  has a redaction layer; use it.
- **Time is UTC at rest, local for display.** Every timestamp column is
  `TIMESTAMP WITH TIME ZONE` (Postgres) or ISO-8601 UTC text in SQLite.
  Property-local time is computed on the fly from `property.timezone`.
- **Playwright screenshots go to `.playwright-mcp/`.** When using the
  Playwright MCP tools (`mcp__playwright__browser_take_screenshot`,
  etc.), always pass a `filename` under `.playwright-mcp/` so the image
  lands in the gitignored screenshots directory rather than the repo
  root. Use descriptive names (`homepage-desktop.png`,
  `bug-shift-timeline-overflow.png`) — see `.playwright-mcp/README.md`
  for the naming convention. Close the browser
  (`mcp__playwright__browser_close`) when verification is done.

## Session wrap-up

Before handing a session back to the user:

- **File follow-ups.** Anything you discovered but deliberately did not
  do becomes a Beads issue, not a line in the commit message.
- **Close what you finished.** `bd close <id>` for anything you claimed
  and completed; `bd update <id> --status blocked` (with a comment) for
  anything stuck.
- **Sync Beads.** `bd sync` so the jsonl matches the Dolt state.
- **Run the quality gates** that apply to what changed — whichever of
  `/pre-commit-check`, `pytest <scope>`, `mypy`, `ruff` the situation
  calls for.
- **Commit and push.** Delegate to the `commiter` agent
  (`.claude/agents/commiter.md`) for narrow Conventional-Commits
  commits; include `.beads/` changes in the same commit. Then
  `git push` — and only `git pull --rebase && git push` if the push
  is rejected as non-fast-forward (see the push rules above).
- **Summarise briefly.** One short paragraph: what changed, where it
  lives, what's still open, what the next agent should pick up from
  `bd ready`.

If a push fails, diagnose the root cause — do not force-push, do not
`--no-verify`, do not bypass hooks.
