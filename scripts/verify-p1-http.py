"""Two real loopback API processes against the P0 persistent PG; no model calls."""

import argparse
import asyncio
import json
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

p = argparse.ArgumentParser()
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
secrets = dict(
    line.split("=", 1) for line in Path(".local/application.env").read_text().splitlines() if line
)
ports = []
for _ in range(2):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        ports.append(sock.getsockname()[1])
processes = []
with Path(".local/p1-http-server.log").open("a") as log:

    def start(port):
        proc = subprocess.Popen(
            [sys.executable, "scripts/serve.py", "--backend", "postgres", "--port", str(port)],
            stdout=log,
            stderr=log,
        )
        processes.append(proc)
        return proc

    async def ready(client, port):
        for _ in range(100):
            try:
                if (
                    await client.get(f"http://127.0.0.1:{port}/support/tickets/not-a-uuid")
                ).status_code == 404:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        raise RuntimeError("API did not become ready")

    async def verify():
        for port in ports:
            start(port)
        key = "p1-http-synthetic-" + str(uuid4())
        payload = {"message": "我要退款"}
        headers = {"Idempotency-Key": key}
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            await asyncio.gather(*(ready(client, port) for port in ports))
            results = await asyncio.gather(
                *(
                    client.post(
                        f"http://127.0.0.1:{ports[i % 2]}/support/requests",
                        json=payload,
                        headers=headers,
                    )
                    for i in range(16)
                )
            )
            assert all(r.status_code == 200 and r.headers.get("x-trace-id") for r in results)
            ids = {r.json()["ticket_id"] for r in results}
            assert len(ids) == 1
            tid = next(iter(ids))
            for proc in processes:
                proc.terminate()
            for proc in processes:
                await asyncio.to_thread(proc.wait, 10)
            start(ports[0])
            await ready(client, ports[0])
            base = f"http://127.0.0.1:{ports[0]}"
            fetched = await client.get(base + "/support/tickets/" + tid)
            assert fetched.status_code == 200 and fetched.json()["revision"] == 1
            resolved = await client.post(
                base + "/support/tickets/" + tid + "/resolve",
                json={"expected_revision": 1},
                headers={"Authorization": "Bearer " + secrets["SUPPORT_HUMAN_TOKEN"]},
            )
            assert resolved.status_code == 200 and resolved.json()["revision"] == 2
            replay = await client.post(base + "/support/requests", json=payload, headers=headers)
            assert replay.status_code == 200 and replay.json()["revision"] == 1
            return {
                "generated_at": datetime.now(UTC).isoformat(),
                "api_processes_initial": len(ports),
                "requests": len(results),
                "unique_tickets": len(ids),
                "duplicate_tickets": len(ids) - 1,
                "all_http_200": all(r.status_code == 200 for r in results),
                "retained_after_api_process_restart": True,
                "resolved_revision": resolved.json()["revision"],
                "replay_revision": replay.json()["revision"],
                "model_calls": 0,
                "scope": "loopback real TCP HTTP, P0 persistent PG; one synthetic verification ticket retained",
            }

    try:
        report = asyncio.run(verify())
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
