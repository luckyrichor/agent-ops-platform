import asyncio

import httpx
import pytest

from agent_ops.agents import demo_agents
from agent_ops.api import create_app
from agent_ops.models import CommerceRequest, Task
from agent_ops.orchestrator import CommercePlanner, Dispatcher, validate_plan


def request() -> CommerceRequest:
    return CommerceRequest(request="选一个水壶", category="kettle", budget_cent=5000, region="上海")


@pytest.mark.asyncio
async def test_asgi_e2e_decomposes_dispatches_and_recommends() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                                 base_url="http://test") as client:
        response = await client.post("/v1/commerce/runs", json=request().model_dump())
        assert response.status_code == 200
        result = response.json()
        assert result["outcome"] == "succeeded"
        assert [t["task"]["agent"] for t in result["tasks"]] == [
            "catalog", "pricing", "shipping", "recommendation"]
        assert result["dispatch_order"][0] == "catalog"
        assert result["dispatch_order"][-1] == "recommendation"
        assert result["recommendation"]["selected"]["id"] == "kettle-basic"
        assert result["recommendation"]["selected"]["total_cent"] <= 5000
        invalid = await client.post("/v1/commerce/runs", json={**request().model_dump(), "budget_cent": -1})
        assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_independent_roles_run_concurrently_without_sleep_based_assertions() -> None:
    handlers = demo_agents()
    pricing_ready, shipping_ready = asyncio.Event(), asyncio.Event()
    original_pricing, original_shipping = handlers["pricing"], handlers["shipping"]
    async def price(req, inputs):
        pricing_ready.set()
        await asyncio.wait_for(shipping_ready.wait(), 1)
        return await original_pricing(req, inputs)
    async def ship(req, inputs):
        shipping_ready.set()
        await asyncio.wait_for(pricing_ready.wait(), 1)
        return await original_shipping(req, inputs)
    handlers.update(pricing=price, shipping=ship)
    result = await Dispatcher(handlers).run(request(), CommercePlanner().plan(request()))
    assert result.outcome == "succeeded"


@pytest.mark.asyncio
async def test_failed_role_blocks_dependents_preserves_other_results_and_redacts_error() -> None:
    async def failing(req, inputs):
        raise RuntimeError("secret tool payload")
    handlers = demo_agents(); handlers["pricing"] = failing
    result = await Dispatcher(handlers).run(request(), CommercePlanner().plan(request()))
    states = {r.task.task_id: r for r in result.tasks}
    assert result.outcome == "failed" and result.recommendation is None
    assert states["pricing"].status == "failed"
    assert states["shipping"].status == "succeeded"
    assert states["recommendation"].status == "blocked"
    assert "secret tool payload" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_missing_agent_and_no_budget_match_have_explicit_outcomes() -> None:
    missing = Dispatcher({})
    result = await missing.run(request(), CommercePlanner().plan(request()))
    assert result.tasks[0].error_code == "AGENT_NOT_REGISTERED"
    low = request().model_copy(update={"budget_cent": 1})
    result = await Dispatcher(demo_agents()).run(low, CommercePlanner().plan(low))
    assert result.outcome == "succeeded"
    assert result.recommendation["selected"] is None
    assert result.recommendation["reason_code"] == "NO_MATCH_IN_BUDGET"


@pytest.mark.parametrize("tasks", [
    [], [Task(task_id="a", agent="a"), Task(task_id="a", agent="b")],
    [Task(task_id="a", agent="a", depends_on=("missing",))],
    [Task(task_id="a", agent="a", depends_on=("a",))],
    [Task(task_id="a", agent="a", depends_on=("b",)),
     Task(task_id="b", agent="b", depends_on=("a",))],
])
def test_invalid_plans_are_rejected_before_dispatch(tasks) -> None:
    with pytest.raises(ValueError):
        validate_plan(tasks)


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_to_role() -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def waiting(req, inputs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    handlers = demo_agents(); handlers["catalog"] = waiting
    task = asyncio.create_task(Dispatcher(handlers).run(request(), CommercePlanner().plan(request())))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
