"""Per-request bounded SSE channel; disconnect cancels that request's tools."""
import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

from agent_ops.models import RunResult


async def stream_run(run: Callable[[], Awaitable[RunResult]],
                     disconnected: Callable[[], Awaitable[bool]], *,
                     interval: float = 0.1, stall_timeout: float = 2) -> AsyncIterator[str]:
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=2)

    async def emit(event: str, data: object) -> None:
        await asyncio.wait_for(queue.put(f"event: {event}\ndata: " +
                                        json.dumps(data, ensure_ascii=False) + "\n\n"),
                               timeout=stall_timeout)

    async def produce() -> None:
        work = asyncio.ensure_future(run())
        try:
            await emit("started", {})
            while not work.done():
                done, _ = await asyncio.wait({work}, timeout=interval)
                if not done:
                    await emit("heartbeat", {})
            result = await work
            await emit("result", result.model_dump(mode="json"))
        except TimeoutError:
            pass  # Slow subscriber loses only its own run.
        except Exception:  # noqa: BLE001 - redact tool failures in transport
            await emit("error", {"code": "RUN_FAILED"})
        finally:
            work.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work

    producer = asyncio.create_task(produce())
    try:
        while not producer.done() or not queue.empty():
            if await disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                continue
            if event is not None:
                yield event
    finally:
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
