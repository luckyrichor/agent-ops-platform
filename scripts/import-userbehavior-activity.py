"""Replace only activity weights in the existing isolated P2 database, transactionally.

Exact ordered COPY hashes prove all other full tables remain unchanged. No drop/truncate,
no main application switch, no credentials or raw rows in reports.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def fingerprints(conn):
    result = {}
    tables = conn.execute("""SELECT schemaname,tablename FROM pg_tables
        WHERE schemaname IN ('commerce','support')
        AND NOT EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=(quote_ident(schemaname)||'.'||quote_ident(tablename))::regclass)
        ORDER BY schemaname,tablename""").fetchall()
    for schema, table in tables:
        if (schema, table) == ('commerce', 'users'):
            continue
        digest = hashlib.sha256()
        query = sql.SQL('COPY (SELECT * FROM {}.{} t ORDER BY to_jsonb(t)::text COLLATE "C") TO STDOUT WITH (FORMAT CSV)').format(sql.Identifier(schema),sql.Identifier(table))
        # Ordering by primary key avoids sorting large message contents into temporary files.
        keys = conn.execute("""SELECT a.attname FROM pg_index i
            JOIN unnest(i.indkey) WITH ORDINALITY k(attnum,pos) ON true
            JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
            WHERE i.indrelid=%s::regclass AND i.indisprimary ORDER BY k.pos""", (schema+'.'+table,)).fetchall()
        if keys:
            query = sql.SQL('COPY (SELECT * FROM {}.{} ORDER BY {}) TO STDOUT WITH (FORMAT CSV)').format(sql.Identifier(schema),sql.Identifier(table),sql.SQL(',').join(sql.Identifier(k[0]) for k in keys))
        with conn.cursor().copy(query) as stream:
            for block in stream:
                digest.update(block)
        result[schema+'.'+table] = {'rows':conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema),sql.Identifier(table))).fetchone()[0], 'ordered_csv_sha256':digest.hexdigest()}
    return result


p=argparse.ArgumentParser()
p.add_argument('--batch',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
if a.output.exists() or '.local' not in a.output.resolve().parts:
    p.error('fresh private report required')
m=json.loads((a.batch/'manifest.json').read_text())
row=m['files']['users'];source=Path(row['path'])
assert sha(source)==row['sha256'] and m['user_activity_fit_complete']
env=dict(line.split('=',1) for line in Path('.local/p2-application.env').read_text().splitlines() if '=' in line)
url=make_url(env['AGENT_OPS_DATABASE_URL']).set(drivername='postgresql')
if url.database!='agent_ops_p2_seed42':
    p.error('only existing explicitly isolated P2 database is authorized')
started=time.monotonic()
with psycopg.connect(url.render_as_string(hide_password=False)) as c:
    c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
    c.execute("SET timezone='UTC'")
    c.execute('SET statement_timeout=0')
    # Prevent concurrent mutations while proving exact before/after equality, reads remain allowed.
    c.execute('LOCK TABLE commerce.users IN SHARE ROW EXCLUSIVE MODE')
    before=fingerprints(c)
    print(json.dumps({'stage':'before_fingerprints_complete','tables':len(before)}),flush=True)
    old=c.execute('SELECT count(*),count(activity_events) FROM commerce.users').fetchone()
    tick=time.monotonic()
    c.execute('CREATE TEMP TABLE fitted_activity(id bigint PRIMARY KEY,activity_events bigint NOT NULL CHECK(activity_events>=0)) ON COMMIT DROP')
    with c.cursor().copy('COPY fitted_activity FROM STDIN WITH (FORMAT CSV)') as copy,source.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            copy.write(block)
    staged=c.execute('SELECT count(*) FROM fitted_activity').fetchone()[0]
    missing=c.execute('SELECT count(*) FROM fitted_activity f FULL JOIN commerce.users u USING(id) WHERE f.id IS NULL OR u.id IS NULL').fetchone()[0]
    assert staged==old[0]==row['rows'] and missing==0
    changed=c.execute('UPDATE commerce.users u SET activity_events=f.activity_events FROM fitted_activity f WHERE u.id=f.id AND u.activity_events IS DISTINCT FROM f.activity_events').rowcount
    mismatch=c.execute('SELECT count(*) FROM commerce.users u JOIN fitted_activity f USING(id) WHERE u.activity_events IS DISTINCT FROM f.activity_events').fetchone()[0]
    assert mismatch==0
    elapsed=time.monotonic()-tick
    after=fingerprints(c)
    assert before==after,'unaffected database tables changed; transaction must roll back'
    counts={**{k:v['rows'] for k,v in after.items()},'commerce.users':old[0]}
    sizes={k:c.execute('SELECT CASE WHEN (SELECT relkind FROM pg_class WHERE oid=%s::regclass)=\'p\' THEN (SELECT sum(pg_total_relation_size(relid))::bigint FROM pg_partition_tree(%s::regclass) WHERE isleaf) ELSE pg_total_relation_size(%s::regclass) END',(k,k,k)).fetchone()[0] for k in counts}
    c.commit()
    c.execute('ANALYZE commerce.users')
    c.commit()
    db_bytes=c.execute('SELECT pg_database_size(current_database())').fetchone()[0]
report={'complete':True,'database':url.database,'changed_columns':['commerce.users.activity_events'],
    'users':staged,'updated_rows':changed,'old_nonnull_activity_rows':old[1],'id_set_unchanged':True,
    'mismatch_rows':mismatch,'unaffected_tables_before':before,'unaffected_tables_after':after,
    'unaffected_full_table_hashes_identical':True,'counts':counts,'table_and_indexes_bytes':sizes,
    'database_bytes':db_bytes,'activity_copy_update_verify_seconds':elapsed,
    'total_seconds_including_full_hashes_and_analyze':time.monotonic()-started,
    'batch_manifest_sha256':sha(a.batch/'manifest.json'),'source_users_sha256':row['sha256'],
    'main_application_database_unchanged':True,'old_frozen_files_unchanged':True,
    'existing_product_order_data_overwritten':False,'no_new_permanent_table':True}
a.output.parent.mkdir(parents=True,exist_ok=True)
a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'complete':True,'users':staged,'updated_rows':changed,'seconds':report['total_seconds_including_full_hashes_and_analyze']}),flush=True)
