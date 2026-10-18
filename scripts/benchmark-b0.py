"""B0 actual loopback HTTP baseline. No paid LLM, no production-capacity claims."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BODY = {'request': 'B0 fixture', 'category': 'kettle', 'budget_cent': 5000, 'region': '上海'}
SUPPORT = {'message': '转人工'}
SCENARIOS = ['commerce', 'sse', 'ticket_create', 'ticket_replay', 'ticket_conflict', 'ticket_get']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def proc_stats(pid):
    stat = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()
    status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
    return {'cpu_seconds': (int(stat[11])+int(stat[12])) / os.sysconf('SC_CLK_TCK'),
            'rss_kib': int(status['VmRSS'].split()[0]), 'peak_rss_kib': int(status['VmHWM'].split()[0])}


def quantiles(values):
    ordered = sorted(values)
    return {f'p{p}': ordered[max(0, math.ceil(p*len(ordered)/100)-1)] for p in (50, 95, 99)}


async def one(client, scenario, index, tickets):
    start = time.perf_counter()
    first_ms = None
    valid = False
    status = None
    reason = None
    try:
        if scenario == 'sse':
            async with client.stream('POST', '/v1/commerce/stream', json=BODY) as response:
                status = response.status_code
                events = []
                payloads = []
                async for line in response.aiter_lines():
                    if line.startswith('event: '):
                        if first_ms is None:
                            first_ms = (time.perf_counter()-start)*1000
                        events.append(line[7:])
                    elif line.startswith('data: '):
                        payloads.append(json.loads(line[6:]))
                valid = (status == 200 and events[0] == 'started' and events[-1] == 'result'
                         and len(payloads) == len(events)
                         and all(p['trace_id'] == response.headers['x-trace-id'] for p in payloads)
                         and payloads[-1]['outcome'] == 'succeeded'
                         and payloads[-1]['recommendation']['selected']['id'] == 'kettle-basic')
        else:
            if scenario == 'commerce':
                response = await client.post('/v1/commerce/runs', json=BODY)
            elif scenario == 'ticket_get':
                response = await client.get('/support/tickets/'+tickets[index])
            else:
                data = {'message': '我要退款'} if scenario == 'ticket_conflict' else SUPPORT
                response = await client.post('/support/requests', json=data,
                    headers={'Idempotency-Key': f'b0-{index}'})
            status = response.status_code
            data = response.json()
            if scenario == 'commerce':
                valid = (status == 200 and data['outcome'] == 'succeeded'
                         and data['recommendation']['selected']['id'] == 'kettle-basic'
                         and data['trace_id'] == response.headers['x-trace-id'])
            elif scenario == 'ticket_conflict':
                valid = status == 409 and data['detail'] == 'IDEMPOTENCY_CONFLICT'
            else:
                valid = status == 200 and data['state'] == 'waiting_human' and data['revision'] == 1
                if scenario == 'ticket_create':
                    tickets[index] = data['ticket_id']
                else:
                    valid = valid and data['ticket_id'] == tickets[index]
            valid = valid and bool(response.headers.get('x-trace-id'))
        if not valid:
            reason = 'CONTRACT_MISMATCH'
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        reason = 'TRANSPORT_OR_RESPONSE_ERROR'
    return {'scenario': scenario, 'index': index, 'status': status, 'valid': valid,
            'latency_ms': (time.perf_counter()-start)*1000, 'first_event_ms': first_ms, 'reason': reason}


async def iteration(args, repeat, server_cpus):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    for key in ('ARK_API_KEY', 'AGENT_MEMORY_URL', 'SUPPORT_HUMAN_TOKEN'):
        env.pop(key, None)
    env.update(AGENT_OPS_PLANNER='deterministic', AGENT_OPS_TRACE_EXPORTER='none',
               AGENT_OPS_TRACE_SAMPLE_RATIO='1')
    log = args.output / f'server-{repeat}.log'
    with log.open('w') as file:
        process = await asyncio.create_subprocess_exec(*[sys.executable, '-m', 'uvicorn', 'agent_ops.api:create_app',
            '--factory', '--host', '127.0.0.1', '--port', str(port), '--workers', '1',
            '--no-access-log', '--log-level', 'warning'], cwd=ROOT, env=env, stdout=file, stderr=file)
        try:
            os.sched_setaffinity(process.pid, server_cpus)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False,
                    timeout=10, limits=httpx.Limits(max_connections=args.concurrency,
                                                  max_keepalive_connections=args.concurrency)) as client:
                for _ in range(200):
                    if process.returncode is not None:
                        raise RuntimeError('B0 server exited; inspect local server log')
                    try:
                        response = await client.get('/openapi.json')
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.05)
                else:
                    raise RuntimeError('B0 server startup timeout')
                tickets = {}
                # Each mode warmed separately; support warmups use a reserved key.
                for _ in range(args.warmup):
                    for scenario in ['commerce', 'sse', 'ticket_create']:
                        row = await one(client, scenario, -1, tickets)
                        if not row['valid']:
                            raise RuntimeError('B0 warmup contract failed')
                raw = []
                summaries = []
                for scenario in SCENARIOS:
                    slots = asyncio.Semaphore(args.concurrency)
                    async def job(index, slots=slots, scenario=scenario):
                        async with slots:
                            # Clock begins after load-generator admission, including HTTP-pool wait.
                            row = await one(client, scenario, index, tickets)
                            row['repeat'] = repeat
                            return row
                    before = proc_stats(process.pid)
                    start = time.perf_counter()
                    group = await asyncio.gather(*(job(i) for i in range(args.requests)))
                    elapsed = time.perf_counter()-start
                    after = proc_stats(process.pid)
                    first = [r['first_event_ms'] for r in group if r['first_event_ms'] is not None]
                    summaries.append({'scenario': scenario, 'repeat': repeat, 'requests': len(group),
                        'valid': sum(r['valid'] for r in group), 'unexpected': sum(not r['valid'] for r in group),
                        'expected_409': sum(r['valid'] and r['status'] == 409 for r in group),
                        'latency_ms': quantiles([r['latency_ms'] for r in group]),
                        'first_event_ms': quantiles(first) if first else None,
                        'elapsed_seconds': elapsed, 'requests_per_second': len(group)/elapsed,
                        'server_cpu_seconds': after['cpu_seconds']-before['cpu_seconds'],
                        'rss_kib': after['rss_kib'], 'process_peak_rss_kib': after['peak_rss_kib']})
                    raw.extend(group)
                    if not all(r['valid'] for r in group):
                        break
                return raw, summaries
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=Path('.local/b0-http'))
    p.add_argument('--repeats', type=int, default=5)
    p.add_argument('--requests', type=int, default=128)
    p.add_argument('--concurrency', type=int, default=8)
    p.add_argument('--warmup', type=int, default=10)
    args = p.parse_args()
    if not 5 <= args.repeats <= 20 or not 1 <= args.requests <= 200 or not 1 <= args.concurrency <= 64 or not 1 <= args.warmup <= 100:
        p.error('bounds: repeats5..20 requests1..200 concurrency1..64 warmup1..100')
    if not sys.platform.startswith('linux'):
        p.error('Linux required for per-process CPU/RSS and affinity evidence')
    if args.output.exists():
        p.error('output exists; use a fresh directory to preserve evidence')
    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 2:
        p.error('need >=2 available logical CPUs for separate client/server affinity')
    args.output.mkdir(parents=True)
    os.sched_setaffinity(0, {cpus[0]})
    raw, summary = [], []
    sources = [*ROOT.joinpath('src').rglob('*.py'), Path(__file__), ROOT/'uv.lock', ROOT/'pyproject.toml']
    metadata = {'generated_at': datetime.now(UTC).isoformat(), 'source_head': (await asyncio.to_thread(subprocess.check_output,
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True)).strip(),
        'source_dirty': await asyncio.to_thread(subprocess.check_output, ['git', 'status', '--porcelain'], cwd=ROOT, text=True),
        'source_sha256': {str(f.relative_to(ROOT)): digest(f) for f in sources},
        'platform': platform.platform(), 'python': platform.python_version(), 'cpu_count': os.cpu_count(),
        'cpu_affinity': {'client': [cpus[0]], 'server': cpus[1:]},
        'cpu_model': next((line.split(':',1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name')), 'unknown'),
        'meminfo': {k: v.strip() for k, v in (line.split(':',1) for line in Path('/proc/meminfo').read_text().splitlines()) if k in ('MemTotal','MemAvailable')},
        'versions': {name: importlib.metadata.version(name) for name in ('fastapi','starlette','uvicorn','httpx','agent-memory','opentelemetry-sdk')},
        'load_before': os.getloadavg(), 'config': vars(args) | {'output': str(args.output)},
        'mode': 'deterministic planner / local demo tools / memory disabled / in-memory support / trace ratio1 exporter none / one uvicorn worker',
        'method': 'closed-loop bounded client; real loopback TCP; latency begins after generator semaphore; includes HTTP pool and parsing, not generator backlog. New server per repeat; fixed scenario order (not randomized), warmup each commerce/SSE/support; service CPU and VmHWM via /proc, process peak cumulative; no allocator attribution; first SSE event is started, not first model token; no live model, stub or paid call; local credentials removed; no capacity claims'}
    (args.output/'inputs.json').write_text(json.dumps({'commerce': BODY, 'support': SUPPORT, 'conflict': {'message':'我要退款'}, 'scenarios': SCENARIOS}, ensure_ascii=False, indent=2))
    metadata['inputs_sha256'] = digest(args.output/'inputs.json')
    try:
        for repeat in range(args.repeats):
            rows, sums = await iteration(args, repeat, set(cpus[1:]))
            raw.extend(rows);summary.extend(sums)
            (args.output/'raw.json').write_text(json.dumps(raw, indent=2))
            print(f'B0 repeat {repeat+1}/{args.repeats}: {sum(r["valid"] for r in rows)}/{len(rows)} contract checks', flush=True)
            if not all(r['valid'] for r in rows):
                break
    finally:
        metadata['load_after'] = os.getloadavg()
        (args.output/'environment.json').write_text(json.dumps(metadata, indent=2))
    (args.output/'summary.json').write_text(json.dumps(summary, indent=2))
    aggregated = []
    for scenario in SCENARIOS:
        group = [r for r in summary if r['scenario'] == scenario]
        if group:
            aggregate = {'scenario': scenario, 'rounds': len(group)}
            for key in ('p50', 'p95', 'p99'):
                values = [r['latency_ms'][key] for r in group]
                aggregate[key+'_ms'] = {'median': statistics.median(values), 'min': min(values), 'max': max(values)}
            values = [r['requests_per_second'] for r in group]
            aggregate['rps'] = {'median': statistics.median(values), 'min': min(values), 'max': max(values)}
            aggregated.append(aggregate)
    (args.output/'aggregate.json').write_text(json.dumps(aggregated, indent=2))
    if len(summary) != args.repeats*len(SCENARIOS) or not all(r['valid'] for r in raw):
        raise SystemExit('B0 incomplete or contract failures: inspect raw results')


if __name__ == '__main__':
    asyncio.run(main())
