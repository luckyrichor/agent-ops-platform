import asyncio
import json

import httpx
import pytest
from agent_memory.sdk import MemoryClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agent_ops.agents import demo_agents
from agent_ops.api import create_app
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import CommercePlanner, Dispatcher


@pytest.mark.parametrize("fail", [False, True])
async def test_request_agent_tool_tree_and_redacted_failure(fail: bool) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    handlers = demo_agents()
    if fail:
        async def failing(req, inputs):
            raise RuntimeError("secret-exception-payload")
        handlers["pricing"] = failing
    dispatcher = Dispatcher(handlers, provider.get_tracer("test"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(dispatcher)),
                                 base_url="http://test") as client:
        response = await client.post("/v1/commerce/runs", json={
            "request": "secret-request-payload", "category": "kettle",
            "budget_cent": 5000, "region": "北京",
        })
    spans = exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    root = by_name["commerce.request"]
    assert response.json()["trace_id"] == f"{root.context.trace_id:032x}"
    assert len({s.context.trace_id for s in spans}) == 1
    for agent in ("catalog", "pricing", "shipping"):
        assert by_name["agent." + agent].parent.span_id == root.context.span_id
        assert by_name["tool." + agent].parent.span_id == by_name["agent." + agent].context.span_id
    assert not any(s.events for s in spans)
    rendered = json.dumps([s.to_json() for s in spans])
    assert "secret-request-payload" not in rendered
    assert "secret-exception-payload" not in rendered
    if fail:
        assert by_name["tool.pricing"].status.status_code == StatusCode.ERROR
        assert by_name["agent.pricing"].attributes["reason_code"] == "AGENT_EXECUTION_FAILED"
        blocked = by_name["agent.recommendation"]
        assert blocked.attributes["outcome"] == "blocked"
        assert blocked.attributes["blocked_by"] == ("pricing",)
        assert blocked.parent.span_id == root.context.span_id
        assert "tool.recommendation" not in by_name
    else:
        assert len(spans) == 10
    provider.shutdown()


async def test_parallel_cancellation_joins_both_roles() -> None:
    started = [asyncio.Event(), asyncio.Event()]
    stopped = [asyncio.Event(), asyncio.Event()]
    handlers = demo_agents()
    for index, name in enumerate(("pricing", "shipping")):
        def build_handler(i):
            async def waiting(req, inputs):
                started[i].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped[i].set()
            return waiting
        handlers[name] = build_handler(index)
    request = CommerceRequest(request="test", category="kettle", budget_cent=5000, region="北京")
    task = asyncio.create_task(Dispatcher(handlers).run(request, CommercePlanner().plan(request)))
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started)), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(e.is_set() for e in stopped)


async def test_memory_http_failure_visible_in_same_trace() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    dispatcher = Dispatcher(demo_agents(), provider.get_tracer("test"))
    request = CommerceRequest(request="test", category="kettle", budget_cent=5000, region="北京")
    async with httpx.AsyncClient(base_url="http://memory.test",
                                transport=httpx.MockTransport(lambda _: httpx.Response(503))) as http:
        result = await MemoryCommerce(dispatcher, MemoryClient(http, token="test")).run(request)
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert result.outcome == "succeeded"
    assert result.memory_status == "read_write_degraded"
    for name in ("tool.memory.search", "tool.memory.remember"):
        assert spans[name].status.status_code == StatusCode.ERROR
        assert spans[name].parent.span_id == spans["commerce.request"].context.span_id
    provider.shutdown()
