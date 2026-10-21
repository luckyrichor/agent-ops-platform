"""Bulk import into a new isolated database on the existing P0 PostgreSQL instance.

Never truncate a business database. Raw rows/credentials are not exported.
"""
import argparse,hashlib,json,time
from pathlib import Path
import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True)
p.add_argument('--database',default='agent_ops_p2_seed42');p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
if not a.database.startswith('agent_ops_p2_') or not a.database.replace('_','').isalnum():p.error('dedicated new P2 database name required')
if a.output.exists() or '.local' not in a.output.resolve().parts:p.error('new private report required')
manifest=json.loads((a.data/'manifest.json').read_text())
for r in manifest['files'].values():
 path=Path(r['path']);h=hashlib.sha256()
 with path.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 if h.hexdigest()!=r['sha256']:raise ValueError('generated data hash mismatch')
secret=dict(l.split('=',1) for l in Path('.local/stack.env').read_text().splitlines() if '=' in l)
app=dict(l.split('=',1) for l in Path('.local/application.env').read_text().splitlines() if '=' in l)
appurl=make_url(app['AGENT_OPS_DATABASE_URL']);owner=appurl.set(username='ops_owner',password=secret['POSTGRES_PASSWORD'],database='postgres',drivername='postgresql')
with psycopg.connect(owner.render_as_string(hide_password=False),autocommit=True) as c:
 if c.execute('SELECT 1 FROM pg_database WHERE datname=%s',(a.database,)).fetchone():raise ValueError('database exists; never drop or overwrite frozen data')
 c.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(a.database)))
url=owner.set(database=a.database,drivername='postgresql+psycopg')
cfg=Config('alembic.ini');cfg.attributes['database_url']=url.render_as_string(hide_password=False)
command.upgrade(cfg,'head')
started=time.monotonic();tables=[]
order=['users','categories','products','inventory','orders','order_items','shipments','tickets','ticket_events','conversations','message_keys','messages']
with psycopg.connect(url.set(drivername='postgresql').render_as_string(hide_password=False)) as c:
 c.execute("SET timezone='UTC'")
 c.execute('SET statement_timeout=0')
 for table in order:
  if table not in manifest['files']:continue
  schema='support' if table in {'tickets','ticket_events'} else 'commerce'
  record=manifest['files'][table];before=time.monotonic()
  query=psycopg.sql.SQL(r"COPY {}.{} ({}) FROM STDIN WITH (FORMAT CSV, NULL '\N')").format(
    psycopg.sql.Identifier(schema),psycopg.sql.Identifier(table),
    psycopg.sql.SQL(',').join(map(psycopg.sql.Identifier,record['columns'])))
  with c.cursor().copy(query) as cp,Path(record['path']).open('rb') as stream:
   for block in iter(lambda:stream.read(1024*1024),b''):cp.write(block)
  count=c.execute(psycopg.sql.SQL('SELECT count(*) FROM {}.{}').format(psycopg.sql.Identifier(schema),psycopg.sql.Identifier(table))).fetchone()[0]
  assert count==record['rows']
  c.commit()
  tables.append({'table':schema+'.'+table,'rows':count,'seconds_including_count':time.monotonic()-before})
  a.output.parent.mkdir(parents=True,exist_ok=True)
  a.output.write_text(json.dumps({'complete':False,'tables':tables},indent=2))
  print(json.dumps(tables[-1]),flush=True)
 c.execute("SELECT setval(pg_get_serial_sequence('commerce.orders','id'),(SELECT max(id) FROM commerce.orders))")
 c.execute('ANALYZE')
 metrics=[]
 for row in tables:
  relation=row['table'];size=c.execute('SELECT pg_total_relation_size(%s::regclass)',(relation,)).fetchone()[0]
  # Partitioned parent reports no physical bytes; sum children for the message relation.
  if relation=='commerce.messages':
   size=c.execute("SELECT sum(pg_total_relation_size(relid))::bigint FROM pg_partition_tree('commerce.messages') WHERE isleaf").fetchone()[0]
  metrics.append({**row,'table_and_indexes_bytes':size})
 total=c.execute('SELECT pg_database_size(current_database())').fetchone()[0]
 report={'complete':True,'database':a.database,'source_manifest_sha256':hashlib.sha256((a.data/'manifest.json').read_bytes()).hexdigest(),
   'tables':metrics,'import_seconds_including_counts_and_analyze':time.monotonic()-started,
   'database_bytes':total,'csv_bytes':manifest['total_csv_bytes'],'new_database_same_p0_instance':True,
   'existing_business_database_untouched':True,'main_business_api_switched':False,
   'user_activity_fit_complete':manifest['user_activity_fit_complete'],
   'tickets_blocked':manifest['ticket_target_blocked'],'phase_p2_accepted':False}
 c.commit()
a.output.write_text(json.dumps(report,indent=2)+'\n')
private_env=Path('.local/p2-application.env')
private_env.write_text('AGENT_OPS_DATABASE_URL='+appurl.set(database=a.database).render_as_string(hide_password=False)+'\n')
private_env.chmod(0o600)
print(json.dumps({'complete':True,'database_bytes':total,'seconds':report['import_seconds_including_counts_and_analyze']}))
