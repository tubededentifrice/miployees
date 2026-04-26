"""Shared fixtures for the end-to-end Playwright suite (cd-ndmv).

Spec: ``docs/specs/17-testing-quality.md`` §"End-to-end" + §"Visual
regression" + §"360 px viewport sitemap".

Layered on top of ``pytest-playwright``'s built-in fixtures (``page``,
``context``, ``browser``, ``browser_type``); we only add the cross-
cutting concerns the spec calls out:

* **Base URL.** Read from ``CREWDAY_E2E_BASE_URL`` (default
  ``http://127.0.0.1:8100`` per ``AGENTS.md`` §Environments). The
  ``base_url`` fixture name is the one ``pytest-playwright`` already
  recognises — exporting it here lets every test reach the dev-stack
  loopback without hard-coding an URL.
* **Dev-stack readiness.** A session-scoped fixture pings ``/healthz``
  before any test runs; if the stack is down the suite skips loudly
  (the smoke message tells the developer to bring compose up). This
  is preferable to a parade of opaque connection-refused traces.
* **Tracing / video / screenshot.** ``pytest-playwright``'s
  ``--tracing`` / ``--video`` / ``--screenshot`` CLI flags drive
  artefact emission per §17 ("first failure → trace.zip artefact").
  The plugin defaults each to ``off`` / ``retain-on-failure`` /
  ``only-on-failure`` respectively, so the operator opts in via the
  CLI; AGENTS.md §"End-to-end Playwright suite" pins the
  recommended invocation that turns all three on.

The pytest-playwright defaults already cover screenshot + video on
failure; we extend the storage location so artefacts land beside the
suite (``tests/e2e/_artifacts/``) rather than the repo root.

The full top-level ``tests/conftest.py`` autouse log-isolation
fixture also applies here — Playwright tests don't touch logging
directly, so the harm-vs-help calculus is fine. We do NOT redeclare
the autouse fixture; pytest discovers it through the parent.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

__all__ = [
    "DEFAULT_BASE_URL",
    "ENV_BASE_URL",
    "base_url",
    "browser_context_args",
    "dev_stack_ready",
]


# ``http://127.0.0.1:8100`` is the loopback published by the
# ``web-dev`` Vite container in ``mocks/docker-compose.yml`` —
# AGENTS.md §Environments calls it the canonical dev entry point for
# scripted verification (the public ``dev.crew.day`` host requires
# Pangolin badger forward-auth). Override via env var when the
# operator runs the suite against a non-default stack.
DEFAULT_BASE_URL: Final[str] = "http://127.0.0.1:8100"
ENV_BASE_URL: Final[str] = "CREWDAY_E2E_BASE_URL"

# Where Playwright drops trace.zip / screenshots / videos when its
# ``--tracing`` / ``--screenshot`` / ``--video`` flags fire. Keeps
# artefacts under the suite directory so a CI ``upload-artifact`` step
# only needs to point at one path; ``--output`` is the
# pytest-playwright knob that wires both pieces into the same dir.
_ARTIFACTS_DIR: Final[Path] = Path(__file__).parent / "_artifacts"


def pytest_configure(config: pytest.Config) -> None:
    """Default ``--output`` to the e2e artefact dir if the user hasn't.

    ``pytest-playwright``'s ``--output`` flag controls where it writes
    trace.zip / screenshots / videos when the matching
    ``--tracing`` / ``--screenshot`` / ``--video`` modes fire. Setting
    a per-suite default keeps the artefact tree predictable for both
    local runs and the CI ``upload-artifact`` step (cd-ndmv follow-up).
    """
    output = config.getoption("--output", default=None)
    if not output:
        _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        config.option.output = str(_ARTIFACTS_DIR)


@pytest.fixture(scope="session")
def base_url() -> str:
    """Origin under test. ``pytest-playwright`` consumes this fixture name.

    Resolves to the env var when set, otherwise the loopback default.
    Tests address the SPA via ``page.goto(f"{base_url}/today")`` and
    so on; the helpers in ``_helpers/`` accept the value through their
    ``base_url`` parameter to stay framework-agnostic.
    """
    return os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL).rstrip("/")


@pytest.fixture(scope="session")
def dev_stack_ready(base_url: str) -> str:
    """Ping ``/healthz`` once per session; skip the suite if the stack is down.

    The dev stack runs in Docker via ``mocks/docker-compose.yml``;
    AGENTS.md §"Bring the dev stack up" describes the bring-up. A
    crashed ``app-api`` returns 502 through the Vite proxy, which the
    test would otherwise see as a parade of opaque
    ``net::ERR_*`` traces inside Playwright. We surface the cause once,
    upfront, with the right hint.

    Returns ``base_url`` for chaining (``def test_foo(dev_stack_ready):
    page.goto(dev_stack_ready + "/today")``).
    """
    healthz = f"{base_url}/healthz"
    try:
        with urllib.request.urlopen(healthz, timeout=5) as resp:
            resp.read()
            if resp.status != 200:
                pytest.skip(
                    f"dev stack /healthz returned {resp.status}; "
                    "run `docker compose -f mocks/docker-compose.yml up -d`"
                )
    except (TimeoutError, urllib.error.URLError, ConnectionError) as exc:
        pytest.skip(
            f"dev stack unreachable at {base_url} ({exc!r}); "
            "run `docker compose -f mocks/docker-compose.yml up -d`"
        )
    return base_url


@pytest.fixture
def browser_context_args(
    browser_context_args: dict[str, object],
    base_url: str,
) -> dict[str, object]:
    """Override ``pytest-playwright``'s context kwargs.

    Pre-seeds ``base_url`` so ``page.goto("/today")`` resolves against
    the dev-stack loopback. Other defaults (locale, timezone) stay on
    Playwright's auto values to mirror what a real worker / manager
    sees.

    The ``# noqa: F811`` is the documented way to extend the upstream
    fixture — pytest's fixture resolution merges the dict, but mypy
    sees the redeclaration. The signature mirrors pytest-playwright's
    own:
    https://playwright.dev/python/docs/test-runners#fixtures.
    """
    return {
        **browser_context_args,
        "base_url": base_url,
        # 360x780 is the §17 "360 px viewport sitemap" mobile target;
        # most pilot journeys use the desktop default but we set it
        # here so the mobile-walk test can override per-test. Default
        # to Playwright's standard 1280x720 desktop until then.
    }


@pytest.fixture(autouse=True)
def _require_dev_stack_for_e2e(dev_stack_ready: str) -> Iterator[None]:
    """Force ``dev_stack_ready`` to run before any e2e test.

    Tests don't always declare ``dev_stack_ready`` directly — the
    helpers consume ``base_url`` instead — so the readiness probe
    needs an autouse hook to gate every test in the directory. Without
    it the first test against a down stack would yield Playwright
    timeouts, not the focused "bring the stack up" hint.
    """
    del dev_stack_ready  # consumed for its side effect (readiness probe)
    yield
