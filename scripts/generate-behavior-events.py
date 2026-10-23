"""Offline workload events from owner-provided UserBehavior fits; no guessed Zipf fallback."""
import argparse,bisect,csv,hashlib,json,math,random,time
from collections import Counter
from itertools import accumulate
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--fit',type=Path,required=True);p.add_argument('--users',type=Path,required=True)
p.add_argument('--products',type=int,default=200000);p.add_argument('--events',type=int,default=1000);p.add_argument('--seed',type=int,default=42)
p.add_argument('--output',type=Path,required=True);a=p.parse_args()
f=json.loads(a.fit.read_text())['userbehavior']
if not f.get('fitted'):p.error('UserBehavior fit missing; no uniform/guessed distribution fallback')
if a.output.exists() or '.local' not in a.output.resolve().parts:p.error('fresh private output required')
if min(a.products,a.events)<=0:p.error('positive scale required')
if not math.isfinite(f['zipf_exponent']) or f['zipf_exponent']<=0:p.error('positive finite fitted exponent required')
started=time.monotonic();observed=Counter()
rng=random.Random(a.seed);rank_cdf=list(accumulate((i+1)**(-f['zipf_exponent']) for i in range(a.products)))
users=[];activity=[]
with a.users.open() as stream:
 for row in csv.reader(stream):
  users.append(int(row[0]));activity.append(int(row[1]))
user_cdf=list(accumulate(activity));types=sorted(f['behavior_counts']);type_weights=[f['behavior_counts'][k] for k in types]
if not user_cdf or user_cdf[-1]<=0:p.error('fitted user activity required')
a.output.parent.mkdir(parents=True,exist_ok=True)
with a.output.open('w') as out:
 for seq in range(a.events):
  product=1+bisect.bisect_left(rank_cdf,rng.random()*rank_cdf[-1])
  user=users[bisect.bisect_left(user_cdf,rng.random()*user_cdf[-1])]
  kind=rng.choices(types,type_weights,k=1)[0]
  out.write(json.dumps({'seq':seq,'product_id':product,'user_id':user,'behavior':kind})+'\n')
  observed[kind]+=1
def sha(path):
 h=hashlib.sha256()
 with path.open('rb') as stream:
  for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
meta={'events':a.events,'seed':a.seed,'synthetic':True,'products':a.products,
 'behavior_counts':dict(sorted(observed.items())),'sha256':sha(a.output),'bytes':a.output.stat().st_size,
 'fit_sha256':sha(a.fit),'users_sha256':sha(a.users),'zipf_exponent':f['zipf_exponent'],
 'popularity_behavior':f.get('popularity_behavior','all-behaviors'),
 'method':'inverse CDF of truncated fitted power rank over synthetic SKU IDs; sample users proportional to resampled activity; behavior independently sampled from full-source ratios',
 'limitations':'synthetic IDs, no semantic mapping from Taobao IDs to Olist; independent marginals, no session/transition/causal conversion model; offline fixture not replay or cache benchmark',
 'generation_seconds':time.monotonic()-started}
a.output.with_suffix('.manifest.json').write_text(json.dumps(meta,sort_keys=True,indent=2)+'\n')
print(json.dumps({'events':a.events,'seed':a.seed,'synthetic':True,'sha256':meta['sha256']}))
