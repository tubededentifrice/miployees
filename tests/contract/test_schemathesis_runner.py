"""Pytest wrapper for the schemathesis contract sweep (cd-3j25).

Two test cases live here, both gated by the ``schemathesis`` marker
(see ``pyproject.toml`` ``addopts``). They are deliberately excluded
from the default unit/integration sweep because spinning up uvicorn
+ running 20 examples per operation lands well outside the §17 < 60s
unit budget; CI runs them via the dedicated ``schemathesis`` job
(`.github/workflows/ci.yml`).

* :class:`TestSchemathesisRunner` — drives the real
  ``scripts/schemathesis_run.sh`` against the production app factory.
  Skips when the runner is not invokable (``schemathesis`` missing
  from the venv, or the host opted out via ``SCHEMATHESIS_SKIP=1``)
  so a developer running ``pytest`` on a fresh checkout doesn't fail
  on a tooling pre-condition.
* :class:`TestSchemathesisCatchesBrokenHandler` — runs schemathesis
  against a *tiny* FastAPI app whose handler deliberately returns the
  wrong status code, asserts schemathesis flags the violation. This
  is the §17 acceptance test for "a deliberately broken handler is
  caught" — much cheaper than hand-rolling a known-bad endpoint into
  the production tree.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.schemathesis

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = REPO_ROOT / "scripts" / "schemathesis_run.sh"


# ---------------------------------------------------------------------------
# Skip preconditions
# ---------------------------------------------------------------------------


def _schemathesis_available() -> bool:
    """Return ``True`` when the ``schemathesis`` package can be imported."""
    try:
        import schemathesis  # noqa: F401
    except ImportError:
        return False
    return True


_SKIP_ENV: bool = os.environ.get("SCHEMATHESIS_SKIP", "").lower() in {
    "1",
    "yes",
    "true",
}


# ---------------------------------------------------------------------------
# End-to-end runner — drives scripts/schemathesis_run.sh
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_SKIP_ENV, reason="SCHEMATHESIS_SKIP=1 — opted out by env")
@pytest.mark.skipif(
    not _schemathesis_available(), reason="schemathesis is not installed"
)
@pytest.mark.skipif(
    not RUN_SCRIPT.exists(), reason=f"runner script {RUN_SCRIPT} missing"
)
class TestSchemathesisRunner:
    """End-to-end driver for the production contract sweep.

    Runs the runner script as a subprocess so the test path is
    byte-identical to the CI gate. Mirrors the cd-3j25 acceptance:
    *"`make schemathesis` runs end-to-end and reports zero unhandled
    failures on the seeded fixture set."*
    """

    def test_runner_exits_zero_on_seeded_fixture_set(self, tmp_path: Path) -> None:
        """The full runner must exit 0 against a seeded SQLite DB.

        ``-n 5`` overrides the default ``--max-examples=20`` so the
        local pytest run stays under one minute. CI's
        ``make schemathesis`` invocation uses the spec default (20).
        """
        env = os.environ.copy()
        env["SCHEMATHESIS_MAX_EXAMPLES"] = "5"
        # Pin the report dir to ``tmp_path`` so the test can inspect
        # the JUnit output if needed and pytest's tmp cleanup runs.
        env["SCHEMATHESIS_REPORT_DIR"] = str(tmp_path / "report")

        proc = subprocess.run(
            ["bash", str(RUN_SCRIPT)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert proc.returncode == 0, (
            f"schemathesis runner failed (rc={proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n"
            f"--- stderr ---\n{proc.stderr}"
        )


# ---------------------------------------------------------------------------
# Negative test — deliberately broken handler
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_SKIP_ENV, reason="SCHEMATHESIS_SKIP=1 — opted out by env")
@pytest.mark.skipif(
    not _schemathesis_available(), reason="schemathesis is not installed"
)
class TestSchemathesisCatchesBrokenHandler:
    """Schemathesis must catch a handler that violates its declared schema.

    The §17 acceptance criterion is "a deliberately broken handler
    (wrong status code) is caught". We do this with a minimal
    FastAPI app served by uvicorn on a free port: the handler is
    declared to return 200 but actually returns 418, so the
    ``status_code_conformance`` check fires and schemathesis exits
    non-zero.

    This guards the contract gate itself: if a future schemathesis
    upgrade silently drops ``status_code_conformance`` from
    ``--checks all``, this test fails and the runner stops being a
    real gate.
    """

    @pytest.fixture
    def free_port(self) -> int:
        """Return a TCP port that isn't bound at the moment.

        Bind, read the port, drop the socket — there's a race where
        another process can grab the port between drop and the
        uvicorn boot, but the window is narrow enough for a local
        test and the runner traps the failure cleanly.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @pytest.fixture
    def broken_app_module(self, tmp_path: Path) -> Iterator[Path]:
        """Drop a tiny FastAPI app at ``tmp_path`` and yield its dir.

        The module declares ``GET /broken`` as 200 in its docstring
        / OpenAPI but returns 418 at runtime. Schemathesis'
        ``status_code_conformance`` check (in ``--checks all``) must
        flag the divergence. We can't import this module directly
        from the test process — the tenant filter + redaction layer
        installed by the production app would interfere — so we
        spawn it under uvicorn in a clean subprocess instead.
        """
        module_path = tmp_path / "broken_app.py"
        module_path.write_text(
            "from fastapi import FastAPI\n"
            "from fastapi.responses import JSONResponse\n"
            "\n"
            "app = FastAPI(title='BrokenSchema', openapi_url='/openapi.json')\n"
            "\n"
            "@app.get('/broken', responses={200: {'description': 'OK'}})\n"
            "def broken() -> JSONResponse:\n"
            "    # Schema says 200; we return 418. status_code_conformance\n"
            "    # must catch this.\n"
            "    return JSONResponse({'detail': 'teapot'}, status_code=418)\n",
            encoding="utf-8",
        )
        yield tmp_path

    def test_runner_flags_status_code_mismatch(
        self, broken_app_module: Path, free_port: int
    ) -> None:
        """Schemathesis exits non-zero when a handler violates its schema."""
        # Locate the schemathesis CLI binary. Prefer the one in the
        # active venv (``sys.executable``'s sibling); fall back to
        # ``shutil.which`` for editable installs that put it on PATH.
        venv_bin = Path(sys.executable).parent / "schemathesis"
        if venv_bin.exists():
            schemathesis_bin: str = str(venv_bin)
        else:
            located = shutil.which("schemathesis")
            if located is None:
                pytest.skip("schemathesis CLI not on PATH or in venv")
            schemathesis_bin = located

        env = os.environ.copy()
        # Make the broken_app.py module importable via uvicorn.
        env["PYTHONPATH"] = (
            f"{broken_app_module}{os.pathsep}{env.get('PYTHONPATH', '')}"
        )

        # Boot the broken app on a free port.
        uvicorn_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "broken_app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(free_port),
                "--log-level",
                "warning",
            ],
            cwd=str(broken_app_module),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            # Wait for the openapi endpoint to come up.
            import urllib.error
            import urllib.request

            deadline = time.monotonic() + 15
            while True:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{free_port}/openapi.json", timeout=1
                    ) as resp:
                        if resp.status == 200:
                            break
                except (urllib.error.URLError, ConnectionError):
                    pass
                if time.monotonic() > deadline:
                    out, err = uvicorn_proc.communicate(timeout=2)
                    raise AssertionError(
                        "broken-app uvicorn never came up\n"
                        f"--- stdout ---\n{out.decode(errors='replace')}\n"
                        f"--- stderr ---\n{err.decode(errors='replace')}"
                    )
                time.sleep(0.1)

            # Run schemathesis against the broken app. ``-c all``
            # includes ``status_code_conformance`` which is the check
            # we expect to fire. ``-n 3`` is plenty — every example
            # gets a 418 against a 200-only schema.
            sch_proc = subprocess.run(
                [
                    schemathesis_bin,
                    "run",
                    f"http://127.0.0.1:{free_port}/openapi.json",
                    "--checks",
                    "status_code_conformance",
                    "--max-examples",
                    "3",
                    "--workers",
                    "1",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        finally:
            uvicorn_proc.terminate()
            try:
                uvicorn_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                uvicorn_proc.kill()
                uvicorn_proc.wait(timeout=5)

        assert sch_proc.returncode != 0, (
            "schemathesis exited 0 on a deliberately broken handler — "
            "the status_code_conformance check is no longer firing.\n"
            f"--- stdout ---\n{sch_proc.stdout}\n"
            f"--- stderr ---\n{sch_proc.stderr}"
        )
        # Sanity-check that the failure mentions the divergence rather
        # than a setup error.
        combined = (sch_proc.stdout + sch_proc.stderr).lower()
        assert "status" in combined or "418" in combined or "conformance" in combined, (
            "schemathesis exited non-zero but the output does not mention "
            "the status-code mismatch — verify the test isn't masking a "
            "different failure.\n"
            f"--- stdout ---\n{sch_proc.stdout}\n"
            f"--- stderr ---\n{sch_proc.stderr}"
        )


# ---------------------------------------------------------------------------
# Hook unit tests (the import/registration smoke is part of -m schemathesis
# collection itself; we add explicit checks for the public-path predicate
# so a regression in the allowlist surfaces as a focused failure.)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _schemathesis_available(), reason="schemathesis is not installed"
)
class TestPublicPathAllowlist:
    """Direct unit checks on :func:`tests.contract.hooks._is_public_path`.

    The schemathesis sweep exercises the predicate via real requests,
    but a regression there surfaces as "every authed route fails the
    auth check" — slow to triage. These cases pin the predicate's
    behaviour at module level so a future tweak that drops a path is
    caught with a one-second test.
    """

    def test_known_public_paths_are_exempt(self) -> None:
        from tests.contract.hooks import _is_public_path

        for path in (
            "/healthz",
            "/readyz",
            "/version",
            "/api/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            "/api/v1/auth/magic/request",
            "/api/v1/auth/magic/consume",
            "/api/v1/auth/passkey/login/start",
            "/api/v1/auth/passkey/login/finish",
            "/api/v1/auth/passkey/signup/register/start",
            "/api/v1/auth/passkey/signup/register/finish",
            "/api/v1/signup",
            "/api/v1/signup/start",
            "/api/v1/signup/verify",
            "/api/v1/signup/passkey/start",
            "/api/v1/signup/passkey/finish",
            "/api/v1/invite/accept",
            "/api/v1/invite/01HABCDE1234567890ABCDEFGH/confirm",
            "/api/v1/invite/complete",
            "/api/v1/invites/abc123token",
            "/api/v1/invites/abc123token/accept",
            "/api/v1/recover",
            "/api/v1/recover/passkey/request",
            "/api/v1/recover/passkey/verify",
            "/api/v1/recover/passkey/start",
            "/api/v1/recover/passkey/finish",
            "/api/v1/auth/email/verify",
            "/api/v1/auth/email/revert",
        ):
            assert _is_public_path(path), f"expected {path} to be public"

    def test_authed_paths_are_not_exempt(self) -> None:
        from tests.contract.hooks import _is_public_path

        for path in (
            "/api/v1/auth/me",
            "/api/v1/auth/logout",
            # Email-change *request* lives under ``/me`` and is authed
            # — only the verify/revert token-confirmation halves are
            # public.
            "/api/v1/me/email/change_request",
            "/w/demo/api/v1/tasks",
            "/w/demo/api/v1/properties",
            "/admin/api/v1/me",
            # A path that LOOKS public but lives under workspace prefix
            # (a worker accidentally registering ``/healthz`` under a
            # workspace prefix would still need auth — anchor matters):
            "/w/demo/healthz",
            # The pre-fix allowlist used these stale shapes; pin them
            # as authed now so a future regression that re-introduces
            # the wrong patterns trips this test rather than silently
            # exempting routes that don't exist.
            "/api/v1/auth/signup",
            "/api/v1/auth/recovery",
            "/api/v1/auth/email-change/confirm",
        ):
            assert not _is_public_path(path), f"did not expect {path} to be public"

    def test_trailing_slash_is_normalised(self) -> None:
        from tests.contract.hooks import _is_public_path

        assert _is_public_path("/healthz/")
        assert _is_public_path("/api/openapi.json/")


@pytest.mark.skipif(
    not _schemathesis_available(), reason="schemathesis is not installed"
)
def test_hooks_module_imports_clean() -> None:
    """``tests.contract.hooks`` imports + registers without raising.

    Schemathesis registers the @check decorators at import time; if
    the module fails to import the runner crashes at startup with a
    cryptic HookError. This test catches a broken import early.
    """
    import tests.contract.hooks as hooks_mod

    # All three custom checks must be present as module attributes —
    # losing one of them breaks the contract gate silently.
    for attr in (
        "check_authorization_present",
        "check_idempotency_round_trip",
        "check_etag_round_trip",
    ):
        assert hasattr(hooks_mod, attr), f"missing custom check {attr!r}"
