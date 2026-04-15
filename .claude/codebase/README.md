# Codebase maps

This directory will hold **generated summaries** of the codebase — one file
per logical slice — that agents read at the start of a session so they do
not re-explore from scratch (see [`AGENTS.md`](../../AGENTS.md) §Session
bootstrap).

> Miployees is currently pre-implementation. There is no code to map yet.
> When code lands, the first few files to write here are likely:
>
> - `app-layout.md` — package structure, entry points, import graph
> - `domain.md` — key entities and relationships (mirrors
>   [`docs/specs/02-domain-model.md`](../../docs/specs/02-domain-model.md)
>   against the actual SQLAlchemy models)
> - `api.md` — REST surface (mirrors
>   [`docs/specs/12-rest-api.md`](../../docs/specs/12-rest-api.md) against
>   the actual FastAPI routers)
> - `cli.md` — command map for the `miployees` CLI (mirrors
>   [`docs/specs/13-cli.md`](../../docs/specs/13-cli.md))
> - `testing.md` — test layout, fixtures, how to run a narrow slice

## File format

Each map is a plain Markdown file with, at minimum:

```markdown
<!-- verified: YYYY-MM-DD -->

# <slice name>

Short orientation — what lives here, the handful of types/functions that
matter most, and pointers into deeper reading.

## Key paths
- `app/domain/tasks.py` — Task model and scheduler entry point
- ...

## Patterns
- ...

## See also
- `docs/specs/06-tasks-and-scheduling.md`
```

The `<!-- verified: YYYY-MM-DD -->` marker tells future agents when the map
was last cross-checked against reality. AGENTS.md requires a spot-check
update if the marker is older than 30 days.
