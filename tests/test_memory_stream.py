import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from agent_memory.sdk import MemoryAPIError, MemoryClient

from agent_ops.agents import demo_agents
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import Dispatcher
from agent_ops.streaming import stream_run


def body():
    return CommerceRequest(request="水壶", category="kettle", budget_cent=5000, region="上海")


@pytest.mark.asyncio
async def test_sdk_order_context_and_stable_write_key():
    calls = []
    memory_id, tenant, version = uuid4(), uuid4(), uuid4()
    def respond(request):
        assert request.headers["authorization"] == "Bearer caller"
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"tenant_id": str(tenant),
                "memory_id": str(memory_id), "memory_type": "episodic", "status": "active",
                "revision": 1, "content": '{"budget_cent":3000}'})
        calls.append(request.headers["idempotency-key"])
        assert json.loads(request.content)["scope"]["workspace_id"] == "commerce"
        return httpx.Response(201, json={"tenant_id": str(tenant), "memory_id": str(memory_id),
            "version_id": str(version), "revision": 1, "status": "active"})
    handlers = demo_agents()
    original = handlers["catalog"]
    async def catalog(req, inputs):
        calls.append("catalog")
        return await original(req, inputs)
    handlers["catalog"] = catalog
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond),
                                 base_url="http://memory") as client:
        service = MemoryCommerce(Dispatcher(handlers), MemoryClient(client, token="caller"))
        request = body().model_copy(update={"memory_id": memory_id})
        first, second = await service.run(request), await service.run(request)
    assert first.memory_id == second.memory_id == memory_id
    assert first.recommendation["selected"] is None
    assert calls == ["GET", "catalog", "POST", f"commerce:{request.run_id}"] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [503, 403])
async def test_memory_failure_does_not_forge_memory_success(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status)), base_url="http://memory") as client:
        result = await MemoryCommerce(Dispatcher(demo_agents()), MemoryClient(
            client, token="caller")).run(body().model_copy(update={"memory_id": uuid4()}))
    assert result.outcome == "succeeded"
    assert result.memory_status == "write_degraded" and result.memory_id is None


@pytest.mark.asyncio
async def test_idempotency_conflict_is_visible():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(409)), base_url="http://memory") as client:
        with pytest.raises(MemoryAPIError):
            await MemoryCommerce(Dispatcher(demo_agents()), MemoryClient(
                client, token="caller")).run(body())


@pytest.mark.asyncio
async def test_disconnect_cancels_inflight_tools():
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def run():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def disconnected():
        return started.is_set()
    async for _ in stream_run(run, disconnected, interval=0.001):
        pass
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_slow_subscriber_isolated_from_32_other_streams():
    cancelled = asyncio.Event()
    async def slow_work():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async def connected():
        return False
    stalled = stream_run(slow_work, connected, interval=0.001, stall_timeout=0.01)
    await anext(stalled)
    async def consume():
        async def run():
            return await MemoryCommerce(Dispatcher(demo_agents()), None).run(body())
        return [event async for event in stream_run(run, connected, interval=0.001)]
    results = await asyncio.wait_for(asyncio.gather(*(consume() for _ in range(32))), 2)
    await asyncio.wait_for(cancelled.wait(), 1)
    await stalled.aclose()
    assert all(any("event: result" in event for event in result) for result in results)
