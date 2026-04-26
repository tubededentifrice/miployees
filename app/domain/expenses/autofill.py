"""Receipt OCR / autofill pipeline for expense claims (cd-95zb).

The :func:`run_extraction` service runs an attached receipt blob
through the LLM port (vision OCR + structured-JSON parse), validates
the result against :class:`ReceiptExtraction`, persists the parsed
payload + per-field confidence on the claim row, and — when the
overall confidence clears the autofill threshold AND the claim is a
fresh draft with no prior autofill run — fills the claim's
worker-typed fields. Every call writes one
:class:`~app.adapters.db.audit.models.AuditLog` row
(``receipt.ocr_completed`` on success, ``receipt.ocr_failed`` on every
failure mode) and one :class:`~app.adapters.db.llm.models.LlmUsage`
row keyed under capability ``"expenses.autofill"``.

Public surface:

* :class:`ReceiptExtraction` — the shape the LLM returns. Validated
  with pydantic v2; the per-field confidence map narrows to the
  five fields the prompt extracts (``vendor``, ``amount``,
  ``currency``, ``purchased_at``, ``category``).
* :func:`overall_confidence` — derived ``min()`` of the per-field
  confidence map, rounded to 2 decimals so it fits the DB's
  ``Numeric(3, 2)`` column without precision loss.
* :func:`extract_from_bytes` — pure helper: image bytes in, parsed
  :class:`ReceiptExtraction` out. No DB. Used by the persist path
  and by the ``POST /expenses/scan`` preview endpoint that runs
  extraction without creating a claim.
* :func:`run_extraction` — full persist path. Loads the claim +
  attachment, calls :func:`extract_from_bytes`, writes the autofill
  fields (when eligible), audits the outcome, and records the LLM
  usage row.
* :func:`run_receipt_ocr` is exported by :mod:`app.worker.tasks.receipt_ocr`
  as the thin wrapper the :func:`~app.domain.expenses.claims.attach_receipt`
  hook invokes.

Error taxonomy (every error inherits a stdlib parent so the router's
generic error map already routes them):

* :class:`ClaimNotFound` (``LookupError``) — claim not in tenant. 404.
* :class:`AttachmentNotFound` (``LookupError``) — attachment not in
  tenant or not on this claim. 404.
* :class:`ExtractionParseError` (``ValueError``) — LLM body did not
  validate against :class:`ReceiptExtraction` (malformed JSON,
  missing keys, naive datetime, unknown currency). 422.
* :class:`ExtractionTimeout` (``TimeoutError``) — provider deadline
  hit. 504.
* :class:`ExtractionRateLimited` (``RuntimeError``) — provider
  rate-limited the call after the adapter's retry budget. 503 with
  the retry-after hint left to the API layer.
* :class:`ExtractionProviderError` (``RuntimeError``) — non-retryable
  provider error (4xx other than 429, malformed body, etc.). 503.

Autofill rule. The first attachment on a fresh draft claim runs the
extraction and — if ``overall_confidence > AUTOFILL_CONFIDENCE_THRESHOLD``
— fills ``vendor`` / ``purchased_at`` / ``currency`` /
``total_amount_cents`` / ``category``. Subsequent attachments on the
same claim still run the extraction, persist a fresh
``llm_autofill_json`` snapshot, and emit the audit row, but do NOT
touch the claim's scalar fields — the worker has already had one shot
at autofill, and a second-attachment overwrite would clobber whatever
they typed afterwards. The "first run vs. follow-up" distinction is
detected by checking whether ``llm_autofill_json IS NULL`` on the
claim BEFORE this run (NULL = "no autofill ever attempted"); after
the first persist any subsequent run sees a non-null value and skips
the field rewrite.

PII contract. The LLM prompt carries only the receipt image bytes and
the optional ``hint_currency`` / ``hint_vendor`` strings the caller
explicitly opts into. No workspace slug, user name, engagement id, or
other tenant identifier is ever forwarded. The audit row's
``after`` diff funnels through :func:`app.audit.write_audit`'s
:func:`~app.util.redact.redact` seam so an accidental PII leak in the
LLM's free-text fields (vendor name, note) is scrubbed before
landing on disk.

Worker-queue note. The v1 deliverable runs the extraction
synchronously in the same transaction as the attach (the wrapper in
:mod:`app.worker.tasks.receipt_ocr` is a single-call shim for the
future async queue, tracked separately). A cd-95zb follow-up will
move the extraction off the request thread once the worker-queue
scaffolding lands; the call site in
:func:`~app.domain.expenses.claims.attach_receipt` already passes the
runner via dependency injection so the swap is a one-line change at
the wiring layer.

See ``docs/specs/09-time-payroll-expenses.md`` §"Submission flow
(worker)", §"LLM accuracy & guardrails";
``docs/specs/11-llm-and-agents.md`` §"Capabilities", §"Redaction
layer"; ``docs/specs/02-domain-model.md`` §"expense_claim".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.expenses.models import ExpenseAttachment, ExpenseClaim
from app.adapters.db.llm.models import LlmUsage as LlmUsageRow
from app.adapters.llm.ports import LLMClient, LLMResponse
from app.adapters.storage.ports import BlobNotFound, Storage
from app.audit import write_audit
from app.config import Settings, get_settings
from app.domain.expenses.claims import _validate_currency
from app.tenancy import WorkspaceContext
from app.util.clock import Clock, SystemClock
from app.util.currency import ISO_4217_ALLOWLIST
from app.util.ulid import new_ulid

__all__ = [
    "AUTOFILL_CAPABILITY",
    "AUTOFILL_CONFIDENCE_THRESHOLD",
    "AttachmentNotFound",
    "ClaimNotFound",
    "ExtractionMetrics",
    "ExtractionParseError",
    "ExtractionProviderError",
    "ExtractionRateLimited",
    "ExtractionResult",
    "ExtractionTimeout",
    "ReceiptCategory",
    "ReceiptExtraction",
    "extract_from_bytes",
    "overall_confidence",
    "run_extraction",
]

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Threshold above which a fresh draft's fields are autofilled. Below
# the threshold the LLM payload + per-field score still land on the
# claim (so the worker UI can show the suggestions), but the scalar
# fields are left for the worker to type / accept manually. The
# threshold is conservative: 0.85 means "every per-field score ≥
# 0.85" since :func:`overall_confidence` returns the ``min()`` over
# the map.
AUTOFILL_CONFIDENCE_THRESHOLD: Final[Decimal] = Decimal("0.85")

# §11 capability key for the LLM-usage ledger row.
AUTOFILL_CAPABILITY: Final[str] = "expenses.autofill"

# Five required confidence keys — one per autofilled field.
_REQUIRED_CONFIDENCE_KEYS: Final[frozenset[str]] = frozenset(
    {"vendor", "amount", "currency", "purchased_at", "category"}
)

# Max length on the structured ``vendor`` echo back from the LLM.
# Mirrors :data:`app.domain.expenses.claims._MAX_VENDOR_LEN` so a
# rogue model that emits a 10 KB block can't exhaust DB / audit
# budget on its own.
_MAX_VENDOR_LEN: Final[int] = 200

# Default prompt the v1 deliverable ships. Real prompt tuning is
# cd-e626 (the receipt-OCR specialisation task); v1 ships a sensible
# single-shot prompt that asks for a JSON object with the five
# fields and a per-field confidence map. The prompt deliberately
# carries NO tenant identifiers — only the OCR text the previous
# step extracted from the image bytes. See module docstring §"PII
# contract".
_OCR_TO_JSON_PROMPT: Final[str] = (
    "You are a receipt-extraction tool. Given the OCR text of a "
    "receipt image, return a JSON object with these keys: "
    "vendor (string, store / merchant name), "
    "amount (string, total paid as a decimal number, no currency symbol), "
    "currency (string, 3-letter ISO 4217 code, uppercase), "
    "purchased_at (string, ISO 8601 timestamp with timezone offset), "
    'category (one of "supplies", "fuel", "food", "transport", '
    '"maintenance", "other"), '
    "confidence (object mapping each of vendor / amount / currency / "
    "purchased_at / category to a float between 0 and 1). "
    "Return ONLY the JSON object — no commentary, no markdown fences."
)

# Cents conversion: most currencies use 2-decimal minor units. The
# 3-decimal currencies (BHD / JOD / KWD / OMR / TND) are handled
# explicitly below so the integer-cents conversion stays exact.
_THREE_DECIMAL_CURRENCIES: Final[frozenset[str]] = frozenset(
    {"BHD", "JOD", "KWD", "OMR", "TND"}
)

# Some currencies have ZERO decimals (JPY, KRW, VND, IDR). The
# allow-list mirrors :data:`app.util.currency.ISO_4217_ALLOWLIST`'s
# coverage. A receipt amount of "1500" in JPY is 1500 minor units,
# not 150000 cents — getting this wrong silently overstates the
# refund by 100x.
_ZERO_DECIMAL_CURRENCIES: Final[frozenset[str]] = frozenset(
    {"JPY", "KRW", "VND", "IDR", "CLP"}
)


ReceiptCategory = Literal[
    "supplies", "fuel", "food", "transport", "maintenance", "other"
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ClaimNotFound(LookupError):
    """The requested claim does not exist in the caller's workspace.

    404-equivalent. Mirrors
    :class:`app.domain.expenses.claims.ClaimNotFound` semantics —
    cross-tenant probes collapse to this same error so the router
    doesn't leak claim presence.
    """


class AttachmentNotFound(LookupError):
    """The attachment does not exist (or does not belong to ``claim_id``).

    404-equivalent. Cross-tenant + wrong-claim probes collapse here.
    """


class ExtractionParseError(ValueError):
    """The LLM body did not parse / validate.

    422-equivalent. Triggered by malformed JSON, missing required
    keys, an out-of-set category, a naive ``purchased_at``, an
    unknown currency, a non-numeric amount, or a confidence value
    outside ``[0, 1]``.

    When the chat call itself returned a body but the parse failed
    AFTER the provider charged tokens, :func:`extract_from_bytes`
    attaches a :class:`ExtractionMetrics` snapshot via
    :attr:`burnt_metrics` so the persist path can record the real
    token counts on the failure-mode :class:`LlmUsageRow`. The
    attribute is ``None`` when the chat itself never landed (a
    :class:`pydantic.ValidationError` raised before any tokens were
    spent goes through the same exception type).
    """

    burnt_metrics: ExtractionMetrics | None

    def __init__(
        self, *args: object, burnt_metrics: ExtractionMetrics | None = None
    ) -> None:
        super().__init__(*args)
        self.burnt_metrics = burnt_metrics


class ExtractionTimeout(TimeoutError):
    """The LLM call exceeded its deadline before returning a body.

    504-equivalent. Adapter-level
    :class:`httpx.TimeoutException`-backed failures bubble up through
    :class:`app.adapters.llm.openrouter.LlmTransportError` with a
    timeout signature; this error narrows that case so the router can
    serve a distinct envelope.
    """


class ExtractionRateLimited(RuntimeError):
    """The provider rate-limited the call past the adapter's retry budget.

    503-equivalent. The router can surface a retry-after hint based
    on the §11 fallback chain; the v1 deliverable just maps to a 503
    envelope.
    """


class ExtractionProviderError(RuntimeError):
    """Non-retryable provider error (4xx other than 429, malformed body, …).

    503-equivalent. Covers everything the LLM adapter raises that
    isn't a timeout or a rate-limit.
    """


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class ReceiptExtraction(BaseModel):
    """Validated structured output from the receipt-extraction LLM call.

    Field-level rules:

    * ``vendor`` — non-empty, capped at 200 chars (mirrors the
      claim's ``vendor`` column max).
    * ``amount_cents`` — non-negative integer cents in ``currency``'s
      minor unit. The LLM emits a decimal amount; the validator
      converts that to cents using the currency's minor-unit
      precision (2 decimals default, 3 for BHD-class currencies, 0
      for JPY-class currencies). A non-numeric / negative amount
      raises :class:`ValueError`.
    * ``currency`` — 3-letter ISO 4217 code, uppercased; must be in
      :data:`~app.util.currency.ISO_4217_ALLOWLIST`.
    * ``purchased_at`` — UTC-aware datetime; naive timestamps are
      rejected.
    * ``category`` — must be one of the six expense categories.
    * ``confidence`` — per-field ``float`` map; required keys are
      ``{vendor, amount, currency, purchased_at, category}``; each
      score must be in ``[0, 1]``.
    """

    model_config = ConfigDict(extra="forbid")

    vendor: str = Field(min_length=1, max_length=_MAX_VENDOR_LEN)
    amount_cents: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    purchased_at: datetime
    category: ReceiptCategory
    confidence: dict[str, float]

    @field_validator("currency")
    @classmethod
    def _currency_in_allowlist(cls, value: str) -> str:
        """Uppercase + narrow to the ISO-4217 allow-list."""
        upper = value.upper()
        if upper not in ISO_4217_ALLOWLIST:
            raise ValueError(f"currency {value!r} is not in the ISO-4217 allow-list")
        return upper

    @field_validator("purchased_at")
    @classmethod
    def _purchased_at_is_aware(cls, value: datetime) -> datetime:
        """Reject naive timestamps — same rule as the claim DTO."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "purchased_at must be a timezone-aware datetime; got naive."
            )
        return value

    @field_validator("confidence")
    @classmethod
    def _confidence_shape(cls, value: dict[str, float]) -> dict[str, float]:
        """Require the five field-level keys with scores in ``[0, 1]``.

        Extra keys are tolerated — a future model could emit a
        confidence for ``property_id`` or a sub-field; the autofill
        rule only consults the five required ones via
        :func:`overall_confidence`.
        """
        missing = _REQUIRED_CONFIDENCE_KEYS - value.keys()
        if missing:
            raise ValueError(
                f"confidence map missing required keys: {sorted(missing)!r}"
            )
        for key, score in value.items():
            try:
                score_f = float(score)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"confidence[{key!r}] must be a float, got {score!r}"
                ) from exc
            if not (0.0 <= score_f <= 1.0):
                raise ValueError(f"confidence[{key!r}]={score_f} is outside [0, 1]")
        return value


def overall_confidence(extraction: ReceiptExtraction) -> Decimal:
    """Return ``min(confidence.values())`` rounded to 2 decimals.

    The DB column ``expense_claim.autofill_confidence_overall`` is
    declared :class:`~sqlalchemy.Numeric` with precision (3, 2), so
    we quantise on the way out to avoid an SQLAlchemy cast warning
    (and to keep the round-trip ``read → recompute → equal`` test
    invariant intact).

    The v1 rule pins overall = min so a single low-confidence field
    pulls the whole row down — autofilling four fields confidently
    and one wildly is worse than asking the worker to fill the lot.
    """
    if not extraction.confidence:
        # Defence-in-depth: the validator already requires non-empty
        # confidence with the five required keys, but a pydantic
        # subclass that loosened the rule would otherwise hit a
        # ``min(empty)`` ValueError downstream.
        return Decimal("0.00")
    lo = min(float(v) for v in extraction.confidence.values())
    return Decimal(str(lo)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Outcome of one :func:`run_extraction` invocation.

    * ``autofilled`` — ``True`` iff this run's confidence cleared the
      threshold AND the claim was a fresh draft with no prior
      autofill payload (the "first attachment" case).
    * ``autofilled_fields`` — names of the columns this run wrote.
      Always either the full five-tuple (when ``autofilled`` is
      ``True``) or empty.
    * ``overall_confidence`` — the per-row score persisted on the
      claim. Returned for the caller's audit log / UI.
    * ``llm_usage_id`` — the id of the :class:`LlmUsageRow` written
      in the same UoW. Useful for /admin/usage smoke tests.
    """

    autofilled: bool
    autofilled_fields: tuple[str, ...]
    overall_confidence: Decimal
    llm_usage_id: str


# ---------------------------------------------------------------------------
# Pure extraction helper (used by both the persist path and the
# preview ``POST /expenses/scan`` route).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractionMetrics:
    """Adapter-reported metrics carried back from :func:`extract_from_bytes`.

    Bundled into a frozen dataclass so the persist path can write
    them to :class:`LlmUsageRow` without re-shuffling the call site.
    The HTTP preview route reads ``extraction`` only and discards
    the rest.

    ``extraction`` is ``None`` when the chat call landed but the
    body failed to parse — the metrics still carry the spent token
    counts so the caller can record a faithful usage row. On the
    happy path ``extraction`` is always a validated
    :class:`ReceiptExtraction`.
    """

    extraction: ReceiptExtraction | None
    model_id: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


def extract_from_bytes(
    image_bytes: bytes,
    *,
    llm: LLMClient,
    settings: Settings | None = None,
    clock: Clock | None = None,
) -> ExtractionMetrics:
    """Run a receipt blob through the LLM and return the validated payload.

    Two-step pipeline:

    1. ``llm.ocr(model_id=..., image_bytes=...)`` → free-form OCR text.
    2. ``llm.chat(model_id=..., messages=[{role: user, content:
       <prompt + ocr_text>}])`` → structured JSON parsed through
       :class:`ReceiptExtraction`.

    The two-step keeps the adapter seam clean — vision-capable OCR
    on one model, JSON-mode parsing on another (or the same model
    twice). Token counts are summed across both calls; latency is
    the wall-clock from before the OCR call to after the parse.

    Pure: no DB, no audit, no usage row. Callers that want the
    persist semantics use :func:`run_extraction`.

    On a downstream parse error, the chat call's tokens have already
    been spent. The raised :class:`ExtractionParseError` carries the
    burnt :class:`ExtractionMetrics` (with ``extraction=None``) on
    its :attr:`~ExtractionParseError.burnt_metrics` attribute so the
    caller can record the real token counts on the failure-mode
    usage row instead of zeroing them out.
    """
    resolved_settings = settings if settings is not None else get_settings()
    resolved_clock = clock if clock is not None else SystemClock()

    model_id = resolved_settings.llm_ocr_model
    if model_id is None:
        # Defensive — callers are expected to gate on this themselves
        # (the API endpoint returns 503 ``scan_not_configured`` and
        # :func:`~app.domain.expenses.claims.attach_receipt` skips the
        # runner). Surfacing the misconfig as a typed error keeps the
        # bypass path traceable.
        raise ExtractionProviderError(
            "settings.llm_ocr_model is not configured; "
            "extraction capability is disabled at the deployment level"
        )
    if not image_bytes:
        raise ExtractionParseError("image_bytes is empty; nothing to extract")

    started = resolved_clock.now()
    try:
        ocr_text = llm.ocr(model_id=model_id, image_bytes=image_bytes)
        chat_response = llm.chat(
            model_id=model_id,
            messages=[
                {
                    "role": "user",
                    "content": f"{_OCR_TO_JSON_PROMPT}\n\n{ocr_text}",
                }
            ],
        )
    except TimeoutError as exc:
        raise ExtractionTimeout(str(exc)) from exc
    except Exception as exc:
        # The OpenRouter adapter raises ``LlmRateLimited`` /
        # ``LlmProviderError`` / ``LlmTransportError`` from the
        # ``app.adapters.llm.openrouter`` namespace; the import would
        # be a cycle from this module (the adapter package is allowed
        # to import the domain via DI but not vice-versa). We
        # therefore branch by class name — the import-cycle-free
        # idiom every other domain seam uses for adapter errors.
        cls = type(exc).__name__
        if cls == "LlmRateLimited":
            raise ExtractionRateLimited(str(exc)) from exc
        if cls == "LlmTransportError":
            # Transport errors that wrap a TimeoutException already
            # surface above via :class:`TimeoutError`; everything else
            # collapses to provider-error.
            raise ExtractionProviderError(str(exc)) from exc
        if cls == "LlmProviderError":
            raise ExtractionProviderError(str(exc)) from exc
        # Unknown exception type — let it propagate (test seam,
        # programming error). A bare ``Exception`` catch that
        # swallows everything would hide real bugs.
        raise

    latency_ms = max(0, int((resolved_clock.now() - started).total_seconds() * 1000))
    prompt_tokens = int(chat_response.usage.prompt_tokens)
    completion_tokens = int(chat_response.usage.completion_tokens)

    try:
        extraction = _parse_chat_response(chat_response)
    except ExtractionParseError as exc:
        # The chat call already spent tokens; surface them so the
        # caller can record a usage row that reflects the real cost
        # rather than zeroing it out on the failure path.
        exc.burnt_metrics = ExtractionMetrics(
            extraction=None,
            model_id=model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )
        raise

    return ExtractionMetrics(
        extraction=extraction,
        model_id=model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
    )


def _parse_chat_response(response: LLMResponse) -> ReceiptExtraction:
    """Parse the chat reply into :class:`ReceiptExtraction` or raise.

    The model is asked to emit a bare JSON object — no markdown
    fences, no commentary. Real models occasionally wrap the body
    in ``​​​``json fences anyway; we strip those
    defensively before parsing.
    """
    text = response.text.strip()
    text = _strip_markdown_fence(text)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionParseError(f"LLM body is not valid JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise ExtractionParseError(
            f"LLM body must be a JSON object; got {type(payload).__name__}"
        )

    # Convert the model's decimal ``amount`` (string or float) into
    # integer cents using the currency's minor-unit precision. The
    # validator below does not have access to the sibling ``currency``
    # field at typing time, so we pre-compute ``amount_cents`` here
    # and feed the cleaned payload through pydantic.
    cleaned = _to_amount_cents(dict(payload))

    try:
        return ReceiptExtraction.model_validate(cleaned)
    except ValidationError as exc:
        raise ExtractionParseError(
            f"LLM body failed schema validation: {exc.errors(include_url=False)!r}"
        ) from exc


_MARKDOWN_FENCE_OPEN = "```"


def _strip_markdown_fence(text: str) -> str:
    """Remove an outer ```json ... ``` fence if the model added one.

    Pure string surgery. A model that returned ``"```json\n{...}\n```"``
    is treated the same as one that returned the bare object.
    """
    if not text.startswith(_MARKDOWN_FENCE_OPEN):
        return text
    # Drop the opening fence (and an optional ``json`` language tag)
    # and the closing fence. Conservative: only strip when both ends
    # match so a malformed body still surfaces a JSON error rather
    # than getting silently chopped.
    if not text.endswith(_MARKDOWN_FENCE_OPEN):
        return text
    inner = text[len(_MARKDOWN_FENCE_OPEN) : -len(_MARKDOWN_FENCE_OPEN)].strip()
    if inner.lower().startswith("json"):
        inner = inner[len("json") :].lstrip()
    return inner


def _to_amount_cents(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Convert the model's decimal ``amount`` field to integer cents.

    Accepts either an ``"amount"`` decimal-string / number OR an
    already-cents ``"amount_cents"`` int (some prompt revisions ask
    for the latter directly). Unknown shapes raise
    :class:`ExtractionParseError`.
    """
    out = dict(payload)
    if "amount_cents" in out and "amount" not in out:
        # Already in the target shape — let the validator handle it.
        return out
    if "amount" not in out:
        raise ExtractionParseError(
            "LLM body missing both 'amount' and 'amount_cents' keys"
        )
    raw_amount = out.pop("amount")
    raw_currency = out.get("currency")
    if not isinstance(raw_currency, str):
        raise ExtractionParseError(
            "LLM body's 'currency' must be a string before amount conversion"
        )
    try:
        decimal_amount = Decimal(str(raw_amount))
    except (ArithmeticError, ValueError) as exc:
        # ``Decimal(str(...))`` raises :class:`decimal.InvalidOperation`
        # (a subclass of :class:`ArithmeticError`) on garbage input;
        # ``str()`` itself can also raise :class:`ValueError` for
        # exotic numeric types. Either path collapses to "not a
        # numeric value" on the caller surface.
        raise ExtractionParseError(
            f"LLM body's 'amount' is not a numeric value: {raw_amount!r}"
        ) from exc
    if decimal_amount < 0:
        raise ExtractionParseError(
            f"LLM body's 'amount' must be non-negative; got {decimal_amount}"
        )
    upper = raw_currency.upper()
    if upper in _ZERO_DECIMAL_CURRENCIES:
        scale = Decimal(1)
    elif upper in _THREE_DECIMAL_CURRENCIES:
        scale = Decimal(1000)
    else:
        scale = Decimal(100)
    cents = (decimal_amount * scale).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    out["amount_cents"] = int(cents)
    return out


# ---------------------------------------------------------------------------
# Persist path
# ---------------------------------------------------------------------------


def _load_claim(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    for_update: bool = False,
) -> ExpenseClaim:
    """Load ``claim_id`` scoped to the caller's workspace, or raise.

    ``for_update`` issues a ``SELECT ... FOR UPDATE`` so concurrent
    autofill runs serialise on the row — the "first run vs follow-up"
    rule (``llm_autofill_json IS NULL``) is a read-then-write
    predicate and would otherwise let two parallel runners both
    decide they were the first attachment. The current call sites
    (:func:`~app.domain.expenses.claims.attach_receipt` already holds
    the row lock for the entire attach + runner flow) make this
    redundant for v1, but a future async-queue worker that calls
    :func:`run_extraction` directly without an outer lock relies on
    this seam.
    """
    stmt = select(ExpenseClaim).where(
        ExpenseClaim.id == claim_id,
        ExpenseClaim.workspace_id == ctx.workspace_id,
        ExpenseClaim.deleted_at.is_(None),
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise ClaimNotFound(claim_id)
    return row


def _load_attachment(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    attachment_id: str,
) -> ExpenseAttachment:
    """Load ``attachment_id`` (scoped to ``claim_id`` + tenant) or raise."""
    stmt = select(ExpenseAttachment).where(
        ExpenseAttachment.id == attachment_id,
        ExpenseAttachment.claim_id == claim_id,
        ExpenseAttachment.workspace_id == ctx.workspace_id,
    )
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise AttachmentNotFound(attachment_id)
    return row


def _read_blob(storage: Storage, *, blob_hash: str) -> bytes:
    """Read the blob bytes via the storage port.

    Surfaces a missing blob as :class:`ExtractionProviderError` —
    the upload pipeline guaranteed presence at attach time, so a
    missing blob between attach and extraction is an infrastructure
    bug, not a caller error.
    """
    try:
        with storage.get(blob_hash) as fh:
            return fh.read()
    except BlobNotFound as exc:
        raise ExtractionProviderError(
            f"blob {blob_hash!r} disappeared between attach and extraction"
        ) from exc


def _write_audit_failure(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    attachment_id: str,
    error: str,
    clock: Clock,
) -> None:
    """Audit a ``receipt.ocr_failed`` row.

    Single seam so every failure mode (parse, timeout, rate-limited,
    provider error) writes a row with the same ``error`` envelope
    shape — the SPA's failure pivot keys on ``error`` directly.
    """
    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=claim_id,
        action="receipt.ocr_failed",
        diff={"after": {"attachment_id": attachment_id, "error": error}},
        clock=clock,
    )


def _record_llm_usage(
    session: Session,
    ctx: WorkspaceContext,
    *,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status: Literal["ok", "error", "timeout"],
    correlation_id: str,
    clock: Clock,
) -> str:
    """Insert one :class:`LlmUsageRow` row and return its id.

    Takes primitive token / latency fields (rather than an
    :class:`ExtractionMetrics` snapshot) so the failure paths can
    record real token counts even when no parsed extraction
    materialised — see :func:`extract_from_bytes` for how the chat
    call's usage survives a downstream parse error.

    Bypasses :mod:`app.domain.llm.budget`'s ``record_usage`` because
    the budget seam requires a seeded ledger row, which the v1
    deliverable does not yet wire (the §11 router + cap-seeding
    landing is a separate Beads task). Writing the row directly
    mirrors how :mod:`app.audit` writes to ``audit_log`` without
    touching the ledger; the §11 router follow-up will swap this
    helper for ``record_usage`` once the cap surface is live.
    """
    usage_id = new_ulid()
    # ``cost_cents=0`` until the deployment-scope ``llm_provider_model``
    # registry lands and the pricing table is seeded — see
    # :mod:`app.domain.llm.budget` ``estimate_cost_cents`` for the
    # current "unknown model → 0" semantic. Tracked together with
    # the §11 router landing.
    row = LlmUsageRow(
        id=usage_id,
        workspace_id=ctx.workspace_id,
        capability=AUTOFILL_CAPABILITY,
        model_id=model_id,
        tokens_in=prompt_tokens,
        tokens_out=completion_tokens,
        cost_cents=0,
        latency_ms=latency_ms,
        status=status,
        correlation_id=correlation_id,
        attempt=0,
        assignment_id=None,
        fallback_attempts=0,
        finish_reason=None,
        actor_user_id=ctx.actor_id,
        token_id=None,
        agent_label=None,
        created_at=clock.now(),
    )
    session.add(row)
    return usage_id


def run_extraction(
    session: Session,
    ctx: WorkspaceContext,
    *,
    claim_id: str,
    attachment_id: str,
    llm: LLMClient,
    storage: Storage,
    clock: Clock | None = None,
    settings: Settings | None = None,
) -> ExtractionResult:
    """Extract one receipt → persist autofill payload → maybe write claim fields.

    See module docstring for the contract. The function is the
    single source of truth for the "how does an OCR run land in the
    DB" question; the worker-task wrapper at
    :mod:`app.worker.tasks.receipt_ocr` is just a renamed call so
    the eventual async queue can swap the front door.

    Behaviour summary:

    * Loads claim + attachment scoped to the caller's tenant.
    * Reads the blob bytes and runs :func:`extract_from_bytes`.
    * On any extraction failure, writes a ``receipt.ocr_failed``
      audit row + an :class:`LlmUsageRow` with the matching status
      (``error`` for parse / provider, ``timeout`` for timeout).
      The claim is NOT mutated on failure.
    * On success, persists ``llm_autofill_json`` +
      ``autofill_confidence_overall`` on the claim. If the claim
      was a fresh draft (state == draft AND ``llm_autofill_json``
      was NULL beforehand) AND ``overall_confidence >
      AUTOFILL_CONFIDENCE_THRESHOLD``, also rewrites the worker-
      typed scalar fields. Audits ``receipt.ocr_completed`` with
      the model id, token counts, latency, and the list of
      autofilled field names.
    """
    resolved_clock = clock if clock is not None else SystemClock()
    resolved_settings = settings if settings is not None else get_settings()

    # ``for_update=True``: serialise concurrent autofill runs on the
    # claim row so the "first run vs follow-up" predicate
    # (``llm_autofill_json IS NULL``) is a single atomic
    # read-then-write per row. See :func:`_load_claim` for the
    # rationale + the v1 redundancy with attach_receipt's own lock.
    claim = _load_claim(session, ctx, claim_id=claim_id, for_update=True)
    attachment = _load_attachment(
        session, ctx, claim_id=claim_id, attachment_id=attachment_id
    )

    # Snapshot the "no autofill yet" predicate BEFORE we run anything —
    # the post-run write would otherwise immediately invalidate it
    # and we'd never autofill the first attachment.
    is_first_run = claim.llm_autofill_json is None
    is_draft = claim.state == "draft"

    correlation_id = new_ulid()
    fallback_model_id = resolved_settings.llm_ocr_model or ""

    image_bytes = _read_blob(storage, blob_hash=attachment.blob_hash)

    try:
        metrics = extract_from_bytes(
            image_bytes,
            llm=llm,
            settings=resolved_settings,
            clock=resolved_clock,
        )
    except ExtractionTimeout as exc:
        _write_audit_failure(
            session,
            ctx,
            claim_id=claim_id,
            attachment_id=attachment_id,
            error=f"timeout: {exc!s}",
            clock=resolved_clock,
        )
        # Timeouts are by definition pre-body — no usage metrics
        # exist to record. Persist a zeroed row so /admin/usage
        # surfaces the failed call and operators can pivot on
        # ``status='timeout'``.
        _record_llm_usage(
            session,
            ctx,
            model_id=fallback_model_id,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
            status="timeout",
            correlation_id=correlation_id,
            clock=resolved_clock,
        )
        raise
    except ExtractionParseError as exc:
        _write_audit_failure(
            session,
            ctx,
            claim_id=claim_id,
            attachment_id=attachment_id,
            error=f"{type(exc).__name__}: {exc!s}",
            clock=resolved_clock,
        )
        # If the chat call landed but the body failed to parse, the
        # provider has already burnt tokens. ``burnt_metrics`` carries
        # the real prompt / completion counts so /admin/usage reflects
        # the spend instead of zeroing it out.
        burnt = exc.burnt_metrics
        _record_llm_usage(
            session,
            ctx,
            model_id=burnt.model_id if burnt is not None else fallback_model_id,
            prompt_tokens=burnt.prompt_tokens if burnt is not None else 0,
            completion_tokens=(burnt.completion_tokens if burnt is not None else 0),
            latency_ms=burnt.latency_ms if burnt is not None else 0,
            status="error",
            correlation_id=correlation_id,
            clock=resolved_clock,
        )
        raise
    except (
        ExtractionRateLimited,
        ExtractionProviderError,
    ) as exc:
        _write_audit_failure(
            session,
            ctx,
            claim_id=claim_id,
            attachment_id=attachment_id,
            error=f"{type(exc).__name__}: {exc!s}",
            clock=resolved_clock,
        )
        # Rate-limit / provider-error paths failed before the chat
        # body landed; no token spend to record.
        _record_llm_usage(
            session,
            ctx,
            model_id=fallback_model_id,
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
            status="error",
            correlation_id=correlation_id,
            clock=resolved_clock,
        )
        raise

    extraction = metrics.extraction
    if extraction is None:
        # Defence-in-depth: the happy path always populates
        # ``extraction``. A ``None`` here implies a future helper
        # bypassed the parse step but didn't raise — surface the
        # invariant violation rather than autofilling a NULL.
        raise ExtractionProviderError(
            "extract_from_bytes returned metrics with no extraction"
        )
    confidence_overall = overall_confidence(extraction)

    # Persist payload + confidence on the claim row, regardless of
    # whether we autofill. The UI shows the suggestions even on a
    # low-confidence run (the worker can accept individual fields).
    payload_dict = _extraction_to_payload(extraction, confidence_overall)
    claim.llm_autofill_json = payload_dict
    claim.autofill_confidence_overall = confidence_overall

    autofilled_fields: tuple[str, ...] = ()
    autofilled = False

    if is_draft and is_first_run and confidence_overall > AUTOFILL_CONFIDENCE_THRESHOLD:
        # First-attachment autofill — rewrite the worker-typed
        # scalars so the claim card lands populated. The currency is
        # re-validated through the claim service's allow-list to
        # keep the DB invariant intact (the schema validator already
        # enforced this once; the second pass is defence-in-depth
        # in case a future subclass loosens the rule).
        claim.vendor = extraction.vendor
        claim.purchased_at = extraction.purchased_at
        claim.currency = _validate_currency(extraction.currency)
        claim.total_amount_cents = extraction.amount_cents
        claim.category = extraction.category
        autofilled_fields = (
            "vendor",
            "purchased_at",
            "currency",
            "total_amount_cents",
            "category",
        )
        autofilled = True

    session.flush()

    usage_id = _record_llm_usage(
        session,
        ctx,
        model_id=metrics.model_id,
        prompt_tokens=metrics.prompt_tokens,
        completion_tokens=metrics.completion_tokens,
        latency_ms=metrics.latency_ms,
        status="ok",
        correlation_id=correlation_id,
        clock=resolved_clock,
    )

    write_audit(
        session,
        ctx,
        entity_kind="expense_claim",
        entity_id=claim_id,
        action="receipt.ocr_completed",
        diff={
            "after": {
                "attachment_id": attachment_id,
                "model_id": metrics.model_id,
                "prompt_tokens": metrics.prompt_tokens,
                "completion_tokens": metrics.completion_tokens,
                "latency_ms": metrics.latency_ms,
                "overall_confidence": str(confidence_overall),
                "autofilled_fields": list(autofilled_fields),
            }
        },
        clock=resolved_clock,
    )

    return ExtractionResult(
        autofilled=autofilled,
        autofilled_fields=autofilled_fields,
        overall_confidence=confidence_overall,
        llm_usage_id=usage_id,
    )


def _extraction_to_payload(
    extraction: ReceiptExtraction, confidence_overall: Decimal
) -> dict[str, Any]:
    """Render :class:`ReceiptExtraction` for the JSON column.

    Datetimes are stringified (the JSON column is portable across
    SQLite + PG and a raw datetime would land as a Python repr on
    SQLite); the per-field confidence map is preserved verbatim so
    the UI can show "we extracted vendor at 0.92, amount at 0.87,
    …" without re-running the LLM.
    """
    return {
        "vendor": extraction.vendor,
        "amount_cents": extraction.amount_cents,
        "currency": extraction.currency,
        "purchased_at": extraction.purchased_at.isoformat(),
        "category": extraction.category,
        "confidence": dict(extraction.confidence),
        "overall_confidence": str(confidence_overall),
    }
