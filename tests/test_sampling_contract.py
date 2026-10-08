import json

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

from agent_ops.agents import demo_agents
from agent_ops.api import create_app
from agent_ops.memory import MemoryCommerce
from agent_ops.orchestrator import Dispatcher


@pytest.mark.parametrize('ratio', [0, 0.5, 1])
async def test_sampling_flag_matches_exported_trace_for_success_rejection_and_sse(ratio):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=TraceIdRatioBased(ratio))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    dispatcher = Dispatcher(demo_agents(), provider.get_tracer('sampling-test'))
    app = create_app(dispatcher)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://test'
    ) as api:
        for endpoint, budget in [('/v1/commerce/runs', 5000),
                                 ('/v1/commerce/runs', -1),
                                 ('/v1/commerce/stream', 5000)]:
            for _ in range(10):
                response = await api.post(endpoint, json={
                    'request': 'fixture', 'category': 'kettle', 'budget_cent': budget,
                    'region': '上海'})
                sampled = response.headers['x-trace-sampled'] == 'true'
                trace_id = response.headers['x-trace-id']
                if endpoint.endswith('stream'):
                    frames = [json.loads(line[6:]) for line in response.text.splitlines()
                              if line.startswith('data: ')]
                else:
                    frames = [response.json()]
                assert frames and all(f['trace_sampled'] == sampled for f in frames)
                assert all(f['trace_id'] == trace_id for f in frames)
                exported = any(f'{s.context.trace_id:032x}' == trace_id
                               for s in exporter.get_finished_spans())
                assert exported == sampled
                if ratio in (0, 1):
                    assert sampled == bool(ratio)
    provider.shutdown()


@pytest.mark.parametrize('status', [409, 500])
async def test_sampling_flag_on_error_response(monkeypatch, status):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=TraceIdRatioBased(0))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    dispatcher = Dispatcher(demo_agents(), provider.get_tracer('errors'))

    async def fail(self, request):
        if status == 409:
            from agent_memory.sdk import MemoryAPIError
            raise MemoryAPIError(409, 'CONFLICT')
        raise RuntimeError('private')

    monkeypatch.setattr(MemoryCommerce, '_run', fail)
    if status == 409:
        for name in ('ALL_PROXY', 'all_proxy', 'HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv('AGENT_MEMORY_URL', 'http://memory.test')
    app = create_app(dispatcher)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://test'
    ) as api:
        response = await api.post('/v1/commerce/runs', json={
            'request': 'fixture', 'category': 'kettle', 'budget_cent': 5000,
            'region': '上海'}, headers={'authorization': 'Bearer fixture'})
    assert response.status_code == status
    assert response.json()['trace_sampled'] is False
    assert response.headers['x-trace-sampled'] == 'false'
    assert not exporter.get_finished_spans()
    provider.shutdown()
