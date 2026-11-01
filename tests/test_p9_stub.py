from __future__ import annotations

import json

import httpx
import pytest

from agent_ops.bench.faults import FaultProfile
from agent_ops.bench.stub import PLAN_CONTENT, LatencyModel, StubServer


def test_latency_model_is_deterministic_and_ordered() -> None:
    model = LatencyModel([100.0, 200.0, 400.0], seed=1, overhead_ms=0.0)
    assert model.sleep_seconds("abc") == model.sleep_seconds("abc")
    delays = [model.sleep_seconds(f"id-{index}") for index in range(200)]
    assert all(0.1 <= delay <= 0.4 for delay in delays)


def test_latency_model_subtracts_measured_overhead() -> None:
    model = LatencyModel([50.0], seed=1, overhead_ms=20.0)
    assert model.sleep_seconds("x") == pytest.approx(0.03)


def test_plan_content_matches_contract() -> None:
    from agent_ops.planner import validated_tasks

    tasks = validated_tasks(PLAN_CONTENT)
    assert {task.task_id for task in tasks} == {"catalog", "pricing", "shipping", "recommendation"}
    assert json.loads(PLAN_CONTENT)["tasks"][1]["depends_on"] == ["catalog"]


async def test_stub_serves_valid_completion() -> None:
    server = StubServer(latency=LatencyModel([1.0], seed=1), profile=FaultProfile())
    await server.start()
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(server.endpoint,
                                         headers={"X-Bench-Request-Id": "req-1"},
                                         json={"model": "stub"})
        assert response.status_code == 200
        content = response.json()["choices"][0]["message"]["content"]
        assert json.loads(content)["tasks"][0]["task_id"] == "catalog"
        assert server.stats.requests == 1
    finally:
        await server.stop()


async def test_stub_requires_request_id() -> None:
    server = StubServer(latency=LatencyModel([1.0], seed=1), profile=FaultProfile())
    await server.start()
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(server.endpoint, json={"model": "stub"})
        assert response.status_code == 400
    finally:
        await server.stop()


async def test_stub_rate_limit_has_retry_after() -> None:
    server = StubServer(latency=LatencyModel([1.0], seed=1),
                        profile=FaultProfile(rate_limit=1.0, retry_after_seconds=2))
    await server.start()
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(server.endpoint,
                                         headers={"X-Bench-Request-Id": "req-2"},
                                         json={"model": "stub"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "2"
    finally:
        await server.stop()


async def test_stub_connection_reset_surfaces_transport_error() -> None:
    server = StubServer(latency=LatencyModel([1.0], seed=1),
                        profile=FaultProfile(connection_reset=1.0))
    await server.start()
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            with pytest.raises(httpx.TransportError):
                await client.post(server.endpoint,
                                  headers={"X-Bench-Request-Id": "req-3"},
                                  json={"model": "stub"})
    finally:
        await server.stop()
