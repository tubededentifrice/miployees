"""Boundary tests for the import-linter gate (cd-ev0).

These tests exercise the import-boundary contracts declared in
``pyproject.toml`` under ``[tool.importlinter]``. The spec is
``docs/specs/01-architecture.md`` §"Module boundaries" (rules 1-6)
and ``docs/specs/17-testing-quality.md`` §"Import boundaries".

Two scenarios:

* **Positive** — ``uv run lint-imports`` on the clean tree exits 0.
  Guards against a future change accidentally introducing a
  cross-boundary import or breaking the config.
* **Negative** — writing a deliberately bad file at
  ``app/domain/identity/_bad_cross_boundary_test.py`` that imports
  ``app.adapters.db.session`` causes ``lint-imports`` to exit
  non-zero. Guards against a silent misconfiguration of the gate
  (e.g. a typo in ``source_modules`` would still report "all
  kept").

The bad file lives inside ``app/`` only for the duration of the
negative test; an autouse fixture unlinks it both before and after
so a crash between steps — or a stale file from an earlier
interrupted run — cannot leak into other test runs or, worse, into
git.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

# Repository root = three levels above this file
# (tests/boundary/test_import_gate.py -> tests/boundary -> tests -> repo).
REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# Path of the deliberately-bad fixture file written by the negative
# test. Kept at module scope so the autouse cleanup fixture can
# reference the same path the test writes to.
BAD_FILE: Path = (
    REPO_ROOT / "app" / "domain" / "identity" / "_bad_cross_boundary_test.py"
)

BAD_FILE_CONTENTS: str = (
    '"""Deliberately bad import used by tests/boundary/test_import_gate.py.\n\n'
    "This file must never be committed. If you are reading it outside a\n"
    "test run, delete it.\n"
    '"""\n\n'
    "from app.adapters.db.session import make_engine  # noqa: F401\n"
)


@pytest.fixture(autouse=True)
def _ensure_bad_file_absent() -> Iterator[None]:
    """Guarantee the bad fixture file is gone before and after every test.

    Without this, a crash mid-test (or a killed pytest process)
    would leave ``_bad_cross_boundary_test.py`` sitting inside
    ``app/domain/identity/`` — where it would both fail subsequent
    ``lint-imports`` runs and risk being committed by a careless
    ``git add``.
    """
    BAD_FILE.unlink(missing_ok=True)
    try:
        yield
    finally:
        BAD_FILE.unlink(missing_ok=True)


def _run_lint_imports() -> subprocess.CompletedProcess[str]:
    """Invoke ``uv run lint-imports`` from the repo root and capture output."""
    return subprocess.run(
        ["uv", "run", "lint-imports"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_tree_passes() -> None:
    """``lint-imports`` on the untouched repo must exit 0.

    Acts as the baseline that proves the three boundary contracts
    are satisfied today. A regression here means something in
    ``app/`` started importing across a forbidden seam.
    """
    result = _run_lint_imports()
    assert result.returncode == 0, (
        f"lint-imports unexpectedly failed on the clean tree.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_cross_boundary_import_is_rejected() -> None:
    """A deliberately bad cross-boundary import must fail ``lint-imports``.

    Writes a minimal file at
    ``app/domain/identity/_bad_cross_boundary_test.py`` that imports
    from ``app.adapters.db.session`` — a violation of the "Domain
    forbids adapters (except ports)" contract. Exit code must be
    non-zero and the bad import must appear in stdout.
    """
    BAD_FILE.write_text(BAD_FILE_CONTENTS, encoding="utf-8")
    result = _run_lint_imports()
    assert result.returncode != 0, (
        "lint-imports accepted a domain -> adapters import. "
        "The boundary gate is not enforcing rule 1.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The import-linter report names the offending edge. Assert it
    # surfaced so a future config change that silently flips the
    # contract into "skip" mode still fails the test.
    combined = result.stdout + result.stderr
    assert "app.adapters.db.session" in combined, (
        "lint-imports exited non-zero but did not report the expected "
        f"offending edge.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
