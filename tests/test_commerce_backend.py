"""P2 real DB invariants; no fabricated dataset or live model calls."""

import asyncio

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

from agent_ops.commerce_backend import CommerceBackend, CommerceConflict, PlaceOrder


@pytest.fixture(scope="module")
def commerce_db():
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        owner = pg.get_connection_url()
        config = Config("alembic.ini")
        config.attributes["database_url"] = owner
        command.upgrade(config, "head")
        engine = create_engine(owner)
        with engine.begin() as c:
            c.execute(
                text(
                    "CREATE ROLE commerce_test LOGIN PASSWORD 'fixture-only' NOSUPERUSER NOBYPASSRLS"
                )
            )
            c.execute(text("GRANT agent_ops_app TO commerce_test"))
            c.execute(text("INSERT INTO commerce.users(id) VALUES (1),(2)"))
            c.execute(text("INSERT INTO commerce.categories VALUES (1,'fixture')"))
            c.execute(
                text(
                    "INSERT INTO commerce.products(id,sku,title,category_id,price_cents,freight_cents,status) VALUES (1,'fixture1','fixture',1,1000,10,'active'),(2,'fixture2','fixture',1,2000,20,'active'),(3,'fixture3','fixture',1,100,0,'active')"
                )
            )
            c.execute(
                text("INSERT INTO commerce.inventory(product_id,stock) VALUES (1,10),(2,1),(3,10)")
            )
        runtime = (
            make_url(owner)
            .set(username="commerce_test", password="fixture-only")
            .render_as_string(hide_password=False)
        )
        yield runtime, engine
        engine.dispose()


async def test_atomic_order_idempotency_inventory_and_cancellation(commerce_db):
    url, engine = commerce_db
    service = CommerceBackend(url)
    try:
        req = PlaceOrder.model_validate({"user_id": 1, "lines": [{"product_id": 1, "quantity": 2}]})
        result = await asyncio.gather(*(service.place_order(req, "same-key") for _ in range(16)))
        assert len({r["order_id"] for r in result}) == 1
        assert result[0]["total_cents"] == 2020
        with engine.connect() as c:
            assert c.scalar(text("SELECT reserved FROM commerce.inventory WHERE product_id=1")) == 2
        with pytest.raises(CommerceConflict, match="IDEMPOTENCY"):
            await service.place_order(req.model_copy(update={"user_id": 2}), "same-key")
        with pytest.raises(KeyError):
            await service.order(result[0]["order_id"], 2)
        resolved = await asyncio.gather(
            *(service.cancel_order(result[0]["order_id"], 1, 1) for _ in range(8)),
            return_exceptions=True,
        )
        assert sum(isinstance(r, dict) for r in resolved) == 1
        with engine.connect() as c:
            assert c.scalar(text("SELECT reserved FROM commerce.inventory WHERE product_id=1")) == 0
    finally:
        await service.aclose()


async def test_no_oversell_and_multi_item_failure_rolls_back(commerce_db):
    url, engine = commerce_db
    service = CommerceBackend(url)
    try:
        req = PlaceOrder.model_validate({"user_id": 1, "lines": [{"product_id": 2, "quantity": 1}]})
        result = await asyncio.gather(
            *(service.place_order(req, f"unique-{i}") for i in range(8)), return_exceptions=True
        )
        assert sum(isinstance(r, dict) for r in result) == 1
        fail = PlaceOrder.model_validate(
            {
                "user_id": 1,
                "lines": [{"product_id": 1, "quantity": 1}, {"product_id": 2, "quantity": 1}],
            }
        )
        with pytest.raises(CommerceConflict):
            await service.place_order(fail, "failed-basket")
        with engine.connect() as c:
            assert c.scalar(text("SELECT reserved FROM commerce.inventory WHERE product_id=1")) == 0
            assert (
                c.scalar(
                    text("SELECT count(*) FROM commerce.idempotency_keys WHERE key='failed-basket'")
                )
                == 0
            )
        result = await asyncio.gather(
            *(service.reserve(3, 1, 1) for _ in range(8)), return_exceptions=True
        )
        assert sum(isinstance(r, int) for r in result) == 1
        assert sum(isinstance(r, CommerceConflict) for r in result) == 7
        assert len(await service.products(limit=1)) == 1
        with pytest.raises(ValueError):
            await service.products(limit=101)
    finally:
        await service.aclose()


def test_partitioned_message_global_sequence_unique_and_migration_rollback():
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        url = pg.get_connection_url()
        cfg = Config("alembic.ini")
        cfg.attributes["database_url"] = url
        command.upgrade(cfg, "head")
        with psycopg.connect(url.replace("postgresql+psycopg:", "postgresql:")) as c:
            c.execute("INSERT INTO commerce.users(id) VALUES(1)")
            c.execute("INSERT INTO commerce.conversations(id,user_id) VALUES(1,1)")
            c.execute("INSERT INTO commerce.message_keys VALUES(1,1,'2025-01-01')")
            c.execute("INSERT INTO commerce.messages VALUES(1,1,'2025-01-01','source','fixture')")
            c.commit()
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                c.execute(
                    "INSERT INTO commerce.messages VALUES(1,1,'2025-02-01','source','duplicate cross partition')"
                )
            c.rollback()
        command.downgrade(cfg, "0001_support")
        engine = create_engine(url)
        with engine.connect() as c:
            assert (
                c.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='commerce'"
                    )
                )
                == 0
            )
            assert (
                c.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='support'"
                    )
                )
                > 0
            )
        command.upgrade(cfg, "head")
        engine.dispose()
