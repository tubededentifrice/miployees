"""Thin re-export shim for the FastAPI application factory.

The factory historically lived here; cd-ika7 moved the body to
:mod:`app.api.factory` so the router-registration surface has a
home under ``app/api/`` alongside the routers it mounts. Every
public name the factory module ever exported is re-exported here
so existing imports — ``from app.main import create_app`` in
``cli/``, ``uvicorn app.main:create_app --factory`` in deployment
recipes, ``monkeypatch.setattr("app.main.setup_logging", …)`` in
the test suite — keep working verbatim.

The private helpers (``_SPA_DIST``, ``_SPA_STUB_HTML``,
``_is_api_path``, ``setup_logging``, ``get_settings``) are re-
exported for the monkeypatch paths in ``tests/unit/test_main.py``.
New call sites should import from :mod:`app.api.factory`
directly.

See ``docs/specs/01-architecture.md`` §"Repo layout"; Beads
``cd-ika7``.
"""

from __future__ import annotations

# Public API — the factory + its exception type.
from app.api.factory import (
    _SPA_DIST,
    _SPA_STUB_HTML,
    PublicBindRefused,
    _is_api_path,
    _resolve_spa_dist,
    _resolve_version,
    create_app,
)

# Re-exported so ``from app.main import get_settings`` still resolves
# for historical imports. The factory's own call sites read
# ``app.api.factory.get_settings`` / ``app.api.factory.setup_logging``
# directly — monkeypatches that need to intercept those reads should
# target the factory module (see tests/unit/test_main.py).
from app.config import get_settings
from app.util.logging import setup_logging

__all__ = [
    "_SPA_DIST",
    "_SPA_STUB_HTML",
    "PublicBindRefused",
    "_is_api_path",
    "_resolve_spa_dist",
    "_resolve_version",
    "create_app",
    "get_settings",
    "setup_logging",
]
