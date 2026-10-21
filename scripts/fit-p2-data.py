"""Empirical distributions from verified complete archives; no substitute data sources."""
import argparse,csv,hashlib,io,json,math,random,statistics,zipfile
from collections import Counter,defaultdict
from decimal import Decimal,ROUND_HALF_UP
from pathlib import Path
from agent_ops.support import route_message

p=argparse.ArgumentParser();p.add_argument('--verified',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
if '.local' not in a.output.resolve().parts or a.output.exists():p.error('new private output required')
a.output.mkdir(parents=True,mode=0o700)
v=json.loads(a.verified.read_text());rng=random.Random(42)
def dump(name,value): (a.output/name).write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n')
def cents(value):return int((Decimal(value)*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def csvs(z,name):return csv.DictReader(io.TextIOWrapper(z.open(name),encoding='utf-8-sig'))
def distribution(values):
 values=sorted(values)
 return {'n':len(values),'min':values[0],'max':values[-1],**{f'p{q}':values[min(len(values)-1,math.ceil(len(values)*q/100)-1)] for q in (50,95,99)}}
with zipfile.ZipFile(v['datasets']['olist']['path']) as z:
 products={r['product_id']:r['product_category_name'] or 'uncategorized' for r in csvs(z,'olist_products_dataset.csv')}
 carts=defaultdict(list);prices=defaultdict(list);freights=defaultdict(list)
 for r in csvs(z,'olist_order_items_dataset.csv'):
  price,freight=cents(r['price']),cents(r['freight_value'])
  if price<0 or freight<0:raise ValueError('negative source money')
  pid=r['product_id'];prices[pid].append(price);freights[pid].append(freight)
  carts[r['order_id']].append({'product':pid,'price':price,'freight':freight})
 pdata=[{'source_id':pid,'category':cat,'price':int(statistics.median(prices[pid])),
          'freight':int(statistics.median(freights[pid]))} for pid,cat in sorted(products.items()) if prices[pid]]
 orders=[{'source_id':r['order_id'],'status':r['order_status'],'created_at':r['order_purchase_timestamp'],
           'items':carts[r['order_id']]} for r in csvs(z,'olist_orders_dataset.csv')]
 assert all(i['product'] in products for r in orders for i in r['items'])
 dump('olist-bootstrap.json',{'products':pdata,'orders':orders})
 olist={'products_with_observed_price':len(pdata),'products_without_price':len(products)-len(pdata),
   'orders':len(orders),'order_status':dict(Counter(r['status'] for r in orders)),
   'order_month':dict(Counter(r['created_at'][:7] for r in orders)),
   'product_categories':dict(Counter(r['category'] for r in pdata)),
   'price_cents':distribution([r['price'] for r in pdata]),'freight_cents':distribution([r['freight'] for r in pdata]),
   'cart_items':distribution([len(r['items']) for r in orders]),
   'method':'empirical joint bootstrap; preserve category/observed per-product median price and freight; sample complete order status/time/cart bundles',
   'limitations':'products without observed price excluded, not filled with invented prices; historical order price may differ from current product median'}
lengths=Counter();chars=Counter();routes=Counter();labels=Counter();seen=set();buckets=defaultdict(list);by_length=Counter()
with zipfile.ZipFile(v['datasets']['ecd']['path']) as z:
 for member in sorted(z.namelist()):
  if not member.endswith('.txt'):continue
  with z.open(member) as f:
   for raw in f:
    fields=raw.decode('utf-8-sig').rstrip('\r\n').split('\t')
    if fields[0] not in {'0','1'} or len(fields)<3:raise ValueError('invalid ECD row')
    labels[fields[0]]+=1
    if fields[0]!='1':continue # negative response choices are not actual dialogue continuations
    context=fields[1:-1];key=hashlib.sha256('\t'.join(context).encode()).digest()[:16]
    if key in seen:continue
    seen.add(key)
    turns=[''.join(t.split()) for t in fields[1:]]
    lengths[len(turns)]+=1;chars.update(len(t) for t in turns)
    routes[route_message(turns[0])]+=1
    n=len(turns);by_length[n]+=1;bucket=buckets[n]
    if len(bucket)<200:bucket.append(turns)
    else:
     index=rng.randrange(by_length[n])
     if index<200:bucket[index]=turns
 ecd={'labels':dict(labels),'unique_positive_contexts':len(seen),'turn_count_histogram':dict(sorted(lengths.items())),
      'utterance_chars_histogram':dict(sorted(chars.items())),
      'rule_route_first_segment_unverified':dict(routes),
      'intent_proxy_note':'规则代理，非人工真实意图分布，首片段说话人未确认',
      'intent_distribution_status':'NOT human intent labels; first-segment speaker attribution unknown; use as generator weight only after owner approval',
      'method':'deduplicate positive-response contexts; negative retrieval candidates excluded; remove segmentation spaces; length-stratified seeded reservoir for text templates',
      'text_reservoir_per_length':200,'seed':42}
 dump('ecd-bootstrap-local.json',{'by_length':dict(buckets),'fit':ecd})
user=v['datasets']['userbehavior'];ub={'status':user['status'],'fitted':False,'zipf_exponent':None,'user_activity_histogram':None}
if user['status']=='available':
 user_counts=Counter();item_counts=Counter();types=Counter();n=0
 with zipfile.ZipFile(user['path']) as z:
  members=[m for m in z.namelist() if m.lower().endswith('.csv')]
  if len(members)!=1:raise ValueError('expected one headerless UserBehavior CSV')
  for row in csv.reader(io.TextIOWrapper(z.open(members[0]),encoding='utf-8-sig')):
   if len(row)!=5 or row[3] not in {'pv','buy','cart','fav'}:raise ValueError('unexpected UserBehavior schema')
   uid,item,cat=int(row[0]),int(row[1]),int(row[2]);int(row[4])
   user_counts[uid]+=1;item_counts[item]+=1;types[row[3]]+=1;n+=1
 frequencies=sorted(item_counts.values(),reverse=True)
 xs=[math.log(i+1) for i in range(len(frequencies))];ys=[math.log(f) for f in frequencies]
 meanx=sum(xs)/len(xs);meany=sum(ys)/len(ys)
 slope=sum((x-meanx)*(y-meany) for x,y in zip(xs,ys))/sum((x-meanx)**2 for x in xs)
 ub={'status':'available','fitted':True,'rows':n,'users':len(user_counts),'items':len(item_counts),
     'behavior_counts':dict(types),'zipf_exponent':-slope,'fit_method':'OLS log item count versus log popularity rank; diagnostic, not a goodness-of-fit claim',
     'user_activity_histogram':dict(Counter(user_counts.values()))}
summary={'seed':42,'source_archive_sha256':{k:r.get('archive_sha256') for k,r in v['datasets'].items()},
         'olist':olist,'ecd':ecd,'userbehavior':ub,'raw_text_in_git':False,'synthetic':False}
dump('fits.json',summary)
print(json.dumps({'olist_orders':len(orders),'ecd_unique_contexts':len(seen),'userbehavior_fitted':ub['fitted']}),flush=True)
