# Codebase maps

This directory will hold **generated summaries** of the codebase — one file
per logical slice — that agents read at the start of a session so they do
not re-explore from scratch (see [`AGENTS.md`](../../AGENTS.md) §Session
bootstrap).

> crew.day has a growing implementation as of 2026-04-20. Slice maps have not
> been written yet. Priority files to create (tracked by the first agent that
> writes a substantial new module):
>
> - `app-layout.md` — package structure, entry points, import graph.
>   Key entry points as of cd-ika7:
>   - `app/api/factory.py` — `create_app(settings) -> FastAPI`; the
>     composition root that wires middleware, routers, OpenAPI, and SPA.
>   - `app/main.py` — thin re-export shim; `from app.main import create_app`
>     still works for backward compat (uvicorn `--factory`, test monkeypatches).
>   - `app/api/v1/__init__.py` — `CONTEXT_ROUTERS` registry of the 13
>     bounded-context routers in canonical §01 order; each scaffold is
>     an `APIRouter(tags=["<context>"])` with no routes yet.
>   - `app/api/admin/__init__.py` — `admin_router` scaffold mounted at
>     `/admin/api/v1`; routes land with cd-jlms et al.
> - `domain.md` — key entities and relationships; at minimum document
>   `app/adapters/db/tasks/models.py` (TaskTemplate, Schedule, Occurrence,
>   ChecklistTemplateItem, Evidence, Comment) and
>   `app/domain/tasks/templates.py` (CRUD service, DTOs, typed errors)
> - `api.md` — REST surface (mirrors
>   [`docs/specs/12-rest-api.md`](../../docs/specs/12-rest-api.md) against
>   the actual FastAPI routers)
> - `cli.md` — command map for the `crewday` CLI (mirrors
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
