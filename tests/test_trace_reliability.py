import asyncio
import json

import httpx
import pytest
from agent_memory.sdk import MemoryAPIError, MemoryClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from agent_ops.agents import demo_agents
from agent_ops.api import create_app
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import Dispatcher
from agent_ops.planner import PlanningError
from agent_ops.streaming import stream_run


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    # These tests never contact a provider; local HTTP clients need no system proxy.
    for name in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(name, raising=False)


def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter, Dispatcher(demo_agents(), provider.get_tracer('test'))


def body():
    return CommerceRequest(request='fixture', category='kettle', budget_cent=5000, region='上海')


@pytest.mark.parametrize('code,status', [('LLM_TIMEOUT', 503), ('CONFLICT', 409), ('unexpected', 500)])
async def test_http_errors_return_trace_header_and_body(monkeypatch, code, status):
    provider, exporter, dispatcher = tracing()
    monkeypatch.setenv('AGENT_MEMORY_URL', 'http://memory.test')

    async def fail(self, request):
        if status == 503:
            raise PlanningError(code)
        if status == 409:
            raise MemoryAPIError(status, code)
        raise RuntimeError('private-exception')

    monkeypatch.setattr(MemoryCommerce, '_run', fail)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(dispatcher)),
                                 base_url='http://test') as client:
        response = await client.post('/v1/commerce/runs', json=body().model_dump(mode='json'),
                                     headers={'authorization': 'Bearer fixture'})
    spans = exporter.get_finished_spans()
    trace_id = response.headers['x-trace-id']
    assert response.status_code == status
    assert response.json()['trace_id'] == trace_id
    assert len(trace_id) == 32 and int(trace_id, 16) != 0
    assert all(f'{s.context.trace_id:032x}' == trace_id for s in spans)
    assert 'private-exception' not in response.text
    provider.shutdown()


async def test_actual_memory_write_conflict_stream_has_trace(monkeypatch):
    provider, exporter, dispatcher = tracing()
    monkeypatch.setenv('AGENT_MEMORY_URL', 'http://memory.test')

    async def conflict(self, *args, **kwargs):
        raise MemoryAPIError(409, 'IDEMPOTENCY_CONFLICT')

    monkeypatch.setattr(MemoryClient, 'remember', conflict)
    # Avoid network for the preceding read; exercise the actual memory write/error path.
    async def degraded(self, *args, **kwargs):
        raise MemoryAPIError(503, 'UNAVAILABLE')
    monkeypatch.setattr(MemoryClient, 'search', degraded)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(dispatcher)),
                                 base_url='http://test') as client:
        response = await client.post('/v1/commerce/stream', json=body().model_dump(mode='json'),
                                     headers={'authorization': 'Bearer fixture'})
    frames = [json.loads(part.split('data: ', 1)[1]) for part in response.text.strip().split('\n\n')]
    assert frames[0]['trace_id'] == response.headers['x-trace-id']
    assert frames[-1] == {'code': 'IDEMPOTENCY_CONFLICT', 'trace_id': response.headers['x-trace-id']}
    assert 'event: error' in response.text
    assert any(s.name == 'tool.memory.remember' for s in exporter.get_finished_spans())
    provider.shutdown()


async def test_cancellation_is_not_error_and_joins_tools():
    provider, exporter, dispatcher = tracing()
    started, stopped = asyncio.Event(), asyncio.Event()

    async def waiting(request, inputs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    dispatcher.handlers['catalog'] = waiting
    task = asyncio.create_task(MemoryCommerce(dispatcher, None).run(body()))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    spans = exporter.get_finished_spans()
    assert {s.name for s in spans} == {'commerce.request', 'agent.catalog', 'tool.catalog'}
    assert all(s.status.status_code != StatusCode.ERROR for s in spans)
    assert all(s.attributes['outcome'] == 'cancelled' for s in spans)
    provider.shutdown()


async def test_full_queue_retains_terminal_abort_without_blocking_cleanup():
    stopped = asyncio.Event()
    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async def connected():
        return False
    stream = stream_run(work, connected, interval=0.001, stall_timeout=0.01, trace_id='fixture-trace')
    assert 'event: started' in await anext(stream)
    await asyncio.wait_for(stopped.wait(), 1)
    remainder = [event async for event in stream]
    assert len(remainder) <= 3
    assert remainder[-1].startswith('event: aborted\n')
    assert json.loads(remainder[-1].split('data: ', 1)[1]) == {
        'code': 'SLOW_CONSUMER', 'trace_id': 'fixture-trace'}
    assert not any('event: result' in event for event in remainder)


async def test_business_timeout_is_not_slow_consumer():
    async def work():
        raise TimeoutError
    async def connected():
        return False
    events = [event async for event in stream_run(work, connected, interval=0.001)]
    assert 'event: error' in events[-1] and 'RUN_FAILED' in events[-1]
    assert not any('SLOW_CONSUMER' in event for event in events)


async def test_transport_disconnect_is_not_server_failure():
    from starlette.requests import ClientDisconnect

    from agent_ops.api import RequestTraceMiddleware

    provider, exporter, dispatcher = tracing()
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
    async def receive():
        return {'type': 'http.disconnect'}
    async def send(message):
        raise OSError('disconnected fixture socket')
    with pytest.raises(ClientDisconnect):
        await RequestTraceMiddleware(app, dispatcher)({'type': 'http'}, receive, send)
    span, = exporter.get_finished_spans()
    assert span.status.status_code != StatusCode.ERROR
    assert span.attributes['outcome'] == 'cancelled'
    provider.shutdown()


@pytest.mark.parametrize('path', ['/v1/commerce/runs', '/v1/commerce/stream'])
async def test_validation_body_trace_and_server_4xx_status(path):
    from opentelemetry.trace import SpanKind
    provider, exporter, dispatcher = tracing()
    payload = body().model_dump(mode='json')
    payload['budget_cent'] = -1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(dispatcher)),
                                 base_url='http://test') as client:
        response = await client.post(path, json=payload)
    assert response.status_code == 422
    assert response.json()['trace_id'] == response.headers['x-trace-id']
    assert response.json()['detail'][0]['loc'] == ['body', 'budget_cent']
    assert 'input' not in response.json()['detail'][0]
    span, = exporter.get_finished_spans()
    assert span.kind == SpanKind.SERVER
    assert span.attributes['http.response.status_code'] == 422
    assert span.status.status_code == StatusCode.UNSET
    provider.shutdown()


@pytest.mark.parametrize('status', [401, 404, 409, 500])
async def test_http_server_status_semantics(monkeypatch, status):
    from fastapi import HTTPException
    provider, exporter, dispatcher = tracing()
    async def rejected(self, request):
        raise HTTPException(status, 'FIXTURE_REJECTION')
    monkeypatch.setattr(MemoryCommerce, '_run', rejected)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(dispatcher)),
                                 base_url='http://test') as client:
        response = await client.post('/v1/commerce/runs', json=body().model_dump(mode='json'))
    assert response.status_code == status
    server = next(s for s in exporter.get_finished_spans() if s.name == 'commerce.http')
    assert server.status.status_code == (StatusCode.ERROR if status >= 500 else StatusCode.UNSET)
    assert response.json()['trace_id'] == response.headers['x-trace-id']
    provider.shutdown()
