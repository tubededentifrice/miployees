# Remove AI code slop

Check the diff against `main` and remove all AI-generated slop introduced
on this branch. Slop includes:

- Comments that a human wouldn't add or that are inconsistent with the rest
  of the file (restating what the code already says, "added for X ticket",
  "this handles the case where…" walkthroughs of obvious logic).
- Extra defensive checks or `try` / `except` blocks that are abnormal for
  that area of the codebase — especially when called from trusted,
  already-validated code paths.
- Any `# type: ignore`, `cast(...)`, or `Any` that papers over a real type
  issue instead of fixing it. `mypy --strict` must stay clean.
- Unnecessary abstractions, premature helpers, half-finished "future-proof"
  scaffolding, feature flags for things that have only one caller.
- Backwards-compatibility shims, re-exports, or `# removed` comment stubs
  for code that has in fact been deleted on this branch.
- Any style or naming that is inconsistent with the file or project
  patterns (see [`AGENTS.md`](../../AGENTS.md) §"Code quality bar" and
  §"Editing constraints").

Treat specs the same way — overly hedged wording, bullet-list bloat, and
"this section summarises the above" paragraphs are slop in prose too.

## Workflow

1. `git diff main...HEAD` to see everything introduced on the branch.
2. Strip slop in place; keep behaviour identical.
3. Re-run `/pre-commit-check` (or the relevant quality gates) to confirm
   nothing broke.

## Output

Report at the end with **only** a 1–3 sentence summary of what you changed.
No bullet lists, no file dumps.
