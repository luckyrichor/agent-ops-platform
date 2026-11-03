#!/usr/bin/env python3
"""S2 功能基线：TTL 边界、跨连接池重复创建（等价两进程）、同键异体冲突。

直接调用 WorkbenchService（数据库路径）；HTTP/uvicorn 并发压测后续单独跑。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry import trace
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_ops.support import SupportRequest
from agent_ops.workbench import WorkbenchService

ROOT = Path(__file__).resolve().parents[1]


def url() -> str:
    override = os.environ.get("S1_DATABASE_URL", "")
    if override:
        return override.replace("postgresql+psycopg://", "postgresql+psycopg://")
    values = {}
    for line in (ROOT / ".local" / "application.env").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values["AGENT_OPS_DATABASE_URL"]


async def ttl_case(service: WorkbenchService, key: str) -> dict[str, object]:
    request = SupportRequest(message="s2 ttl probe", order_id=None)
    first = await service.create(request, key, None)
    async with service.engine.begin() as c:
        await c.execute(text("UPDATE support.idempotency_keys SET expires_at=now()-interval '1 second'"
                             " WHERE scope=:s AND key=:k"),
                        {"s": "support.request", "k": key})
    second = await service.create(request, key, None)
    async with service.engine.connect() as c:
        row = (
            await c.execute(text("SELECT expires_at>now() AS fresh FROM support.idempotency_keys"
                                 " WHERE scope='support.request' AND key=:k"), {"k": key})
        ).one()
    return {
        "first_ticket": first.ticket_id,
        "second_ticket": second.ticket_id,
        "reexecuted": first.ticket_id != second.ticket_id,
        "expiry_refreshed": bool(row.fresh),
    }


async def duplicate_case(service_a: WorkbenchService, service_b: WorkbenchService,
                         *, unique: int, duplicate_ratio: float,
                         no_idempotency: bool = False) -> dict[str, object]:
    request = SupportRequest(message="s2 duplicate probe", order_id=None)
    duplicate_count = int(unique * duplicate_ratio)
    keys = [f"s2-dup-{index}" for index in range(unique)]
    keys += [keys[index % unique] for index in range(duplicate_count)]
    latencies: list[float] = []
    created: dict[str, str] = {}
    errors: dict[str, int] = {}

    async def one(index: int, key: str) -> None:
        service = service_a if index % 2 == 0 else service_b
        effective = f"{key}-{uuid.uuid4().hex}" if no_idempotency else key
        started = time.perf_counter()
        try:
            ticket = await service.create(request, effective, None)
            created[key] = ticket.ticket_id
        except Exception as error:  # noqa: BLE001 - 记录分类，不吞掉
            errors[type(error).__name__] = errors.get(type(error).__name__, 0) + 1
        finally:
            latencies.append((time.perf_counter() - started) * 1000)

    started_at = datetime.now(UTC)
    started = time.perf_counter()
    await asyncio.gather(*(one(index, key) for index, key in enumerate(keys)))
    elapsed = time.perf_counter() - started
    async with service_a.engine.connect() as c:
        tickets_created = await c.scalar(
            text("SELECT count(DISTINCT m.ticket_id) FROM support.messages m"
                 " WHERE m.content='s2 duplicate probe' AND m.created_at>=:t"), {"t": started_at})
        distinct = tickets_created
    return {
        "requests": len(keys),
        "unique_keys": unique,
        "duplicate_requests": duplicate_count,
        "distinct_tickets_for_keys": distinct,
            "duplicate_tickets": int(distinct or 0) - unique,
        "errors": errors,
        "elapsed_seconds": elapsed,
        "throughput_per_second": len(keys) / elapsed,
        "latency_ms": {
            "p50": statistics.median(latencies),
            "p95": sorted(latencies)[int(0.95 * len(latencies)) - 1],
            "max": max(latencies),
        },
        "tickets_created": int(tickets_created or 0),
    }


async def main_async(args: argparse.Namespace) -> dict[str, object]:
    database = url()
    engine_a = create_async_engine(database, pool_size=20, max_overflow=10)
    engine_b = create_async_engine(database, pool_size=20, max_overflow=10)
    tracer = trace.get_tracer("s2-bench")
    service_a = WorkbenchService(database, tracer)
    service_b = WorkbenchService(database, tracer)
    document: dict[str, object] = {"started_at": datetime.now(UTC).isoformat()}
    if args.mode == "ttl":
        document["ttl"] = await ttl_case(service_a, args.key)
    else:
        document["duplicate"] = await duplicate_case(
            service_a, service_b, unique=args.unique, duplicate_ratio=args.duplicate_ratio,
            no_idempotency=args.no_idempotency)
    document["finished_at"] = datetime.now(UTC).isoformat()
    await service_a.aclose()
    await service_b.aclose()
    await engine_a.dispose()
    await engine_b.dispose()
    return document


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("ttl", "duplicate"))
    parser.add_argument("--key", default="s2-ttl-probe")
    parser.add_argument("--unique", type=int, default=140)
    parser.add_argument("--duplicate-ratio", type=float, default=0.3)
    parser.add_argument("--no-idempotency", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    document = asyncio.run(main_async(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False)[:800])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
