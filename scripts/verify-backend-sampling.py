"""Real OTLP/Jaeger sampling contract; no provider/model or memory requests."""
import asyncio
import json
import os
from pathlib import Path

import httpx

from agent_ops.api import create_app


async def main() -> None:
    os.environ['AGENT_OPS_PLANNER'] = 'deterministic'
    os.environ['AGENT_OPS_TRACE_EXPORTER'] = 'otlp'
    os.environ.pop('AGENT_MEMORY_URL', None)
    rows = []
    for ratio in (0, 0.5, 1):
        os.environ['AGENT_OPS_TRACE_SAMPLE_RATIO'] = str(ratio)
        app = create_app()
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://test'
        ) as api:
            for _ in range(10):
                reply = await api.post('/v1/commerce/runs', json={
                    'request': 'sampling-fixture', 'category': 'kettle',
                    'budget_cent': 5000, 'region': '上海'})
                assert reply.status_code == 200
                data = reply.json()
                sampled = data['trace_sampled']
                assert reply.headers['x-trace-sampled'] == str(sampled).lower()
                assert reply.headers['x-trace-id'] == data['trace_id']
                rows.append({'ratio': ratio, 'trace_id': data['trace_id'], 'sampled': sampled})
    async with httpx.AsyncClient(trust_env=False) as query:
        for row in rows:
            for _ in range(50):
                reply = await query.get('http://127.0.0.1:16686/api/v3/traces/' + row['trace_id'])
                assert reply.status_code in (200, 404)
                spans = reply.json().get('result', {}).get('resourceSpans', [])
                found = bool(spans)
                if found or not row['sampled']:
                    break
                await asyncio.sleep(0.1)
            assert found == row['sampled'], row
            row['query_status'] = reply.status_code
            row['found'] = found
    report_path = Path('docs/measurements/2026-10-08-tracing-review.json')
    report = json.loads(report_path.read_text())
    report['sampling_cases'] = rows
    report['sampling_contract_verified'] = True
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'verified': len(rows), 'sampled': sum(r['sampled'] for r in rows),
                      'unsampled': sum(not r['sampled'] for r in rows)}))


asyncio.run(main())
