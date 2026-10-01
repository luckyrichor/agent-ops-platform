"""Bounded stream contention measurement, local fixtures, no production claims."""
import asyncio
import hashlib
import json
import platform
import subprocess
import time
from pathlib import Path

from agent_ops.agents import demo_agents
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import Dispatcher
from agent_ops.streaming import stream_run


async def measure():
    cancelled = asyncio.Event()
    async def slow():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def connected():
        return False
    stalled = stream_run(slow, connected, interval=0.001, stall_timeout=0.02)
    await anext(stalled)
    async def consume():
        start = time.perf_counter()
        async def run():
            return await MemoryCommerce(Dispatcher(demo_agents()), None).run(CommerceRequest(
                request="水壶", category="kettle", budget_cent=5000, region="上海"))
        events = [event async for event in stream_run(run, connected, interval=0.001)]
        assert any('event: result' in event for event in events)
        return (time.perf_counter()-start)*1000
    start = time.perf_counter()
    latencies = await asyncio.gather(*(consume() for _ in range(32)))
    elapsed = (time.perf_counter()-start)*1000
    await asyncio.wait_for(cancelled.wait(), 1)
    await stalled.aclose()
    return {"normal_streams": 32, "completed": len(latencies), "stalled_streams": 1,
            "stalled_cancelled": cancelled.is_set(), "normal_batch_ms": elapsed,
            "normal_latency_ms": latencies, "queue_capacity": 2,
            "method": "same asyncio loop, deterministic commerce handlers, one unread iterator"}


result = asyncio.run(measure())
result.update({"machine": platform.node(), "platform": platform.platform(),
    "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "uncommitted": subprocess.check_output(["git", "status", "--short"], text=True),
    "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in Path("src").rglob("*.py")}})
destination = Path("docs/measurements/2026-10-02-streams.json")
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(result, indent=2))
print(json.dumps({k: v for k, v in result.items() if k in {
    "completed", "stalled_cancelled", "normal_batch_ms"}}))
