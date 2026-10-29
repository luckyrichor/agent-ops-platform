"""P4 real PostgreSQL transactions, ownership, leases, audit and replay. No seed datasets."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from opentelemetry.trace import get_tracer
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

from agent_ops.api import create_app
from agent_ops.auth import AuthService, Principal, TokenCodec
from agent_ops.auth_postgres import PostgresAccountStore
from agent_ops.support import SupportConflict, SupportRequest
from agent_ops.workbench import WorkbenchService

PASSWORD = "fixture-workbench-password"
SECRET = "fixture-workbench-signing-secret-" + "w" * 32


@pytest.fixture(scope="module")
def database():
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        owner = pg.get_connection_url()
        config = Config("alembic.ini")
        config.attributes["database_url"] = owner
        command.upgrade(config, "head")
        engine = create_engine(owner)
        with engine.begin() as c:
            c.execute(
                text("CREATE ROLE wb_runtime LOGIN PASSWORD 'fixture-only' NOSUPERUSER NOBYPASSRLS")
            )
            c.execute(text("GRANT agent_ops_app TO wb_runtime"))
            c.execute(text("INSERT INTO commerce.users(id) VALUES(1),(2)"))
            c.execute(
                text(
                    "INSERT INTO commerce.orders(id,user_id,status,total_cents,created_at) VALUES(11,1,'shipped',1234,now()),(21,2,'delivered',5678,now())"
                )
            )
            c.execute(text("INSERT INTO commerce.shipments VALUES(11,'fixture','FX11','shipped')"))
        runtime = (
            make_url(owner)
            .set(username="wb_runtime", password="fixture-only")
            .render_as_string(hide_password=False)
        )

        async def accounts():
            store = PostgresAccountStore(runtime)
            auth = AuthService(store, TokenCodec(SECRET))
            try:
                for name, role, user in [
                    ("c1", "customer", 1),
                    ("c2", "customer", 2),
                    ("a1", "agent", None),
                    ("a2", "agent", None),
                    ("admin", "admin", None),
                ]:
                    await auth.create_account(name + "@wb.invalid", PASSWORD, role, user)
            finally:
                await store.aclose()

        asyncio.run(accounts())
        yield runtime, engine, config
        engine.dispose()


@pytest.fixture
async def api(database, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_SUPPORT_BACKEND", "postgres")
    monkeypatch.setenv("AGENT_OPS_DATABASE_URL", database[0])
    monkeypatch.setenv("AGENT_OPS_AUTH_SECRET", SECRET)
    app = create_app(lifecycle_interval=0)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        headers = {}
        for name in ["c1", "c2", "a1", "a2", "admin"]:
            r = await client.post(
                "/auth/login", json={"email": name + "@wb.invalid", "password": PASSWORD}
            )
            assert r.status_code == 200
            headers[name] = {"Authorization": "Bearer " + r.json()["access_token"]}
        yield client, headers, app.state.support


async def create(api, message="转人工", order="11", customer="c1"):
    client, headers, _ = api
    r = await client.post(
        "/support/requests",
        json={"message": message, "order_id": order},
        headers={**headers[customer], "Idempotency-Key": str(uuid4())},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def post(api, tid, op, rev, who="a1", key=None, **extra):
    client, headers, _ = api
    prefix = "support" if op in {"information", "messages"} else "workbench"
    return await client.post(
        f"/{prefix}/tickets/{tid}/{op}",
        json={"expected_revision": rev, **extra},
        headers={**headers[who], "Idempotency-Key": key or str(uuid4())},
    )


async def test_real_orders_missing_information_and_returns(api):
    found = await create(api, "查物流")
    assert (
        found["state"] == "resolved"
        and found["order_id"] == 11
        and found["order_status"] == "shipped"
    )
    for order in [None, "21", "demo-100", str(2**64)]:
        missing = await create(api, "查物流", order)
        assert missing["state"] == "needs_information" and missing["order_id"] is None
        wrong = await post(api, missing["ticket_id"], "information", 1, "c2", order_id=21)
        assert wrong.status_code == 404
        wrong = await post(api, missing["ticket_id"], "information", 1, "c1", order_id=21)
        assert wrong.status_code == 404
        key = str(uuid4())
        filled = await post(api, missing["ticket_id"], "information", 1, "c1", key, order_id=11)
        assert (
            filled.status_code == 200
            and filled.json()["state"] == "resolved"
            and filled.json()["revision"] == 2
        )
        assert (
            await post(api, missing["ticket_id"], "information", 1, "c1", key, order_id=11)
        ).json() == filled.json()
        assert (
            await post(api, missing["ticket_id"], "information", 2, "c1", order_id=11)
        ).status_code == 409
    client, headers, _ = api
    missing = (
        await client.post(
            "/support/returns",
            json={"message": "request"},
            headers={**headers["c1"], "Idempotency-Key": str(uuid4())},
        )
    ).json()
    assert missing["state"] == "waiting_human"
    filled = await post(api, missing["ticket_id"], "information", 1, "c1", order_id=11)
    assert filled.json()["state"] == "waiting_human"
    direct = (
        await client.post(
            "/support/returns",
            json={"message": "request", "order_id": "11"},
            headers={**headers["c1"], "Idempotency-Key": str(uuid4())},
        )
    ).json()
    assert direct["state"] == "waiting_human" and direct["route"] == "return_request"
    # P6 exposes gated payment APIs; an unconfigured P4 fixture cannot move money.
    denied = await client.post(f"/workbench/tickets/{direct['ticket_id']}/refund",
        json={"expected_revision": direct["revision"]},
        headers={**headers["a1"], "Idempotency-Key": str(uuid4())})
    assert denied.status_code == 503 and denied.json()["detail"] == "PAYMENTS_NOT_CONFIGURED"


async def test_claim_one_winner_across_connections_and_operation_replay(api, database):
    _, _, service = api
    t = await create(api)
    other = WorkbenchService(database[0], get_tracer("other-process"))
    try:
        results = await asyncio.gather(
            *[
                (service if i % 2 else other).change(
                    t["ticket_id"],
                    "claim",
                    1,
                    Principal(3 + i % 2, "agent", None, "fixture"),
                    str(uuid4()),
                )
                for i in range(20)
            ],
            return_exceptions=True,
        )
        assert sum(not isinstance(r, BaseException) for r in results) == 1
        assert sum(isinstance(r, SupportConflict) for r in results) == 19
        ticket = await service.get(t["ticket_id"])
        assert ticket.state == "claimed" and ticket.revision == 2
        assert [r["type"] for r in await service.audit(t["ticket_id"], 100, 0)] == [
            "created",
            "claimed",
        ]
    finally:
        await other.aclose()
    t = await create(api)
    key = str(uuid4())
    first = await post(api, t["ticket_id"], "claim", 1, key=key)
    assert first.status_code == 200
    assert (await post(api, t["ticket_id"], "claim", 1, key=key)).json() == first.json()
    assert (await post(api, t["ticket_id"], "claim", 2, key=key)).status_code == 409


async def test_claim_holder_messages_transitions_and_role_labels(api, database):
    client, headers, service = api
    t = await create(api)
    tid = t["ticket_id"]
    assert (await post(api, tid, "resolve", 1)).status_code == 403
    claimed = await post(api, tid, "claim", 1)
    assert claimed.status_code == 200
    assert (await post(api, tid, "reply", 2, "a2", message="wrong seat")).status_code == 403
    assert (await post(api, tid, "claim", 2, "c1")).status_code == 403
    customer = await post(api, tid, "messages", 2, "c1", message="customer update")
    assert customer.status_code == 200
    reply = await post(api, tid, "reply", 3, message="agent reply")
    assert reply.status_code == 200 and reply.json()["revision"] == 4
    messages = (await client.get(f"/support/tickets/{tid}/messages", headers=headers["c1"])).json()[
        "items"
    ]
    assert [(m["sender_role"], m["content"]) for m in messages] == [
        ("customer", "转人工"),
        ("system", "HUMAN_APPROVAL_REQUIRED"),
        ("customer", "customer update"),
        ("agent", "agent reply"),
    ]
    assert (
        await client.get(f"/support/tickets/{tid}/messages", headers=headers["c2"])
    ).status_code == 404
    released = await post(api, tid, "release", 4)
    assert (
        released.json()["state"] == "waiting_human"
        and released.json()["assigned_account_id"] is None
    )
    assert (await post(api, tid, "resolve", 5)).status_code == 403
    assert (await post(api, tid, "claim", 5, "a2")).json()["revision"] == 6
    done = await post(api, tid, "resolve", 6, "a2")
    assert done.json()["state"] == "resolved" and done.json()["revision"] == 7
    assert (await post(api, tid, "claim", 7)).status_code == 409
    assert (await post(api, tid, "messages", 7, "c1", message="reopen")).status_code == 409
    audit = await service.audit(tid, 100, 0)
    assert [r["revision"] for r in audit] == list(range(1, 8))
    assert [r["type"] for r in audit] == [
        "created",
        "claimed",
        "message_created",
        "message_created",
        "released",
        "claimed",
        "resolved",
    ]
    assert audit[1]["payload"]["assigned_account_id"] == 3
    assert audit[1]["payload"]["lease_expires_at"]
    assert audit[0]["payload"]["order_id"] == 11 and len(audit[0]["payload"]["message_ids"]) == 2
    assert all("agent reply" not in str(r) and "customer update" not in str(r) for r in audit)


async def test_lease_renew_expiry_and_concurrent_reaper(api, database):
    _, _, service = api
    t = await create(api)
    tid = t["ticket_id"]
    await post(api, tid, "claim", 1)
    with database[1].begin() as c:
        c.execute(
            text(
                "UPDATE support.tickets SET lease_expires_at=now()+interval '1 minute' WHERE ticket_id=:id"
            ),
            {"id": tid},
        )
    reply = await post(api, tid, "reply", 2, message="renew")
    assert reply.status_code == 200
    with database[1].connect() as c:
        assert c.scalar(
            text(
                "SELECT lease_expires_at>now()+interval '9 minutes' FROM support.tickets WHERE ticket_id=:id"
            ),
            {"id": tid},
        )
    detail = await api[0].get(f"/workbench/tickets/{tid}", headers=api[1]["a1"])
    assert detail.json()["ticket"]["revision"] == 3  # renewal is operational, not a business revision
    with database[1].begin() as c:
        c.execute(
            text(
                "UPDATE support.tickets SET lease_expires_at=now()-interval '1 second' WHERE ticket_id=:id"
            ),
            {"id": tid},
        )
    assert (await post(api, tid, "reply", 3, message="late")).status_code == 409
    other = WorkbenchService(database[0], get_tracer("reaper-two"))
    try:
        await asyncio.gather(service.maintain(), other.maintain())
    finally:
        await other.aclose()
    freed = await service.get(tid)
    assert (
        freed.state == "waiting_human" and freed.revision == 4 and freed.assigned_account_id is None
    )
    assert len([r for r in await service.audit(tid, 100, 0) if r["type"] == "released"]) == 1
    assert (await post(api, tid, "claim", 4, "a2")).status_code == 200


async def test_sla_warning_overdue_only_once_and_stats(api, database):
    client, headers, service = api
    t = await create(api)
    tid = t["ticket_id"]
    with database[1].begin() as c:
        c.execute(
            text(
                "UPDATE support.tickets SET sla_due_at=now()-interval '1 second' WHERE ticket_id=:id"
            ),
            {"id": tid},
        )
    await asyncio.gather(service.maintain(), service.maintain())
    await service.maintain()
    ticket = await service.get(tid)
    assert ticket.sla_warned and ticket.sla_overdue and ticket.revision == 3
    assert [r["type"] for r in await service.audit(tid, 100, 0)] == [
        "created",
        "sla_warning",
        "sla_overdue",
    ]
    stats = await client.get("/admin/support-stats", headers=headers["admin"])
    assert (
        stats.status_code == 200
        and stats.json()["sla_overdue"] >= 1
        and stats.json()["queue_length"] >= 1
    )
    for who in ["c1", "a1"]:
        assert (await client.get("/admin/support-stats", headers=headers[who])).status_code == 403
    events = await service.events(Principal(3, "agent", None, "fixture"), 0)
    assert any(
        e["kind"] == "ticket_sla_warning" and e["payload"]["ticket_id"] == tid for e in events
    )


async def test_order_only_linked_to_ticket_each_view_audited(api, database):
    client, headers, service = api
    t = await create(api)
    tid = t["ticket_id"]
    for _ in range(2):
        r = await client.get(f"/workbench/tickets/{tid}/order", headers=headers["a1"])
        assert (
            r.status_code == 200
            and r.json()["order"]["id"] == 11
            and r.json()["order"]["user_id"] == 1
        )
        assert r.json()["order"]["shipment"]["tracking_no"] == "FX11"
    assert (await service.get(tid)).revision == 3
    assert len([r for r in await service.audit(tid, 100, 0) if r["type"] == "order_viewed"]) == 2
    assert (await client.get("/orders/11", headers=headers["a1"])).status_code == 403
    assert (
        await client.get(f"/workbench/tickets/{tid}/order", headers=headers["c1"])
    ).status_code == 403
    assert (
        await client.get(f"/workbench/tickets/{tid}/order?order_id=21", headers=headers["a1"])
    ).json()["order"]["id"] == 11
    with database[1].begin() as c:
        c.execute(text("UPDATE support.tickets SET order_id=21 WHERE ticket_id=:id"), {"id": tid})
    assert (
        await client.get(f"/workbench/tickets/{tid}/order", headers=headers["a1"])
    ).status_code == 404
    assert (await service.get(tid)).revision == 4


async def test_seed_sender_unchanged_and_new_messages_labeled(api, database):
    client, headers, _ = api
    t = await create(api)
    tid = t["ticket_id"]
    with database[1].begin() as c:
        c.execute(
            text("INSERT INTO commerce.conversations(id,user_id,ticket_id) VALUES(8001,1,:tid)"),
            {"tid": tid},
        )
        c.execute(text("INSERT INTO commerce.message_keys VALUES(8001,1,'2025-01-01')"))
        c.execute(
            text(
                "INSERT INTO commerce.messages VALUES(8001,1,'2025-01-01','source','ECD fixture unchanged')"
            )
        )
    messages = (await client.get(f"/support/tickets/{tid}/messages", headers=headers["c1"])).json()[
        "items"
    ]
    assert messages[0]["origin"] == "seed" and messages[0]["sender_role"] == "source"
    assert {m["sender_role"] for m in messages if m["origin"] == "live"} == {"customer", "system"}
    with database[1].connect() as c:
        assert (
            c.scalar(text("SELECT sender FROM commerce.messages WHERE conversation_id=8001"))
            == "source"
        )


async def test_offset_queue_stable_and_no_s1_index(api, database):
    client, headers, service = api
    baseline = await service.queue(100, 0)
    t = await create(api)
    first = (await client.get("/workbench/tickets?limit=2&offset=0", headers=headers["a1"])).json()
    second = (await client.get("/workbench/tickets?limit=2&offset=2", headers=headers["a1"])).json()
    expected = await service.queue(4, 0)
    assert [r["ticket_id"] for r in first["items"] + second["items"]] == [
        r.ticket_id for r in expected
    ]
    assert first["offset"] == 0 and second["offset"] == 2 and "next_cursor" not in first
    assert len(await service.queue(100, 0)) == len(baseline) + 1
    with database[1].connect() as c:
        indices = (
            c.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname='support' AND tablename='tickets'"
                )
            )
            .scalars()
            .all()
        )
    assert not any("waiting_human" in i for i in indices)
    assert t["ticket_id"]


async def test_audit_failure_rolls_back_message_operation_and_push(api, database):
    from sqlalchemy.exc import DBAPIError

    _, _, service = api
    t = await create(api)
    tid = t["ticket_id"]
    await post(api, tid, "claim", 1)
    before = await service.last_sequence()
    with database[1].begin() as c:
        c.execute(
            text(f"""CREATE FUNCTION support.fail_p4_event() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.ticket_id='{tid}'::uuid THEN RAISE EXCEPTION 'injected failure'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER injected_p4 BEFORE INSERT ON support.ticket_events FOR EACH ROW EXECUTE FUNCTION support.fail_p4_event()""")
        )
    key = str(uuid4())
    try:
        with pytest.raises(DBAPIError):
            await service.change(
                tid,
                "reply",
                2,
                Principal(3, "agent", None, "fixture"),
                key,
                message="not committed",
            )
    finally:
        with database[1].begin() as c:
            c.execute(
                text(
                    "DROP TRIGGER injected_p4 ON support.ticket_events; DROP FUNCTION support.fail_p4_event()"
                )
            )
    assert (await service.get(tid)).revision == 2 and await service.last_sequence() == before
    assert not any(
        m["content"] == "not committed"
        for m in await service.messages(tid, Principal(3, "agent", None, "fixture"), 100, 0)
    )
    retry = await service.change(
        tid, "reply", 2, Principal(3, "agent", None, "fixture"), key, message="not committed"
    )
    assert retry.revision == 3


async def test_durable_sequence_orders_commits_and_isolation(api, database):
    _, _, service = api
    t = await create(api)
    ticket = await service.get(t["ticket_id"])
    before = await service.last_sequence()
    async with service.engine.connect() as first:
        await first.begin()
        await service._push(first, ticket, "fixture-first", staff=True)

        async def later():
            async with service.engine.begin() as second:
                await service._push(second, ticket, "fixture-second", staff=True)

        task = asyncio.create_task(later())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        assert await service.last_sequence() == before  # uncommitted sequence invisible
        await first.commit()
        await asyncio.wait_for(task, 3)
    events = await service.events(Principal(3, "agent", None, "fixture"), before)
    assert [e["kind"] for e in events] == ["fixture-first", "fixture-second"]
    assert [e["sequence"] for e in events] == [before + 1, before + 2]
    assert await service.events(Principal(2, "customer", 2, "fixture"), before) == []
    recreated = WorkbenchService(database[0], get_tracer("restart"))
    try:
        assert await recreated.events(Principal(3, "agent", None, "fixture"), before) == events
    finally:
        await recreated.aclose()


async def test_real_tcp_websocket_customer_to_seat_reconnect_and_complete_audit(database, tmp_path):
    import json
    import os
    import socket
    import subprocess
    import sys

    from websockets.asyncio.client import connect

    ports = []
    for _ in range(2):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
    bases = [f"http://127.0.0.1:{p}" for p in ports]
    env = {
        **os.environ,
        "AGENT_OPS_SUPPORT_BACKEND": "postgres",
        "AGENT_OPS_DATABASE_URL": database[0],
        "AGENT_OPS_AUTH_SECRET": SECRET,
        "AGENT_OPS_PLANNER": "deterministic",
        "AGENT_OPS_TRACE_EXPORTER": "none",
    }
    env.pop("ARK_API_KEY", None)
    logs = tmp_path / "servers.log"
    processes = []
    service = WorkbenchService(database[0], get_tracer("e2e-reader"))
    token_values = []
    try:
        with logs.open("w") as stream:
            for port in ports:
                processes.append(
                    await asyncio.to_thread(
                        subprocess.Popen,
                        [
                            sys.executable,
                            "-m",
                            "uvicorn",
                            "agent_ops.api:create_app",
                            "--factory",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(port),
                            "--no-access-log",
                            "--log-level",
                            "warning",
                        ],
                        env=env,
                        stdout=stream,
                        stderr=stream,
                    )
                )
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                for base in bases:
                    for _ in range(100):
                        try:
                            if (await client.get(base + "/auth/me")).status_code == 401:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)
                    else:
                        pytest.fail("TCP server did not start")
                sessions = {}
                for name in ["c1", "c2", "a1", "admin"]:
                    r = await client.post(
                        bases[0] + "/auth/login",
                        json={"email": name + "@wb.invalid", "password": PASSWORD},
                    )
                    assert r.status_code == 200
                    sessions[name] = r.json()
                    token_values.extend([r.json()["access_token"], r.json()["refresh_token"]])

                def headers(who, key=None):
                    return {
                        "Authorization": "Bearer " + sessions[who]["access_token"],
                        **({"Idempotency-Key": key} if key else {}),
                    }

                after = await service.last_sequence()
                uri0 = f"ws://127.0.0.1:{ports[0]}/ws"
                uri1 = f"ws://127.0.0.1:{ports[1]}/ws"
                seen = []
                async with connect(uri0) as seat, connect(uri1) as customer:
                    await seat.send(
                        json.dumps({"access_token": sessions["a1"]["access_token"], "after": after})
                    )
                    await customer.send(
                        json.dumps({"access_token": sessions["c1"]["access_token"], "after": after})
                    )
                    ticket = await client.post(
                        bases[0] + "/support/requests",
                        json={"message": "我要退货", "order_id": "11"},
                        headers=headers("c1", str(uuid4())),
                    )
                    assert ticket.status_code == 200
                    tid = ticket.json()["ticket_id"]
                    created = json.loads(await asyncio.wait_for(seat.recv(), 3))
                    assert (
                        created["kind"] == "ticket_created"
                        and created["payload"]["ticket_id"] == tid
                    )
                    seen.append(created["sequence"])
                    claimed = await client.post(
                        bases[1] + f"/workbench/tickets/{tid}/claim",
                        json={"expected_revision": 1},
                        headers=headers("a1", "ws-claim-" + tid),
                    )
                    assert claimed.status_code == 200
                    event = json.loads(await asyncio.wait_for(seat.recv(), 3))
                    assert event["kind"] == "ticket_claimed"
                    seen.append(event["sequence"])
                    last = event["sequence"]
                    customer_created = json.loads(await asyncio.wait_for(customer.recv(), 3))
                    customer_claimed = json.loads(await asyncio.wait_for(customer.recv(), 3))
                    assert [e["kind"] for e in [customer_created, customer_claimed]] == [
                        "ticket_status_changed"
                    ] * 2
                # Seat disconnected. Mutate on two different OS workers while no seat socket exists.
                key = "ws-reply-" + tid
                reply = await client.post(
                    bases[0] + f"/workbench/tickets/{tid}/reply",
                    json={"expected_revision": 2, "message": "客服真实回复"},
                    headers=headers("a1", key),
                )
                assert reply.status_code == 200 and reply.json()["revision"] == 3
                resolved = await client.post(
                    bases[1] + f"/workbench/tickets/{tid}/resolve",
                    json={"expected_revision": 3},
                    headers=headers("a1", "ws-resolve-" + tid),
                )
                assert resolved.status_code == 200 and resolved.json()["revision"] == 4
                repeated = await client.post(
                    bases[1] + f"/workbench/tickets/{tid}/reply",
                    json={"expected_revision": 2, "message": "客服真实回复"},
                    headers=headers("a1", key),
                )
                assert repeated.json() == reply.json()
                # Resume on a different worker: after is strict, no replay of the claimed event.
                async with connect(uri1) as resumed:
                    await resumed.send(
                        json.dumps({"access_token": sessions["a1"]["access_token"], "after": last})
                    )
                    events = [
                        json.loads(await asyncio.wait_for(resumed.recv(), 3)) for _ in range(2)
                    ]
                    assert [e["kind"] for e in events] == ["message_created", "ticket_resolved"]
                    assert all(e["payload"]["ticket_id"] == tid for e in events)
                    seen.extend(e["sequence"] for e in events)
                    await resumed.send("ping")
                    assert json.loads(await asyncio.wait_for(resumed.recv(), 3)) == {"type": "pong"}
                assert seen == sorted(set(seen))
                # Customer reconnect sees only own events, including reply and resolution.
                async with connect(uri0) as customer:
                    await customer.send(
                        json.dumps(
                            {
                                "access_token": sessions["c1"]["access_token"],
                                "after": customer_claimed["sequence"],
                            }
                        )
                    )
                    events = [
                        json.loads(await asyncio.wait_for(customer.recv(), 3)) for _ in range(2)
                    ]
                    assert [e["kind"] for e in events] == [
                        "message_created",
                        "ticket_status_changed",
                    ]
                async with connect(uri1) as other:
                    await other.send(
                        json.dumps({"access_token": sessions["c2"]["access_token"], "after": after})
                    )
                    await other.send("ping")
                    assert json.loads(await asyncio.wait_for(other.recv(), 3)) == {"type": "pong"}
                audit = await client.get(
                    bases[0] + f"/workbench/tickets/{tid}/events", headers=headers("a1")
                )
                assert [e["revision"] for e in audit.json()["items"]] == [1, 2, 3, 4]
                assert [e["type"] for e in audit.json()["items"]] == [
                    "created",
                    "claimed",
                    "message_created",
                    "resolved",
                ]
                messages = await client.get(
                    bases[1] + f"/support/tickets/{tid}/messages", headers=headers("c1")
                )
                assert any(
                    m["content"] == "客服真实回复" and m["sender_role"] == "agent"
                    for m in messages.json()["items"]
                )
                # Wrong token and cursor are closed, never allowed to receive the journal.
                from websockets.exceptions import ConnectionClosed

                for first in [
                    {"access_token": "invalid", "after": 0},
                    {"access_token": sessions["a1"]["access_token"], "after": 2**62},
                ]:
                    async with connect(uri0) as invalid:
                        await invalid.send(json.dumps(first))
                        with pytest.raises(ConnectionClosed):
                            await asyncio.wait_for(invalid.recv(), 3)
    finally:
        await service.aclose()
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 5)
    content = logs.read_text()
    assert PASSWORD not in content and SECRET not in content and "Bearer " not in content
    assert not any(token in content for token in token_values)


async def test_lifespan_runs_lease_and_sla_worker_without_manual_trigger(database, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_SUPPORT_BACKEND", "postgres")
    monkeypatch.setenv("AGENT_OPS_DATABASE_URL", database[0])
    monkeypatch.setenv("AGENT_OPS_AUTH_SECRET", SECRET)
    app = create_app(lifecycle_interval=0.02)
    async with app.router.lifespan_context(app):
        service = app.state.support
        t = await service.handle(SupportRequest(message="转人工"), str(uuid4()), 1)
        await service.change(
            t.ticket_id, "claim", 1, Principal(3, "agent", None, "fixture"), str(uuid4())
        )
        with database[1].begin() as c:
            c.execute(
                text(
                    "UPDATE support.tickets SET lease_expires_at=now()-interval '1 second',sla_due_at=now()-interval '1 second' WHERE ticket_id=:id"
                ),
                {"id": t.ticket_id},
            )
        async with asyncio.timeout(3):
            while True:
                current = await service.get(t.ticket_id)
                if current.state == "waiting_human" and current.sla_overdue:
                    break
                await asyncio.sleep(0.02)
        events = await service.audit(t.ticket_id, 100, 0)
        assert [e["type"] for e in events] == [
            "created",
            "claimed",
            "released",
            "sla_warning",
            "sla_overdue",
        ]
        assert current.revision == 5


def test_experimental_naive_claim_rejects_non_experiment_database(database, monkeypatch):
    import os
    import subprocess
    import sys

    env = {**os.environ, "AGENT_OPS_S1_DATABASE_URL": database[0]}
    result = subprocess.run(
        [
            sys.executable,
            "scripts/experiments/s1-naive-claim.py",
            "--enable-unsafe-s1-baseline",
            "--ticket-id",
            str(uuid4()),
            "--agent-id",
            "3",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2 and "only isolated agent_ops_s1_" in result.stderr
    assert "fixture-only" not in result.stdout + result.stderr


def test_workbench_migration_roundtrip_and_claimed_downgrade_guard():
    from sqlalchemy.exc import DBAPIError

    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        cfg = Config("alembic.ini")
        cfg.attributes["database_url"] = pg.get_connection_url()
        command.upgrade(cfg, "head")
        engine = create_engine(pg.get_connection_url())
        tid = str(uuid4())
        try:
            with engine.begin() as c:
                c.execute(
                    text(
                        "INSERT INTO auth.accounts(email,password_hash,role) VALUES('migration@fixture.invalid','$argon2id$fixture','agent')"
                    )
                )
                c.execute(
                    text("""INSERT INTO support.tickets(ticket_id,route,state,revision,reason_code,history,assigned_account_id,lease_expires_at)
                    VALUES(:id,'human','claimed',2,'FIXTURE','[]',1,now()+interval '10 minutes')"""),
                    {"id": tid},
                )
            with pytest.raises(DBAPIError, match="release claimed tickets"):
                command.downgrade(cfg, "0004_auth")
            with engine.begin() as c:
                assert c.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(cfg).get_current_head()
                c.execute(
                    text(
                        "UPDATE support.tickets SET state='waiting_human',assigned_account_id=NULL,lease_expires_at=NULL WHERE ticket_id=:id"
                    ),
                    {"id": tid},
                )
            command.downgrade(cfg, "0004_auth")
            with engine.connect() as c:
                assert c.scalar(text("SELECT to_regclass('support.push_events')")) is None
                assert (
                    c.scalar(
                        text("SELECT count(*) FROM support.tickets WHERE ticket_id=:id"),
                        {"id": tid},
                    )
                    == 1
                )
            command.upgrade(cfg, "head")
            with engine.connect() as c:
                assert c.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(cfg).get_current_head()
                assert (
                    c.scalar(
                        text("SELECT state FROM support.tickets WHERE ticket_id=:id"), {"id": tid}
                    )
                    == "waiting_human"
                )
        finally:
            engine.dispose()


async def test_customer_can_supply_missing_order_after_seat_has_claimed(api):
    ticket = await create(api, order=None)
    tid = ticket["ticket_id"]
    claimed = (await post(api, tid, "claim", 1)).json()
    response = await post(api, tid, "information", 2, "c1", order_id=11)
    assert response.status_code == 200
    supplied = response.json()
    assert (
        supplied["state"] == "claimed" and supplied["revision"] == 3 and supplied["order_id"] == 11
    )
    assert supplied["assigned_account_id"] == claimed["assigned_account_id"]
    assert (
        supplied["lease_expires_at"] == claimed["lease_expires_at"]
    )  # customer does not renew seat lease
    assert (await post(api, tid, "reply", 3, message="已核对您补充的订单")).status_code == 200


async def test_p5_claimed_queue_is_scoped_and_renewal_is_silent(api):
    client, headers, service = api
    t = await create(api)
    assert (await post(api, t['ticket_id'], 'claim', 1)).status_code == 200
    mine = await client.get('/workbench/tickets?state=claimed&mine=true', headers=headers['a1'])
    assert mine.status_code == 200 and any(row['ticket_id'] == t['ticket_id'] for row in mine.json()['items'])
    assert all(row['assigned_account_id'] == 3 for row in mine.json()['items'])
    others = await client.get('/workbench/tickets?state=claimed&mine=true', headers=headers['a2'])
    assert all(row['assigned_account_id'] == 4 for row in others.json()['items'])
    assert (await client.get('/workbench/tickets?state=resolved',headers=headers['a1'])).status_code == 422
    after = await service.last_sequence()
    assert (await client.get('/workbench/tickets/'+t['ticket_id'],headers=headers['a1'])).status_code == 200
    events = await service.events(Principal(3,'agent',None,'fixture'),after)
    assert events == []


async def test_five_lease_renewals_preserve_revision_and_audit(api, database):
    client, headers, service = api
    ticket = await create(api)
    tid = ticket["ticket_id"]
    assert (await post(api, tid, "claim", 1)).status_code == 200
    before = await service.get(tid)
    audit = await service.audit(tid, 100, 0)
    sequence = await service.last_sequence()
    with database[1].begin() as c:
        c.execute(text("UPDATE support.tickets SET lease_expires_at=now()+interval '1 minute' WHERE ticket_id=:id"), {"id": tid})
    for _ in range(5):
        response = await client.get(f"/workbench/tickets/{tid}", headers=headers["a1"])
        assert response.status_code == 200
        assert response.json()["ticket"]["revision"] == before.revision
    assert await service.audit(tid, 100, 0) == audit
    assert await service.last_sequence() == sequence
    with database[1].connect() as c:
        assert c.scalar(text("SELECT lease_expires_at>now()+interval '9 minutes' FROM support.tickets WHERE ticket_id=:id"), {"id": tid})
