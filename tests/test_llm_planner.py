import asyncio
import json

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_commerce import request

from agent_ops.api import create_app
from agent_ops.memory import MemoryCommerce
from agent_ops.orchestrator import CommercePlanner, Dispatcher
from agent_ops.planner import (
    LLMConfig,
    LLMPlanner,
    PlanningError,
    planner_from_env,
    validated_tasks,
)


def valid_plan():
    return {"tasks": [t.model_dump(mode="json") for t in CommercePlanner().plan(request())]}


def response(plan=None):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(valid_plan() if plan is None else plan)}}
            ]
        },
    )


async def test_real_api_path_uses_llm_and_tool_results_without_sending_identity():
    calls = []

    def respond(req):
        assert req.headers["authorization"] == "Bearer fixture-private-key"
        body = json.loads(req.content)
        user = json.loads(body["messages"][1]["content"])
        assert set(user) == {"request", "category", "budget_cent", "region"}
        assert body["model"] == "fixture"
        calls.append(req)
        return response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as provider:
        planner = LLMPlanner(LLMConfig("fixture-private-key", model="fixture"), provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(planner=planner)), base_url="http://test"
        ) as api:
            result = await api.post("/v1/commerce/runs", json=request().model_dump(mode="json"))
            assert result.status_code == 200
            data = result.json()
            assert data["planner_mode"] == "llm" and data["planner_model"] == "fixture"
            assert data["recommendation"]["selected"]["total_cent"] == 3990
            assert len(calls) == 1
            async with api.stream(
                "POST", "/v1/commerce/stream", json=request().model_dump(mode="json")
            ) as stream:
                text = await stream.aread()
                assert b"event: result" in text and b'"planner_mode": "llm"' in text
            assert len(calls) == 2


@pytest.mark.parametrize(
    "fault", ["extra", "unknown", "missing", "cycle", "duplicate", "authority"]
)
def test_untrusted_model_plans_cannot_execute_invalid_graphs(fault):
    plan = valid_plan()
    if fault == "extra":
        plan["tasks"][0]["command"] = "untrusted command"
    elif fault == "unknown":
        plan["tasks"][0]["agent"] = "shell"
    elif fault == "missing":
        plan["tasks"][3]["depends_on"] = ["pricing"]
    elif fault == "cycle":
        plan["tasks"][0]["depends_on"] = ["recommendation"]
    elif fault == "duplicate":
        plan["tasks"][2] = plan["tasks"][1]
    else:
        plan["budget_cent"] = 999999
    with pytest.raises(PlanningError, match="LLM_INVALID_PLAN"):
        validated_tasks(json.dumps(plan))


async def test_429_then_success_retries_and_respects_retry_after(monkeypatch):
    calls, sleeps = [], []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("agent_ops.planner.asyncio.sleep", sleep)

    def respond(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "2"}) if len(calls) == 1 else response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        planner = LLMPlanner(LLMConfig("fixture"), client)
        assert len(await planner.plan(request())) == 4
    assert len(calls) == 2 and sleeps == [2]


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "LLM_CONFIGURATION_ERROR"),
        (403, "LLM_CONFIGURATION_ERROR"),
        (404, "LLM_MODEL_UNAVAILABLE"),
        (400, "LLM_REQUEST_REJECTED"),
    ],
)
async def test_permanent_provider_errors_are_redacted_and_never_fallback(status, code):
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(status, text="private provider response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        planner = LLMPlanner(LLMConfig("fixture"), client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(planner=planner)), base_url="http://test"
        ) as api:
            reply = await api.post("/v1/commerce/runs", json=request().model_dump(mode="json"))
            assert reply.status_code == 503 and reply.json()["detail"] == code
            assert "private" not in reply.text
    assert len(calls) == 1


async def test_total_timeout_and_cancel_propagate_to_http():
    started, stopped = asyncio.Event(), asyncio.Event()

    async def respond(req):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        planner = LLMPlanner(LLMConfig("fixture", timeout_seconds=0.02), client)
        with pytest.raises(PlanningError, match="LLM_TIMEOUT"):
            await planner.plan(request())
        assert stopped.is_set()
        stopped.clear()
        planner = LLMPlanner(LLMConfig("fixture"), client)
        task = asyncio.create_task(planner.plan(request()))
        await started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()


async def test_invalid_plan_stops_before_any_tool_and_trace_redacts_content():
    called = []

    async def handler(req, inputs):
        called.append(True)
        return {}

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response({"tasks": []}))
    ) as client:
        dispatch = Dispatcher({"catalog": handler}, provider.get_tracer("test"))
        with pytest.raises(PlanningError):
            await MemoryCommerce(
                dispatch, None, LLMPlanner(LLMConfig("private-token"), client)
            ).run(request())
    assert called == []
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} == {"commerce.request", "planner.llm"}
    rendered = json.dumps([s.to_json() for s in spans])
    assert "private-token" not in rendered and request().request not in rendered
    assert all(not s.events for s in spans)
    provider.shutdown()


def test_explicit_llm_configuration_cannot_silently_use_static_planner(monkeypatch):
    monkeypatch.setenv("AGENT_OPS_PLANNER", "llm")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="invalid LLM configuration"):
        planner_from_env()
    assert "private-token" not in repr(LLMConfig("private-token"))


async def test_exhausted_transient_errors_do_not_run_static_plan(monkeypatch):
    calls = []

    async def sleep(delay):
        pass

    monkeypatch.setattr("agent_ops.planner.asyncio.sleep", sleep)

    def respond(req):
        calls.append(req)
        return httpx.Response(503, text="private failure")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        planner = LLMPlanner(LLMConfig("fixture"), client)
        with pytest.raises(PlanningError, match="LLM_UNAVAILABLE"):
            await planner.plan(request())
    assert len(calls) == 3


async def test_concurrency_limit_and_shared_client():
    active = 0
    peak = 0
    release, both = asyncio.Event(), asyncio.Event()

    async def respond(req):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both.set()
        try:
            await release.wait()
            return response()
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        planner = LLMPlanner(LLMConfig("fixture", concurrency=2), client)
        tasks = [asyncio.create_task(planner.plan(request())) for _ in range(6)]
        await asyncio.wait_for(both.wait(), 1)
        assert active == 2
        release.set()
        assert all(len(plan) == 4 for plan in await asyncio.gather(*tasks))
        assert planner._client is client and peak == 2
        await planner.aclose()
        assert not client.is_closed  # injected clients remain caller-owned


async def test_sse_model_failure_exposes_safe_code():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(404))
    ) as provider:
        planner = LLMPlanner(LLMConfig("fixture"), provider)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(planner=planner)), base_url="http://test"
        ) as api:
            reply = await api.post("/v1/commerce/stream", json=request().model_dump(mode="json"))
            assert "event: error" in reply.text and "LLM_MODEL_UNAVAILABLE" in reply.text
            assert "event: result" not in reply.text


@pytest.mark.parametrize("role,extra", [("shipping", "pricing"), ("pricing", "shipping"),
                                       ("recommendation", "catalog")])
def test_extra_dependencies_are_rejected_even_if_acyclic(role, extra):
    plan = valid_plan()
    task = next(t for t in plan["tasks"] if t["task_id"] == role)
    task["depends_on"].append(extra)
    with pytest.raises(PlanningError, match="LLM_INVALID_PLAN"):
        validated_tasks(json.dumps(plan))


def test_reordered_tasks_and_dependencies_keep_same_valid_graph():
    plan = valid_plan()
    plan["tasks"].reverse()
    for task in plan["tasks"]:
        task["depends_on"].reverse()
    tasks = validated_tasks(json.dumps(plan))
    assert {t.task_id: set(t.depends_on) for t in tasks} == {
        t.task_id: set(t.depends_on) for t in CommercePlanner().plan(request())}
