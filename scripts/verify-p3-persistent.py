"""Read-only business smoke on P2 with real demo login and two temporary loopback workers.

Only auth session rows are written. No new orders/tickets, no model/memory calls, no secrets printed.
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx

parser = argparse.ArgumentParser()
parser.add_argument(
    "--output-dir", type=Path, default=Path(".local/p3-followup-2026-10-26/persistent-http")
)
parser.add_argument("--workbench", action="store_true")
args = parser.parse_args()
folder = args.output_dir
folder.mkdir(mode=0o700, exist_ok=True)
if (folder / "result.json").exists():
    raise SystemExit("existing evidence; refusing overwrite")
values = dict(
    l.split("=", 1) for l in Path(".local/p2-application.env").read_text().splitlines() if "=" in l
)
values.update(
    AGENT_OPS_SUPPORT_BACKEND="postgres",
    AGENT_OPS_TRACE_EXPORTER="none",
    AGENT_OPS_PLANNER="deterministic",
    AGENT_MEMORY_URL="http://127.0.0.1:9",
)
roster = json.loads(Path(".local/demo-accounts.json").read_text())["accounts"]
checks = {}
credentials = [values["AGENT_OPS_AUTH_SECRET"], *[r["password"] for r in roster]]
ports = []
for _ in range(2):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        ports.append(sock.getsockname()[1])
processes = []


def check(name, passed):
    checks[name] = bool(passed)
    if not passed:
        raise RuntimeError("smoke check failed: " + name)


async def exercise():
    bases = [f"http://127.0.0.1:{p}" for p in ports]
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        for base in bases:
            for _ in range(100):
                try:
                    if (await client.get(base + "/auth/me")).status_code == 401:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError("server startup failed")
        sessions = []
        for row in roster:
            response = await client.post(
                bases[0] + "/auth/login", json={"email": row["email"], "password": row["password"]}
            )
            check("login-" + row["email"], response.status_code == 200)
            session = response.json()
            credentials.extend([session["access_token"], session["refresh_token"]])
            headers = {"Authorization": "Bearer " + session["access_token"]}
            check(
                "cross-worker-" + row["email"],
                (await client.get(bases[1] + "/auth/me", headers=headers)).status_code == 200,
            )
            sessions.append((row, session, headers))
        customers = [s for s in sessions if s[0]["role"] == "customer"]
        for row, session, headers in sessions:
            orders = await client.get(bases[1] + "/orders", headers=headers)
            if row["role"] == "customer":
                details = [
                    (
                        await client.get(
                            bases[1] + "/orders/" + str(o["order_id"]), headers=headers
                        )
                    ).json()
                    for o in orders.json()["items"]
                ]
                check(
                    "own-orders-" + row["email"],
                    orders.status_code == 200
                    and bool(details)
                    and all(
                        o["user_id"] == session["account"]["customer_user_id"] for o in details
                    ),
                )
                tickets = await client.get(bases[1] + "/support/tickets", headers=headers)
                check(
                    "own-seed-tickets-" + row["email"],
                    tickets.status_code == 200
                    and tickets.json()["items"]
                    and all(
                        t["customer_user_id"] == session["account"]["customer_user_id"]
                        for t in tickets.json()["items"]
                    ),
                )
            else:
                check("staff-orders-forbidden-" + row["email"], orders.status_code == 403)
        order = (await client.get(bases[0] + "/orders", headers=customers[1][2])).json()["items"][0]
        check(
            "other-customer-order-404",
            (
                await client.get(
                    bases[1] + "/orders/" + str(order["order_id"]), headers=customers[0][2]
                )
            ).status_code
            == 404,
        )
        ticket = (await client.get(bases[0] + "/support/tickets", headers=customers[1][2])).json()[
            "items"
        ][0]
        check(
            "other-customer-ticket-404",
            (
                await client.get(
                    bases[1] + "/support/tickets/" + ticket["ticket_id"], headers=customers[0][2]
                )
            ).status_code
            == 404,
        )
        body = {"request": "fixture", "category": "kettle", "budget_cent": 5000, "region": "上海"}
        for endpoint in ["/v1/commerce/runs", "/v1/commerce/stream"]:
            check(
                endpoint + "-anonymous-401",
                (await client.post(bases[1] + endpoint, json=body)).status_code == 401,
            )
            check(
                endpoint + "-staff-403",
                (
                    await client.post(bases[1] + endpoint, json=body, headers=sessions[3][2])
                ).status_code
                == 403,
            )
            response = await client.post(bases[1] + endpoint, json=body, headers=customers[0][2])
            result = (
                response.json()
                if endpoint.endswith("runs")
                else [
                    json.loads(l[6:]) for l in response.text.splitlines() if l.startswith("data: ")
                ][-1]
            )
            check(
                endpoint + "-customer-memory-disabled",
                response.status_code == 200
                and result["outcome"] == "succeeded"
                and result["memory_status"]
                == result["memory_read_status"]
                == result["memory_write_status"]
                == "not_configured",
            )
        if args.workbench:
            import psycopg
            from websockets.asyncio.client import connect

            def last_sequence():
                with psycopg.connect(
                    values["AGENT_OPS_DATABASE_URL"].replace("postgresql+psycopg:", "postgresql:")
                ) as conn:
                    return conn.execute("SELECT value FROM support.push_clock").fetchone()[0]

            after = await asyncio.to_thread(last_sequence)
            staff = next(s for s in sessions if s[0]["role"] == "agent")
            admin = next(s for s in sessions if s[0]["role"] == "admin")
            queue = await client.get(
                bases[0] + "/workbench/tickets?limit=2&offset=0", headers=staff[2]
            )
            check(
                "P4-offset-queue",
                queue.status_code == 200
                and queue.json()["offset"] == 0
                and "next_cursor" not in queue.json(),
            )
            stats = await client.get(bases[1] + "/admin/support-stats", headers=admin[2])
            check("P4-admin-stats", stats.status_code == 200 and stats.json()["queue_length"] >= 0)
            check(
                "P4-staff-stats-forbidden",
                (await client.get(bases[0] + "/admin/support-stats", headers=staff[2])).status_code
                == 403,
            )
            async with connect(f"ws://127.0.0.1:{ports[1]}/ws") as ws:
                await ws.send(
                    json.dumps({"access_token": staff[1]["access_token"], "after": after})
                )
                await ws.send("ping")
                check(
                    "P4-real-P2-websocket-auth-and-cursor",
                    json.loads(await asyncio.wait_for(ws.recv(), 5)) == {"type": "pong"},
                )
        for _, _, headers in sessions:
            check(
                "logout-" + str(len(checks)),
                (await client.post(bases[1] + "/auth/logout", headers=headers)).status_code == 204,
            )


with (folder / "server.log").open("w") as log:
    try:
        env = {**os.environ, **values}
        env.pop("ARK_API_KEY", None)
        for port in ports:
            processes.append(
                subprocess.Popen(
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
                    stdout=log,
                    stderr=log,
                )
            )
        asyncio.run(exercise())
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
logs = (folder / "server.log").read_text()
check("no-credentials-in-server-log", not any(secret in logs for secret in credentials))
check("no-bearer-in-server-log", "Bearer " not in logs)
report = {
    "checks": checks,
    "passed": sum(checks.values()),
    "total": len(checks),
    "business_writes": 0,
    "database": "agent_ops_p2_seed42",
    "processes": 2,
    "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "dirty": True,
}
(folder / "result.json").write_text(json.dumps(report, indent=2) + "\n")
print(
    json.dumps(
        {
            "passed": report["passed"],
            "total": report["total"],
            "report": str(folder / "result.json"),
        }
    )
)
