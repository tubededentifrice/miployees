---
name: commiter
description: Commit workflow for crew.day. Use to close Beads tasks, export Beads state, stage explicit paths, create a signed-off Conventional Commit, and push.
---

# Commiter Skill

You are running the **Commiter** workflow for crew.day. You are a git
operator: close Beads tasks, export metadata, stage the requested
paths, commit, and push. Do not implement or review code in this
workflow.

Follow root [`AGENTS.md`](../../../AGENTS.md) for shared-worktree and git
safety rules.

## Inputs

The caller must provide:

- Beads ids to close, or `none`.
- Paths to stage, or explicit permission to stage everything.
- Commit type/scope or enough context to choose one.
- Any branch or PR requirement that overrides the repo default.

## Workflow

### 1. Inspect Status

```bash
git status --short
```

If the tree is clean, report `nothing to commit` and stop.

### 2. Close Beads, Then Export

Close only the Beads ids the caller provided:

```bash
bd close <main-task-id>
bd close <selfreview-task-id>
bd export
```

If no Beads ids were provided, skip closes but still run `bd export` when
Beads is available so pending metadata exports are current.

### 3. Stage Explicit Paths

Prefer explicit paths and include `.beads/` whenever Beads changed:

```bash
git add <paths> .beads
```

Use `git add -A` only when the caller explicitly asked for everything.
Warn and stop before staging likely secrets such as `.env*`, `*.pem`,
`*.key`, or `secrets.*`.

### 4. Commit

Use a signed-off Conventional Commit:

```bash
git commit -s -m "<type>(<scope>): <imperative summary>"
```

Include a body when useful and reference every closed Beads id:

```text
Refs: cd-1234, cd-1234-sr
```

If GPG signing is not configured, retry with `--no-gpg-sign -s`; do not
change git config.

### 5. Push

Run plain push:

```bash
git push
```

Only run `git pull --rebase` if the push is rejected as non-fast-forward.
Never force-push, amend, rewrite history, or skip hooks. Follow
`AGENTS.md` for branch choice: commit directly to `main` by default,
unless the caller asked for review or the change is risky enough to
warrant a branch and PR.

### 6. Response Format

```text
Commit result:
- Status before: <git status --short>
- Staged: <paths>
- Commit: <hash and subject, or failure>
- Push: <success/failure and reason>
- Summary: <one-line outcome>
```
