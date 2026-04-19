# AGENTS.md

Working rules for coding agents (Claude Code, Codex, Cursor, Hermes,
OpenClaw, etc.) operating on this repository.

> **Operating the running system as an LLM agent** (acting on behalf of
> the household manager)? This file is not for you — see
> [`docs/specs/11-llm-and-agents.md`](docs/specs/11-llm-and-agents.md)
> and [`docs/specs/13-cli.md`](docs/specs/13-cli.md). This file is for
> agents writing code in the repo.

## Environments

- **Dev**: <https://dev.crew.day> is gated by Pangolin badger
  forward-auth and is the user's remote entry point. Agents on this
  host can't pass badger and must use the loopback equivalent
  <http://127.0.0.1:8100> (same Vite container, paths 1:1). Point
  `curl`, Playwright, and scripted verification there.
- **Production**: not yet deployed — only specs and mocks live in
  this repo. See `docs/specs/19-roadmap.md`.
- **Bring the dev stack up**: `docker compose -f mocks/docker-compose.yml up -d --build`.
- **Never bind to the public interface.** Use `127.0.0.1` or the
  `tailscale0` interface only — a misbound port is a blocker bug.
  See `docs/specs/16`.

## Ask first

- **Use `AskUserQuestion` for any non-obvious decision.** Batch
  related questions, but never silently guess at ambiguous
  requirements — especially in auth, privacy, payroll, and anything
  touching PII.
- **Use `AskUserQuestion` before any irreversible operation** (delete,
  purge, force-push, overwrite committed work or production data).
  Confirmed per-invocation, not once-per-session.
- **Shared worktree**: multiple agents may be working concurrently.
  Run `git status` before any destructive op; never discard changes
  you didn't make; stop and ask if you see unexpected edits mid-task.

## Session bootstrap

1. Run `/resume-work` (or equivalent) to read recent git log, open
   PRs and issues, uncommitted changes, and active worktrees.
2. Read the codebase map at `.claude/codebase/*.md`. Each carries a
   `<!-- verified: YYYY-MM-DD -->` marker; spot-check and update if
   older than 30 days.
3. Read the relevant spec under `docs/specs/`. **Spec is the source
   of truth; code follows.** Default to updating code on divergence
   unless an ADR or postmortem says otherwise.
4. If `bd` is on `PATH`, skim `bd ready` and claim
   (`bd update <id> --claim`) any task that covers what you're about
   to do. If not, skip — don't block on Beads availability.

## Autonomy and persistence

- **Default to delivering working code**, not a plan. Make the
  reasonable assumption, state it, proceed.
- Gather context, plan, implement, test, refine — within the turn
  when feasible. Bias to action; don't end on clarifying questions
  unless truly blocked.
- Stop if you catch yourself looping — re-reading or re-editing the
  same files without progress.
- **Never call work "done" without verifying it.** Type-checks and
  unit tests prove code compiles, not that the feature works.
  Exercise end-to-end — Playwright for UI, `curl`/CLI for APIs, real
  invocation for scripts. If you can't verify, say so explicitly
  instead of claiming success.

## Partner in thought

The user expects pushback, not compliance. Flag before acting when:

- The change is materially larger than the user seems to expect.
- You see unintended consequences (perf, security, PII, cross-module
  coupling, spec drift).
- The request contradicts a spec, a recent decision, or itself.
- A simpler or cheaper alternative exists.

Say what you'd do instead in one or two lines. Don't silently "fix"
a request you disagree with.

## Keep this file fresh

Treat `CLAUDE.md`, `.claude/skills/`, and `.claude/agents/` as living
instructions. Update them in the same turn when:

- An instruction was wrong, stale, or missing and cost you a retry.
- A skill's procedure failed or produced the wrong output shape.
- You discovered a convention or trap the next agent will also hit.
- The user corrected you on something that will recur.

Prefer editing existing files over adding new ones. Mention the
update in your wrap-up.

## Code quality bar

- **DRY is first-class.** Search (`rg`, `fd`, codebase map) for an
  existing helper or pattern before writing. Extract when two copies
  share a reason to change; wait for the third use otherwise. Same
  for prose — docs reference code, they don't restate it.
- **Correctness and clarity over speed.** Fix root causes, not
  symptoms. Refactor when it genuinely improves things — but
  **confirm intent via `AskUserQuestion` before starting**, so scope
  creep is conscious.
- Follow existing conventions. If you must diverge, say why in the
  PR.
- Preserve behavior unless the task is explicitly about changing it.
  When behavior changes, gate it (feature flag or release note) and
  add tests.
- Tight error handling. No bare `except:`, no silent
  `except Exception: pass`.
- Type safety: `mypy --strict` passes. Avoid `Any` and
  `# type: ignore`.

## Git and editing rules

- Default to ASCII; only introduce non-ASCII when the file already
  uses it or there's a clear reason (user-facing content, examples).
- Rare, concise comments — only where the **why** is non-obvious.
- **Dirty worktree:** never revert edits you didn't make; work with
  overlapping changes; ignore unrelated ones; stop and ask if
  unexpected changes appear mid-task.
- **No destructive git** (`reset --hard`, `checkout --`, `clean -fd`,
  branch deletion) without explicit approval.
- No `--amend` unless requested. No revert commits on unpushed
  work — use `git reset HEAD~1`.
- **Push after every commit.** Run `git push` directly; only reach
  for `git pull --rebase` if the push is rejected as
  non-fast-forward (an unconditional rebase can collide with another
  agent's in-progress work).
- **Never force-push.** Commit directly to `main` by default; only
  cut a branch + PR if the user asks for review or the change is
  risky enough to warrant one.
- If a push fails, diagnose the root cause; do not `--no-verify`,
  don't bypass hooks.

## Tooling conventions

- **No `&&`** in tool calls. Run commands as separate calls; run
  independent ones in parallel.
- **JSON with `jq`**, not Python. Use `gh`'s `--jq` for GitHub
  payloads.
- **Modern CLI tools**: `rg` over `grep`, `fd` over `find`, `sd` over
  `sed`.
- **Read dependencies from the local env**: Python packages under
  the active venv; don't curl GitHub.
- **Never `cd` out of the worktree root** — always use absolute
  paths.

## Skill triggers (repo slash-commands)

Procedures live in `.claude/skills/<name>/` (except `/ai-slop`,
which is `.claude/commands/ai-slop.md`).

| Skill | When |
|-------|------|
| `/create-pr` | Every PR body, rebase merges, description upkeep |
| `/specs` | Interactive spec + mock co-evolution while there's no prod code |
| `/audit-spec` | After any feature adding or removing behavior |
| `/selfreview` | Skeptical pass on your own changes before handoff |
| `/security-check` | Red-team pass on a feature or spec |
| `/gap-finder` | Pre-implementation walk of a spec section, filing Beads tasks for gaps |
| `/director` | Top-level planning across specs / modules |
| `/beads` | Create well-formed Beads tasks from a prompt |
| `/frontend-design:frontend-design` | **Mandatory** before any frontend change under `mocks/web/` (or future `app/web/`) |
| `/ai-slop` | Strip AI-generated noise from a branch before it ships |
| `/update-openapi` | After any change under `app/api/` |
| `/bump-deps` | Periodic dependency bump (uv + Python + JS tooling) |
| `/fix-osv-finding` | Every OSV finding is a blocker |
| `/pre-commit-check` | Before committing — lint, type, unit tests |
| `/new-entity` | Adding a new domain entity |
| `/new-migration` | Every Alembic migration |
| `/record-demo` | After any UI change (tape + GIF committed) |

## Specialised agents

For larger changes, split work across the agents in
[`.claude/agents/`](.claude/agents/):

| Agent | Role |
|-------|------|
| `director` (skill) | Plans, tracks via Beads, delegates |
| `coder` | Implements within a narrow scope; runs only its module's tests |
| `reviewer` | Returns `APPROVED` or `CHANGES_REQUIRED` |
| `documenter` | Updates specs, READMEs, codebase maps, OpenAPI |
| `commiter` | Stages, signs off, commits, pushes — nothing else |
| `oracle` | Deep research for hard decisions; no edits, just advice |

Default flow: `director → coder → reviewer → documenter → commiter →
/selfreview`. Pull in `oracle` when a decision is genuinely hard.
See [`.claude/README.md`](.claude/README.md).

**Every plan must end with `/selfreview`** — regardless of scope —
to catch bugs, missing pieces, and unintended consequences before
pushing.

## Issue tracking with Beads

crew.day uses **Beads** (`bd` CLI) as its task queue. Anything
bigger than a typo or obvious same-file fix should have a Beads
issue so follow-ups don't get lost between sessions.

If `bd` isn't on `PATH`, install it via your package manager
(system, Homebrew, `uv tool install`) — never `curl … | bash`.
Until it's available, skip Beads steps and leave a note for a
Beads-equipped agent.

```bash
bd ready                              # what's unblocked right now
bd show <id>                          # full task context
bd update <id> --claim                # claim it
# … do the work …
bd close <id>                         # done
bd sync                               # export jsonl → git
```

- **Create issues** for anything you discover but won't do this
  turn — not just a line in a commit message.
- **Atomic tasks** — one concern each (see
  [`.claude/skills/beads/SKILL.md`](.claude/skills/beads/SKILL.md)).
- **Link dependencies** with `bd dep <blocker> --blocks <blocked>`
  only when one task literally cannot start before another.
- **Commit the jsonl**: after any `bd` change, `bd sync` and include
  `.beads/` in the same commit (the `commiter` agent handles this).
- **Close what you claim** before handing off, so `bd ready` stays
  honest.

## Presenting your work

Plain text to the user; CLI handles styling.

- Lead with the change and where it lives, not "Summary:".
- Inline code for paths and identifiers. Reference files as
  `app/domain/tasks.py:42`.
- Flat bullets. Short **bold** headings when grouping. No nested
  bullets.
- Don't dump files — reference paths and summarize diffs.

## Application-specific notes

- **Python 3.12+** for all server code.
- **SQLite default; Postgres 15+ supported** — CI runs both. Use
  portable SQL or SQLAlchemy idioms.
- **React frontend.** Mocks (and the upcoming production frontend)
  are a Vite + React + TypeScript strict SPA served by FastAPI from
  `mocks/web/dist`. TanStack Query with optimistic mutations;
  cross-client coherence is SSE-driven (one
  `EventSource('/events')` feeds `queryClient.invalidateQueries`).
  No Alpine, Vue, Tailwind, or HTMX. See `docs/specs/14`.
- **Two spec trees, two surfaces.** App specs live under
  `docs/specs/` and govern everything at `app.crew.day` (and the
  demo at `demo.crew.day`). Marketing-site specs live under
  [`docs/specs-site/`](docs/specs-site/) and govern everything at
  `crew.day` — the landing pages and the agent-clustered
  suggestion box. Keep substantive changes in their own tree;
  cross-tree pointers are fine when one surface needs to know
  the other exists (e.g. the app's env-var table mentioning the
  feedback bridge), but actual content lives where it's owned.
  Site is optional for self-hosters and has its own build +
  deploy under `site/`.
- **Site stack.** `site/web/` is **Astro 4+ with React islands**
  (not Vite-SPA — different SEO and first-paint needs on a
  brochure site), built to static HTML. `site/api/` is FastAPI
  + SQLite, matching the app's Python toolchain. Design tokens
  and icons flow one-way app → site. See `docs/specs-site/00-overview.md`.
- **Semantic CSS classes only.** Name after the thing
  (`task-card`, `shift-timeline`, `payroll-summary`), not the look.
  No utility/atomic classes (Tailwind-style), no inline `style=""`,
  no presentational attributes (`bgcolor`, `align`). Reuse before
  inventing; promote variants via modifiers (`task-card--overdue`).
  Justify one-offs in the PR.
- **No PII to upstream LLMs without explicit opt-in.** Use the
  model client's redaction layer.
- **Time is UTC at rest, local for display.** Timestamp columns are
  `TIMESTAMP WITH TIME ZONE` (Postgres) or ISO-8601 UTC text
  (SQLite). Property-local time is computed on the fly from
  `property.timezone`.
- **Playwright screenshots go to `.playwright-mcp/`.** Always pass
  `filename` under that directory with a descriptive name (see
  `.playwright-mcp/README.md`). Close the browser
  (`mcp__playwright__browser_close`) when done.

## Session wrap-up

- **File follow-ups** as Beads issues, not commit-message
  footnotes.
- **Close or block** what you claimed (`bd close <id>` /
  `bd update <id> --status blocked`); then `bd sync`.
- **Run the quality gates** that apply (`/pre-commit-check`,
  `pytest <scope>`, `mypy`, `ruff`).
- **Commit and push** via the `commiter` agent
  (`.claude/agents/commiter.md`); include `.beads/` in the same
  commit. Push rules in §"Git and editing rules".
- **Summarise briefly**: what changed, where, what's still open,
  what the next agent should pick up from `bd ready`.
