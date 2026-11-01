"""DeepSeek-compatible 模型桩（asyncio 原生 HTTP/1.1），可控延迟与确定性故障。

只接受 POST /v1/chat/completions；不做代理、不访问真实 API、不记录请求正文。
延迟按 request_id 从经验 CDF 取分位点，固定种子，可复现。
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_ops.bench.faults import FaultProfile

PLAN_CONTENT = json.dumps(
    {
        "tasks": [
            {"task_id": "catalog", "agent": "catalog", "depends_on": []},
            {"task_id": "pricing", "agent": "pricing", "depends_on": ["catalog"]},
            {"task_id": "shipping", "agent": "shipping", "depends_on": ["catalog"]},
            {
                "task_id": "recommendation",
                "agent": "recommendation",
                "depends_on": ["pricing", "shipping"],
            },
        ]
    },
    ensure_ascii=False,
    separators=(",", ":"),
)


def _draw_index(request_id: str, seed: int, size: int) -> float:
    import hashlib

    digest = hashlib.sha256(f"{seed}:latency:{request_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 * size


@dataclass
class LatencyModel:
    """经验 CDF：样本按毫秒升序存档，按 request_id 取分位点，线性插值。"""

    samples_ms: list[float]
    seed: int = 42
    overhead_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.samples_ms:
            raise ValueError("latency samples required")
        self.samples_ms = sorted(float(value) for value in self.samples_ms)
        if self.samples_ms[0] < 0:
            raise ValueError("negative latency sample")

    def sleep_seconds(self, request_id: str) -> float:
        position = _draw_index(request_id, self.seed, len(self.samples_ms) - 1)
        low = int(position)
        high = min(low + 1, len(self.samples_ms) - 1)
        fraction = position - low
        value = self.samples_ms[low] + (self.samples_ms[high] - self.samples_ms[low]) * fraction
        return max(0.0, value - self.overhead_ms) / 1000.0


@dataclass
class StubStats:
    requests: int = 0
    faults: dict[str, int] = field(default_factory=dict)
    status_200: int = 0

    def record(self, kind: str) -> None:
        self.requests += 1
        self.faults[kind] = self.faults.get(kind, 0) + 1
        if kind == "none":
            self.status_200 += 1


class StubServer:
    def __init__(self, *, latency: LatencyModel, profile: FaultProfile,
                 host: str = "127.0.0.1", port: int = 0) -> None:
        self.latency, self.profile = latency, profile
        self.host, self.port = host, port
        self.stats = StubStats()
        self.started_at: float | None = None
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port, backlog=1024)
        self.started_at = asyncio.get_running_loop().time()
        sockets: list[socket.socket] = list(self._server.sockets or [])
        if sockets:
            self.port = sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}/v1/chat/completions"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "")
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin-1").partition(":")
                headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0") or 0)
            if length:
                await asyncio.wait_for(reader.readexactly(length), timeout=10)

            if method == "GET" and path.startswith("/healthz"):
                await self._json(writer, 200, {"status": "ok"})
                return
            if method == "GET" and path.startswith("/stats"):
                await self._json(writer, 200, {"requests": self.stats.requests,
                                               "faults": self.stats.faults,
                                               "status_200": self.stats.status_200})
                return
            if method != "POST" or not path.startswith("/v1/chat/completions"):
                await self._json(writer, 404, {"error": "not_found"})
                return

            request_id = headers.get("x-bench-request-id", "")
            if not request_id:
                await self._json(writer, 400, {"error": "missing_request_id"})
                return
            kind = self.profile.choose(request_id)
            delay = self.latency.sleep_seconds(request_id)
            if kind == "slow_tail":
                delay += self.profile.extra_delay_ms(request_id) / 1000.0
            self.stats.record(kind)
            if delay > 0:
                await asyncio.sleep(delay)

            if kind == "rate_limit":
                await self._json(
                    writer,
                    429,
                    {"error": {"message": "rate limited", "type": "rate_limit"}},
                    extra={"Retry-After": str(self.profile.retry_after_seconds)},
                )
            elif kind == "server_error":
                await self._json(writer, 500, {"error": {"message": "stub failure"}})
            elif kind == "connection_reset":
                self._reset(writer)
            elif kind == "disconnect_after_headers":
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 512\r\n\r\n"
                )
                await writer.drain()
                writer.close()
            elif kind == "truncated_body":
                body = json.dumps(self._payload()).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body[: len(body) // 2]
                )
                await writer.drain()
                writer.close()
            else:
                await self._json(writer, 200, self._payload())
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except (ConnectionError, OSError):
                pass

    def _payload(self) -> dict[str, Any]:
        return {
            "id": "stub",
            "object": "chat.completion",
            "model": "deepseek-bench-stub",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": PLAN_CONTENT},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def _json(self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any],
                    extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        head = [f"HTTP/1.1 {status} X", "Content-Type: application/json",
                f"Content-Length: {len(body)}", "Connection: close"]
        for key, value in (extra or {}).items():
            head.append(f"{key}: {value}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)
        await writer.drain()

    @staticmethod
    def _reset(writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass
        writer.transport.abort()


def main() -> int:
    """独立运行：python -m agent_ops.bench.stub --latency <json> --port 18899"""
    import argparse
    import asyncio as _asyncio

    parser = argparse.ArgumentParser()
    parser.add_argument("--latency", required=True, help="JSON 文件：毫秒样本数组或 {samples:[...]}")
    parser.add_argument("--port", type=int, default=18899)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--overhead-ms", type=float, default=0.0)
    parser.add_argument("--profile", default="healthy", choices=("healthy", "mixed_low"))
    parser.add_argument("--ready-file", default="")
    args = parser.parse_args()

    payload = json.loads(Path(args.latency).read_text(encoding="utf-8"))
    samples = payload["samples"] if isinstance(payload, dict) else payload
    from agent_ops.bench.faults import default_matrix

    profile = default_matrix(args.seed).get(args.profile)

    async def serve() -> None:
        server = StubServer(latency=LatencyModel([float(v) for v in samples], seed=args.seed,
                                                 overhead_ms=args.overhead_ms),
                            profile=profile, host=args.host, port=args.port)
        await server.start()
        if args.ready_file:
            Path(args.ready_file).write_text(str(server.port), encoding="utf-8")
        print(json.dumps({"port": server.port}), flush=True)
        await _asyncio.Event().wait()

    try:
        _asyncio.run(serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
