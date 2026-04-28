"""Migration smoke for cd-jkwr inventory item schema changes."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.db.inventory.models import Item
from app.adapters.db.places.models import Property, PropertyWorkspace
from app.adapters.db.session import make_engine
from app.adapters.db.workspace.models import Workspace
from app.config import get_settings
from app.tenancy import tenant_agnostic
from tests.factories.identity import bootstrap_user, bootstrap_workspace

pytestmark = pytest.mark.integration

_PINNED = datetime(2026, 4, 28, 16, 0, tzinfo=UTC)
_PREVIOUS_REVISION_ID = "f4a6b8c0d2e4"


def _alembic_ini() -> Path:
    return Path(__file__).resolve().parents[2] / "alembic.ini"


@contextmanager
def _override_database_url(url: str) -> Iterator[None]:
    original = os.environ.get("CREWDAY_DATABASE_URL")
    os.environ["CREWDAY_DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("CREWDAY_DATABASE_URL", None)
        else:
            os.environ["CREWDAY_DATABASE_URL"] = original
        get_settings.cache_clear()


class TestInventoryItemsMigration:
    """cd-jkwr migration is reversible around SKU shape changes."""

    def test_downgrade_normalizes_property_scoped_and_null_skus(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        db_path = tmp_path_factory.mktemp("cd-jkwr-mig-down") / "mig.db"
        url = f"sqlite:///{db_path}"
        engine = make_engine(url)
        try:
            with _override_database_url(url):
                cfg = AlembicConfig(str(_alembic_ini()))
                cfg.set_main_option("sqlalchemy.url", url)
                command.upgrade(cfg, "head")

                factory = sessionmaker(bind=engine, expire_on_commit=False)
                with factory() as session:
                    workspace = _seed_workspace(session)
                    prop_a = _seed_property(session, workspace.id, "prop-a")
                    prop_b = _seed_property(session, workspace.id, "prop-b")
                    session.add_all(
                        [
                            Item(
                                id="01HWA00000000000000000JWA",
                                workspace_id=workspace.id,
                                property_id=prop_a,
                                sku="DUP",
                                name="Duplicate A",
                                unit="each",
                                created_at=_PINNED,
                            ),
                            Item(
                                id="01HWA00000000000000000JWB",
                                workspace_id=workspace.id,
                                property_id=prop_b,
                                sku="DUP",
                                name="Duplicate B",
                                unit="each",
                                created_at=_PINNED,
                            ),
                            Item(
                                id="01HWA00000000000000000JWC",
                                workspace_id=workspace.id,
                                property_id=prop_a,
                                sku=None,
                                name="No SKU",
                                unit="each",
                                created_at=_PINNED,
                            ),
                        ]
                    )
                    session.commit()

                command.downgrade(cfg, _PREVIOUS_REVISION_ID)

            insp = inspect(engine)
            cols = {c["name"]: c for c in insp.get_columns("inventory_item")}
            assert "property_id" not in cols
            assert cols["sku"]["nullable"] is False

            with Session(engine) as session:
                skus = session.scalars(
                    select(Item.sku).order_by(Item.id)
                ).all()
            assert len(skus) == len(set(skus))
            assert all(skus)
            assert skus[0] == "DUP"
        finally:
            engine.dispose()


def _seed_workspace(session: Session) -> Workspace:
    user = bootstrap_user(
        session,
        email="cd-jkwr-migration@example.com",
        display_name="Migration",
    )
    return bootstrap_workspace(
        session,
        slug="cd-jkwr-migration",
        name="Migration",
        owner_user_id=user.id,
    )


def _seed_property(session: Session, workspace_id: str, suffix: str) -> str:
    property_id = f"01HWA0000000000000000{suffix.replace('-', '').upper()}"
    with tenant_agnostic():
        session.add(
            Property(
                id=property_id,
                address=f"{suffix} address",
                timezone="UTC",
                tags_json=[],
                created_at=_PINNED,
            )
        )
        session.flush()
        session.add(
            PropertyWorkspace(
                property_id=property_id,
                workspace_id=workspace_id,
                label=suffix,
                membership_role="owner_workspace",
                status="active",
                created_at=_PINNED,
            )
        )
        session.flush()
    return property_id
