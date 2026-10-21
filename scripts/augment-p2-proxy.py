"""Apply owner-approved category proxy to an isolated P2 import, without reloading raw text.

规则代理，非人工真实意图分布，首片段说话人未确认
"""
import argparse,hashlib,json,time
from pathlib import Path
import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
p=argparse.ArgumentParser()
for n in ['data','base-data','base-import','output']:p.add_argument('--'+n,type=Path,required=True)
a=p.parse_args()
if a.output.exists() or '.local' not in a.output.resolve().parts:p.error('new private output required')
new=json.loads((a.data/'manifest.json').read_text());old=json.loads((a.base_data/'manifest.json').read_text());prior=json.loads(a.base_import.read_text())
assert prior['complete'] and new['intent_proxy_owner_approved'] and not old['intent_proxy_owner_approved']
assert prior['source_manifest_sha256']==hashlib.sha256((a.base_data/'manifest.json').read_bytes()).hexdigest()
for name,r in old['files'].items():
 if name!='conversations':assert r['sha256']==new['files'][name]['sha256']
for r in new['files'].values():
 h=hashlib.sha256()
 with Path(r['path']).open('rb') as f:
  for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
 assert h.hexdigest()==r['sha256']
secret=dict(l.split('=',1) for l in Path('.local/stack.env').read_text().splitlines() if '=' in l)
app=dict(l.split('=',1) for l in Path('.local/p2-application.env').read_text().splitlines() if '=' in l)
url=make_url(app['AGENT_OPS_DATABASE_URL']).set(username='ops_owner',password=secret['POSTGRES_PASSWORD'])
assert url.database==prior['database'] and url.database.startswith('agent_ops_p2_')
started=time.monotonic();cfg=Config('alembic.ini');cfg.attributes['database_url']=url.render_as_string(hide_password=False);command.upgrade(cfg,'head')
with psycopg.connect(url.set(drivername='postgresql').render_as_string(hide_password=False)) as c:
 assert c.execute('SELECT count(*) FROM support.tickets').fetchone()[0]==0
 assert c.execute('SELECT count(*) FROM commerce.conversations WHERE ticket_id IS NOT NULL').fetchone()[0]==0
 extra=[]
 for name in ['tickets','ticket_events']:
  r=new['files'][name];start=time.monotonic()
  query=psycopg.sql.SQL(r"COPY support.{} ({}) FROM STDIN WITH (FORMAT CSV,NULL '\N')").format(psycopg.sql.Identifier(name),psycopg.sql.SQL(',').join(map(psycopg.sql.Identifier,r['columns'])))
  with c.cursor().copy(query) as cp,Path(r['path']).open('rb') as f:
   for block in iter(lambda:f.read(1024*1024),b''):cp.write(block)
  c.commit();extra.append({'table':'support.'+name,'rows':r['rows'],'seconds_including_count':time.monotonic()-start})
  print(json.dumps(extra[-1]),flush=True)
 n=new['files']['tickets']['rows'];update_start=time.monotonic()
 c.execute("UPDATE commerce.conversations SET ticket_id=lpad(to_hex(1+(id-1)%% %s),32,'0')::uuid",(n,))
 c.commit();update_seconds=time.monotonic()-update_start
 assert c.execute('SELECT count(*) FROM support.tickets WHERE state=%s',('waiting_human',)).fetchone()[0]==new['waiting_human_count']
 assert c.execute('SELECT count(*) FROM support.tickets WHERE synthetic_distribution_note IS DISTINCT FROM %s',(new['intent_proxy_note'],)).fetchone()[0]==0
 c.execute('ANALYZE');c.commit()
 tables=[]
 for name,r in new['files'].items():
  schema='support' if name in {'tickets','ticket_events'} else 'commerce';relation=schema+'.'+name
  actual=c.execute(psycopg.sql.SQL('SELECT count(*) FROM {}.{}').format(psycopg.sql.Identifier(schema),psycopg.sql.Identifier(name))).fetchone()[0]
  assert actual==r['rows']
  size=c.execute('SELECT pg_total_relation_size(%s::regclass)',(relation,)).fetchone()[0]
  if name=='messages':size=c.execute("SELECT sum(pg_total_relation_size(relid))::bigint FROM pg_partition_tree('commerce.messages') WHERE isleaf").fetchone()[0]
  tables.append({'table':relation,'rows':actual,'table_and_indexes_bytes':size})
 total=c.execute('SELECT pg_database_size(current_database())').fetchone()[0]
 report={**prior,'source_manifest_sha256':hashlib.sha256((a.data/'manifest.json').read_bytes()).hexdigest(),
   'tables':tables,'database_bytes':total,'csv_bytes':new['total_csv_bytes'],'tickets_blocked':False,
   'proxy_augmentation_seconds':time.monotonic()-started,'proxy_table_imports':extra,'conversation_link_update_seconds':update_seconds,
   'import_method':'base load plus approved ticket/event COPY and deterministic conversation linkage; unchanged table source SHA256 verified',
   'intent_proxy_note':new['intent_proxy_note'],'waiting_human_ratio':new['waiting_human_ratio'],
   'waiting_human_count':new['waiting_human_count'],'base_import_report_sha256':hashlib.sha256(a.base_import.read_bytes()).hexdigest(),
   'phase_p2_accepted':False}
 report['total_data_import_seconds']=prior.get('import_seconds_measured_copy_and_count',prior.get('import_seconds_including_counts_and_analyze'))+report['proxy_augmentation_seconds']
a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'complete':True,'database_bytes':total,'seconds':report['total_data_import_seconds']}))
