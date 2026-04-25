"""Integration tests for :mod:`app.fixtures.llm` (cd-4btd).

The default-registry seed must:

* Land all three rows (:class:`LlmProvider`, :class:`LlmModel`,
  :class:`LlmProviderModel`) on first call.
* Be idempotent — a second call returns the same join row without
  duplicating the trio.
* Produce a row a workspace can assign ``chat.manager`` to (i.e. an
  ``LlmProviderModel.id`` the cd-4btd FK on
  ``model_assignment.model_id`` accepts).

See ``docs/specs/11-llm-and-agents.md`` §"Provider / model /
provider-model registry".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.db.llm.models import (
    LlmModel,
    LlmProvider,
    LlmProviderModel,
    ModelAssignment,
)
from app.fixtures.llm import (
    DEFAULT_MODEL_CANONICAL_NAME,
    DEFAULT_PROVIDER_NAME,
    seed_default_registry,
)
from app.tenancy.context import WorkspaceContext
from app.tenancy.current import reset_current, set_current
from app.util.clock import FrozenClock
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration


_PINNED = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


class TestSeedDefaultRegistry:
    def test_first_call_lands_trio(self, db_session: Session) -> None:
        clock = FrozenClock(_PINNED)
        pm = seed_default_registry(db_session, clock=clock)

        # Provider + Model + ProviderModel all exist with the
        # expected stable identifiers.
        provider = db_session.get(LlmProvider, pm.provider_id)
        model = db_session.get(LlmModel, pm.model_id)
        assert provider is not None
        assert model is not None
        assert provider.name == DEFAULT_PROVIDER_NAME
        assert model.canonical_name == DEFAULT_MODEL_CANONICAL_NAME
        assert pm.api_model_id == "default/chat-base"
        assert pm.is_enabled is True

    def test_idempotent_re_seed_returns_same_row(self, db_session: Session) -> None:
        """Calling the seed twice does not duplicate the trio."""
        clock = FrozenClock(_PINNED)
        first = seed_default_registry(db_session, clock=clock)
        second = seed_default_registry(db_session, clock=clock)

        assert first.id == second.id

        # Exactly one provider / model / provider_model row landed —
        # the upstream uniques would already block a duplicate, but
        # this assertion makes the idempotency guarantee explicit.
        providers = db_session.execute(
            select(LlmProvider).where(LlmProvider.name == DEFAULT_PROVIDER_NAME)
        ).all()
        assert len(providers) == 1
        models = db_session.execute(
            select(LlmModel).where(
                LlmModel.canonical_name == DEFAULT_MODEL_CANONICAL_NAME
            )
        ).all()
        assert len(models) == 1
        provider_models = db_session.execute(
            select(LlmProviderModel).where(LlmProviderModel.id == first.id)
        ).all()
        assert len(provider_models) == 1

    def test_seed_satisfies_chat_manager_assignment(self, db_session: Session) -> None:
        """A workspace assignment can FK at the seeded provider_model.

        Proves the cd-4btd FK on ``model_assignment.model_id`` accepts
        the seed's ULID — i.e. the seed is the smallest unit that
        unblocks a fresh deployment from creating a working
        ``chat.manager`` chain.
        """
        clock = FrozenClock(_PINNED)
        pm = seed_default_registry(db_session, clock=clock)

        user = bootstrap_user(
            db_session,
            email="seed@example.com",
            display_name="Seed",
            clock=clock,
        )
        workspace = bootstrap_workspace(
            db_session,
            slug="seed-ws",
            name="SeedWs",
            owner_user_id=user.id,
            clock=clock,
        )
        ctx = WorkspaceContext(
            workspace_id=workspace.id,
            workspace_slug=workspace.slug,
            actor_id=user.id,
            actor_kind="user",
            actor_grant_role="manager",
            actor_was_owner_member=True,
            audit_correlation_id="01HWA00000000000000000SEED",
        )
        token = set_current(ctx)
        try:
            row = ModelAssignment(
                id="01HWA00000000000000000SEDA",
                workspace_id=workspace.id,
                capability="chat.manager",
                model_id=pm.id,
                provider="openrouter",
                created_at=_PINNED,
            )
            db_session.add(row)
            # No FK violation — the seed's id is a real registry row.
            db_session.flush()

            loaded = db_session.get(ModelAssignment, row.id)
            assert loaded is not None
            assert loaded.model_id == pm.id
        finally:
            reset_current(token)
