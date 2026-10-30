"""Authorized private development calls: 2000 shared classifications + 1200 ECD silver jobs."""
import argparse
import asyncio
import fcntl
import json
import os
import shlex
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from agent_ops.deepseek import DeepSeekConfig, DeepSeekEvaluation, EvaluationUnavailable
from agent_ops.p8_pilot import PilotAnswer, private_write
from agent_ops.routing_evaluation import (
    DevelopmentSample,
    SilverAnswer,
    development_versions,
    group_digest,
    request_messages,
)


async def run(args):
    samples=[DevelopmentSample.model_validate(r) for r in json.loads(args.dataset.read_text())['samples']]
    if len(samples)!=2000 or Counter(s.source for s in samples)!={'p2_synthetic':800,'ecd_redacted':1200}:
        raise ValueError('exactly approved 800 P2 + 1200 redacted ECD required')
    digest=group_digest([s.model_dump_json() for s in samples])
    service=DeepSeekEvaluation(DeepSeekConfig.from_env(),args.cache or args.output/'cache',
        allow_network=args.allow_network,allow_ecd=args.allow_ecd)
    jobs=[(s,'classifier') for s in samples]+[(s,'silver') for s in samples if s.source=='ecd_redacted']
    queue:asyncio.Queue[Any]=asyncio.Queue()
    for job in jobs:queue.put_nowait(job)
    records=[];started=perf_counter()

    async def worker():
        while not queue.empty():
            sample,purpose=queue.get_nowait()
            message=request_messages(sample,purpose)
            private_write(args.output/'requests'/f'{sample.id}-{purpose}.json',{
                'source':sample.source,'purpose':purpose,'dataset_digest':digest,'model':service.config.model,
                'temperature':0,'thinking':{'type':'disabled'},'max_tokens':512,
                'response_format':{'type':'json_object'},'messages':message})
            begin=perf_counter()
            row={'id':sample.id,'source':sample.source,'purpose':purpose}
            try:
                result=await service.complete(dataset_digest=digest,source=sample.source,messages=message)
                row['provider']=result
                private_write(args.output/'returns'/f'{sample.id}-{purpose}.json',result)
                try:
                    parsed=(PilotAnswer if purpose=='classifier' else SilverAnswer).model_validate_json(result['content'])
                    row['answer']=parsed.model_dump();row['status']='succeeded'
                except ValidationError:
                    row['status']='invalid_schema'
            except EvaluationUnavailable as e:
                row.update(status='failed',error_code=str(e))
            row['observed_seconds']=perf_counter()-begin
            private_write(args.output/'records'/f'{sample.id}-{purpose}.json',row)
            records.append(row);queue.task_done()
            if len(records)%200==0:
                print(json.dumps({'completed_jobs':len(records),'total_jobs':len(jobs),
                     'network_requests':service.network_requests,'retries':service.retries,'rate_limits':service.rate_limits}),flush=True)
    try:
        await asyncio.gather(*(worker() for _ in range(service.config.concurrency)))
        byjob={(r['id'],r['purpose']):r for r in records}
        versions=[]
        for sample in samples:
            classification=byjob[(sample.id,'classifier')]
            route=classification.get('answer',{}).get('route')
            versions.append({'id':sample.id,'source':sample.source,
                **development_versions(sample,route)})
        private_write(args.output/'development-versions.json',versions)
        grouped=defaultdict(list)
        for r in records:grouped[(r['source'],r['purpose'])].append(r)
        summary={'samples':2000,'jobs':len(jobs),'network_requests':service.network_requests,
                 'retries':service.retries,'rate_limits':service.rate_limits,'cache_hits':service.cache_hits,
                 'elapsed_wall_seconds':perf_counter()-started,'dataset_digest':digest,
                 'reported_at':datetime.now(UTC).isoformat(),'requested_model':service.config.model,
                 'temperature':0,'groups':{},'hybrid_model_branch_count':sum(v['hybrid_model_used'] for v in versions),
                 'hybrid_additional_requests':0,'final_test_sampled':False,
                 'human_metrics':'not measured; awaiting 100 blind human development labels',
                 'final_metrics':'not measured; freeze and later unseen 200 selection not performed'}
        for (source,purpose),rows in grouped.items():
            returned=list({r['provider']['fingerprint']:r['provider'] for r in rows if 'provider' in r}.values())
            usage={k:sum(r['usage'].get(k,0) for r in returned) for k in [
                   'prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens']}
            failures=sum(r['status']!='succeeded' for r in rows)
            summary['groups'][source+'-'+purpose]={'jobs':len(rows),'valid':len(rows)-failures,
                'failed_or_invalid':failures,'failure_rate':failures/len(rows),'usage':usage,
                'unique_cached_responses':len(returned),'usage_scope':'unique stored responses; cache-only replay adds no billable tokens',
                'average_seconds':sum(r['observed_seconds'] for r in rows)/len(rows),
                'returned_models':sorted({r['returned_model'] for r in returned}),
                'called_at':[min(r['called_at'] for r in returned),max(r['called_at'] for r in returned)] if returned else [],
                'model_label_counts':dict(Counter(r.get('answer',{}).get('route',r.get('answer',{}).get('label','invalid')) for r in rows)),
                'silver_uncertain':sum(r.get('answer',{}).get('uncertain',False) for r in rows) if purpose=='silver' else None}
        private_write(args.output/'summary.json',summary)
        print(json.dumps(summary),flush=True)
    finally:await service.aclose()


def main():
    p=argparse.ArgumentParser(__doc__)
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cache',type=Path);p.add_argument('--key-file',type=Path,required=True)
    p.add_argument('--allow-network',action='store_true');p.add_argument('--allow-ecd',action='store_true')
    args=p.parse_args();os.umask(0o077)
    root=(Path.cwd()/'.local').resolve()
    for path in [args.dataset,args.output]:
        if path.is_symlink() or not path.resolve().is_relative_to(root):p.error('private .local paths required')
    if args.key_file.is_symlink() or args.key_file.stat().st_mode&0o777!=0o600:p.error('private mode600 key environment required')
    for line in args.key_file.read_text().splitlines():
        for word in shlex.split(line,comments=True):
            if word.startswith('DEEPSEEK_API_KEY='):os.environ['DEEPSEEK_API_KEY']=word.split('=',1)[1]
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (args.output/'run.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);asyncio.run(run(args))


if __name__=='__main__':
    try:main()
    except Exception as e:  # noqa: BLE001 -- suppress sensitive exception text/rows
        print(json.dumps({'run_failed_type':type(e).__name__}),flush=True);raise SystemExit(1) from None
