"""Only AFTER a pushed freeze tag: unseen ECD silver strata + synthetic paraphrase fill."""
import argparse
import asyncio
import hashlib
import heapq
import json
import os
import random
import shlex
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.datavalidation import DataValidation

from agent_ops.deepseek import DeepSeekConfig, DeepSeekEvaluation, EvaluationUnavailable
from agent_ops.p8_pilot import private_write
from agent_ops.p8_release import verify_freeze
from agent_ops.routing_evaluation import (
    DevelopmentSample,
    SilverAnswer,
    group_digest,
    redacted_turns,
    request_messages,
)
from agent_ops.routing_policy import CLARIFY_REPORT_TAXONOMY

GENERATION_PROMPT='''生成口语化、有表达变化的合成顾客句子，用于人工盲标，不提供人工真值。
依据给定动作定义，写4个彼此不同的顾客当前句。不要改写、引用任何真实语料。
不含真实人名、公司/品牌/机构、地址、手机号、邮箱、账号、订单号、URL；用“这单/我之前买的”。
用不同长度、语序、礼貌程度和自然口头表达，不要机械复述类名或标签定义。
只输出json：{"sentences":["句子一","句子二","句子三","句子四"]}。'''
INTENTS={
 'order_status':'顾客已经买过商品，想查询已发生订单的履约/配送进度，不是选择承运商。',
 'return_request':'顾客明确申请退换货或退款，不仅询问历史退款进度。',
 'human':'顾客明确需要人工接管，或提出需要人工处理的投诉/隐私/盗刷事项。',
 'clarify':'顾客暂时没有清楚可执行的售后诉求，或售前/承运商选择咨询，或只有致谢确认。',
}


def shingles(s):
    return {s[i:i+3] for i in range(max(0,len(s)-2))}


def near(s,others):
    a=shingles(s)
    return any(s==t or (len(s)>=8 and a and a|b and len(a&b)/len(a|b)>=.85) for t,b in others)


def workbook(path,rows):
    b=Workbook();g=b.active;g.title='01_标注说明'
    guide=[['P8 最终200条盲标','规则/提示/阈值已冻结后抽取；不含预测或抽样层标签。'],
      ['来源','ECD=本地脱敏真实语料片段、原角色未知；合成改写=模型生成的口语顾客句，须同样按文本判断。'],
      ['填写顺序','先填角色（顾客/客服/无法确认），只对顾客填四根类；客服/无法确认留标签空并写原因，不猜测。'],
      ['order_status','已有订单履约/物流进度查询；缺订单号仍可属于此类。承运方式咨询、指定/更换承运商不是查单。'],
      ['return_request','明确申请退货、换货、退款。'],['human','明确找人工，或需要人工处理的投诉、隐私、盗刷。'],
      ['clarify','信息不足、超范围咨询、无需处理或多意图无主次。不要因为出现发/快递就推断查单。'],
      ['信息不足','系统应追问；根类仍填clarify。'],['超范围咨询','系统应转售前或告知不处理；根类仍填clarify。'],
      ['无需处理','致谢/确认/下单或购买计划告知，系统不需要动作；根类仍填clarify。'],
      ['子类列','若根类clarify，可填已固定三子类；不是模型预测。其余根类子类留空。'],
      ['重要','不看模型答案；每条请填写角色。角色无法确认可弃权，不把未完成标注当弃权。'],
      ['回传','只填人工角色、人工标签、clarify子类、备注，保留编号和来源/文本。保存xlsx发回，最终评测只跑一次。'],
      ['隐私','含脱敏ECD，不上传公开仓库。占位符是模式替换；不需要恢复原始信息。']]
    for row in guide:g.append(row)
    g.column_dimensions['A'].width=22;g.column_dimensions['B'].width=105
    for row in g:
        for c in row:c.alignment=Alignment(wrap_text=True,vertical='top')
    for i in range(1,len(guide)+1):g.row_dimensions[i].height=42
    g.sheet_properties.pageSetUpPr.fitToPage=True;g.page_setup.orientation='landscape';g.page_setup.fitToWidth=1;g.page_setup.fitToHeight=1
    s=b.create_sheet('02_最终盲标200条');s.append(['编号','source','角色状态','前文片段','当前句','人工角色','人工根标签','clarify子类','备注'])
    for row in rows:
        s.append([row['id'],'ECD' if row['source']=='ecd_redacted' else '合成改写',
          '未知' if row['source']=='ecd_redacted' else '合成顾客','\n'.join(row['context']),row['message'],'','','',''])
    for sheet in b:
        for row in sheet:
            for c in row:
                if c.value is not None:c.value=str(c.value);c.data_type='s'
                c.alignment=Alignment(wrap_text=True,vertical='top')
    for c in s[1]:c.font=Font(bold=True)
    for col,width in zip('ABCDEFGHI',[18,18,16,60,60,16,24,22,40],strict=True):s.column_dimensions[col].width=width
    for i in range(2,202):s.row_dimensions[i].height=95
    for col,values in [('F','顾客,客服,无法确认'),('G','order_status,return_request,human,clarify'),('H','信息不足,超范围咨询,无需处理')]:
        v=DataValidation(type='list',formula1='"'+values+'"',allow_blank=True);s.add_data_validation(v);v.add(col+'2:'+col+'201')
    s.freeze_panes='F2';s.auto_filter.ref='A1:I201';b.save(path);path.chmod(0o600)


def pool(args):
    used=json.loads(Path('.local/p8-development/dataset/used-groups.json').read_text())
    excluded=set(used['groups'])|set(used['excluded_prior_p2_groups']);utterances=set(used['ecd_utterance_hashes'])
    development=json.loads(Path('.local/p8-development/dataset/dataset.json').read_text())['samples']
    comparison=[(r['message'],shingles(r['message'])) for r in development]
    heap=[];seen=set();stats=Counter()
    with zipfile.ZipFile('.local/datasets/raw/ecd.zip') as z,z.open('E-commerce dataset/train.txt') as f:
        for line in f:
            stats['rows_scanned']+=1
            fields=line.decode('utf-8-sig').rstrip('\r\n').split('\t')
            if len(fields)<3 or fields[0]!='1':continue
            turns=[''.join(t.split()) for t in fields[1:-1]];key=group_digest(turns)
            if not turns or key in seen:continue
            seen.add(key)
            if key in excluded or (len(turns[-1])>=8 and hashlib.sha256(turns[-1].encode()).hexdigest() in utterances):continue
            if max(map(len,turns))>2000 or len(turns[-1])<2:continue
            priority=int(hashlib.sha256((str(args.seed)+':final:'+key).encode()).hexdigest(),16)
            item=(-priority,key,turns)
            if len(heap)<2400:heapq.heappush(heap,item)
            elif item>heap[0]:heapq.heapreplace(heap,item)
    rows=[];views=set();hits=Counter()
    for _,key,turns in sorted(heap,reverse=True):
        clean,counts=redacted_turns(turns[-4:]);view=group_digest(clean)
        if view in views or near(clean[-1],comparison):stats['duplicate_or_near_excluded']+=1;continue
        views.add(view);hits.update(counts)
        comparison.append((clean[-1],shingles(clean[-1])))
        rows.append(DevelopmentSample(id=f'pool-{len(rows)+1:04d}',source='ecd_redacted',message=clean[-1],context=clean[:-1],group_hash=key,speaker_role='unknown').model_dump())
        if len(rows)==600:break
    if len(rows)<200:raise ValueError('INSUFFICIENT_UNUSED_ECD_POOL')
    return rows,{'counts':dict(stats),'redaction':dict(hits),'eligible_pool':len(rows)}


async def prepare(args):
    frozen=verify_freeze(args.freeze_tag,Path('evals/p8/freeze.json'))
    config=DeepSeekConfig.from_env()
    if any(getattr(config,key)!=frozen[key] for key in ['model','endpoint','concurrency','max_attempts','timeout_seconds']):
        raise ValueError('FINAL_PROVIDER_CONFIG_CHANGED')
    if (args.output/'blind-final.xlsx').exists():raise ValueError('FINAL_ALREADY_PREPARED')
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    if (args.output/'candidate-pool.json').exists():
        data=json.loads((args.output/'candidate-pool.json').read_text());rows=data['rows'];prep=data['preparation']
        if data['freeze_commit']!=frozen['commit']:raise ValueError('POOL_FREEZE_MISMATCH')
    else:
        rows,prep=pool(args);private_write(args.output/'candidate-pool.json',{'freeze_commit':frozen['commit'],'rows':rows,'preparation':prep})
    digest=group_digest([r['group_hash'] for r in rows])
    service=DeepSeekEvaluation(config,args.output/'cache',allow_network=True,allow_ecd=True)
    semaphore=asyncio.Semaphore(service.config.concurrency)
    async def silver(row):
        sample=DevelopmentSample.model_validate(row);messages=request_messages(sample,'silver')
        async with semaphore:
            request_id=row['id']+'-'+hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest()[:16]
            private_write(args.output/'requests'/(request_id+'.json'),messages)
            try:
                result=await service.complete(dataset_digest=digest,source=row['source'],messages=messages)
                private_write(args.output/'returns'/(request_id+'.json'),result)
                return SilverAnswer.model_validate_json(result['content']).label
            except (EvaluationUnavailable,ValueError):return None
    try:
        # Worker-sized batches avoid a large semaphore queue consuming request timeouts.
        labels=[]
        for start in range(0,len(rows),service.config.concurrency):
            labels.extend(await asyncio.gather(*(silver(r) for r in rows[start:start+service.config.concurrency])))
        strata=defaultdict(list)
        for row,label in zip(rows,labels,strict=True):
            if label is not None:strata[label].append(row)
        selected=[];plan=[];rng=random.Random(args.seed);duplicates=[(r['message'],shingles(r['message'])) for r in json.loads(Path('.local/p8-development/dataset/dataset.json').read_text())['samples']]
        generated=[];generation_calls=0
        for label,intent in INTENTS.items():
            choices=rng.sample(strata[label],min(50,len(strata[label])))
            for row in choices:
                selected.append(row);plan.append({'source':'ecd_redacted','silver_layer':label,'N_h':len(strata[label]),'n_h':len(choices),
                  'inclusion_probability':len(choices)/len(strata[label]),'weight':len(strata[label])/len(choices)})
                duplicates.append((row['message'],shingles(row['message'])))
            missing=50-len(choices);accepted=0;batch=0
            while accepted<missing and batch<50 and generation_calls<80:
                batch+=1;generation_calls+=1
                messages=[{'role':'system','content':GENERATION_PROMPT},{'role':'user','content':json.dumps({'intent':intent,
                  'batch':batch,'style':['简短口语','礼貌商量','着急但清楚','犹豫表达'][batch%4]},ensure_ascii=False)}]
                private_write(args.output/'generation-requests'/f'{label}-{batch}.json',messages)
                result=await service.complete(dataset_digest=digest+':generation',source='synthetic_rewrite',messages=messages)
                private_write(args.output/'generation-returns'/f'{label}-{batch}.json',result)
                try:phrases=json.loads(result['content']).get('sentences',[])
                except (ValueError,AttributeError):continue
                if not isinstance(phrases,list):continue
                for phrase in phrases[:4]:
                    if accepted>=missing:break
                    if not isinstance(phrase,str) or not 8<=len(phrase)<=160:continue
                    clean,_hits=redacted_turns([phrase]);phrase=clean[0]
                    if near(phrase,duplicates):continue
                    row=DevelopmentSample(id=f'generated-{len(generated)+1:04d}',source='synthetic_rewrite',message=phrase,context=[],
                      group_hash=hashlib.sha256(phrase.encode()).hexdigest(),speaker_role='synthetic_customer').model_dump()
                    check=await silver(row)
                    if check!=label:continue
                    selected.append(row);generated.append(row);duplicates.append((phrase,shingles(phrase)))
                    plan.append({'source':'synthetic_rewrite','silver_layer':label,'N_h':None,'n_h':missing,
                                 'inclusion_probability':None,'weight':None,'reason':'generated fill, not a probability sample'})
                    accepted+=1
            if accepted<missing:raise ValueError('SYNTHETIC_FILL_INCOMPLETE')
        order=list(range(200));rng.shuffle(order);final=[];private_plan=[]
        for i,index in enumerate(order,1):
            row={**selected[index],'id':f'final-{i:03d}'};final.append(row);private_plan.append({'id':row['id'],**plan[index]})
        private_write(args.output/'final-dataset.json',{'freeze_commit':frozen['commit'],'freeze_tag':args.freeze_tag,'rows':final})
        private_write(args.output/'sampling-plan-private.json',private_plan)
        workbook(args.output/'blind-final.xlsx',final)
        public=Path('evals/p8');public.mkdir(parents=True,exist_ok=True)
        with (public/'final-synthetic.jsonl').open('w') as f:
            for row in final:
                if row['source']=='synthetic_rewrite':f.write(json.dumps({'id':row['id'],'source':'合成改写','message':row['message'],'context':row['context']},ensure_ascii=False)+'\n')
        stats={'freeze_tag':args.freeze_tag,'freeze_commit':frozen['commit'],'seed':args.seed,'pool':prep,
          'ecd_silver_counts':{k:len(v) for k,v in strata.items()},'selected_sources':dict(Counter(r['source'] for r in final)),
          'silver_layers':{k:{'ECD_N_h':len(strata[k]),'ECD_n_h':min(50,len(strata[k])),
            'ECD_weight':len(strata[k])/min(50,len(strata[k])) if strata[k] else None,
            'synthetic_fill':50-min(50,len(strata[k])),'total':50} for k in INTENTS},
          'network_requests':service.network_requests,'retries':service.retries,'rate_limits':service.rate_limits,
          'model':config.model,'endpoint':config.endpoint,'temperature':0,'reported_at':datetime.now(UTC).isoformat(),
          'pool_silver_failed':sum(label is None for label in labels),'http_statuses':dict(service.http_statuses),
          'generation_batches':generation_calls,'blind_rows':len(final),'predictions_exported':False,
          'human_gold_distribution':'unknown; awaiting blind annotation','synthetic_weight':'not applicable; not natural ECD',
          'dataset_sha256':hashlib.sha256((args.output/'final-dataset.json').read_bytes()).hexdigest(),
          'workbook_sha256':hashlib.sha256((args.output/'blind-final.xlsx').read_bytes()).hexdigest(),
          'clarify_report_taxonomy':CLARIFY_REPORT_TAXONOMY}
        private_write(args.output/'preparation-summary.json',stats)
        Path('docs/reports/p8-final-preparation-2026-10-30').mkdir(parents=True,exist_ok=True)
        Path('docs/reports/p8-final-preparation-2026-10-30/sampling.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2))
        print(json.dumps(stats,ensure_ascii=False))
    finally:await service.aclose()


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('--freeze-tag',required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seed',type=int,default=20261030);p.add_argument('--key-file',type=Path,required=True)
    a=p.parse_args();os.umask(0o077)
    if not a.output.resolve().is_relative_to((Path.cwd()/'.local').resolve()):p.error('private output required')
    if a.key_file.stat().st_mode&0o777!=0o600:p.error('key environment mode600 required')
    for line in a.key_file.read_text().splitlines():
        for word in shlex.split(line,comments=True):
            if word.startswith('DEEPSEEK_API_KEY='):os.environ['DEEPSEEK_API_KEY']=word.split('=',1)[1]
    asyncio.run(prepare(a))


if __name__=='__main__':
    try:main()
    except Exception as e:  # noqa: BLE001 -- never emit final text or credentials
        print(json.dumps({'preparation_failed_type':type(e).__name__}));raise SystemExit(1) from None
