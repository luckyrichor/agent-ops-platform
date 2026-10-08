"""Per-request bounded SSE channel; disconnect cancels that request's tools."""
import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

from fastapi import HTTPException
from opentelemetry.trace import Status, StatusCode, get_current_span

from agent_ops.models import RunResult
from agent_ops.planner import PlanningError


class SlowConsumer(TimeoutError):
    pass


async def stream_run(run: Callable[[], Awaitable[RunResult]],
                     disconnected: Callable[[], Awaitable[bool]], *,
                     interval: float = 0.1, stall_timeout: float = 2,
                     trace_id: str | None = None) -> AsyncIterator[str]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
    terminal: str | None = None
    span = get_current_span()

    def frame(event: str, data: dict[str, object]) -> str:
        return f"event: {event}\ndata: " + json.dumps(
            {**data, "trace_id": trace_id or data.get("trace_id")}, ensure_ascii=False) + "\n\n"

    async def emit(event: str, data: dict[str, object]) -> None:
        try:
            await asyncio.wait_for(queue.put(frame(event, data)), timeout=stall_timeout)
        except TimeoutError:
            raise SlowConsumer from None

    async def produce() -> None:
        nonlocal terminal
        work = asyncio.ensure_future(run())
        try:
            await emit("started", {})
            while not work.done():
                done, _ = await asyncio.wait({work}, timeout=interval)
                if not done:
                    await emit("heartbeat", {})
            result = await work
            await emit("result", result.model_dump(mode="json"))
        except SlowConsumer:
            # Reserve a terminal frame outside the full queue; deliver when reads resume.
            terminal = frame("aborted", {"code": "SLOW_CONSUMER"})
            span.set_attribute("outcome", "aborted")
            span.set_attribute("reason_code", "SLOW_CONSUMER")
        except PlanningError as error:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("reason_code", error.code)
            terminal = frame("error", {"code": error.code})
        except HTTPException as error:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("reason_code", "HTTP_REJECTED")
            terminal = frame("error", {"code": error.detail})
        except Exception:  # noqa: BLE001 - redact tool failures in transport
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("reason_code", "RUN_FAILED")
            terminal = frame("error", {"code": "RUN_FAILED"})
        finally:
            work.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work

    producer = asyncio.create_task(produce())
    try:
        while not producer.done() or not queue.empty():
            if await disconnected():
                span.set_attribute("outcome", "cancelled")
                span.set_attribute("reason_code", "CLIENT_DISCONNECTED")
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                continue
            yield event
        else:
            if terminal is not None:
                yield terminal
    finally:
        if not producer.done() and terminal is None:
            span.set_attribute("outcome", "cancelled")
            span.set_attribute("reason_code", "CLIENT_DISCONNECTED")
        producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
