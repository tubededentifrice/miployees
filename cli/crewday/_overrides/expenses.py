"""Hand-written ``crewday expenses submit`` override.

Spec ``docs/specs/13-cli.md`` §"crewday expenses": ``submit`` is one
user-facing verb wrapping three HTTP calls — scan the receipt for an
LLM-suggested fill, ask the operator to confirm, then create + submit
the claim. The intermediate state lives only in the user's terminal,
which is exactly the gap the SPA's review screen plugs and which the
generated codegen path cannot model (every codegen verb is one HTTP
call).

Two notes on shape:

* The ``POST /expenses`` API today consumes JSON
  (:class:`app.domain.expenses.claims.ExpenseClaimCreate`); the spec
  table at ``docs/specs/12-rest-api.md`` line 1519 says "multipart
  for receipts" but that wording predates the split into the
  separate ``POST /uploads`` (TBD) and ``POST /expenses/{id}/
  attachments`` flow. We follow the live contract here so the
  override actually works against the running server; the
  ``attachments`` step is an out-of-scope follow-up tracked under
  the wider expenses CLI work (cd-fzyg). A spec-drift Beads task is
  filed if one does not already exist.
* The scan endpoint returns ``{value, confidence}`` cells; we project
  each to its plain value for the create payload but surface the
  confidence in the user-visible review table so the operator can
  spot a low-confidence cell before confirming.
"""

from __future__ import annotations

import json
import mimetypes
import pathlib
from typing import Any, Final

import click

from crewday._client import CrewdayClient
from crewday._globals import CrewdayContext
from crewday._main import ConfigError, CrewdayError
from crewday._overrides import cli_override

__all__ = ["register"]


# Receipt scan accepts JPEG / PNG / HEIC / PDF per the route's
# allow-list (see ``app.api.v1.expenses._SCAN_ALLOWED_MIME``); we
# fall back to ``image/jpeg`` for an unguessable extension because
# the most common ``--receipt`` source is a phone camera roll.
_SCAN_FALLBACK_CONTENT_TYPE: Final[str] = "image/jpeg"


def _guess_content_type(path: pathlib.Path) -> str:
    """Return a best-guess MIME for the receipt upload."""
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or _SCAN_FALLBACK_CONTENT_TYPE


def _extract_value(scan: Any, field: str) -> Any:
    """Pull the ``.value`` out of a scan-result ``{value, confidence}`` cell.

    Surfaces a clean error when the server returns a malformed cell —
    a low-confidence value is still a value (the operator confirms it
    on screen); a *missing* value is a server-side bug that we'd
    rather catch loudly than translate into a 422 from the create
    endpoint.
    """
    if not isinstance(scan, dict):
        raise CrewdayError(
            f"unexpected scan result shape: expected JSON object, "
            f"got {type(scan).__name__}"
        )
    cell = scan.get(field)
    if not isinstance(cell, dict) or "value" not in cell:
        raise CrewdayError(
            f"scan result is missing the {field!r} cell or its value; got {cell!r}"
        )
    return cell["value"]


def _format_confidence(scan: Any, field: str) -> str:
    """Render a ``{value, confidence}`` cell as ``value (NN%)``.

    Used only for the operator-facing review table; the create payload
    sees the raw value from :func:`_extract_value`.
    """
    if not isinstance(scan, dict):
        return "?"
    cell = scan.get(field)
    if not isinstance(cell, dict):
        return "?"
    value = cell.get("value", "?")
    conf = cell.get("confidence")
    if isinstance(conf, int | float):
        return f"{value} ({conf * 100:.0f}%)"
    return f"{value}"


def _render_scan_table(scan: dict[str, Any]) -> str:
    """Build a small multi-line summary of the scan result."""
    lines = [
        "Scan result:",
        f"  Vendor:          {_format_confidence(scan, 'vendor')}",
        f"  Amount (cents):  {_format_confidence(scan, 'total_amount_cents')}",
        f"  Currency:        {_format_confidence(scan, 'currency')}",
        f"  Purchased at:    {_format_confidence(scan, 'purchased_at')}",
        f"  Category:        {_format_confidence(scan, 'category')}",
    ]
    question = scan.get("agent_question")
    if isinstance(question, str) and question.strip():
        lines.append(f"  Question:        {question.strip()}")
    return "\n".join(lines)


def _scan_receipt(
    client: CrewdayClient,
    *,
    ctx: CrewdayContext,
    workspace_url_root: str,
    receipt_path: pathlib.Path,
) -> dict[str, Any]:
    """Call ``POST /expenses/scan`` and return the parsed result.

    Mints a fresh ``Idempotency-Key`` per call (§12 "Idempotency") so a
    transient transport error during the multipart upload is safely
    retried by the client. Without the key the retry loop declines
    POST retries (see :func:`crewday._client._should_retry`), and the
    operator would re-run the whole composite — paying the
    LLM-inference cost twice on what is already an expensive call.
    """
    contents = receipt_path.read_bytes()
    response = client.request(
        "POST",
        f"{workspace_url_root}/expenses/scan",
        files={
            "image": (
                receipt_path.name,
                contents,
                _guess_content_type(receipt_path),
            ),
        },
        idempotency_key=ctx.idempotency_key_factory(),
    )
    payload = response.json()
    if not isinstance(payload, dict):
        raise CrewdayError(
            f"unexpected scan response: expected JSON object, "
            f"got {type(payload).__name__}"
        )
    return payload


def _create_claim(
    client: CrewdayClient,
    *,
    ctx: CrewdayContext,
    workspace_url_root: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """POST a draft claim and return the JSON payload."""
    response = client.request(
        "POST",
        f"{workspace_url_root}/expenses",
        json=body,
        idempotency_key=ctx.idempotency_key_factory(),
    )
    payload = response.json()
    if not isinstance(payload, dict):
        raise CrewdayError(
            f"unexpected /expenses response: expected JSON object, "
            f"got {type(payload).__name__}"
        )
    return payload


def _submit_claim(
    client: CrewdayClient,
    *,
    ctx: CrewdayContext,
    workspace_url_root: str,
    claim_id: str,
) -> dict[str, Any]:
    """POST submit on a draft claim and return the JSON payload."""
    response = client.request(
        "POST",
        f"{workspace_url_root}/expenses/{claim_id}/submit",
        json={},
        idempotency_key=ctx.idempotency_key_factory(),
    )
    payload = response.json()
    if not isinstance(payload, dict):
        raise CrewdayError(
            f"unexpected /expenses/.../submit response: "
            f"expected JSON object, got {type(payload).__name__}"
        )
    return payload


@click.command(name="submit")
@click.argument(
    "receipt_path",
    metavar="RECEIPT_PATH",
    type=click.Path(
        exists=True,
        dir_okay=False,
        readable=True,
        path_type=pathlib.Path,
    ),
)
@click.option(
    "--work-engagement",
    "work_engagement_id",
    required=True,
    help=(
        "Work engagement id the claim is bound to (the worker's "
        "active engagement at the property). Required by the API."
    ),
)
@click.option(
    "--category",
    "category_override",
    default=None,
    help="Override the scanned category (e.g. 'meals', 'fuel').",
)
@click.option(
    "--task",
    "task_id",
    default=None,
    help="Optional task id to associate with the claim (forwarded as note).",
)
@click.option(
    "--yes",
    "skip_confirm",
    is_flag=True,
    default=False,
    help="Skip the interactive confirm prompt (use in scripts / agents).",
)
@click.pass_obj
def submit(
    ctx: CrewdayContext,
    *,
    receipt_path: pathlib.Path,
    work_engagement_id: str,
    category_override: str | None,
    task_id: str | None,
    skip_confirm: bool,
) -> None:
    """Scan a receipt and submit the resulting expense claim.

    Composite flow:

    1. ``POST /expenses/scan`` — multipart upload, returns the
       LLM-suggested fields (vendor / amount / currency / date /
       category) each with a confidence score.
    2. Print the suggestions; unless ``--yes`` is set, ask the user
       to confirm. ``--category`` overrides the scanned category.
    3. ``POST /expenses`` (JSON ``ExpenseClaimCreate``) to create the
       draft claim.
    4. ``POST /expenses/{id}/submit`` to transition draft → submitted.

    Output is one summary line — claim id, amount + currency, vendor,
    new state — followed by the JSON body for downstream tooling.
    """
    if ctx.workspace is None or not ctx.workspace:
        raise ConfigError(
            "this command targets /w/<slug>/... but no workspace is set "
            "(pass --workspace or set CREWDAY_WORKSPACE)."
        )

    workspace_url_root = f"/w/{ctx.workspace}/api/v1"
    with _client_factory_for(ctx) as client:
        scan = _scan_receipt(
            client,
            ctx=ctx,
            workspace_url_root=workspace_url_root,
            receipt_path=receipt_path,
        )

        click.echo(_render_scan_table(scan))

        if not skip_confirm and not click.confirm(
            "Create + submit this claim?",
            default=True,
        ):
            click.echo("Aborted; no claim was created.")
            return

        scanned_category = _extract_value(scan, "category")
        category = category_override or scanned_category
        note = f"Linked task: {task_id}" if task_id else ""
        claim_body: dict[str, Any] = {
            "work_engagement_id": work_engagement_id,
            "vendor": _extract_value(scan, "vendor"),
            "purchased_at": _extract_value(scan, "purchased_at"),
            "currency": _extract_value(scan, "currency"),
            "total_amount_cents": _extract_value(scan, "total_amount_cents"),
            "category": category,
            "note_md": note,
        }

        created = _create_claim(
            client,
            ctx=ctx,
            workspace_url_root=workspace_url_root,
            body=claim_body,
        )
        claim_id = created.get("id")
        if not isinstance(claim_id, str) or not claim_id:
            raise CrewdayError(f"server did not return a claim id: {created!r}")

        submitted = _submit_claim(
            client,
            ctx=ctx,
            workspace_url_root=workspace_url_root,
            claim_id=claim_id,
        )

    state = submitted.get("state", "?")
    amount = submitted.get("total_amount_cents", "?")
    currency = submitted.get("currency", "?")
    vendor = submitted.get("vendor", "?")
    click.echo(f"Claim {claim_id} ({vendor}): {amount} {currency} → state={state}")
    click.echo(json.dumps(submitted, indent=2, sort_keys=False, default=str))


def _client_factory_for(ctx: CrewdayContext) -> CrewdayClient:
    """Return a configured :class:`CrewdayClient` for the active context.

    Same indirection seam as :mod:`crewday._overrides.tasks`. Tests
    patch this symbol with a closure that returns a
    :class:`CrewdayClient` wired to :class:`httpx.MockTransport`.
    """
    raise ConfigError(
        "profile resolution is not yet implemented; the test suite patches "
        "crewday._overrides.expenses._client_factory_for. Production wiring "
        "lands with Beads cd-cksj."
    )


# Stamp the override metadata. The composite covers three operationIds
# from the OpenAPI surface; the parity gate uses these to mark them
# off the "must have a CLI command" list.
submit = cli_override(
    "expenses",
    "submit",
    covers=[
        "scan_expense_receipt",
        "create_expense_claim",
        "submit_expense_claim",
    ],
)(submit)


def register(root: click.Group) -> None:
    """Attach ``expenses submit`` under the existing ``expenses`` group."""
    expenses_group = root.commands.get("expenses")
    if expenses_group is None:
        expenses_group = click.Group(name="expenses", help="expenses commands")
        root.add_command(expenses_group)
    if not isinstance(expenses_group, click.Group):
        raise RuntimeError(
            "expected 'expenses' to be a click.Group; cannot attach 'submit' "
            "override to a leaf command."
        )
    expenses_group.add_command(submit)
