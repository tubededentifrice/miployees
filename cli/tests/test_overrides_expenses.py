"""Unit tests for :mod:`crewday._overrides.expenses`.

Covers cd-qnz3 acceptance criteria for ``expenses submit``: happy
path with `--yes`, declined confirmation aborts cleanly, scan
failures propagate, the missing-receipt path is rejected at the
boundary, and the metadata stamp matches the parity-gate contract.
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
from crewday._overrides import expenses as expenses_override


def _no_sleep(_seconds: float) -> None:
    return None


def _wire_factory(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
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

    monkeypatch.setattr(expenses_override, "_client_factory_for", factory)
    return captured


def _ctx() -> CrewdayContext:
    return CrewdayContext(profile=None, workspace="smoke", output="json")


def _scan_payload() -> dict[str, object]:
    """Return a representative ExpenseScanResultPayload shape."""
    return {
        "vendor": {"value": "Best Coffee", "confidence": 0.94},
        "purchased_at": {"value": "2026-04-01T10:30:00+00:00", "confidence": 0.91},
        "currency": {"value": "EUR", "confidence": 0.99},
        "total_amount_cents": {"value": 450, "confidence": 0.93},
        "category": {"value": "meals", "confidence": 0.88},
        "note_md": {"value": "", "confidence": 0.0},
        "agent_question": None,
    }


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def receipt(tmp_path: pathlib.Path) -> pathlib.Path:
    """A small valid receipt-shaped file."""
    path = tmp_path / "receipt.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    return path


def test_submit_happy_path_with_yes(
    runner: CliRunner,
    receipt: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: scan → create → submit, all three calls landed."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/scan"):
            return httpx.Response(200, json=_scan_payload())
        if path.endswith("/expenses"):
            return httpx.Response(
                201,
                json={
                    "id": "claim-1",
                    "state": "draft",
                    "vendor": "Best Coffee",
                    "currency": "EUR",
                    "total_amount_cents": 450,
                },
            )
        # /expenses/{id}/submit
        return httpx.Response(
            200,
            json={
                "id": "claim-1",
                "state": "submitted",
                "vendor": "Best Coffee",
                "currency": "EUR",
                "total_amount_cents": 450,
            },
        )

    captured = _wire_factory(monkeypatch, handler)
    result = runner.invoke(
        expenses_override.submit,
        [
            str(receipt),
            "--work-engagement",
            "we-1",
            "--yes",
        ],
        obj=_ctx(),
    )
    assert result.exit_code == 0, result.output
    # 3 calls: scan, create, submit.
    assert len(captured) == 3
    assert captured[0].url.path.endswith("/expenses/scan")
    assert captured[1].url.path.endswith("/expenses")
    assert captured[2].url.path.endswith("/expenses/claim-1/submit")

    # Scan and submit are multipart / no-body respectively; the create
    # call carries the JSON-projected scan fields.
    create_body = json.loads(captured[1].content)
    assert create_body["work_engagement_id"] == "we-1"
    assert create_body["vendor"] == "Best Coffee"
    assert create_body["currency"] == "EUR"
    assert create_body["total_amount_cents"] == 450
    assert create_body["category"] == "meals"
    assert create_body["purchased_at"] == "2026-04-01T10:30:00+00:00"

    assert "Claim claim-1" in result.output
    assert "state=submitted" in result.output


def test_submit_category_override(
    runner: CliRunner,
    receipt: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--category` overrides the scanned category in the create body."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/scan"):
            return httpx.Response(200, json=_scan_payload())
        if path.endswith("/expenses"):
            return httpx.Response(201, json={"id": "claim-2"})
        return httpx.Response(200, json={"id": "claim-2", "state": "submitted"})

    captured = _wire_factory(monkeypatch, handler)
    result = runner.invoke(
        expenses_override.submit,
        [
            str(receipt),
            "--work-engagement",
            "we-1",
            "--category",
            "fuel",
            "--yes",
        ],
        obj=_ctx(),
    )
    assert result.exit_code == 0, result.output
    create_body = json.loads(captured[1].content)
    assert create_body["category"] == "fuel"


def test_submit_user_declines_confirmation(
    runner: CliRunner,
    receipt: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user typing 'n' at the prompt aborts before /expenses is touched."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scan"):
            return httpx.Response(200, json=_scan_payload())
        # Reaching here means the override called /expenses despite
        # the operator saying no.
        raise AssertionError(
            f"unexpected call after declined confirm: {request.url.path}"
        )

    captured = _wire_factory(monkeypatch, handler)
    result = runner.invoke(
        expenses_override.submit,
        [
            str(receipt),
            "--work-engagement",
            "we-1",
        ],
        obj=_ctx(),
        input="n\n",
    )
    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0].url.path.endswith("/scan")
    assert "Aborted" in result.output


def test_submit_scan_error_propagates(
    runner: CliRunner,
    receipt: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 on /scan surfaces as ApiError without calling /expenses.

    Scan POSTs now carry an Idempotency-Key (cd-1up1 selfreview), so the
    client retries the request through the full §13 budget before
    surfacing the failure — verify both the retry pattern and that no
    downstream /expenses call is reached.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scan"):
            return httpx.Response(
                503,
                json={
                    "type": "https://crewday.dev/errors/scan_not_configured",
                    "title": "Scan not configured",
                    "detail": "settings.llm_ocr_model is unset",
                },
            )
        raise AssertionError("create or submit reached on a failed scan")

    captured = _wire_factory(monkeypatch, handler)
    result = runner.invoke(
        expenses_override.submit,
        [str(receipt), "--work-engagement", "we-1", "--yes"],
        obj=_ctx(),
    )
    assert result.exit_code != 0
    # All captured calls hit /scan — no /expenses or /submit was made.
    # Three attempts is the §13 retry budget (one initial + two retries),
    # all safely re-issuable thanks to the Idempotency-Key.
    assert len(captured) == 3
    assert all(req.url.path.endswith("/scan") for req in captured)
    combined = (result.output or "") + (result.stderr or "")
    assert "scan_not_configured" in combined or "settings.llm_ocr_model" in combined


def test_submit_create_error_blocks_submit(
    runner: CliRunner,
    receipt: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 422 on /expenses surfaces and never reaches /submit."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/scan"):
            return httpx.Response(200, json=_scan_payload())
        if path.endswith("/expenses"):
            return httpx.Response(
                422,
                json={
                    "type": "https://crewday.dev/errors/validation",
                    "title": "Validation",
                    "detail": "currency must be 3 chars",
                },
            )
        raise AssertionError("/submit reached after failed /expenses")

    captured = _wire_factory(monkeypatch, handler)
    result = runner.invoke(
        expenses_override.submit,
        [str(receipt), "--work-engagement", "we-1", "--yes"],
        obj=_ctx(),
    )
    assert result.exit_code != 0
    # Scan + failed create.
    assert len(captured) == 2


def test_submit_missing_receipt_path(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nonexistent receipt path is rejected by Click before any HTTP."""

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP layer reached on missing receipt")

    _wire_factory(monkeypatch, handler)
    result = runner.invoke(
        expenses_override.submit,
        [
            "/nonexistent/receipt.jpg",
            "--work-engagement",
            "we-1",
            "--yes",
        ],
        obj=_ctx(),
    )
    assert result.exit_code != 0


def test_submit_requires_workspace(
    runner: CliRunner,
    receipt: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --workspace the override raises ConfigError (exit 5)."""

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP layer should not be reached without workspace")

    _wire_factory(monkeypatch, handler)
    no_workspace_ctx = CrewdayContext(profile=None, workspace=None, output="json")
    result = runner.invoke(
        expenses_override.submit,
        [str(receipt), "--work-engagement", "we-1", "--yes"],
        obj=no_workspace_ctx,
    )
    assert result.exit_code == ExitCode.CONFIG_ERROR


def test_submit_metadata_attached() -> None:
    """The decorator stamps the override metadata with the three covered ops."""
    metadata = getattr(expenses_override.submit, "_cli_override", None)
    assert metadata is not None
    group, verb, covers = metadata
    assert group == "expenses"
    assert verb == "submit"
    assert set(covers) == {
        "scan_expense_receipt",
        "create_expense_claim",
        "submit_expense_claim",
    }


def test_submit_help_renders(runner: CliRunner) -> None:
    """`expenses submit --help` should render without crashing."""
    result = runner.invoke(expenses_override.submit, ["--help"])
    assert result.exit_code == 0
    assert "RECEIPT_PATH" in result.output
    assert "--work-engagement" in result.output
    assert "--yes" in result.output


def test_register_attaches_under_existing_expenses_group() -> None:
    """`register(root)` adds `submit` under the `expenses` subgroup."""
    root = click.Group(name="root")
    expenses = click.Group(name="expenses")
    root.add_command(expenses)
    expenses_override.register(root)
    assert "submit" in expenses.commands


def test_register_creates_expenses_group_when_missing() -> None:
    """`register(root)` works even when codegen has not seeded `expenses`."""
    root = click.Group(name="root")
    expenses_override.register(root)
    assert "expenses" in root.commands
    expenses_group = root.commands["expenses"]
    assert isinstance(expenses_group, click.Group)
    assert "submit" in expenses_group.commands
