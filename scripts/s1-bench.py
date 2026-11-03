#!/usr/bin/env python3
"""S1 领取与列表基准：A（CAS+OFFSET）/ B（部分索引+keyset+SKIP LOCKED）/ N（两步）。

只读写 agent_ops_p10_s1。W1=思考 2s、W2=思考 0；W2 通过定期把已领取行重置回
待人工维持供给，重置时返回 ticket_id 并清空本地去重表，避免把“重新领取”误记成重复。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]

CLAIM_CAS = """
UPDATE support.tickets SET state='claimed', revision=revision+1,
  assigned_account_id=%s, lease_expires_at=now()+interval '10 minutes',
  reason_code='HUMAN_CLAIMED', updated_at=now(), history=history || '["claimed"]'::jsonb
WHERE ticket_id=%s AND state='waiting_human' AND revision=%s
RETURNING ticket_id, revision
"""

CLAIM_SKIP = """
UPDATE support.tickets SET state='claimed', revision=revision+1,
  assigned_account_id=%s, lease_expires_at=now()+interval '10 minutes',
  reason_code='HUMAN_CLAIMED', updated_at=now(), history=history || '["claimed"]'::jsonb
WHERE ticket_id = (
  SELECT ticket_id FROM support.tickets WHERE state='waiting_human'
  ORDER BY priority DESC, created_at, ticket_id FOR UPDATE SKIP LOCKED LIMIT 1)
  AND state='waiting_human'
RETURNING ticket_id, revision
"""

CLAIM_TWO_STEP = """
UPDATE support.tickets SET state='claimed', revision=revision+1,
  assigned_account_id=%s, lease_expires_at=now()+interval '10 minutes',
  reason_code='HUMAN_CLAIMED', updated_at=now(), history=history || '["claimed"]'::jsonb
WHERE ticket_id=%s
RETURNING ticket_id, revision
"""

PICK = """
SELECT ticket_id, revision FROM support.tickets WHERE state='waiting_human'
ORDER BY priority DESC, created_at, ticket_id LIMIT 20 OFFSET 0
"""

PAGE_OFFSET = """
SELECT ticket_id, priority, created_at FROM support.tickets WHERE state='waiting_human'
ORDER BY priority DESC, created_at, ticket_id LIMIT %s OFFSET %s
"""

PAGE_FIRST = """
SELECT ticket_id, priority, created_at FROM support.tickets WHERE state='waiting_human'
ORDER BY priority DESC, created_at, ticket_id LIMIT %s
"""

PAGE_KEYSET = """
SELECT ticket_id, priority, created_at FROM support.tickets WHERE state='waiting_human'
  AND (priority, created_at, ticket_id) < (%s, %s, %s)
ORDER BY priority DESC, created_at, ticket_id LIMIT %s
"""


def database_url(name: str) -> str:
    override = os.environ.get("S1_DATABASE_URL", "")
    if override:
        return override.replace("postgresql+psycopg://", "postgresql://")
    values = {}
    for line in (ROOT / ".local" / "application.env").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    base = values["AGENT_OPS_DATABASE_URL"].rsplit("/", 1)[0]
    return f"{base.replace('postgresql+psycopg://', 'postgresql://')}/{name}"


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q / 100
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "mean": None}
    return {
        "count": len(values),
        "mean": statistics.mean(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "min": min(values),
        "max": max(values),
    }


async def reset_queue(connection: psycopg.AsyncConnection) -> int:
    async with connection.cursor() as cursor:
        await cursor.execute(
            "UPDATE support.tickets SET state='waiting_human', assigned_account_id=NULL,"
            " lease_expires_at=NULL WHERE state='claimed'"
        )
        count = cursor.rowcount
        await cursor.execute("ANALYZE support.tickets")
    await connection.commit()
    return count


async def refill(connection: psycopg.AsyncConnection, limit: int) -> list[str]:
    async with connection.cursor() as cursor:
        await cursor.execute(
            "UPDATE support.tickets SET state='waiting_human', assigned_account_id=NULL,"
            " lease_expires_at=NULL, revision=revision+1 WHERE ticket_id IN ("
            " SELECT ticket_id FROM support.tickets WHERE state='claimed'"
            " AND updated_at < now()-interval '1 second' LIMIT %s)"
            " RETURNING ticket_id",
            (limit,),
        )
        ids = [row[0] for row in await cursor.fetchall()]
    await connection.commit()
    return ids


class Stats:
    def __init__(self) -> None:
        self.claims = 0
        self.conflicts = 0
        self.duplicates = 0
        self.latencies: list[float] = []
        self.seen: dict[str, int] = {}
        self.pages: dict[int, list[float]] = {}


async def claim_once(connection: psycopg.AsyncConnection, args: argparse.Namespace,
                     agent: int, stats: Stats) -> bool:
    started = time.perf_counter()
    async with connection.cursor() as cursor:
        if args.claim_mode == "skip_locked":
            await cursor.execute(CLAIM_SKIP, (agent,))
            row = await cursor.fetchone()
            await connection.commit()
            ok = row is not None
        else:
            await cursor.execute(PICK)
            candidates = await cursor.fetchall()
            if not candidates:
                await connection.rollback()
                return False
            # 真实工作台：座席从首页 20 条里挑一条，而不是固定抢队首。
            picked = candidates[random.Random(args.seed * 7919 + agent).randrange(len(candidates))]
            ticket_id, revision = picked
            if args.claim_mode == "cas":
                await cursor.execute(CLAIM_CAS, (agent, ticket_id, revision))
            else:
                await cursor.execute(CLAIM_TWO_STEP, (agent, ticket_id))
            row = await cursor.fetchone()
            await connection.commit()
            ok = row is not None
            if not ok:
                stats.conflicts += 1
    elapsed = (time.perf_counter() - started) * 1000
    stats.latencies.append(elapsed)
    if ok:
        stats.claims += 1
        ticket_key = str(row[0])
        previous = stats.seen.get(ticket_key)
        if previous is not None and previous != agent:
            stats.duplicates += 1
        stats.seen[ticket_key] = agent
    return ok


async def agent_loop(agent: int, args: argparse.Namespace, stats: Stats,
                     deadline: float, connection: psycopg.AsyncConnection) -> None:
    rng = random.Random(args.seed * 1000 + agent)
    while time.monotonic() < deadline:
        claimed = await claim_once(connection, args, agent, stats)
        if not claimed and args.claim_mode == "skip_locked":
            await asyncio.sleep(0.002)
        if args.think > 0:
            await asyncio.sleep(max(0.0, args.think + rng.uniform(-0.2, 0.2)))


async def cursor_for(connection: psycopg.AsyncConnection, page: int, size: int):
    """返回上一页最后一行的游标；第 1 页返回 None（走无游标查询）。"""
    if page == 1:
        return None
    async with connection.cursor() as cursor:
        await cursor.execute(PAGE_OFFSET, (1, (page - 1) * size - 1))
        row = await cursor.fetchone()
    await connection.rollback()
    return (row[1], row[2], row[0]) if row else None


async def list_loop(client: int, args: argparse.Namespace, stats: Stats, deadline: float,
                    connection: psycopg.AsyncConnection) -> None:
    rng = random.Random(args.seed * 100 + client)
    pages = [int(value) for value in args.pages.split(",")]
    cursors = {}
    if args.page_mode == "keyset":
        for page in pages:
            cursors[page] = await cursor_for(connection, page, args.page_size)
    while time.monotonic() < deadline:
        page = rng.choice(pages)
        started = time.perf_counter()
        async with connection.cursor() as cursor:
            if args.page_mode == "keyset":
                cursor_value = cursors.get(page)
                if cursor_value is None:
                    await cursor.execute(PAGE_FIRST, (args.page_size,))
                else:
                    await cursor.execute(
                        PAGE_KEYSET,
                        (cursor_value[0], cursor_value[1], cursor_value[2], args.page_size))
            else:
                await cursor.execute(PAGE_OFFSET, (args.page_size, (page - 1) * args.page_size))
            await cursor.fetchall()
        await connection.rollback()
        stats.pages.setdefault(page, []).append((time.perf_counter() - started) * 1000)


async def run(args: argparse.Namespace) -> dict[str, object]:
    url = database_url(args.database)
    admin = await psycopg.AsyncConnection.connect(url, autocommit=True)
    reset = await reset_queue(admin)

    stats = Stats()
    connections = []
    started = time.monotonic()
    deadline = started + args.seconds
    refill_ids: list[str] = []

    async def refill_loop() -> None:
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            ids = await refill(admin, args.refill_batch)
            for ticket_id in ids:
                stats.seen.pop(str(ticket_id), None)
            refill_ids.extend(ids)

    agents = []
    clients = []
    for index in range(args.agents):
        connection = await psycopg.AsyncConnection.connect(url)
        connections.append(connection)
        agents.append(asyncio.create_task(
            agent_loop(index + 1, args, stats, deadline, connection)))
    for index in range(args.list_clients):
        connection = await psycopg.AsyncConnection.connect(url)
        connections.append(connection)
        clients.append(asyncio.create_task(
            list_loop(index, args, stats, deadline, connection)))
    refill_task = asyncio.create_task(refill_loop()) if args.refill else None

    await asyncio.gather(*agents)
    await asyncio.gather(*clients)
    if refill_task is not None:
        refill_task.cancel()
        await asyncio.gather(refill_task, return_exceptions=True)
    elapsed = time.monotonic() - started
    for connection in connections:
        await connection.close()

    index_rows = []
    async with admin.cursor() as cursor:
        await cursor.execute(
            "SELECT indexname, pg_relation_size(schemaname||'.'||indexname)"
            " FROM pg_indexes WHERE schemaname='support' AND tablename='tickets'"
        )
        index_rows = [(row[0], int(row[1])) for row in await cursor.fetchall()]
        await cursor.execute("SHOW shared_buffers")
        shared = (await cursor.fetchone())[0]
        await cursor.execute("SHOW effective_cache_size")
        cache = (await cursor.fetchone())[0]
    await admin.close()

    return {
        "label": args.label,
        "group": args.group,
        "claim_mode": args.claim_mode,
        "page_mode": args.page_mode,
        "think_seconds": args.think,
        "agents": args.agents,
        "list_clients": args.list_clients,
        "page_size": args.page_size,
        "pages": [int(value) for value in args.pages.split(",")],
        "seconds": elapsed,
        "claims": stats.claims,
        "conflicts": stats.conflicts,
        "duplicates": stats.duplicates,
        "throughput_per_second": stats.claims / elapsed if elapsed > 0 else 0.0,
        "refilled": len(refill_ids),
        "reset_before_run": reset,
        "claim_latency_ms": summarize(stats.latencies),
        "page_latency_ms": {str(page): summarize(values) for page, values in stats.pages.items()},
        "indexes": [{"name": name, "bytes": size} for name, size in index_rows],
        "postgres": {"shared_buffers": shared, "effective_cache_size": cache},
        "started_at": datetime.now(UTC).isoformat(),
        "seed": args.seed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="agent_ops_p10_s1")
    parser.add_argument("--group", default="A")
    parser.add_argument("--claim-mode", choices=("cas", "skip_locked", "two_step"), required=True)
    parser.add_argument("--page-mode", choices=("offset", "keyset"), required=True)
    parser.add_argument("--agents", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--think", type=float, default=0.0)
    parser.add_argument("--list-clients", type=int, default=20)
    parser.add_argument("--pages", default="1,100,1000,2500")
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--refill", action="store_true")
    parser.add_argument("--refill-batch", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    document = asyncio.run(run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.out)
    print(json.dumps({key: document[key] for key in
                      ("label", "claims", "throughput_per_second", "duplicates", "conflicts")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
