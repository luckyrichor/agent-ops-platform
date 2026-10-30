import json
import shutil
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_ops.deepseek import DeepSeekConfig, DeepSeekEvaluation
from agent_ops.p8_pilot import PilotContext, PilotSample, run_pilot


@pytest.fixture
def artifacts():
    p=Path('.local/test-p8-pilot')/uuid4().hex
    yield p
    shutil.rmtree(p,ignore_errors=True)


def samples():
    return [PilotSample(id=f'p8-pilot-{i:03d}',source='p2_synthetic',message=f'测试物流{i}',
        context=PilotContext(order_reference=f'PILOT-{i:03d}',status='shipped',total_cents=100,category_id=1))
        for i in range(50)]


async def test_fifty_request_response_cache_metrics_and_no_new_calls(artifacts):
    calls=0
    def send(request):
        nonlocal calls
        calls+=1
        assert json.loads(request.content)['temperature']==0
        return httpx.Response(200,json={'model':'fixture-returned','usage':{
            'prompt_tokens':120,'completion_tokens':20},'choices':[{'message':{
            'content':'{"route":"order_status","reason_code":"ORDER_QUERY"}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        config=DeepSeekConfig('fixture-key','https://provider.invalid/chat/completions','fixture-model')
        service=DeepSeekEvaluation(config,artifacts/'cache',allow_network=True,client=client)
        summary=await run_pilot(samples(),service,artifacts)
        assert summary['valid_answers']==calls==50
        assert summary['average_total_tokens']==140 and summary['failure_rate']==0
        assert summary['total_prompt_tokens']==6000 and summary['retries']==0
        assert len(list((artifacts/'requests').glob('*.json')))==50
        assert len(list((artifacts/'returns').glob('*.json')))==50
        service.allow_network=False
        repeat=await run_pilot(samples(),service,artifacts)
        assert repeat['network_requests']==0 and repeat['cache_hits']==50 and calls==50
        assert next((artifacts/'requests').glob('*.json')).stat().st_mode & 0o777==0o600
        assert 'fixture-key' not in (artifacts/'summary.json').read_text()
        with pytest.raises(ValueError,match='EXACTLY_50'):
            await run_pilot(samples()[:49],service,artifacts)


async def test_canary_fail_stops_remaining_and_reports_actual_denominator(artifacts):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(401))) as client:
        service=DeepSeekEvaluation(DeepSeekConfig('fixture','https://provider.invalid/c','fixture'),
                                  artifacts/'cache',allow_network=True,client=client)
        summary=await run_pilot(samples(),service,artifacts)
        assert summary['attempted_items']==summary['network_requests']==1
        assert summary['failure_rate']==1 and summary['average_total_tokens'] is None
        assert summary['valid_answers']==0 and summary['retries']==0


async def test_invalid_answer_preserves_usage_and_is_failure(artifacts):
    def send(_):
        return httpx.Response(200,json={'model':'fixture','usage':{
            'prompt_tokens':2,'completion_tokens':1},'choices':[{'message':{'content':'{"route":"invented"}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        service=DeepSeekEvaluation(DeepSeekConfig('fixture','https://provider.invalid/c','fixture'),
                                  artifacts/'cache',allow_network=True,client=client)
        summary=await run_pilot(samples(),service,artifacts)
        assert summary['valid_answers']==0 and summary['failure_rate']==1
        assert summary['average_total_tokens']==3 and len(list((artifacts/'returns').glob('*.json')))==50
