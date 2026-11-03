#!/usr/bin/env python3
"""S2 HTTP 负载：200 并发、30% 重复键，交替打两个实例。"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx


async def run(args: argparse.Namespace) -> dict[str, object]:
    unique = args.unique
    duplicate = int(unique * args.duplicate_ratio)
    keys = [f"s2-http-{uuid.uuid4().hex}" for _ in range(unique)]
    keys += [keys[index % unique] for index in range(duplicate)]
    ports = [int(port) for port in args.ports.split(",")]
    body = {"message": "s2 http probe", "order_id": None}
    latencies: list[float] = []
    errors: dict[str, int] = {}
    statuses: dict[str, int] = {}
    semaphore = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(timeout=60, trust_env=False,
                                 limits=httpx.Limits(max_connections=args.concurrency)) as client:
        async def one(index: int, key: str) -> None:
            port = ports[index % len(ports)]
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.post(
                        f"http://127.0.0.1:{port}/bench/requests",
                        json=body, headers={"Idempotency-Key": key},
                    )
                    statuses[str(response.status_code)] = statuses.get(str(response.status_code), 0) + 1
                    if response.status_code != 200:
                        errors[f"http_{response.status_code}"] = errors.get(f"http_{response.status_code}", 0) + 1
                except httpx.HTTPError as error:
                    errors[type(error).__name__] = errors.get(type(error).__name__, 0) + 1
                finally:
                    latencies.append((time.perf_counter() - started) * 1000)

        started_at = datetime.now(UTC)
        started = time.perf_counter()
        await asyncio.gather(*(one(index, key) for index, key in enumerate(keys)))
        elapsed = time.perf_counter() - started

    return {
        "requests": len(keys),
        "unique_keys": unique,
        "duplicate_requests": duplicate,
        "concurrency": args.concurrency,
        "ports": len(ports),
        "elapsed_seconds": elapsed,
        "throughput_per_second": len(keys) / elapsed,
        "latency_ms": {
            "p50": statistics.median(latencies),
            "p95": sorted(latencies)[int(0.95 * len(latencies)) - 1],
            "max": max(latencies),
        },
        "statuses": statuses,
        "errors": errors,
        "started_at": started_at.isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports", default="18911,18912")
    parser.add_argument("--unique", type=int, default=154)
    parser.add_argument("--duplicate-ratio", type=float, default=0.3)
    parser.add_argument("--concurrency", type=int, default=200)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    document = asyncio.run(run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False)[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
