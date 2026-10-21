"""Deterministic streamed P2 scale data. Never hide missing distributions or approvals."""
import argparse,csv,hashlib,json,random,time,subprocess,sys
from collections import Counter,defaultdict
from pathlib import Path
from uuid import UUID
PROXY_NOTE='规则代理，非人工真实意图分布，首片段说话人未确认'

p=argparse.ArgumentParser()
p.add_argument('--fit',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
p.add_argument('--seed',type=int,default=42)
for name,n in [('products',200000),('users',1000000),('orders',2000000),('tickets',1000000),('messages',10000000)]:p.add_argument('--'+name,type=int,default=n)
p.add_argument('--waiting-human-ratio',type=float,default=.05)
p.add_argument('--owner-approved-ecd-proxy',action='store_true',help='only after explicit owner approval; not an automatic fallback')
a=p.parse_args()
if '.local' not in a.output.resolve().parts or a.output.exists():p.error('new private output required')
if not 0<=a.waiting_human_ratio<=1:p.error('waiting-human-ratio must be in [0,1]')
if min(a.products,a.users,a.orders,a.messages)<=0 or a.tickets<0:p.error('positive scale required')
a.output.mkdir(parents=True,mode=0o700)
fit=json.loads((a.fit/'fits.json').read_text());base=json.loads((a.fit/'olist-bootstrap.json').read_text())
ecd=json.loads((a.fit/'ecd-bootstrap-local.json').read_text())
started=time.monotonic();files={};handles=[]
def writer(name,columns):
 f=(a.output/(name+'.csv')).open('w',newline='',encoding='utf-8');handles.append(f)
 files[name]={'path':str(a.output/(name+'.csv')),'columns':columns,'rows':0}
 w=csv.writer(f,lineterminator='\n')
 def row(values):w.writerow(values);files[name]['rows']+=1
 return row
try:
 user=writer('users',['id','activity_events']);rng=random.Random(a.seed+1)
 ub=fit['userbehavior'];hist=ub.get('user_activity_histogram')
 counts=list(hist) if hist else [];weights=[hist[k] for k in counts]
 for i in range(1,a.users+1):user([i,int(rng.choices(counts,weights,k=1)[0]) if hist else '\\N'])
 catnames=sorted({r['category'] for r in base['products']});categories={c:i+1 for i,c in enumerate(catnames)}
 category=writer('categories',['id','name'])
 for name,i in categories.items():category([i,name])
 product=writer('products',['id','sku','title','category_id','price_cents','freight_cents','attrs','status'])
 inventory=writer('inventory',['product_id','stock','reserved','version'])
 rng=random.Random(a.seed+2);mapping=defaultdict(list);category_count=Counter()
 # Ensure every source product is represented when the requested scale permits it.
 for i in range(1,a.products+1):
  r=base['products'][i-1] if i<=len(base['products']) else rng.choice(base['products'])
  mapping[r['source_id']].append(i);category_count[r['category']]+=1
  product([i,f'SYN-{i:09d}',f"合成商品·{r['category']}·{i}",categories[r['category']],r['price'],r['freight'],'{}','active'])
  inventory([i,1000,0,1])
 order=writer('orders',['id','user_id','status','total_cents','created_at','revision'])
 items=writer('order_items',['order_id','line_no','product_id','quantity','unit_price_cents','freight_cents'])
 shipments=writer('shipments',['order_id','carrier','tracking_no','status'])
 rng=random.Random(a.seed+3);statuses=Counter();months=Counter();omitted_source_lines=0
 for i in range(1,a.orders+1):
  r=rng.choice(base['orders']);lines=[line for line in r['items'] if line['product'] in mapping]
  omitted_source_lines+=len(r['items'])-len(lines)
  if omitted_source_lines:raise ValueError('product scale too small to preserve every source cart; use full source coverage')
  order([i,1+(i-1)%a.users,r['status'],sum(l['price']+l['freight'] for l in lines),r['created_at']+'+00:00',1])
  statuses[r['status']]+=1;months[r['created_at'][:7]]+=1
  for n,l in enumerate(lines,1):items([i,n,rng.choice(mapping[l['product']]),1,l['price'],l['freight']])
  if r['status'] in ('shipped','delivered'):
   shipments([i,'synthetic-local-carrier',f'SYN-TRACK-{i:012d}',r['status']])
 # Intent-dependent scale is gated; no rule distribution is silently presented as true labels.
 ticket_count=a.tickets if a.owner_approved_ecd_proxy else 0
 if ticket_count:
  ticket=writer('tickets',['ticket_id','route','state','revision','reason_code','order_status','history','created_at','updated_at','synthetic_distribution_note'])
  events=writer('ticket_events',['ticket_id','revision','type','actor','payload','created_at'])
  waiting_count=round(ticket_count*a.waiting_human_ratio)
  waiting=set(random.Random(a.seed+40).sample(range(1,ticket_count+1),waiting_count))
  rng=random.Random(a.seed+4);routes=fit['ecd']['rule_route_first_segment_unverified'];route_names=sorted(routes)
  for i in range(1,ticket_count+1):
   route=rng.choices(route_names,[routes[k] for k in route_names],k=1)[0]
   state='waiting_human' if i in waiting else 'resolved'
   reason='SYNTHETIC_WAITING' if state=='waiting_human' else 'SYNTHETIC_RESOLVED'
   tid=str(UUID(int=i));date='2025-01-01 00:00:00+00'
   ticket([tid,route,state,1,reason,'\\N',json.dumps(['created',state]),date,date,PROXY_NOTE])
   events([tid,1,'created','synthetic-import',json.dumps({'state':state,'reason_code':reason,'distribution_note':PROXY_NOTE},ensure_ascii=False),date])
 conversations=writer('conversations',['id','user_id','ticket_id'])
 keys=writer('message_keys',['conversation_id','seq','created_at'])
 messages=writer('messages',['conversation_id','seq','created_at','sender','content'])
 rng=random.Random(a.seed+5);lengths=sorted(map(int,fit['ecd']['turn_count_histogram']));weights=[fit['ecd']['turn_count_histogram'][str(k)] for k in lengths]
 remaining=a.messages;cid=0;generated_lengths=Counter();final_truncation=0
 while remaining:
  cid+=1;n=rng.choices(lengths,weights,k=1)[0];turns=rng.choice(ecd['by_length'][str(n)])
  actual=min(n,remaining);final_truncation+=n-actual
  conversations([cid,1+(cid-1)%a.users,str(UUID(int=1+(cid-1)%ticket_count)) if ticket_count else '\\N'])
  generated_lengths[actual]+=1;month=1+(cid-1)%12
  for seq,content in enumerate(turns[:actual],1):
   date=f'2025-{month:02d}-01 00:{seq//60:02d}:{seq%60:02d}+00'
   keys([cid,seq,date]);messages([cid,seq,date,'source',content])
  remaining-=actual
finally:
 for f in handles:f.close()
for name,record in files.items():
 path=Path(record['path']);h=hashlib.sha256()
 with path.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 record.update(bytes=path.stat().st_size,sha256=h.hexdigest(),intent_proxy_note=PROXY_NOTE if name in {'tickets','ticket_events'} else None)
source_hashes={}
for path in sorted(a.fit.glob('*.json')):source_hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
manifest={'seed':a.seed,'synthetic':True,'files':files,'generation_seconds':time.monotonic()-started,
 'total_csv_bytes':sum(r['bytes'] for r in files.values()),'source_fit_sha256':source_hashes,
 'generated_distributions':{'category_counts':dict(category_count),'order_status':dict(statuses),'order_month':dict(months),
                            'conversation_turns':dict(sorted(generated_lengths.items()))},
 'user_activity_fit_complete':ub['fitted'],'user_assignment':'round-robin identity linkage only, NOT a UserBehavior-fitted activity or order distribution',
 'intent_proxy_note':PROXY_NOTE,
 'waiting_human_ratio':a.waiting_human_ratio,'waiting_human_count':round(ticket_count*a.waiting_human_ratio),
 'state_occupancy_note':'independently configured queue-state occupancy; not route-to-outcome labels, not workflow quality evidence',
 'intent_proxy_owner_approved':a.owner_approved_ecd_proxy,'ticket_target_blocked':not a.owner_approved_ecd_proxy,
 'true_intent_fit_complete':False,'sender_attribution':'source: ECD does not identify speaker roles; no fabricated customer attribution',
 'stock_method':'fixed1000 synthetic fixture; no real inventory distribution claim',
 'source_text_distribution':'private only; ECD raw/synthetic reused text must not be redistributed',
 'final_conversation_truncated_turns':final_truncation,'omitted_source_order_lines':omitted_source_lines,
 'quality_or_production_claim':False,'phase_p2_accepted':False}
(a.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n')
subprocess.run([sys.executable,'scripts/p2-provenance.py','--data',str(a.output)],check=True)
print(json.dumps({'rows':{k:r['rows'] for k,r in files.items()},'bytes':manifest['total_csv_bytes'],'seconds':manifest['generation_seconds'],'tickets_blocked':manifest['ticket_target_blocked']}),flush=True)
