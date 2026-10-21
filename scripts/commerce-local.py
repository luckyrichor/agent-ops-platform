"""Private local P2 demonstration, no HTTP customer endpoints or authentication claims."""
import argparse,asyncio,json
from pathlib import Path
from agent_ops.commerce_backend import CommerceBackend,PlaceOrder
async def main():
 p=argparse.ArgumentParser();p.add_argument('--env',type=Path,default=Path('.local/p2-application.env'))
 p.add_argument('--action',choices=('products','place-order','order','cancel'),required=True)
 p.add_argument('--input',type=Path);p.add_argument('--key');p.add_argument('--order-id',type=int)
 p.add_argument('--user-id',type=int);p.add_argument('--revision',type=int,default=1)
 a=p.parse_args();env=dict(l.split('=',1) for l in a.env.read_text().splitlines() if '=' in l)
 service=CommerceBackend(env['AGENT_OPS_DATABASE_URL'])
 try:
  if a.action=='products':r={'product_rows':len(await service.products()),'local_only':True}
  elif a.action=='place-order':
   if not a.input or not a.key:p.error('input JSON and idempotency key required')
   r=await service.place_order(PlaceOrder.model_validate_json(a.input.read_text()),a.key)
  elif a.action=='order':
   if not a.order_id or not a.user_id:p.error('order ID and trusted local user ID required')
   order=await service.order(a.order_id,a.user_id)
   r={k:order[k] for k in ('id','status','total_cents')};r['item_count']=len(order['items']);r['shipment_present']=bool(order['shipment'])
  else:
   if not a.order_id or not a.user_id:p.error('order/user IDs required')
   r=await service.cancel_order(a.order_id,a.user_id,a.revision)
  print(json.dumps(r))
 finally:await service.aclose()
if __name__=='__main__':asyncio.run(main())
