"""Read-only recover a completed load whose JSON summary failed, never invent lost wall time."""
import argparse,hashlib,json
from pathlib import Path
import psycopg
from sqlalchemy.engine import make_url
p=argparse.ArgumentParser();p.add_argument('--data',type=Path,required=True);p.add_argument('--progress',type=Path,required=True)
p.add_argument('--database',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
prior=json.loads(a.progress.read_text());m=json.loads((a.data/'manifest.json').read_text())
assert not prior['complete'] and len(prior['tables'])==len(m['files'])
assert a.database.startswith('agent_ops_p2_') and not a.output.exists()
secret=dict(l.split('=',1) for l in Path('.local/stack.env').read_text().splitlines() if '=' in l)
app=dict(l.split('=',1) for l in Path('.local/application.env').read_text().splitlines() if '=' in l)
appurl=make_url(app['AGENT_OPS_DATABASE_URL']).set(database=a.database)
u=appurl.set(username='ops_owner',password=secret['POSTGRES_PASSWORD'],drivername='postgresql')
with psycopg.connect(u.render_as_string(hide_password=False)) as c:
 c.execute('SET TRANSACTION READ ONLY');tables=[]
 for row in prior['tables']:
  schema,name=row['table'].split('.')
  count=c.execute(psycopg.sql.SQL('SELECT count(*) FROM {}.{}').format(psycopg.sql.Identifier(schema),psycopg.sql.Identifier(name))).fetchone()[0]
  assert count==row['rows']==m['files'][name]['rows']
  size=c.execute('SELECT pg_total_relation_size(%s::regclass)',(row['table'],)).fetchone()[0]
  if name=='messages':size=c.execute("SELECT sum(pg_total_relation_size(relid))::bigint FROM pg_partition_tree('commerce.messages') WHERE isleaf").fetchone()[0]
  tables.append({**row,'table_and_indexes_bytes':size})
 total=c.execute('SELECT pg_database_size(current_database())').fetchone()[0]
r={'complete':True,'database':a.database,'tables':tables,'database_bytes':total,
 'source_manifest_sha256':hashlib.sha256((a.data/'manifest.json').read_bytes()).hexdigest(),
 'import_seconds_measured_copy_and_count':sum(row['seconds_including_count'] for row in tables),
 'original_total_wall_time_including_analyze':None,'analyze_elapsed_recorded':False,
 'timing_definition':'sum of observed per-table COPY+count stages; initial ANALYZE/summary wall time not preserved and not invented',
 'csv_bytes':m['total_csv_bytes'],'new_database_same_p0_instance':True,'existing_business_database_untouched':True,
 'main_business_api_switched':False,'user_activity_fit_complete':False,'tickets_blocked':True,'phase_p2_accepted':False,
 'json_decimal_failure_recovered_read_only':True}
a.output.write_text(json.dumps(r,indent=2)+'\n')
env=Path('.local/p2-application.env');env.write_text('AGENT_OPS_DATABASE_URL='+appurl.render_as_string(hide_password=False)+'\n');env.chmod(0o600)
print(json.dumps({'complete':True,'database_bytes':total,'observed_import_seconds':r['import_seconds_measured_copy_and_count']}))
