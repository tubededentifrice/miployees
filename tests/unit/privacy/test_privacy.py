from __future__ import annotations

import gzip
import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.db.audit.models import AuditLog
from app.adapters.db.base import Base
from app.adapters.db.identity.models import User
from app.adapters.db.integrations.models import WebhookDelivery, WebhookSubscription
from app.adapters.db.llm.models import LlmUsage
from app.adapters.db.messaging.models import EmailDelivery
from app.adapters.db.payroll.models import (
    PayoutDestination,
    PayPeriod,
    Payslip,
)
from app.adapters.db.privacy.models import PrivacyExport
from app.adapters.db.secrets.models import SecretEnvelope
from app.adapters.db.workspace.models import UserWorkspace, Workspace
from app.domain.privacy import (
    payout_manifest_available,
    purge_person,
    request_user_export,
    rotate_operational_logs,
)
from app.tenancy import tenant_agnostic
from tests._fakes.storage import InMemoryStorage


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)()


def _workspace(session: Session, workspace_id: str = "w1") -> Workspace:
    now = datetime(2026, 4, 28, tzinfo=UTC)
    row = Workspace(
        id=workspace_id,
        slug=workspace_id,
        name=workspace_id,
        plan="free",
        quota_json={},
        settings_json={},
        default_timezone="UTC",
        default_locale="en",
        default_currency="USD",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    return row


def _user(session: Session, user_id: str, email: str) -> User:
    now = datetime(2026, 4, 28, tzinfo=UTC)
    row = User(
        id=user_id,
        email=email,
        email_lower=email.lower(),
        display_name=user_id,
        locale="en",
        timezone="UTC",
        avatar_blob_hash=None,
        created_at=now,
        last_login_at=None,
        archived_at=None,
    )
    session.add(row)
    return row


def _membership(session: Session, user_id: str, workspace_id: str = "w1") -> None:
    session.add(
        UserWorkspace(
            user_id=user_id,
            workspace_id=workspace_id,
            source="workspace_grant",
            added_at=datetime(2026, 4, 28, tzinfo=UTC),
        )
    )


def test_export_is_limited_to_requesting_user() -> None:
    session = _session()
    storage = InMemoryStorage()
    with tenant_agnostic():
        _workspace(session)
        _user(session, "u1", "u1@example.test")
        _user(session, "u2", "u2@example.test")
        _membership(session, "u1")
        _membership(session, "u2")
        session.commit()

        result = request_user_export(session, storage, user_id="u1")
        job = session.get(PrivacyExport, result.id)
        assert job is not None
        assert job.blob_hash is not None
        with zipfile.ZipFile(storage.get(job.blob_hash)) as archive:
            payload = json.loads(archive.read("export.json"))

    users = payload["tables"]["user"]
    assert [row["id"] for row in users] == ["u1"]
    assert result.download_url is not None
    assert "memory://" in result.download_url
    audit = session.scalars(select(AuditLog)).one()
    assert audit.action == "audit.privacy.export.issued"


def test_purge_scrubs_routing_data_and_preserves_amounts() -> None:
    session = _session()
    now = datetime(2026, 4, 28, tzinfo=UTC)
    with tenant_agnostic():
        _workspace(session)
        _user(session, "u1", "u1@example.test")
        _membership(session, "u1")
        secret = SecretEnvelope(
            id="sec1",
            owner_entity_kind="payout_destination",
            owner_entity_id="pd1",
            purpose="payout-routing",
            ciphertext=b"cipher",
            nonce=b"1" * 12,
            key_fp=b"2" * 8,
            created_at=now,
            rotated_at=None,
        )
        destination = PayoutDestination(
            id="pd1",
            workspace_id="w1",
            user_id="u1",
            kind="bank",
            currency="EUR",
            display_stub="****1234",
            secret_ref_id="sec1",
            country="FR",
            label="Main",
            archived_at=None,
            created_at=now,
            updated_at=now,
        )
        period = PayPeriod(
            id="pp1",
            workspace_id="w1",
            starts_at=now - timedelta(days=30),
            ends_at=now,
            state="paid",
            locked_at=now,
            locked_by="manager",
            created_at=now,
        )
        payslip = Payslip(
            id="ps1",
            workspace_id="w1",
            pay_period_id="pp1",
            user_id="u1",
            shift_hours_decimal=Decimal("10.00"),
            overtime_hours_decimal=Decimal("0.00"),
            gross_cents=10000,
            deductions_cents={},
            net_cents=10000,
            pdf_blob_hash=None,
            payout_snapshot_json={
                "destination_id": "pd1",
                "kind": "bank",
                "currency": "EUR",
                "amount_cents": 10000,
                "label": "Main",
                "display_stub": "****1234",
            },
            payout_manifest_purged_at=None,
            created_at=now,
        )
        session.add_all([secret, destination, period, payslip])
        session.commit()

        result = purge_person(session, person_id="u1")
        session.commit()

        scrubbed = session.get(PayoutDestination, "pd1")
        assert scrubbed is not None
        assert scrubbed.secret_ref_id is None
        assert scrubbed.display_stub is None
        assert session.get(SecretEnvelope, "sec1") is None
        purged_payslip = session.get(Payslip, "ps1")
        assert purged_payslip is not None
        assert purged_payslip.gross_cents == 10000
        assert purged_payslip.payout_snapshot_json == {
            "destination_id": "pd1",
            "kind": "bank",
            "currency": "EUR",
            "amount_cents": 10000,
            "label": None,
            "display_stub": None,
        }
        assert not payout_manifest_available(
            session,
            payslip_id="ps1",
            workspace_id="w1",
        )

    assert result.deleted_secret_envelopes == 1
    assert result.scrubbed_payslips == 1


def test_retention_archives_jsonl_gz_and_deletes_rows(tmp_path: Path) -> None:
    session = _session()
    old = datetime(2025, 1, 1, tzinfo=UTC)
    with tenant_agnostic():
        _workspace(session)
        _user(session, "u1", "u1@example.test")
        session.add_all(
            [
                AuditLog(
                    id="aud1",
                    workspace_id="w1",
                    actor_id="u1",
                    actor_kind="user",
                    actor_grant_role="manager",
                    actor_was_owner_member=True,
                    entity_kind="user",
                    entity_id="u1",
                    action="test",
                    diff={},
                    correlation_id="corr",
                    scope_kind="workspace",
                    created_at=old,
                ),
                LlmUsage(
                    id="llm1",
                    workspace_id="w1",
                    capability="receipt_ocr",
                    model_id="fake",
                    tokens_in=1,
                    tokens_out=1,
                    cost_cents=1,
                    latency_ms=1,
                    status="ok",
                    correlation_id="corr",
                    attempt=0,
                    assignment_id=None,
                    fallback_attempts=0,
                    finish_reason="stop",
                    actor_user_id=None,
                    token_id=None,
                    agent_label=None,
                    created_at=old,
                ),
                EmailDelivery(
                    id="email1",
                    workspace_id="w1",
                    to_person_id="u1",
                    to_email_at_send="u1@example.test",
                    template_key="privacy_export",
                    context_snapshot_json={},
                    sent_at=old,
                    provider_message_id=None,
                    delivery_state="sent",
                    first_error=None,
                    retry_count=0,
                    inbound_linkage=None,
                    created_at=old,
                ),
                WebhookSubscription(
                    id="sub1",
                    workspace_id="w1",
                    name="test",
                    url="https://example.test/hook",
                    secret_blob="secret",
                    secret_last_4="cret",
                    events_json=["test"],
                    active=True,
                    created_at=old,
                    updated_at=old,
                ),
                WebhookDelivery(
                    id="wh1",
                    workspace_id="w1",
                    subscription_id="sub1",
                    event="test",
                    payload_json={},
                    status="succeeded",
                    attempt=1,
                    next_attempt_at=None,
                    last_status_code=200,
                    last_error=None,
                    last_attempted_at=old,
                    succeeded_at=old,
                    dead_lettered_at=None,
                    replayed_from_id=None,
                    created_at=old,
                ),
            ]
        )
        session.commit()

        results = rotate_operational_logs(
            session,
            data_dir=tmp_path,
            clock=None,
        )
        session.commit()

    assert {result.table for result in results} >= {
        "llm_usage",
        "email_delivery",
        "webhook_delivery",
    }
    archive = tmp_path / "archive" / "llm_usage.jsonl.gz"
    with gzip.open(archive, "rt", encoding="utf-8") as fh:
        line = json.loads(fh.readline())
    assert line["id"] == "llm1"
    assert session.get(LlmUsage, "llm1") is None
