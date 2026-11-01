from __future__ import annotations

import argparse
import asyncio

from agent_ops.bench import loadgen


def namespace(**overrides: object) -> argparse.Namespace:
    base = {"host": "127.0.0.1", "port": 0, "path": "/v1/commerce/stream", "token": "",
            "connect_timeout": 2, "request_timeout": 5, "slow_bps": 1_000_000,
            "category": "electronics", "region": "north", "slow_ratio": 0.0,
            "seed": 42, "hold_seconds": 1, "kind": "business", "connections": 1,
            "warmup": 0, "ramp": 0, "steady": 1, "drain": 0, "rows": False, "out": None}
    base.update(overrides)
    return argparse.Namespace(**base)


def test_parse_events_keeps_partial_buffer() -> None:
    events, rest = loadgen.parse_events(b"event: started\ndata: {}\n\nevent: hear")
    assert events == ["started"]
    assert rest == b"event: hear"


def test_slow_selection_is_deterministic() -> None:
    assert loadgen.is_slow(3, 0.5, 9) == loadgen.is_slow(3, 0.5, 9)
    assert loadgen.is_slow(1, 0.0, 9) is False
    assert loadgen.is_slow(1, 1.0, 9) is True


def test_recorder_tracks_peak_and_errors() -> None:
    recorder = loadgen.Recorder()
    recorder.begin(1)
    recorder.begin(2)
    assert recorder.max_open == 2
    recorder.end(loadgen.ConnResult(index=1, slow=False, status=200, first_event_ms=5.0))
    recorder.end(loadgen.ConnResult(index=2, slow=True, status=None, error="connect:OSError"))
    assert recorder.max_open == 2
    assert recorder.open_now == 0
    assert recorder.completed == 1
    assert recorder.healthy == {1}
    assert recorder.errors["connect:OSError"] == 1


async def serve_once(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readuntil(b"\r\n\r\n")
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
        b"event: started\ndata: {}\n\n"
        b"event: result\ndata: {\"ok\":true}\n\n"
    )
    await writer.drain()
    writer.close()


async def test_business_once_completes_against_real_socket() -> None:
    server = await asyncio.start_server(serve_once, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    recorder = loadgen.Recorder()
    try:
        result = await loadgen.business_once(namespace(port=port), 1, False, recorder)
    finally:
        server.close()
        await server.wait_closed()
    assert result.status == 200
    assert result.error is None
    assert result.frames == 2
    assert result.first_event_ms > 0
    assert recorder.completed == 1


async def test_business_once_reports_connection_failure() -> None:
    recorder = loadgen.Recorder()
    result = await loadgen.business_once(namespace(port=1), 1, False, recorder)
    assert result.status is None
    assert result.error is not None
    assert recorder.errors


async def test_transport_hold_reads_heartbeats() -> None:
    async def serve_hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
                     b"event: started\ndata: {}\n\nevent: heartbeat\ndata: {}\n\n")
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(serve_hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    recorder = loadgen.Recorder()
    try:
        result = await loadgen.transport_once(
            namespace(port=port, kind="transport", hold_seconds=0.3), 1, False, recorder)
    finally:
        server.close()
        await server.wait_closed()
    assert result.status == 200
    assert result.frames >= 1
