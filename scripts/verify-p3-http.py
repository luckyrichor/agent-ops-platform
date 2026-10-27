"""P3 over real loopback TCP: two uvicorn processes, a disposable PostgreSQL container,
demo accounts created through the CLI. Never touches the P0 main or P2 acceptance database.

    uv run python scripts/verify-p3-http.py --output .local/p3-http/result.json
"""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
a.output.parent.mkdir(parents=True, exist_ok=True)
log_path = a.output.parent / "server.log"
checks: dict[str, bool] = {}


def check(name: str, ok: bool) -> None:
    checks[name] = bool(ok)
    print(("PASS " if ok else "FAIL ") + name)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def seed(owner: str) -> str:
    config = Config("alembic.ini")
    config.attributes["database_url"] = owner
    command.upgrade(config, "head")
    password = secrets.token_urlsafe(24)
    engine = create_engine(owner)
    with engine.begin() as c:
        c.execute(text(f"CREATE ROLE p3_http LOGIN PASSWORD '{password}' NOSUPERUSER"))
        c.execute(text("GRANT agent_ops_app TO p3_http"))
        c.execute(text("INSERT INTO commerce.users(id) VALUES (1),(2),(3),(4)"))
        c.execute(text("INSERT INTO commerce.categories VALUES (1,'fixture')"))
        c.execute(text("""INSERT INTO commerce.products(id,sku,title,category_id,price_cents,
            freight_cents,status) VALUES (1,'fx1','fixture',1,1000,10,'active')"""))
        c.execute(text("INSERT INTO commerce.inventory(product_id,stock) VALUES (1,100)"))
        c.execute(text("""INSERT INTO commerce.orders(id,user_id,status,total_cents,created_at)
            VALUES (101,1,'delivered',1010,'2025-01-01'),(201,2,'delivered',1010,'2025-01-02'),
                   (301,3,'shipped',1010,'2025-01-03')"""))
        c.execute(text("INSERT INTO commerce.conversations(id,user_id) VALUES (11,1),(21,2)"))
    engine.dispose()
    return (make_url(owner).set(username="p3_http", password=password)
            .render_as_string(hide_password=False))


async def ready(client: httpx.AsyncClient, base: str) -> None:
    for _ in range(100):
        try:
            if (await client.post(base + "/auth/refresh",
                                  json={"refresh_token": "x" * 43})).status_code == 401:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("server did not start: " + base)


async def exercise(bases: list[str], roster: dict) -> None:
    by_role: dict[str, list[dict]] = {}
    for entry in roster["accounts"]:
        by_role.setdefault(entry["role"], []).append(entry)
    one, two = bases
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for base in bases:
            await ready(client, base)

        async def login(base: str, entry: dict) -> dict:
            response = await client.post(base + "/auth/login", json={
                "email": entry["email"], "password": entry["password"]})
            response.raise_for_status()
            return dict(response.json())

        def auth(session: dict) -> dict:
            return {"Authorization": "Bearer " + session["access_token"]}

        c1 = await login(one, by_role["customer"][0])
        agent = await login(one, by_role["agent"][0])
        admin = await login(two, by_role["admin"][0])
        check("token issued by process A is accepted by process B",
              (await client.get(two + "/auth/me", headers=auth(c1))).json()["role"] == "customer")
        body = {'request': 'fixture', 'category': 'kettle', 'budget_cent': 5000, 'region': '上海'}
        for endpoint in ['/v1/commerce/runs', '/v1/commerce/stream']:
            check(endpoint + ' rejects anonymous before SSE starts',
                  (await client.post(two + endpoint, json=body)).status_code == 401)
            check(endpoint + ' rejects agent',
                  (await client.post(two + endpoint, json=body, headers=auth(agent))).status_code == 403)
            response = await client.post(two + endpoint, json=body, headers=auth(c1))
            if endpoint.endswith('stream'):
                payload = [json.loads(l[6:]) for l in response.text.splitlines() if l.startswith('data: ')][-1]
            else:
                payload = response.json()
            check(endpoint + ' accepts customer with memory not_configured', response.status_code == 200
                  and payload['outcome'] == 'succeeded' and payload['memory_status'] == 'not_configured')
        own = await client.get(two + "/orders", headers=auth(c1))
        check("customer sees only own orders over TCP",
              [o["order_id"] for o in own.json()["items"]] == [101])
        check("other customer's order is 404",
              (await client.get(one + "/orders/201", headers=auth(c1))).status_code == 404)
        check("other customer's conversation is 404", (await client.get(
            one + "/support/conversations/21/messages", headers=auth(c1))).status_code == 404)
        check("agent cannot read orders (403)",
              (await client.get(two + "/orders/101", headers=auth(agent))).status_code == 403)
        check("agent cannot read conversations (403)", (await client.get(
            two + "/support/conversations", headers=auth(agent))).status_code == 403)
        ticket = await client.post(one + "/support/requests", json={"message": "转人工"},
                                   headers={"Idempotency-Key": "p3-http", **auth(c1)})
        tid = ticket.json()["ticket_id"]
        check("customer cannot use workbench (403)", (await client.post(
            two + f"/workbench/tickets/{tid}/resolve", json={"expected_revision": 1},
            headers=auth(c1))).status_code == 403)
        check("customer cannot use admin (403)",
              (await client.get(one + "/admin/accounts", headers=auth(c1))).status_code == 403)
        check("agent resolves on the other process", (await client.post(
            two + f"/workbench/tickets/{tid}/resolve", json={"expected_revision": 1},
            headers=auth(agent))).status_code == 200)
        anonymous = [await client.request(m, one + path, json=body) for m, path, body in [
            ("GET", "/orders", None), ("GET", f"/support/tickets/{tid}", None),
            ("POST", "/support/requests", {"message": "x"}), ("GET", "/admin/accounts", None),
            ("POST", f"/workbench/tickets/{tid}/resolve", {"expected_revision": 2})]]
        check("anonymous requests are 401", all(r.status_code == 401 for r in anonymous))
        rotated = await client.post(two + "/auth/refresh",
                                    json={"refresh_token": c1["refresh_token"]})
        check("refresh on process B", rotated.status_code == 200)
        replay = await client.post(one + "/auth/refresh",
                                   json={"refresh_token": c1["refresh_token"]})
        check("replayed refresh token rejected on process A as reuse",
              replay.status_code == 401 and replay.json()["detail"] == "REFRESH_TOKEN_REUSED")
        check("reuse revoked the rotated successor", (await client.post(
            two + "/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]})
        ).status_code == 401)
        listed = (await client.get(two + "/admin/accounts", headers=auth(admin))).json()
        check("admin lists exactly the demo roster", len(listed["items"]) == len(roster["accounts"]))
        check("logout returns 204",
              (await client.post(one + "/auth/logout", headers=auth(admin))).status_code == 204)


def main() -> None:
    started = datetime.now(UTC)
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as pg, \
            tempfile.TemporaryDirectory() as tmp:
        url = seed(pg.get_connection_url())
        roster_path = Path(tmp) / "roster.json"
        env = {**os.environ, "AGENT_OPS_DATABASE_URL": url}
        subprocess.run([sys.executable, "-m", "agent_ops.demo_accounts", "--init",
                        "--roster", str(roster_path)], check=True, env=env,
                       stdout=subprocess.DEVNULL)
        cli = subprocess.run([sys.executable, "-m", "agent_ops.demo_accounts",
                              "--roster", str(roster_path)], check=True, env=env,
                             capture_output=True, text=True)
        roster = json.loads(roster_path.read_text())
        passwords = [e["password"] for e in roster["accounts"]]
        rows = [json.loads(line) for line in cli.stdout.splitlines()]
        check("CLI created the 6-account roster", [r["action"] for r in rows] == ["created"] * 6)
        check("CLI linked customers to the 3 users that have orders",
              sorted(r["customer_user_id"] for r in rows if r["role"] == "customer") == [1, 2, 3])
        check("CLI output has no passwords",
              not any(pw in cli.stdout + cli.stderr for pw in passwords))
        server_env = {**env, "AGENT_OPS_SUPPORT_BACKEND": "postgres",
                      "AGENT_OPS_AUTH_SECRET": secrets.token_urlsafe(48),
                      "AGENT_OPS_PLANNER": "deterministic", "AGENT_OPS_TRACE_EXPORTER": "none",
                      "AGENT_MEMORY_URL": "http://127.0.0.1:9"}
        server_env.pop("ARK_API_KEY", None)
        ports = [free_port(), free_port()]
        processes = []
        with log_path.open("w") as log:
            for port in ports:
                processes.append(subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "agent_ops.api:create_app", "--factory",
                     "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
                    env=server_env, stdout=log, stderr=log))
            try:
                asyncio.run(exercise([f"http://127.0.0.1:{p}" for p in ports], roster))
            finally:
                for proc in processes:
                    proc.terminate()
                    proc.wait(10)
        server_log = log_path.read_text()
        check("server log has no passwords", not any(pw in server_log for pw in passwords))
        check("server log has no bearer tokens", "eyJ" not in server_log)
    result = {"started_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
              "database": "disposable testcontainers pgvector/pgvector:pg16 (fixture rows only)",
              "servers": "2 uvicorn processes, 127.0.0.1, shared signing key, no model calls",
              "checks": checks, "passed": sum(checks.values()), "total": len(checks)}
    a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if not all(checks.values()):
        raise SystemExit(1)


main()
