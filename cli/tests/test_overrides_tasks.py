"""Unit tests for :mod:`crewday._overrides.tasks`.

Covers cd-qnz3 acceptance criteria for ``tasks complete``: happy
path with multiple evidence files, partial-failure atomicity (one
upload fails → no ``/complete`` call), the no-evidence branch
(``/complete`` called immediately), the ``--note`` body wiring, and
the workspace-required guard.
"""

from __future__ import annotations

import json
import pathlib
import random
from collections.abc import Callable

import click
import httpx
import pytest
from click.testing import CliRunner
from crewday._client import CrewdayClient
from crewday._globals import CrewdayContext
from crewday._main import ExitCode
from crewday._overrides import tasks as tasks_override


def _no_sleep(_seconds: float) -> None:
    return None


def _wire_factory(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Patch the override's client factory to use a mock transport.

    Returns a list captured by the handler so each test can assert on
    the exact request shape (URL, headers, body) the override sent.
    """
    captured: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    def factory(_ctx: CrewdayContext) -> CrewdayClient:
        return CrewdayClient(
            base_url="https://api.test.local",
            token="test-token",
            workspace="smoke",
            transport=httpx.MockTransport(recording),
            rng=random.Random(0),
            sleep=_no_sleep,
        )

    monkeypatch.setattr(tasks_override, "_client_factory_for", factory)
    return captured


def _ctx() -> CrewdayContext:
    return CrewdayContext(profile=None, workspace="smoke", output="json")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_complete_no_evidence_calls_complete_immediately(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --evidence → just GET /tasks/{id} then POST /complete."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": "task-1", "state": "pending"},
            )
        # POST /complete
        return httpx.Response(
            200,
            json={"id": "task-1", "state": "done"},
        )

    captured = _wire_factory(monkeypatch, handler)

    result = runner.invoke(
        tasks_override.complete,
        ["task-1"],
        obj=_ctx(),
    )
    assert result.exit_code == 0, result.output
    # Two calls: GET sanity check + POST /complete.
    assert len(captured) == 2
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/w/smoke/api/v1/tasks/task-1"
    assert captured[1].method == "POST"
    assert captured[1].url.path == "/w/smoke/api/v1/tasks/task-1/complete"

    body = json.loads(captured[1].content)
    assert body == {"photo_evidence_ids": []}

    assert "Task task-1" in result.output
    assert "state=done" in result.output
    assert "evidence uploaded: 0" in result.output


def test_complete_uploads_evidence_then_completes(
    runner: CliRunner,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each --evidence path uploads first; IDs flow into /complete."""
    photo_a = tmp_path / "a.jpg"
    photo_a.write_bytes(b"binary-a")
    photo_b = tmp_path / "b.png"
    photo_b.write_bytes(b"binary-b")

    evidence_counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "task-2", "state": "pending"})
        if request.url.path.endswith("/evidence"):
            evidence_counter["n"] += 1
            return httpx.Response(
                201,
                json={
                    "id": f"ev-{evidence_counter['n']}",
                    "workspace_id": "ws",
                    "occurrence_id": "task-2",
                    "kind": "photo",
                    "blob_hash": "abc",
                    "note_md": None,
                    "created_at": "2026-04-01T00:00:00Z",
                    "created_by_user_id": "u-1",
                },
            )
        # /complete
        return httpx.Response(200, json={"id": "task-2", "state": "done"})

    captured = _wire_factory(monkeypatch, handler)

    result = runner.invoke(
        tasks_override.complete,
        [
            "task-2",
            "--evidence",
            str(photo_a),
            "--evidence",
            str(photo_b),
        ],
        obj=_ctx(),
    )
    assert result.exit_code == 0, result.output
    # GET + 2 evidence uploads + complete = 4 calls.
    assert len(captured) == 4
    assert captured[0].method == "GET"
    assert captured[1].method == "POST"
    assert captured[1].url.path.endswith("/evidence")
    assert captured[2].method == "POST"
    assert captured[2].url.path.endswith("/evidence")
    assert captured[3].method == "POST"
    assert captured[3].url.path.endswith("/complete")

    body = json.loads(captured[3].content)
    assert body["photo_evidence_ids"] == ["ev-1", "ev-2"]

    assert "evidence uploaded: 2" in result.output


def test_complete_note_flows_into_body(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--note` is forwarded as `note_md` in the /complete body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "task-3", "state": "pending"})
        return httpx.Response(200, json={"id": "task-3", "state": "done"})

    captured = _wire_factory(monkeypatch, handler)

    result = runner.invoke(
        tasks_override.complete,
        ["task-3", "--note", "all clean"],
        obj=_ctx(),
    )
    assert result.exit_code == 0, result.output
    body = json.loads(captured[-1].content)
    assert body == {"photo_evidence_ids": [], "note_md": "all clean"}


def test_complete_evidence_upload_failure_blocks_complete(
    runner: CliRunner,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx on /evidence stops the loop and never calls /complete."""
    photo = tmp_path / "x.jpg"
    photo.write_bytes(b"binary")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": "task-4", "state": "pending"})
        if request.url.path.endswith("/evidence"):
            return httpx.Response(
                422,
                json={
                    "type": "https://crewday.dev/errors/evidence_too_large",
                    "title": "Evidence too large",
                    "detail": "test rejection",
                },
            )
        # Should never be reached.
        raise AssertionError("/complete called despite failed evidence upload")

    captured = _wire_factory(monkeypatch, handler)

    result = runner.invoke(
        tasks_override.complete,
        ["task-4", "--evidence", str(photo)],
        obj=_ctx(),
    )
    assert result.exit_code != 0
    # GET + the failed POST /evidence; no /complete.
    assert len(captured) == 2
    assert captured[1].url.path.endswith("/evidence")


def test_complete_get_404_aborts(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 on the sanity GET aborts before any side effect."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "type": "https://crewday.dev/errors/not_found",
                "title": "Not Found",
                "detail": "no such task",
            },
        )

    captured = _wire_factory(monkeypatch, handler)
    result = runner.invoke(tasks_override.complete, ["missing"], obj=_ctx())
    assert result.exit_code != 0
    assert len(captured) == 1


def test_complete_requires_workspace(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --workspace the override raises ConfigError (exit 5)."""

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP layer should not be reached without workspace")

    _wire_factory(monkeypatch, handler)
    no_workspace_ctx = CrewdayContext(profile=None, workspace=None, output="json")

    result = runner.invoke(
        tasks_override.complete,
        ["task-x"],
        obj=no_workspace_ctx,
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR


def test_complete_metadata_attached() -> None:
    """The decorator stamps the override metadata for the parity gate."""
    metadata = getattr(tasks_override.complete, "_cli_override", None)
    assert metadata is not None
    group, verb, covers = metadata
    assert group == "tasks"
    assert verb == "complete"
    assert covers == ("upload_task_evidence", "complete_task")


def test_complete_help_renders(runner: CliRunner) -> None:
    """`tasks complete --help` should render without crashing."""
    result = runner.invoke(tasks_override.complete, ["--help"])
    assert result.exit_code == 0
    assert "--note" in result.output
    assert "--evidence" in result.output


def test_register_attaches_under_existing_tasks_group() -> None:
    """`register(root)` adds `complete` under the `tasks` subgroup."""
    root = click.Group(name="root")
    tasks = click.Group(name="tasks")
    root.add_command(tasks)

    tasks_override.register(root)
    assert "complete" in tasks.commands


def test_register_creates_tasks_group_when_missing() -> None:
    """`register(root)` works even when codegen has not seeded `tasks`."""
    root = click.Group(name="root")
    tasks_override.register(root)
    assert "tasks" in root.commands
    tasks_group = root.commands["tasks"]
    assert isinstance(tasks_group, click.Group)
    assert "complete" in tasks_group.commands


def test_evidence_path_must_exist(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Click's `exists=True` rejects a missing --evidence path before HTTP."""

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP layer should not be reached on missing file")

    _wire_factory(monkeypatch, handler)
    result = runner.invoke(
        tasks_override.complete,
        ["task-z", "--evidence", "/nonexistent/path.jpg"],
        obj=_ctx(),
    )
    # Click usage error → exit 2.
    assert result.exit_code != 0
    combined = (result.output or "") + (result.stderr or "")
    assert "exist" in combined.lower() or "invalid" in combined.lower()
