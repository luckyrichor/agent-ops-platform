import httpx
import pytest
from http_login import login_customer
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_ops.api import create_app
from agent_ops.telemetry import Telemetry


@pytest.mark.parametrize('ratio', ['-0.1', '1.1', 'nan'])
def test_invalid_trace_sampling_ratio_rejected(monkeypatch, ratio):
    monkeypatch.setenv('AGENT_OPS_TRACE_SAMPLE_RATIO', ratio)
    with pytest.raises(ValueError):
        Telemetry()


async def test_owned_batch_exporter_drained_and_closed_by_app_lifespan(monkeypatch):
    from opentelemetry.exporter.otlp.proto.http import trace_exporter

    captured = []
    class Exporter(InMemorySpanExporter):
        closed = False
        def shutdown(self):
            self.closed = True
    exporter = Exporter()
    def build(**kwargs):
        captured.append(kwargs)
        return exporter
    monkeypatch.setattr(trace_exporter, 'OTLPSpanExporter', build)
    monkeypatch.setenv('AGENT_OPS_TRACE_EXPORTER', 'otlp')
    app = create_app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://test'
    ) as client:
        await login_customer(client)
        exporter.clear()
        response = await client.post('/v1/commerce/runs', json={
            'request':'fixture', 'category':'kettle', 'region':'上海', 'budget_cent':5000})
    assert response.status_code == 200
    assert exporter.closed
    spans = exporter.get_finished_spans()
    business = [s for s in spans if f"{s.context.trace_id:032x}" == response.headers["x-trace-id"]]
    assert len(business) == 10
    assert len(spans) == 11  # separate login trace is also drained at shutdown
    assert all(s.resource.attributes['service.name'] == 'agent-ops-platform' for s in spans)
    assert captured[0]['session'].trust_env is False
    assert captured[0]['timeout'] == 3
    captured[0]['session'].close()
