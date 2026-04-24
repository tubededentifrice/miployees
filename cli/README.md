# crewday CLI

The `crewday` command is a thin Click-based client over the crew.day
REST API. Everything a user can do in the web UI is also a CLI verb —
the command tree is generated from the API's OpenAPI schema at build
time (see `cli/crewday/_surface.json`, landing in Beads `cd-1cfg`).

See [`docs/specs/13-cli.md`](../docs/specs/13-cli.md) for the full
spec: command tree, global flags, profile config, exit codes, output
formats, streaming / piping conventions, and agent UX rules. Entry
point is `crewday._main.main`, wired into `[project.scripts]` in the
top-level `pyproject.toml`; internal modules use a leading
underscore so they never collide with generated command names.
