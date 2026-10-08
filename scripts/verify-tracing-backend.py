"""Real OTLP export/query verification against the running local Jaeger v2 backend."""
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agent_ops.agents import demo_agents
from agent_ops.api import create_app
from agent_ops.memory import MemoryCommerce
from agent_ops.models import CommerceRequest
from agent_ops.orchestrator import Dispatcher
from agent_ops.telemetry import hop


async def main() -> None:
    os.environ['AGENT_OPS_TRACE_EXPORTER'] = 'otlp'
    os.environ['AGENT_OPS_PLANNER'] = 'deterministic'
    os.environ['AGENT_OPS_TRACE_SAMPLE_RATIO'] = '1'
    os.environ.pop('AGENT_MEMORY_URL', None)
    markers = ['private-request-never-export', 'private-failure-never-export']
    body = {'request': markers[0], 'category': 'kettle', 'budget_cent': 5000, 'region': '上海'}
    cases = []
    for failed in (False, True):
        handlers = demo_agents()
        if failed:
            async def failing(request, inputs):
                raise RuntimeError(markers[1])
            handlers['pricing'] = failing
        dispatcher = Dispatcher(handlers)
        try:
            app = create_app(dispatcher)
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url='http://fixture'
            ) as client:
                response = await client.post('/v1/commerce/runs', json=body)
                cases.append({'case': 'blocked' if failed else 'success',
                              'trace_id': response.headers['x-trace-id']})
                if not failed:
                    rejected = await client.post('/v1/commerce/runs',
                                                  json={**body, 'budget_cent': -1})
                    assert rejected.status_code == 422
                    cases.append({'case': 'validation',
                                  'trace_id': rejected.json()['trace_id']})
        finally:
            await dispatcher.aclose()
    started = asyncio.Event()
    async def waiting(request, inputs):
        started.set()
        await asyncio.Event().wait()
    handlers = demo_agents()
    handlers['catalog'] = waiting
    dispatcher = Dispatcher(handlers)
    async def cancel_work():
        with hop(dispatcher.tracer, 'verification.cancel') as span:
            cases.append({'case': 'cancelled', 'trace_id': f'{span.get_span_context().trace_id:032x}'})
            await MemoryCommerce(dispatcher, None).run(CommerceRequest(**body))
    task = asyncio.create_task(cancel_work())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await dispatcher.aclose()
    async with httpx.AsyncClient(trust_env=False, timeout=3) as query:
        health = await query.get('http://127.0.0.1:13133/status')
        health.raise_for_status()
        for case in cases:
            for _ in range(40):
                response = await query.get('http://127.0.0.1:16686/api/v3/traces/' + case['trace_id'])
                if response.status_code == 200:
                    break
                await asyncio.sleep(0.1)
            response.raise_for_status()
            assert all(marker not in response.text for marker in markers)
            data = response.json()
            spans = [span for resource in data['result']['resourceSpans']
                     for scope in resource['scopeSpans'] for span in scope['spans']]
            assert spans and all(span['traceId'] == case['trace_id'] for span in spans)
            if case['case'] == 'blocked':
                blocked = next(s for s in spans if s['name'] == 'agent.recommendation')
                attributes = {a['key']: a['value'] for a in blocked['attributes']}
                assert attributes['outcome']['stringValue'] == 'blocked'
                blockers = attributes['blocked_by']
                # Jaeger Badger's legacy storage conversion returns array tags as JSON strings.
                values = ([v['stringValue'] for v in blockers['arrayValue']['values']]
                          if 'arrayValue' in blockers else json.loads(blockers['stringValue']))
                assert values == ['pricing']
            if case['case'] in {'validation', 'cancelled'}:
                assert all(s.get('status', {}).get('code', 0) != 2 for s in spans)
            case['span_count'] = len(spans)
            Path('.local').mkdir(exist_ok=True)
            Path('.local/backend-trace-' + case['case'] + '.json').write_text(json.dumps(data))
            case['query_status'] = response.status_code
    report = {'generated_at': datetime.now(UTC).isoformat(), 'backend': 'Jaeger 2.22.0 / Badger',
              'health': health.json(), 'cases': cases, 'private_markers_exported': False,
              'source_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(Path('src/agent_ops').glob('*.py'))},
              'config_sha256': hashlib.sha256(Path('deploy/jaeger.yaml').read_bytes()).hexdigest()}
    Path('docs/measurements/2026-10-08-tracing-backend.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'verified': len(cases), 'cases': cases}, ensure_ascii=False))


asyncio.run(main())
