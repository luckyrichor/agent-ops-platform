import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from agent_ops.deepseek import DeepSeekConfig, DeepSeekEvaluation, EvaluationUnavailable


@pytest.fixture
def cache():
    import shutil
    from pathlib import Path
    path = Path('.local/test-deepseek')/uuid4().hex
    yield path
    shutil.rmtree(path, ignore_errors=True)


def config(**kw):
    return DeepSeekConfig('fixture-token', 'https://provider.invalid/chat/completions',
                          'fixture-model', **kw)


def response():
    return httpx.Response(200, json={'model':'fixture-returned', 'usage':{
        'prompt_tokens':100,'completion_tokens':10}, 'choices':[{'message':{
        'content':'{"route":"clarify"}'}}]})


async def test_approval_source_cache_and_exact_fingerprint(cache):
    requests = []
    def send(request):
        requests.append(request)
        assert json.loads(request.content)['temperature'] == 0
        return response()
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        service = DeepSeekEvaluation(config(), cache, client=http)
        body = {'dataset_digest':'synthetic-fixture', 'source':'p2_synthetic',
                    'messages':[{'role':'user','content':'synthetic-only'}]}
        with pytest.raises(EvaluationUnavailable, match='NOT_APPROVED'):
            await service.complete(**body)
        service.allow_network = True
        with pytest.raises(EvaluationUnavailable, match='NOT_AUTHORIZED'):
            await service.complete(**{**body,'source':'ecd'})
        results = await asyncio.gather(*(service.complete(**body) for _ in range(5)))
        assert len(requests) == 1 and service.cache_hits == 4
        assert all(r == results[0] for r in results)
        service.allow_network = False
        assert (await service.complete(**body)) == results[0]
        with pytest.raises(EvaluationUnavailable, match='NOT_APPROVED'):
            await service.complete(**{**body,'dataset_digest':'changed'})
        raw = next(cache.glob('*.json')).read_text()
        assert 'fixture-token' not in raw and 'synthetic-only' not in raw
        assert results[0]['returned_model'] == 'fixture-returned'
        assert next(cache.glob('*.json')).stat().st_mode & 0o777 == 0o600


async def test_rate_limit_backoff_count_and_shared_concurrency(cache, monkeypatch):
    waits = []
    real_sleep = asyncio.sleep
    async def sleep(delay):
        waits.append(delay)
        await real_sleep(0)
    monkeypatch.setattr('agent_ops.deepseek.asyncio.sleep', sleep)
    active = peak = count = 0
    async def send(request):
        nonlocal active, peak, count
        count += 1
        if count == 1:
            return httpx.Response(429, headers={'Retry-After':'2'})
        active += 1
        peak = max(peak, active)
        await real_sleep(.01)
        active -= 1
        return response()
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        service = DeepSeekEvaluation(config(concurrency=2), cache, client=http, allow_network=True,
                                     slots=asyncio.Semaphore(2))
        await asyncio.gather(*(service.complete(dataset_digest=str(i), source='p2_synthetic',
            messages=[{'role':'user','content':'fixture'}]) for i in range(5)))
        assert peak <= 2 and count == 6
        assert service.rate_limits == service.retries == 1 and waits == [2]


@pytest.mark.parametrize('status', [401, 429, 503])
async def test_failures_not_cached_or_leaked(cache, monkeypatch, status):
    async def sleep(_): pass
    monkeypatch.setattr('agent_ops.deepseek.asyncio.sleep',sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as http:
        service = DeepSeekEvaluation(config(), cache, client=http, allow_network=True)
        with pytest.raises(EvaluationUnavailable) as e:
            await service.complete(dataset_digest='fixture',source='p2_synthetic',messages=[{'role':'user','content':'fixture'}])
        assert 'fixture-token' not in str(e.value)
        assert service.network_requests == (1 if status == 401 else 3)
        assert not list(cache.glob('*.json'))
