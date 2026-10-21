"""Quick private cue check only, not human labels or speaker confirmation."""
import hashlib,json,random
from collections import Counter
from pathlib import Path
p=Path('.local/datasets/fit-v1/ecd-bootstrap-local.json')
d=json.loads(p.read_text());candidates=[t for n,rows in sorted(d['by_length'].items(),key=lambda r:int(r[0])) for t in rows]
rows=[]
for turns in random.Random(42).sample(candidates,40):
 first=turns[0]
 customer=any(s in first for s in ['我买','我的订单','我拍','我要退','我想买','我付款'])
 service=any(s in first for s in ['亲','您好','这边','您','稍等'])
 label='both-cues' if customer and service else 'customer-cue' if customer else 'service-cue' if service else 'no-clear-cue'
 rows.append({'first_segment_sha256':hashlib.sha256(first.encode()).hexdigest(),'cue':label})
r={'seed':42,'sample_n':len(rows),'cue_counts':dict(Counter(r['cue'] for r in rows)),
 'human_reviewed':False,'speaker_confirmed':False,
 'conclusion':'quick lexical cues cannot establish speaker identity; no explicit role fields in ECD. Keep first-speaker-unconfirmed note.',
 'intent_proxy_note':'规则代理，非人工真实意图分布，首片段说话人未确认','records':rows}
Path('.local/datasets/speaker-check.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in r.items() if k!='records'},ensure_ascii=False))
