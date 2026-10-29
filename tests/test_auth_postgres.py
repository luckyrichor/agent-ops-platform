"""P3 on real PostgreSQL: owner-scoped orders/conversations/tickets, refresh rotation under
concurrency, the demo-account script and the 0004 migration. Fixture rows only, no datasets."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

from agent_ops import demo_accounts
from agent_ops.api import create_app

ROSTER = {"accounts": [
    {"email": "Customer1@Demo.invalid", "password": "fixture-demo-customer-1", "role": "customer"},
    {"email": "customer2@demo.invalid", "password": "fixture-demo-customer-2", "role": "customer"},
    {"email": "agent1@demo.invalid", "password": "fixture-demo-agent-0001", "role": "agent"},
    {"email": "admin1@demo.invalid", "password": "fixture-demo-admin-0001", "role": "admin"},
]}
PASSWORD = {a["email"].lower(): a["password"] for a in ROSTER["accounts"]}


@pytest.fixture(scope="module")
def database():
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        owner = pg.get_connection_url()
        config = Config("alembic.ini")
        config.attributes["database_url"] = owner
        command.upgrade(config, "head")
        engine = create_engine(owner)
        with engine.begin() as c:
            c.execute(text("CREATE ROLE p3_runtime LOGIN PASSWORD 'fixture-only' NOSUPERUSER"))
            c.execute(text("GRANT agent_ops_app TO p3_runtime"))
            # Users 1/2 have orders (demo script links these); 3 has none and stays credential-less.
            c.execute(text("INSERT INTO commerce.users(id) VALUES (1),(2),(3)"))
            c.execute(text("INSERT INTO commerce.categories VALUES (1,'fixture')"))
            c.execute(text("""INSERT INTO commerce.products
                (id,sku,title,category_id,price_cents,freight_cents,status)
                VALUES (1,'fx1','fixture',1,1000,10,'active')"""))
            c.execute(text("INSERT INTO commerce.inventory(product_id,stock) VALUES (1,100)"))
            c.execute(text("""INSERT INTO commerce.orders(id,user_id,status,total_cents,created_at)
                VALUES (101,1,'delivered',1010,'2025-01-01'),(102,1,'shipped',1010,'2025-02-01'),
                       (201,2,'delivered',1010,'2025-01-15')"""))
            c.execute(text("""INSERT INTO commerce.order_items VALUES
                (101,1,1,1,1000,10),(102,1,1,1,1000,10),(201,1,1,1,1000,10)"""))
            c.execute(text("""INSERT INTO commerce.shipments VALUES
                (101,'fixture-carrier','FX101','delivered'),(201,'fixture-carrier','FX201','delivered')"""))
            c.execute(text("INSERT INTO commerce.conversations(id,user_id) VALUES (11,1),(21,2)"))
            for conv, sender, body in ((11, "customer", "c1 question"), (21, "customer", "c2 secret")):
                c.execute(text("INSERT INTO commerce.message_keys VALUES (:c,1,'2025-03-01')"),
                          {"c": conv})
                c.execute(text("INSERT INTO commerce.messages VALUES (:c,1,'2025-03-01',:s,:b)"),
                          {"c": conv, "s": sender, "b": body})
        runtime = (make_url(owner).set(username="p3_runtime", password="fixture-only")
                   .render_as_string(hide_password=False))
        yield runtime, engine, config
        engine.dispose()


@pytest.fixture(scope="module")
def provisioned(database):
    url, _, _ = database
    report = asyncio.run(demo_accounts.provision(
        url, demo_accounts.Roster.model_validate(ROSTER), reset_passwords=False))
    return {row["email"]: row for row in report}


@pytest.fixture
async def client(database, provisioned, monkeypatch):
    url, _, _ = database
    monkeypatch.setenv("AGENT_OPS_SUPPORT_BACKEND", "postgres")
    monkeypatch.setenv("AGENT_OPS_DATABASE_URL", url)
    monkeypatch.setenv("AGENT_OPS_AUTH_SECRET", "fixture-signing-secret-" + "p" * 32)
    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def login(http, email):
    response = await http.post("/auth/login", json={"email": email, "password": PASSWORD[email]})
    assert response.status_code == 200, response.text
    return response.json()


async def bearer(http, email):
    return {"Authorization": "Bearer " + (await login(http, email))["access_token"]}


def test_demo_script_links_only_few_users_and_keeps_secrets_out(database, provisioned, capsys,
                                                                monkeypatch):
    url, engine, _ = database
    assert provisioned["customer1@demo.invalid"]["customer_user_id"] == 1
    assert provisioned["customer2@demo.invalid"]["customer_user_id"] == 2
    assert {r["role"] for r in provisioned.values()} == {"customer", "agent", "admin"}
    with engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM auth.accounts")) == 4
        assert c.scalar(text("""SELECT count(*) FROM commerce.users u WHERE NOT EXISTS
            (SELECT 1 FROM auth.accounts a WHERE a.customer_user_id=u.id)""")) == 1
        hashes = c.execute(text("SELECT password_hash FROM auth.accounts")).scalars().all()
    assert all(h.startswith("$argon2id$") for h in hashes)
    assert not any(p in h for h in hashes for p in PASSWORD.values())
    # CLI path: roster from the environment, rerun is idempotent, stdout carries no secrets.
    monkeypatch.setenv("AGENT_OPS_DATABASE_URL", url)
    monkeypatch.setenv("AGENT_OPS_DEMO_ACCOUNTS", json.dumps(ROSTER))
    demo_accounts.main([])
    out = capsys.readouterr().out
    rows = [json.loads(line) for line in out.splitlines()]
    assert [r["action"] for r in rows] == ["exists"] * 4
    assert not any(p in out for p in PASSWORD.values()) and "argon2" not in out
    bad = {"accounts": [{"email": "x@demo.invalid", "password": "short", "role": "agent"}]}
    monkeypatch.setenv("AGENT_OPS_DEMO_ACCOUNTS", json.dumps(bad))
    with pytest.raises(SystemExit) as error:
        demo_accounts.main([])
    assert "accounts.0.password" in str(error.value) and "short" not in str(error.value)


def test_demo_roster_init_writes_private_file_without_printing(tmp_path, capsys):
    path = tmp_path / "demo-accounts.json"
    demo_accounts.main(["--init", "--roster", str(path)])
    assert path.stat().st_mode & 0o777 == 0o600
    roster = demo_accounts.Roster.model_validate_json(path.read_text())
    assert [a.role for a in roster.accounts].count("customer") == 3
    assert len({a.password for a in roster.accounts}) == 6
    out = capsys.readouterr().out
    assert not any(a.password in out for a in roster.accounts)
    with pytest.raises(SystemExit):
        demo_accounts.main(["--init", "--roster", str(path)])


async def test_orders_are_owner_scoped(client):
    c1 = await bearer(client, "customer1@demo.invalid")
    c2 = await bearer(client, "customer2@demo.invalid")
    agent = await bearer(client, "agent1@demo.invalid")
    admin = await bearer(client, "admin1@demo.invalid")
    page = (await client.get("/orders?limit=1", headers=c1)).json()
    assert [o["order_id"] for o in page["items"]] == [102] and page["next_cursor"] == "102"
    rest = (await client.get("/orders?limit=1&cursor=102", headers=c1)).json()
    assert [o["order_id"] for o in rest["items"]] == [101]
    detail = await client.get("/orders/101", headers=c1)
    assert detail.status_code == 200 and detail.json()["shipment"]["tracking_no"] == "FX101"
    assert detail.json()["items"][0]["product_id"] == 1
    # Another customer's order is indistinguishable from a missing one, including via cursor.
    for path in ("/orders/201", "/orders/999999"):
        response = await client.get(path, headers=c1)
        assert response.status_code == 404 and response.json()["detail"] == "ORDER_NOT_FOUND"
    assert (await client.get("/orders?cursor=201", headers=c1)).json()["items"] == []
    assert (await client.post("/orders/201/cancel", json={"expected_revision": 1},
                              headers=c1)).status_code == 404
    assert [o["order_id"] for o in (await client.get("/orders", headers=c2)).json()["items"]] \
        == [201]
    for staff in (agent, admin):
        for path in ("/orders", "/orders/101", "/orders/201"):
            response = await client.get(path, headers=staff)
            assert response.status_code == 403 and response.json()["detail"] == "ROLE_FORBIDDEN"


async def test_place_and_cancel_order_as_token_owner(client, database):
    _, engine, _ = database
    c2 = await bearer(client, "customer2@demo.invalid")
    lines = {"lines": [{"product_id": 1, "quantity": 2}]}
    spoof = await client.post("/orders", json={**lines, "user_id": 1},
                              headers={"Idempotency-Key": "spoof", **c2})
    assert spoof.status_code == 422
    placed = await client.post("/orders", json=lines, headers={"Idempotency-Key": "p3", **c2})
    assert placed.status_code == 200 and placed.json()["user_id"] == 2
    oid = placed.json()["order_id"]
    replay = await client.post("/orders", json=lines, headers={"Idempotency-Key": "p3", **c2})
    assert replay.json()["order_id"] == oid
    c1 = await bearer(client, "customer1@demo.invalid")
    assert (await client.get(f"/orders/{oid}", headers=c1)).status_code == 404
    assert (await client.post(f"/orders/{oid}/cancel", json={"expected_revision": 1},
                              headers=c1)).status_code == 404
    done = await client.post(f"/orders/{oid}/cancel", json={"expected_revision": 1}, headers=c2)
    assert done.status_code == 200 and done.json()["status"] == "canceled"
    with engine.connect() as c:
        assert c.scalar(text("SELECT user_id FROM commerce.orders WHERE id=:id"), {"id": oid}) == 2
        assert c.scalar(text("SELECT reserved FROM commerce.inventory WHERE product_id=1")) == 0


async def test_conversations_are_owner_scoped(client):
    c1 = await bearer(client, "customer1@demo.invalid")
    agent = await bearer(client, "agent1@demo.invalid")
    listed = (await client.get("/support/conversations", headers=c1)).json()
    assert [c["conversation_id"] for c in listed["items"]] == [11]
    own = await client.get("/support/conversations/11/messages", headers=c1)
    assert own.status_code == 200 and own.json()["items"][0]["content"] == "c1 question"
    other = await client.get("/support/conversations/21/messages", headers=c1)
    assert other.status_code == 404 and "c2 secret" not in other.text
    for path in ("/support/conversations", "/support/conversations/11/messages",
                 "/support/conversations/21/messages"):
        response = await client.get(path, headers=agent)
        assert response.status_code == 403 and "secret" not in response.text


async def test_ticket_ownership_scope_and_audit_actor(client, database):
    _, engine, _ = database
    c1 = await bearer(client, "customer1@demo.invalid")
    c2 = await bearer(client, "customer2@demo.invalid")
    agent_session = await login(client, "agent1@demo.invalid")
    agent = {"Authorization": "Bearer " + agent_session["access_token"]}
    key = "pg-" + str(uuid4())
    mine = await client.post("/support/requests", json={"message": "转人工"},
                             headers={"Idempotency-Key": key, **c1})
    theirs = await client.post("/support/requests", json={"message": "转人工"},
                               headers={"Idempotency-Key": key, **c2})
    tid = mine.json()["ticket_id"]
    assert theirs.json()["ticket_id"] != tid and theirs.json()["customer_user_id"] == 2
    assert (await client.get(f"/support/tickets/{tid}", headers=c2)).status_code == 404
    assert (await client.get(f"/support/tickets/{tid}", headers=agent)).status_code == 200
    listed = (await client.get("/support/tickets", headers=c1)).json()["items"]
    assert tid in [t["ticket_id"] for t in listed]
    assert theirs.json()["ticket_id"] not in [t["ticket_id"] for t in listed]
    claimed = await client.post(f"/workbench/tickets/{tid}/claim",
                                json={"expected_revision":1}, headers={**agent,"Idempotency-Key":"ownership-claim"})
    assert claimed.status_code == 200
    resolved = await client.post(f"/workbench/tickets/{tid}/resolve",
                                 json={"expected_revision": 2}, headers={**agent,"Idempotency-Key":"ownership-resolve"})
    assert resolved.status_code == 200
    with engine.connect() as c:
        actor = c.scalar(text("""SELECT actor FROM support.ticket_events
            WHERE ticket_id=:id AND type='resolved'"""), {"id": tid})
        owner = c.scalar(text("SELECT customer_user_id FROM support.tickets WHERE ticket_id=:id"),
                         {"id": tid})
    assert actor == f"account:{agent_session['account']['id']}" and owner == 1


async def test_concurrent_refresh_of_one_token_has_one_winner(client, database):
    _, engine, _ = database
    session = await login(client, "customer1@demo.invalid")
    results = await asyncio.gather(*(client.post(
        "/auth/refresh", json={"refresh_token": session["refresh_token"]}) for _ in range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1 and codes.count(401) == 7
    # Losers look like reuse, so the family is revoked: the winner's new token is dead too.
    winner = next(r for r in results if r.status_code == 200).json()
    after = await client.post("/auth/refresh", json={"refresh_token": winner["refresh_token"]})
    assert after.status_code == 401
    with engine.connect() as c:
        stored = c.execute(text("SELECT token_hash FROM auth.refresh_tokens")).scalars().all()
    assert session["refresh_token"] not in stored and all(len(h) == 64 for h in stored)


async def test_disable_and_logout_persist(client):
    admin = await bearer(client, "admin1@demo.invalid")
    session = await login(client, "customer2@demo.invalid")
    accounts = (await client.get("/admin/accounts", headers=admin)).json()["items"]
    target = next(a["id"] for a in accounts if a["email"] == "customer2@demo.invalid")
    assert (await client.post(f"/admin/accounts/{target}/status", json={"status": "disabled"},
                              headers=admin)).status_code == 200
    assert (await client.post("/auth/refresh", json={"refresh_token": session["refresh_token"]})
            ).status_code == 401
    assert (await client.post("/auth/login", json={
        "email": "customer2@demo.invalid", "password": PASSWORD["customer2@demo.invalid"]})
    ).status_code == 401
    await client.post(f"/admin/accounts/{target}/status", json={"status": "active"},
                      headers=admin)
    fresh = await login(client, "customer2@demo.invalid")
    out = await client.post("/auth/logout",
                            headers={"Authorization": "Bearer " + fresh["access_token"]})
    assert out.status_code == 204
    assert (await client.post("/auth/refresh", json={"refresh_token": fresh["refresh_token"]})
            ).status_code == 401


def test_runtime_role_cannot_bypass_constraints(database):
    url, _, _ = database
    import psycopg

    with psycopg.connect(url.replace("postgresql+psycopg:", "postgresql:")) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("""INSERT INTO auth.accounts(email,password_hash,role)
                VALUES ('plain@demo.invalid','plaintext-password','agent')""")
        c.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("""INSERT INTO auth.accounts(email,password_hash,role)
                VALUES ('nolink@demo.invalid','$argon2id$fixture','customer')""")
        c.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM auth.accounts")


def test_auth_migration_downgrade_and_reupgrade():
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg:
        config = Config("alembic.ini")
        config.attributes["database_url"] = pg.get_connection_url()
        command.upgrade(config, "head")
        command.downgrade(config, "0003_proxy_provenance")
        engine = create_engine(pg.get_connection_url())
        with engine.connect() as c:
            assert c.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname='auth'")) == 0
            assert c.scalar(text("""SELECT count(*) FROM information_schema.columns
                WHERE table_schema='support' AND column_name='customer_user_id'""")) == 0
        command.upgrade(config, "head")
        with engine.connect() as c:
            assert c.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(config).get_current_head()
        engine.dispose()


async def test_seed_ownership_requires_certain_conversation_and_preserves_unowned_access(database, client):
    import psycopg

    from agent_ops.seed_ownership import backfill_seed_owners

    _, engine, _ = database
    ids = [str(uuid4()) for _ in range(5)]
    with engine.begin() as c:
        for index, tid in enumerate(ids):
            c.execute(text("""INSERT INTO support.tickets(ticket_id,route,state,revision,reason_code,history,
                synthetic_distribution_note) VALUES (:id,'human','waiting_human',1,'FIXTURE','[]',:note)"""),
                {'id': tid, 'note': 'fixture-proxy' if index != 4 else None})
        # Certain; several agreeing conversations; conflicting users; no conversation; unmarked old row.
        for cid, tid, uid in [(991,ids[0],1),(992,ids[1],1),(993,ids[1],1),
                              (994,ids[2],1),(995,ids[2],2),(996,ids[4],1)]:
            c.execute(text('INSERT INTO commerce.conversations(id,user_id,ticket_id) VALUES (:c,:u,:t)'),
                      {'c': cid, 'u': uid, 't': tid})
    dsn = engine.url.set(drivername='postgresql').render_as_string(hide_password=False)
    with psycopg.connect(dsn) as connection:
        assert backfill_seed_owners(connection) == 2
        assert backfill_seed_owners(connection) == 0
    customer = await bearer(client, 'customer1@demo.invalid')
    other = await bearer(client, 'customer2@demo.invalid')
    staff = await bearer(client, 'agent1@demo.invalid')
    admin = await bearer(client, 'admin1@demo.invalid')
    for index, tid in enumerate(ids):
        response = await client.get('/support/tickets/' + tid, headers=customer)
        assert response.status_code == (200 if index < 2 else 404)
        assert (await client.get('/support/tickets/' + tid, headers=other)).status_code == 404
        assert (await client.get('/support/tickets/' + tid, headers=staff)).status_code == 200
        assert (await client.get('/support/tickets/' + tid, headers=admin)).status_code == 200
    listed = (await client.get('/support/tickets', headers=customer)).json()['items']
    returned = {row['ticket_id'] for row in listed}
    assert set(ids[:2]) <= returned and not set(ids[2:]) & returned


async def test_public_real_catalog_and_detail_do_not_expose_other_resources(client):
    listed = await client.get('/products?limit=1')
    assert listed.status_code == 200 and listed.json()['items'][0]['id'] == 1
    assert listed.headers['x-trace-id']
    detail = await client.get('/products/1')
    assert detail.status_code == 200 and detail.json()['sku'] == 'fx1'
    assert detail.json()['available'] >= 0
    assert (await client.get('/products/99999999')).status_code == 404
    assert (await client.get('/products?limit=101')).status_code == 422
    assert (await client.get('/orders')).status_code == 401
