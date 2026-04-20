"""REST API v1 routers — one thin router per bounded context.

The :data:`CONTEXT_ROUTERS` registry is the single seam between the
app factory and the 13 bounded-context routers. Order matches the
§01 "Context map" table, which is also the stable tag order rendered
in the merged OpenAPI document.

Explicit registration (over autoload) keeps import-linter happy and
makes it impossible for a new router to appear in the OpenAPI
surface without an explicit line here — i.e. without a reviewer
noticing.

See ``docs/specs/01-architecture.md`` §"Context map" and
``docs/specs/12-rest-api.md`` §"Base URL".
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from .assets import router as assets_router
from .billing import router as billing_router
from .expenses import router as expenses_router
from .identity import router as identity_router
from .instructions import router as instructions_router
from .inventory import router as inventory_router
from .llm import router as llm_router
from .messaging import router as messaging_router
from .payroll import router as payroll_router
from .places import router as places_router
from .stays import router as stays_router
from .tasks import router as tasks_router
from .time import router as time_router

# Ordered registry of (context_name, router). The name is the tag
# key used in the merged OpenAPI doc and the URL segment under
# ``/w/<slug>/api/v1/<context>``; order matches the spec §01
# "Context map" table so the generated OpenAPI has a predictable,
# reviewable shape.
CONTEXT_ROUTERS: Sequence[tuple[str, APIRouter]] = (
    ("identity", identity_router),
    ("places", places_router),
    ("tasks", tasks_router),
    ("stays", stays_router),
    ("instructions", instructions_router),
    ("inventory", inventory_router),
    ("assets", assets_router),
    ("time", time_router),
    ("payroll", payroll_router),
    ("expenses", expenses_router),
    ("billing", billing_router),
    ("messaging", messaging_router),
    ("llm", llm_router),
)

__all__ = ["CONTEXT_ROUTERS"]
