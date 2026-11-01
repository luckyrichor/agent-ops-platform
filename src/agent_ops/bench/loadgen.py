#!/usr/bin/env python3
"""P9 负载生成器（仅标准库）：在 Mac 上运行，对 bench 实例发真实 HTTP/1.1 连接。

两种场景：
  business  POST /v1/commerce/stream，走 planner 桩/四角色编排/SSE
  transport GET  /bench/hold，显式长连接 fixture，只测传输与慢读，不代表业务完成率

固定并发 N 个 worker 闭环、按 ramp 逐步建连；记录实际并发连接上限、健康首帧、
错误分类与延迟分位。慢读按字节/秒限制真实 socket 读取，不是只在回调里 sleep。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# macOS 自带 Python 3.9 的 asyncio.TimeoutError 与内建 TimeoutError 不是同一个类，
# 生成器在 Mac 上运行，必须两者都捕获。
TIMEOUT_ERRORS = (asyncio.TimeoutError, TimeoutError)


def percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("empty sample")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q / 100
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values),
    }


def is_slow(index: int, ratio: float, seed: int) -> bool:
    if ratio <= 0:
        return False
    if ratio >= 1:
        return True
    digest = hashlib.sha256(f"{seed}:slow:{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < ratio


@dataclass
class ConnResult:
    index: int
    slow: bool
    status: int | None = None
    connect_ms: float = 0.0
    ttfb_ms: float = 0.0
    first_event_ms: float = 0.0
    full_ms: float = 0.0
    frames: int = 0
    bytes_read: int = 0
    held_ms: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200


@dataclass
class Recorder:
    results: list[ConnResult] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)
    open_now: int = 0
    max_open: int = 0
    slow_seen: set[int] = field(default_factory=set)
    healthy: set[int] = field(default_factory=set)
    started: set[int] = field(default_factory=set)
    completed: int = 0

    def begin(self, index: int) -> None:
        self.open_now += 1
        self.max_open = max(self.max_open, self.open_now)
        self.started.add(index)

    def end(self, result: ConnResult) -> None:
        self.open_now -= 1
        self.results.append(result)
        if result.slow:
            self.slow_seen.add(result.index)
        if result.status == 200 and result.first_event_ms > 0:
            self.healthy.add(result.index)
        if result.ok:
            self.completed += 1
        if result.error:
            self.errors[result.error] += 1


class SlowReader:
    """按字节/秒真实限速读取；暂停期间不读 socket，让内核接收缓冲形成背压。"""

    def __init__(self, reader: asyncio.StreamReader, bytes_per_second: int) -> None:
        self.reader, self.rate = reader, max(1, bytes_per_second)
        self.allowance = 0.0
        self.updated = time.monotonic()

    async def read(self, size: int = 4096) -> bytes:
        now = time.monotonic()
        self.allowance = min(self.rate, self.allowance + (now - self.updated) * self.rate)
        self.updated = now
        if self.allowance < 1:
            await asyncio.sleep((1 - self.allowance) / self.rate)
            self.allowance, self.updated = 0.0, time.monotonic()
        chunk = await self.reader.read(min(size, max(1, int(self.allowance))))
        self.allowance = max(0.0, self.allowance - len(chunk))
        return chunk


async def read_headers(reader: asyncio.StreamReader, deadline: float) -> tuple[int, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("headers_timeout")
        chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
        if not chunk:
            raise ConnectionError("eof_before_headers")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    fields = lines[0].split()
    status = int(fields[1]) if len(fields) > 1 else 0
    return status, rest


def parse_events(buffer: bytes) -> tuple[list[str], bytes]:
    events: list[str] = []
    while b"\n\n" in buffer:
        raw, _, buffer = buffer.partition(b"\n\n")
        event = ""
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
        events.append(event)
    return events, buffer


async def open_connection(args: argparse.Namespace) -> tuple[asyncio.StreamReader, Any]:
    return await asyncio.wait_for(
        asyncio.open_connection(args.host, args.port), timeout=args.connect_timeout
    )


async def business_once(args: argparse.Namespace, index: int, slow: bool,
                        recorder: Recorder) -> ConnResult:
    result = ConnResult(index=index, slow=slow)
    started = time.monotonic()
    recorder.begin(index)
    writer = None
    try:
        try:
            reader, writer = await open_connection(args)
        except (OSError, *TIMEOUT_ERRORS) as error:
            result.error = f"connect:{type(error).__name__}"
            return result
        result.connect_ms = (time.monotonic() - started) * 1000
        body = json.dumps({
            "request": f"bench query {index}",
            "category": args.category,
            "budget_cent": 5000,
            "region": args.region,
            "run_id": str(uuid.uuid4()),
        }).encode()
        headers = [
            f"POST {args.path} HTTP/1.1",
            f"Host: {args.host}:{args.port}",
            "Content-Type: application/json",
            f"Content-Length: {len(body)}",
            "Accept: text/event-stream",
            "Connection: close",
        ]
        if args.token:
            headers.append(f"Authorization: Bearer {args.token}")
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode() + body)
        await writer.drain()
        deadline = time.monotonic() + args.request_timeout
        status, rest = await read_headers(reader, deadline)
        result.status = status
        result.ttfb_ms = (time.monotonic() - started) * 1000
        if status != 200:
            result.error = f"http_{status}"
            return result
        stream = SlowReader(reader, args.slow_bps) if slow else reader
        buffer = rest
        while True:
            events, buffer = parse_events(buffer)
            for event in events:
                result.frames += 1
                if result.first_event_ms == 0:
                    result.first_event_ms = (time.monotonic() - started) * 1000
                if event == "result":
                    result.full_ms = (time.monotonic() - started) * 1000
                    return result
                if event == "error":
                    result.error = "sse_error"
                    result.full_ms = (time.monotonic() - started) * 1000
                    return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result.error = "request_timeout"
                return result
            chunk = await asyncio.wait_for(stream.read(8192), timeout=remaining)
            if not chunk:
                result.error = "eof_before_result"
                return result
            result.bytes_read += len(chunk)
            buffer += chunk
    except TIMEOUT_ERRORS:
        result.error = "timeout"
    except (ConnectionError, OSError) as error:
        result.error = f"transport:{type(error).__name__}"
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        recorder.end(result)
    return result


async def transport_once(args: argparse.Namespace, index: int, slow: bool,
                         recorder: Recorder) -> ConnResult:
    result = ConnResult(index=index, slow=slow)
    started = time.monotonic()
    recorder.begin(index)
    writer = None
    try:
        try:
            reader, writer = await open_connection(args)
        except (OSError, *TIMEOUT_ERRORS) as error:
            result.error = f"connect:{type(error).__name__}"
            return result
        result.connect_ms = (time.monotonic() - started) * 1000
        writer.write((
            f"GET /bench/hold?seconds={args.hold_seconds + 10} HTTP/1.1\r\n"
            f"Host: {args.host}:{args.port}\r\nAccept: text/event-stream\r\n"
            "Connection: close\r\n\r\n"
        ).encode())
        await writer.drain()
        deadline = time.monotonic() + args.hold_seconds + 15
        status, rest = await read_headers(reader, deadline)
        result.status = status
        result.ttfb_ms = (time.monotonic() - started) * 1000
        if status != 200:
            result.error = f"http_{status}"
            return result
        stream = SlowReader(reader, args.slow_bps) if slow else reader
        buffer = rest
        while time.monotonic() - started < args.hold_seconds:
            events, buffer = parse_events(buffer)
            if events:
                result.frames += len(events)
                if result.first_event_ms == 0:
                    result.first_event_ms = (time.monotonic() - started) * 1000
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = await asyncio.wait_for(stream.read(8192), timeout=remaining)
            if not chunk:
                result.error = "eof_during_hold"
                break
            result.bytes_read += len(chunk)
            buffer += chunk
        result.held_ms = (time.monotonic() - started) * 1000
    except TIMEOUT_ERRORS:
        result.error = "timeout"
    except (ConnectionError, OSError) as error:
        result.error = f"transport:{type(error).__name__}"
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
        recorder.end(result)
    return result


async def run(args: argparse.Namespace) -> dict[str, Any]:
    recorder = Recorder()
    started = time.monotonic()
    total = args.warmup + args.ramp + args.steady + args.drain
    stop_at = started + total
    args.hold_seconds = args.hold_seconds or total

    async def worker(number: int) -> None:
        if args.ramp > 0:
            await asyncio.sleep(args.ramp * number / max(1, args.connections))
        index = number
        while time.monotonic() < stop_at:
            try:
                if args.kind == "transport":
                    await transport_once(args, index, is_slow(index, args.slow_ratio, args.seed),
                                         recorder)
                else:
                    await business_once(args, index, is_slow(index, args.slow_ratio, args.seed),
                                        recorder)
            except Exception as error:  # noqa: BLE001 - 单个 worker 失败不能拖垮整轮
                recorder.errors[f"worker:{type(error).__name__}"] += 1
                await asyncio.sleep(0.05)
            index += args.connections

    await asyncio.gather(*(worker(i + 1) for i in range(args.connections)),
                         return_exceptions=True)
    ended = time.monotonic()

    def sample(attr: str) -> list[float]:
        return [getattr(row, attr) for row in recorder.results
                if row.status == 200 and getattr(row, attr) > 0]

    return {
        "config": {
            "kind": args.kind,
            "connections": args.connections,
            "slow_reader_ratio": args.slow_ratio,
            "slow_bps": args.slow_bps,
            "seed": args.seed,
            "timings": {
                "warmup": args.warmup, "ramp": args.ramp,
                "steady": args.steady, "drain": args.drain,
                "hold": args.hold_seconds,
            },
            "path": args.path,
        },
        "started_at_unix": started,
        "ended_at_unix": ended,
        "elapsed_seconds": ended - started,
        "target_connections": args.connections,
        "achieved_connections": recorder.max_open,
        "distinct_connections": len(recorder.started),
        "healthy_connections": len(recorder.healthy),
        "completed_requests": recorder.completed,
        "slow_connections": len(recorder.slow_seen),
        "throughput": {
            "completed": recorder.completed,
            "steady_seconds": args.steady,
            "per_second": recorder.completed / max(1e-9, args.steady),
        },
        "errors": dict(recorder.errors),
        "latency_ms": {
            "connect": summarize(sample("connect_ms")),
            "ttfb": summarize(sample("ttfb_ms")),
            "first_event": summarize(sample("first_event_ms")),
            "full": summarize(sample("full_ms")),
            "held": summarize(sample("held_ms")),
        },
        "frames_total": sum(row.frames for row in recorder.results),
        "bytes_total": sum(row.bytes_read for row in recorder.results),
        "rows": [row.__dict__ for row in recorder.results] if args.rows else None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--kind", choices=("business", "transport"), default="business")
    parser.add_argument("--path", default="/v1/commerce/stream")
    parser.add_argument("--connections", type=int, default=100)
    parser.add_argument("--warmup", type=float, default=60)
    parser.add_argument("--ramp", type=float, default=20)
    parser.add_argument("--steady", type=float, default=180)
    parser.add_argument("--drain", type=float, default=30)
    parser.add_argument("--hold-seconds", type=float, default=0)
    parser.add_argument("--slow-ratio", type=float, default=0.0)
    parser.add_argument("--slow-bps", type=int, default=1024)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--category", default="electronics")
    parser.add_argument("--region", default="north")
    parser.add_argument("--token", default="")
    parser.add_argument("--connect-timeout", type=float, default=10)
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument("--rows", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    document = asyncio.run(run(args))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_suffix(args.out.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2))
    temporary.replace(args.out)
    print(json.dumps({key: document[key] for key in
                      ("target_connections", "achieved_connections", "healthy_connections",
                       "completed_requests", "errors", "throughput")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
