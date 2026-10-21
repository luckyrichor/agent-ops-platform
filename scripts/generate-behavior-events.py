"""Offline workload events from owner-provided UserBehavior fits; no guessed Zipf fallback."""
import argparse,bisect,csv,json,random
from itertools import accumulate
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--fit',type=Path,required=True);p.add_argument('--users',type=Path,required=True)
p.add_argument('--products',type=int,default=200000);p.add_argument('--events',type=int,default=1000);p.add_argument('--seed',type=int,default=42)
p.add_argument('--output',type=Path,required=True);a=p.parse_args()
f=json.loads(a.fit.read_text())['userbehavior']
if not f.get('fitted'):p.error('UserBehavior fit missing; no uniform/guessed distribution fallback')
if a.output.exists() or '.local' not in a.output.resolve().parts:p.error('fresh private output required')
if min(a.products,a.events)<=0:p.error('positive scale required')
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
print(json.dumps({'events':a.events,'seed':a.seed,'synthetic':True}))
