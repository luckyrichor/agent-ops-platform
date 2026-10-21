"""Read-only verification of the actual full-scale P2 database."""
import argparse,json,time
from pathlib import Path
import psycopg
from sqlalchemy.engine import make_url
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
v=dict(l.split('=',1) for l in Path('.local/p2-application.env').read_text().splitlines() if '=' in l)
u=make_url(v['AGENT_OPS_DATABASE_URL']).set(drivername='postgresql');started=time.monotonic()
with psycopg.connect(u.render_as_string(hide_password=False)) as c:
 c.execute('SET TRANSACTION READ ONLY')
 bad=c.execute('''SELECT count(*) FROM commerce.orders o LEFT JOIN
    (SELECT order_id,sum(quantity*(unit_price_cents+freight_cents)) AS total
     FROM commerce.order_items GROUP BY order_id) i ON i.order_id=o.id
    WHERE o.total_cents IS DISTINCT FROM coalesce(i.total,0)''').fetchone()[0]
 inventory=c.execute('SELECT count(*) FROM commerce.inventory WHERE stock<reserved OR reserved<0').fetchone()[0]
 unchecked=c.execute("SELECT count(*) FROM pg_constraint WHERE connamespace IN ('commerce'::regnamespace,'support'::regnamespace) AND NOT convalidated").fetchone()[0]
 proxy=c.execute("SELECT count(*) FROM support.tickets WHERE synthetic_distribution_note IS DISTINCT FROM %s",('规则代理，非人工真实意图分布，首片段说话人未确认',)).fetchone()[0]
 waiting=c.execute("SELECT count(*) FILTER(WHERE state='waiting_human'),count(*) FROM support.tickets").fetchone()
 links=c.execute("SELECT count(*) FROM commerce.conversations WHERE ticket_id IS DISTINCT FROM lpad(to_hex(1+(id-1)%1000000),32,'0')::uuid").fetchone()[0]
 runtime=c.execute('SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()[0]
 version=c.execute('SHOW server_version').fetchone()[0]
 counts={name:c.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ['commerce.products','commerce.users','commerce.orders','support.tickets','commerce.messages']}
 r={'order_total_mismatch':bad,'inventory_check_violations':inventory,'unvalidated_constraints':unchecked,
    'proxy_note_missing_rows':proxy,'waiting_human':waiting[0],'ticket_count':waiting[1],
    'conversation_ticket_link_mismatch':links,'runtime_non_superuser_no_create_privileges':runtime,
    'server_version':version,'counts':counts,'read_only':True,'seconds':time.monotonic()-started,
    'intent_proxy_note':'规则代理，非人工真实意图分布，首片段说话人未确认','routing_accuracy_or_evaluation_use':False}
 assert not any([bad,inventory,unchecked,proxy,links]) and runtime
 assert waiting[0]==50000 and waiting[1]==1000000
 a.output.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:v for k,v in r.items() if k not in {'server_version','intent_proxy_note'}},ensure_ascii=False))
