"""Authorized P3 maintenance on existing P0/P2 only. No credentials in reports/output.

Blocks new connections during migration, refuses existing clients, restores admission in finally.
P0 pg_dump is created and inspected before any migration. P2 round-trip precedes demo accounts.
"""
import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url

ROOT = Path('.local/p3-followup-2026-10-26')
ROOT.mkdir(mode=0o700, exist_ok=True)
REPORT = ROOT / 'databases.json'

values = dict(l.split('=', 1) for l in Path('.local/stack.env').read_text().splitlines() if '=' in l)
app = dict(l.split('=', 1) for l in Path('.local/application.env').read_text().splitlines() if '=' in l)
owner = make_url(app['AGENT_OPS_DATABASE_URL']).set(username='ops_owner', password=values['POSTGRES_PASSWORD'])
report = {'started_at': datetime.now(UTC).isoformat(), 'source_head': subprocess.check_output(
    ['git', 'rev-parse', 'HEAD'], text=True).strip(), 'dirty': True, 'databases': {}}

def save():
    REPORT.write_text(json.dumps(report, indent=2) + '\n')

if REPORT.exists():
    report = json.loads(REPORT.read_text())
    if report.get('complete'):
        raise SystemExit('already complete; refusing to repeat')

def counts(conn):
    return {table: conn.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(
        sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]
        if conn.execute('SELECT to_regclass(%s)', (schema + '.' + table,)).fetchone()[0] else None
        for schema, table in [('commerce', 'users'), ('commerce', 'orders'),
                              ('commerce', 'conversations'), ('support', 'tickets'),
                              ('support', 'ticket_events')]}

with psycopg.connect(owner.set(database='postgres', drivername='postgresql').render_as_string(
        hide_password=False), autocommit=True) as admin:
    for database in ['agent_ops_p2_seed42', 'agent_ops']:
        if report['databases'].get(database, {}).get('complete'):
            continue
        r = report['databases'][database] = {'ddl': []}
        # Do not terminate anybody else's process, including an idle client.
        sessions = admin.execute('SELECT pid,state,application_name FROM pg_stat_activity WHERE datname=%s',
                                 (database,)).fetchall()
        r['preflight_sessions'] = sessions
        save()
        if sessions:
            raise SystemExit('database is in use; no migration performed: ' + database)
        engine = create_engine(owner.set(database=database).render_as_string(hide_password=False))
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            pid = raw.info.backend_pid
            # Close the race between preflight and maintenance: disable new connections,
            # then recheck all clients except this already-established migration connection.
            admin.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS false').format(sql.Identifier(database)))
            try:
                others = admin.execute('SELECT pid,state,application_name FROM pg_stat_activity '
                                       'WHERE datname=%s AND pid<>%s', (database, pid)).fetchall()
                r['sessions_after_admission_closed'] = others
                if others:
                    raise SystemExit('new client detected before maintenance: ' + database)
                before = counts(raw)
                version = raw.execute('SELECT version_num FROM alembic_version').fetchone()[0]
                raw.rollback()
                r['starting_version'] = version
                if version != ('0003_proxy_provenance' if database.endswith('seed42') else '0001_support'):
                    raise SystemExit('unexpected migration version: ' + version)
                r['before_counts'] = before
                if database == 'agent_ops':
                    # pg_dump must connect, so briefly admit only for backup. Check again afterwards.
                    admin.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS true').format(sql.Identifier(database)))
                    folder = Path('.local/backups'); folder.mkdir(mode=0o700, exist_ok=True)
                    backup = folder / ('agent_ops-pre-0004-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '.dump')
                    started = time.perf_counter()
                    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        subprocess.run(['docker', 'exec', 'agent-ops-tracing-postgres-1',
                                        'pg_dump', '-U', 'ops_owner', '-d', database, '-Fc'],
                                       stdout=stream, stderr=subprocess.PIPE, check=True)
                    with backup.open('rb') as stream:
                        inspected = subprocess.run(['docker', 'exec', '-i', 'agent-ops-tracing-postgres-1',
                                                    'pg_restore', '--list'], stdin=stream,
                                                   capture_output=True, check=True)
                    r['backup'] = {'path': str(backup.resolve()), 'bytes': backup.stat().st_size,
                                   'mode': oct(backup.stat().st_mode & 0o777),
                                   'seconds': time.perf_counter() - started,
                                   'sha256': hashlib.sha256(backup.read_bytes()).hexdigest(),
                                   'archive_list_verified': b'support' in inspected.stdout}
                    save()
                    admin.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS false').format(sql.Identifier(database)))
                    assert not admin.execute('SELECT pid FROM pg_stat_activity WHERE datname=%s AND pid<>%s',
                                             (database, pid)).fetchall(), 'database became busy during backup'
                @event.listens_for(connection, 'before_cursor_execute')
                def before_sql(conn, cursor, statement, parameters, context, executemany):
                    context._p3_start = time.perf_counter()
                @event.listens_for(connection, 'after_cursor_execute')
                def after_sql(conn, cursor, statement, parameters, context, executemany, record=r):
                    if any(word in statement for word in ('ADD COLUMN customer_user_id', 'CREATE INDEX',
                            'CREATE UNIQUE INDEX', 'DROP INDEX', 'DROP COLUMN customer_user_id')):
                        # SQL here is static DDL; no bind parameters or credential SQL included.
                        record['ddl'].append({'phase': record['phase'], 'sql': statement.strip(),
                                         'seconds': time.perf_counter() - context._p3_start})
                cfg = Config('alembic.ini'); cfg.attributes.update(connection=connection, database_url=str(owner))
                phases = ['upgrade', 'downgrade', 'upgrade_again'] if database.endswith('seed42') else ['upgrade']
                for phase in phases:
                    r['phase'] = phase
                    started = time.perf_counter()
                    if phase == 'downgrade':
                        command.downgrade(cfg, '0003_proxy_provenance')
                    else:
                        command.upgrade(cfg, '0004_auth')
                    connection.commit()
                    r[phase] = {'seconds': time.perf_counter() - started,
                                'version': raw.execute('SELECT version_num FROM alembic_version').fetchone()[0]}
                    if phase == 'downgrade':
                        assert raw.execute("SELECT to_regnamespace('auth')").fetchone()[0] is None
                        assert raw.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='support' "
                                           "AND table_name='tickets' AND column_name='customer_user_id'").fetchone()[0] == 0
                        assert raw.execute("SELECT to_regclass('commerce.conversations_user')").fetchone()[0] is None
                        assert raw.execute("SELECT to_regclass('support.tickets_customer_created')").fetchone()[0] is None
                    after = counts(raw)
                    assert all(after[k] == v for k, v in before.items() if v is not None)
                    connection.rollback()
                    save()
                r['after_counts'] = counts(raw)
                connection.rollback()
                r['complete'] = True
            finally:
                admin.execute(sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS true').format(sql.Identifier(database)))
                r['connections_restored'] = True
                save()
        engine.dispose()
report['complete'] = True
save()
print(json.dumps({'report': str(REPORT), 'complete': True}))
