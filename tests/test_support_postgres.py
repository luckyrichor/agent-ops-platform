"""Real PostgreSQL including two independent OS processes, never a fake repository."""

import asyncio
import multiprocessing
import socket
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from opentelemetry.trace import get_tracer
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from testcontainers.community.postgres import PostgresContainer

from agent_ops.api import create_app
from agent_ops.support import SupportConflict, SupportRequest
from agent_ops.support_postgres import PostgresSupportService


@pytest.fixture(scope="module")
def database():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    pg = PostgresContainer("pgvector/pgvector:pg16", driver="psycopg")
    pg.with_bind_ports(5432, port)
    with pg:
        owner = pg.get_connection_url()
        config = Config("alembic.ini")
        config.attributes["database_url"] = owner
        command.upgrade(config, "head")
        engine = create_engine(owner, pool_pre_ping=True)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE ROLE ops_runtime LOGIN PASSWORD 'fixture-only' NOSUPERUSER NOBYPASSRLS"
                )
            )
            conn.execute(text("GRANT agent_ops_app TO ops_runtime"))
        url = (
            make_url(owner)
            .set(username="ops_runtime", password="fixture-only")
            .render_as_string(hide_password=False)
        )
        yield url, engine, pg, config
        engine.dispose()


def worker(url, key, start, queue):
    async def run():
        service = PostgresSupportService(url, get_tracer("pg-process"), "fixture-token")
        try:
            start.wait(20)
            tickets = await asyncio.gather(
                *(service.handle(SupportRequest(message="我要退款"), key) for _ in range(8))
            )
            queue.put([t.ticket_id for t in tickets])
        finally:
            await service.aclose()

    asyncio.run(run())


def test_two_processes_unique_key_and_atomic_audit(database, record_property):
    url, engine, _, _ = database
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    queue = ctx.Queue()
    key = "multi-" + str(uuid4())
    processes = [ctx.Process(target=worker, args=(url, key, start, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    try:
        start.set()
        results = [queue.get(timeout=45) for _ in processes]
        for process in processes:
            process.join(20)
            assert process.exitcode == 0
        ids = {item for group in results for item in group}
        assert len(ids) == 1
        record_property("processes", len(processes))
        record_property("requests", sum(len(group) for group in results))
        record_property("unique_tickets", len(ids))
        record_property("duplicate_tickets", len(ids) - 1)
        with engine.connect() as conn:
            tid = next(iter(ids))
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM support.tickets WHERE ticket_id=:id"), {"id": tid}
                )
                == 1
            )
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM support.ticket_events WHERE ticket_id=:id"),
                    {"id": tid},
                )
                == 1
            )
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM support.idempotency_keys WHERE key=:key"),
                    {"key": key},
                )
                == 1
            )
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()


async def test_restart_replay_snapshot_conflict_and_concurrent_resolve(database, record_property):
    url, owner_engine, pg, _ = database
    key = "restart-" + str(uuid4())
    request = SupportRequest(message="转人工")
    service = PostgresSupportService(url, get_tracer("restart"), "fixture-token")
    first = await service.handle(request, key)
    await service.aclose()
    # Restart the actual container, preserving data and explicitly bound host port.
    await asyncio.to_thread(pg.get_wrapped_container().restart)
    owner_engine.dispose()
    recreated = PostgresSupportService(url, get_tracer("restarted"), "fixture-token")
    try:
        for attempt in range(30):
            try:
                await recreated.get(first.ticket_id)
                break
            except Exception:
                if attempt == 29:
                    raise
                await asyncio.sleep(0.5)
        assert (await recreated.get(first.ticket_id)).model_dump() == first.model_dump()
        with pytest.raises(SupportConflict):
            await recreated.handle(SupportRequest(message="我要退款"), key)
        result = await asyncio.gather(
            *(recreated.resolve(first.ticket_id, 1, "fixture-token") for _ in range(8)),
            return_exceptions=True,
        )
        assert sum(not isinstance(r, BaseException) for r in result) == 1
        assert sum(isinstance(r, SupportConflict) for r in result) == 7
        assert (await recreated.get(first.ticket_id)).revision == 2
        assert (await recreated.handle(request, key)).revision == 1
        record_property("retained_after_database_restart", True)
        record_property(
            "successful_resolves", sum(not isinstance(r, BaseException) for r in result)
        )
        record_property("conflicted_resolves", sum(isinstance(r, SupportConflict) for r in result))
        record_property("current_revision", (await recreated.get(first.ticket_id)).revision)
        record_property("replayed_revision", (await recreated.handle(request, key)).revision)
        with pytest.raises(PermissionError):
            await recreated.resolve(first.ticket_id, 2, "bad")
    finally:
        await recreated.aclose()


async def test_event_insert_failure_rolls_back_key_and_ticket(database):
    url, engine, _, _ = database
    key = "rollback-" + str(uuid4())
    with engine.begin() as conn:
        before = conn.scalar(text("SELECT count(*) FROM support.tickets"))
        conn.execute(
            text("""CREATE FUNCTION support.test_reject_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected event failure'; END $$;
            CREATE TRIGGER injected_failure BEFORE INSERT ON support.ticket_events
            FOR EACH ROW EXECUTE FUNCTION support.test_reject_insert()""")
        )
    service = PostgresSupportService(url, get_tracer("atomic"))
    try:
        with pytest.raises(DBAPIError):
            await service.handle(SupportRequest(message="转人工"), key)
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM support.tickets")) == before
            assert (
                conn.scalar(
                    text("SELECT count(*) FROM support.idempotency_keys WHERE key=:key"),
                    {"key": key},
                )
                == 0
            )
    finally:
        await service.aclose()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DROP TRIGGER injected_failure ON support.ticket_events; DROP FUNCTION support.test_reject_insert()"
                )
            )


async def test_http_pg_backend_trace_and_append_only(database, monkeypatch):
    url, engine, _, _ = database
    monkeypatch.setenv("AGENT_OPS_SUPPORT_BACKEND", "postgres")
    monkeypatch.setenv("AGENT_OPS_DATABASE_URL", url)
    monkeypatch.setenv("SUPPORT_HUMAN_TOKEN", "fixture-token")
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client,
    ):
        result = await client.post(
            "/support/requests",
            json={"message": "转人工"},
            headers={"Idempotency-Key": "http-" + str(uuid4())},
        )
        assert result.status_code == 200 and result.headers["x-trace-id"]
        tid = result.json()["ticket_id"]
        missing = await client.get("/support/tickets/not-a-uuid")
        assert missing.status_code == 404
        result = await client.post(
            f"/support/tickets/{tid}/resolve",
            json={"expected_revision": 1},
            headers={"Authorization": "Bearer fixture-token"},
        )
        assert result.status_code == 200 and result.json()["revision"] == 2
    # Trigger protects the audit even against the schema owner; runtime has no delete grant.
    with engine.connect() as conn:
        with pytest.raises(DBAPIError):
            conn.execute(
                text("UPDATE support.ticket_events SET actor=actor WHERE ticket_id=:id"),
                {"id": tid},
            )
        conn.rollback()
    import psycopg

    with (
        psycopg.connect(url.replace("postgresql+psycopg:", "postgresql:")) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("DELETE FROM support.ticket_events")


def test_migration_downgrade_and_reupgrade():
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        config = Config("alembic.ini")
        config.attributes["database_url"] = pg.get_connection_url()
        for target in ["head", "base", "head"]:
            if target == "base":
                command.downgrade(config, target)
                probe = create_engine(pg.get_connection_url())
                with probe.connect() as conn:
                    assert (
                        conn.scalar(
                            text(
                                "SELECT count(*) FROM information_schema.tables WHERE table_schema='support'"
                            )
                        )
                        == 0
                    )
                probe.dispose()
            else:
                command.upgrade(config, target)
        engine = create_engine(pg.get_connection_url())
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0003_proxy_provenance"
        engine.dispose()
