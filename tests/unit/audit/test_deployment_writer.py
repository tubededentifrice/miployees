"""Unit tests for :func:`app.audit.write_deployment_audit`.

The deployment-scope writer is the cd-kgcc sibling of
:func:`write_audit`: it lands a row with ``workspace_id IS NULL`` and
``scope_kind = 'deployment'`` and otherwise shares the redaction +
clock + ULID seam. These tests exercise the construction surface
against a throwaway in-memory engine — no migration, no tenancy
filter — so they stay fast and focus on the field-copy behaviour.

Integration coverage (CHECK-constraint enforcement, tenant-filter
isolation between the two scope partitions) lives in
``tests/integration/test_audit_writer.py``.

See ``docs/specs/02-domain-model.md`` §"audit_log",
``docs/specs/15-security-privacy.md`` §"Audit log", and
``docs/specs/12-rest-api.md`` §"Admin surface" → "Deployment audit".
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TypedDict

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.session import make_engine
from app.audit import write_deployment_audit
from app.tenancy import ActorGrantRole, ActorKind
from app.util.clock import FrozenClock


class _DeploymentActorKW(TypedDict):
    """Reusable kwargs for the deployment-scope writer.

    Pinned actor identity mirroring a real admin caller: a deployment
    admin (``manager`` grant_role on the deployment scope, §05
    "Permissions"), the correlation_id supplied by the admin router,
    and ``actor_was_owner_member = False`` because the deployment
    admin is not a workspace owner.

    Spelled as a TypedDict (rather than a plain dict literal) so
    ``**_DEPLOYMENT_ACTOR`` keeps the typed kwargs of
    :func:`write_deployment_audit` strict under ``mypy --strict`` —
    a plain ``dict[str, object]`` widens every value and erases the
    ``Literal`` enums on ``actor_kind`` / ``actor_grant_role``.
    """

    actor_id: str
    actor_kind: ActorKind
    actor_grant_role: ActorGrantRole
    actor_was_owner_member: bool
    correlation_id: str


_DEPLOYMENT_ACTOR: _DeploymentActorKW = {
    "actor_id": "01HWA00000000000000000ADM1",
    "actor_kind": "user",
    "actor_grant_role": "manager",
    "actor_was_owner_member": False,
    "correlation_id": "01HWA00000000000000000CRLD",
}


@pytest.fixture
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine, schema created from ``Base.metadata``.

    We deliberately do NOT run alembic here: the writer is tested
    purely against the ORM. Integration-level transaction semantics
    are covered in the sibling integration module.
    """
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Fresh session per test; no tenant filter installed here."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with factory() as s:
        yield s


class TestScopeKind:
    """Every deployment-scope row carries the right scope tags."""

    def test_workspace_id_is_null(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.created",
        )
        assert row.workspace_id is None

    def test_scope_kind_is_deployment(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.created",
        )
        assert row.scope_kind == "deployment"


class TestFieldCopy:
    """The actor, correlation, entity, and action fields land verbatim."""

    def test_copies_actor_identity(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="deployment_setting",
            entity_id="01HWADEP00000000000000S01",
            action="deployment_setting.updated",
        )
        assert row.actor_id == _DEPLOYMENT_ACTOR["actor_id"]
        assert row.actor_kind == _DEPLOYMENT_ACTOR["actor_kind"]
        assert row.actor_grant_role == _DEPLOYMENT_ACTOR["actor_grant_role"]
        assert row.actor_was_owner_member is _DEPLOYMENT_ACTOR["actor_was_owner_member"]

    def test_copies_correlation_id(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.created",
        )
        assert row.correlation_id == _DEPLOYMENT_ACTOR["correlation_id"]

    def test_copies_entity_and_action(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="signup_setting",
            entity_id="01HWADEP00000000000000G01",
            action="signup_setting.toggled",
        )
        assert row.entity_kind == "signup_setting"
        assert row.entity_id == "01HWADEP00000000000000G01"
        assert row.action == "signup_setting.toggled"

    def test_system_actor_kind_accepted(self, session: Session) -> None:
        """A worker-emitted deployment row uses ``actor_kind='system'``.

        Routine deployment work (key rotation, audit verifier, signup
        GC) lands as ``system`` — see §15 "Key rotation" and
        §"Audit log" for the canonical call sites.
        """
        row = write_deployment_audit(
            session,
            actor_id="01HWA00000000000000000SYS1",
            actor_kind="system",
            actor_grant_role="manager",
            actor_was_owner_member=False,
            correlation_id="01HWA00000000000000000CRLD",
            entity_kind="audit_log",
            entity_id="01HWA00000000000000000VFY",
            action="audit.verified",
        )
        assert row.actor_kind == "system"


class TestDiffDefaulting:
    """``diff=None`` is persisted as ``{}`` so the NOT NULL contract holds."""

    def test_none_becomes_empty_dict(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.revoked",
            diff=None,
        )
        assert row.diff == {}

    def test_mapping_is_preserved(self, session: Session) -> None:
        payload = {
            "before": {"name": "old-token"},
            "after": {"name": "new-token"},
        }
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.renamed",
            diff=payload,
        )
        assert row.diff == payload

    def test_omitted_defaults_to_empty_dict(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="deployment_setting",
            entity_id="01HWADEP00000000000000S01",
            action="deployment_setting.viewed",
        )
        assert row.diff == {}


class TestRedaction:
    """PII in ``diff`` is redacted before the row is added to the session.

    The deployment-scope writer shares the redaction seam with
    :func:`app.audit.write_audit`; these spot checks pin the seam in
    the deployment path so a future divergence (someone introducing
    a separate code path for the new entry point) cannot silently
    skip the §15 "Audit log" redaction guarantee.
    """

    def test_email_in_diff_is_redacted(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.created",
            diff={"after": {"contact": "ops@example.com"}},
        )
        assert row.diff is not None
        assert isinstance(row.diff, dict)
        after = row.diff["after"]
        assert isinstance(after, dict)
        assert after["contact"] == "<redacted:email>"

    def test_sensitive_key_in_diff_is_redacted(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.created",
            diff={"token": "raw-secret-bytes"},
        )
        assert row.diff is not None
        assert isinstance(row.diff, dict)
        assert "raw-secret-bytes" not in str(row.diff)


class TestClock:
    """The writer uses ``SystemClock`` by default and accepts overrides."""

    def test_frozen_clock_pins_created_at(self, session: Session) -> None:
        pinned = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        clock = FrozenClock(pinned)
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.created",
            clock=clock,
        )
        assert row.created_at == pinned

    def test_default_clock_is_utc_aware(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.created",
        )
        assert row.created_at.tzinfo is not None
        assert row.created_at.utcoffset() == UTC.utcoffset(row.created_at)


class TestUlid:
    """Every row carries a unique monotonic ULID id."""

    def test_rapid_calls_produce_distinct_ids(self, session: Session) -> None:
        """``new_ulid`` is monotonic — two rapid calls must differ.

        Regression guard mirroring the workspace-scope sibling test
        in :mod:`tests.unit.test_audit_writer`.
        """
        ids: set[str] = set()
        for _ in range(50):
            row = write_deployment_audit(
                session,
                **_DEPLOYMENT_ACTOR,
                entity_kind="api_token",
                entity_id="01HWADEP00000000000000T01",
                action="api_token.created",
            )
            ids.add(row.id)
        assert len(ids) == 50


class TestSessionSurface:
    """The writer adds to the session; the caller controls transactions."""

    def test_row_is_registered_on_session(self, session: Session) -> None:
        row = write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.created",
        )
        assert row in session.new

    def test_writer_does_not_commit(self, session: Session) -> None:
        """The writer MUST NOT call ``session.commit()``.

        A rollback right after :func:`write_deployment_audit` must
        therefore discard the row — any stray commit inside the
        writer would invert the Unit-of-Work contract in §01 #3.
        """
        write_deployment_audit(
            session,
            **_DEPLOYMENT_ACTOR,
            entity_kind="api_token",
            entity_id="01HWADEP00000000000000T01",
            action="api_token.created",
        )
        session.rollback()
        rows = session.scalars(select(AuditLog)).all()
        assert rows == []
