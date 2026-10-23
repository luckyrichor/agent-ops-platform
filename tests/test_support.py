import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_ops.agents import demo_agents
from agent_ops.api import create_app
from agent_ops.orchestrator import Dispatcher
from agent_ops.support import SupportConflict, SupportRequest, SupportService


async def test_idempotency_parallel_handoff_and_resolve() -> None:
    provider = TracerProvider()
    export = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(export))
    service = SupportService(provider.get_tracer("support-test"))
    request = SupportRequest(message="我要退款")
    tickets = await asyncio.gather(*(service.handle(request, "same-key") for _ in range(16)))
    assert len({t.ticket_id for t in tickets}) == 1
    assert tickets[0].state == "waiting_human"
    with pytest.raises(SupportConflict):
        await service.handle(SupportRequest(message="我要人工"), "same-key")
    # P3: who may resolve is decided by the HTTP role guard (tests/test_auth.py).
    resolved = await service.resolve(tickets[0].ticket_id, 1, "account:1")
    assert resolved.state == "resolved" and resolved.revision == 2
    assert (await service.handle(request, "same-key")).revision == 1
    assert (await service.get(resolved.ticket_id)).revision == 2
    assert request.message not in await service.snapshot()
    assert {s.name for s in export.get_finished_spans()} >= {"support.route", "support.human_resolve"}
    provider.shutdown()


async def test_support_http_trace_and_order_lookup() -> None:
    dispatch = Dispatcher(demo_agents())
    app = create_app(dispatcher=dispatch)
    await app.state.auth.create_account("c@fixture.invalid", "fixture-password-1", "customer", 1)
    await app.state.auth.create_account("a@fixture.invalid", "fixture-password-2", "agent", None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        auth = {}
        for who, password in (("c", "fixture-password-1"), ("a", "fixture-password-2")):
            login = await client.post("/auth/login", json={"email": f"{who}@fixture.invalid",
                                                           "password": password})
            auth[who] = {"Authorization": "Bearer " + login.json()["access_token"]}
        r = await client.post("/support/requests",
                              headers={"Idempotency-Key": "delivery", **auth["c"]},
                              json={"message": "快递到哪了", "order_id": "demo-100"})
        assert r.status_code == 200 and r.json()["order_status"] == "shipped"
        assert r.headers["x-trace-id"] and r.headers["x-trace-sampled"]
        h = await client.post("/support/requests",
                              headers={"Idempotency-Key": "human", **auth["c"]},
                              json={"message": "转人工"})
        tid = h.json()["ticket_id"]
        # Pre-P3 this was 403 for a missing shared token; now 401 anonymous / 403 wrong role.
        anonymous = await client.post(f"/support/tickets/{tid}/resolve",
                                      json={"expected_revision": 1})
        assert anonymous.status_code == 401
        denied = await client.post(f"/support/tickets/{tid}/resolve",
                                   json={"expected_revision": 1}, headers=auth["c"])
        assert denied.status_code == 403
        done = await client.post(f"/support/tickets/{tid}/resolve", json={"expected_revision": 1},
                                 headers=auth["a"])
        assert done.status_code == 200
        stale = await client.post(f"/support/tickets/{tid}/resolve", json={"expected_revision": 1},
                                  headers=auth["a"])
        assert stale.status_code == 409
    await dispatch.aclose()
