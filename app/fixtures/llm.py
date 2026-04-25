"""Deployment-scope LLM registry seeds (cd-4btd).

The cd-4btd registry trio (:class:`~app.adapters.db.llm.models.LlmProvider`,
:class:`~app.adapters.db.llm.models.LlmModel`,
:class:`~app.adapters.db.llm.models.LlmProviderModel`) is populated
through the ``/admin/llm`` graph editor in production (§11 "LLM
graph admin"), but a fresh deployment needs *some* trio in place
before the first :class:`~app.domain.llm.router.ModelPick` resolves.
This module supplies the minimum-viable trio: a single ``fake``
provider + a generic chat model + their join row, suitable for both
the dev-loop and the test harness.

The seed is intentionally **not** auto-installed at startup — a
deployment operator decides when to call :func:`seed_default_registry`
(or its CLI equivalent, which lands with the future ``/admin/llm``
slice). Calling it twice is safe; the function checks for the
canonical name first and returns the existing row.

`docs/specs/11-llm-and-agents.md` §"Provider / model / provider-model
registry" pins the column shape; this module is the seed-side
implementation.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import LlmModel, LlmProvider, LlmProviderModel
from app.util.clock import Clock, SystemClock
from app.util.ulid import new_ulid

__all__ = [
    "DEFAULT_MODEL_CANONICAL_NAME",
    "DEFAULT_PROVIDER_NAME",
    "seed_default_registry",
]


# Stable identifiers so a re-seed (idempotent retry) lands the same
# row. ``fake`` keeps the seed safe against accidental upstream
# traffic — operators flip the provider type via the admin graph
# once they wire a real key.
DEFAULT_PROVIDER_NAME: str = "default-fake"
DEFAULT_MODEL_CANONICAL_NAME: str = "default/chat-base"


def seed_default_registry(
    session: Session,
    *,
    clock: Clock | None = None,
    api_model_id: str = "default/chat-base",
) -> LlmProviderModel:
    """Insert (or return) the default LLM registry trio.

    Idempotent on ``LlmProvider.name`` and
    ``LlmModel.canonical_name``: a re-seed (e.g. an operator running
    a deployment-bootstrap script twice) returns the existing
    :class:`LlmProviderModel` row instead of duplicating the trio.

    The default trio satisfies the §11 ``chat.manager`` capability —
    callers who need a workspace assignment thread the returned
    row's ``id`` into :attr:`ModelAssignment.model_id` (the cd-4btd
    FK target). Per-call tuning (``temperature``, ``max_tokens``,
    …) lives on the assignment, not the provider_model — the
    deployment seed is intentionally minimal.

    The :class:`Clock` defaults to :class:`SystemClock`; tests
    thread :class:`~app.util.clock.FrozenClock` through so seeded
    timestamps stay deterministic.

    Transaction-neutral: the caller's UoW owns the commit boundary;
    we ``session.flush()`` so subsequent reads in the transaction
    see the new rows.
    """
    c = clock if clock is not None else SystemClock()
    now: datetime = c.now()

    provider = session.execute(
        select(LlmProvider).where(LlmProvider.name == DEFAULT_PROVIDER_NAME)
    ).scalar_one_or_none()
    if provider is None:
        provider = LlmProvider(
            id=new_ulid(c),
            name=DEFAULT_PROVIDER_NAME,
            provider_type="fake",
            timeout_s=60,
            requests_per_minute=60,
            priority=0,
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
        session.add(provider)
        session.flush()

    model = session.execute(
        select(LlmModel).where(LlmModel.canonical_name == DEFAULT_MODEL_CANONICAL_NAME)
    ).scalar_one_or_none()
    if model is None:
        model = LlmModel(
            id=new_ulid(c),
            canonical_name=DEFAULT_MODEL_CANONICAL_NAME,
            display_name="Default Chat Base",
            vendor="other",
            capabilities=["chat"],
            is_active=True,
            price_source="",
            created_at=now,
            updated_at=now,
        )
        session.add(model)
        session.flush()

    # Idempotency key on the join is the unique
    # ``(provider_id, model_id)`` index. A re-seed returns the
    # existing join; otherwise the second insert would collide.
    provider_model = session.execute(
        select(LlmProviderModel).where(
            LlmProviderModel.provider_id == provider.id,
            LlmProviderModel.model_id == model.id,
        )
    ).scalar_one_or_none()
    if provider_model is None:
        provider_model = LlmProviderModel(
            id=new_ulid(c),
            provider_id=provider.id,
            model_id=model.id,
            api_model_id=api_model_id,
            supports_system_prompt=True,
            supports_temperature=True,
            is_enabled=True,
            created_at=now,
            updated_at=now,
        )
        session.add(provider_model)
        session.flush()
    return provider_model
