"""Run the unchanged B0 harness with the P1 PostgreSQL backend and isolated databases."""
import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

p=argparse.ArgumentParser()
p.add_argument('--output',type=Path,required=True)
a=p.parse_args()
if a.output.exists():p.error('new output required; do not overwrite frozen baselines')
spec=importlib.util.spec_from_file_location('b0_harness',Path('scripts/benchmark-b0.py'))
b0=importlib.util.module_from_spec(spec)
spec.loader.exec_module(b0)
original=b0.iteration
cpus=sorted(os.sched_getaffinity(0))
# B0 gives all non-client cores to API. Preserve that placement and disclose DB contention.
with PostgresContainer('pgvector/pgvector:pg16',driver='psycopg') as pg:
    pg.get_wrapped_container().update(cpuset_cpus=','.join(map(str,cpus[1:])))
    owner=make_url(pg.get_connection_url())
    root_dsn=owner.set(drivername='postgresql').render_as_string(hide_password=False)
    with psycopg.connect(root_dsn,autocommit=True) as conn:
        conn.execute("CREATE ROLE b1_runtime LOGIN PASSWORD 'isolated-fixture-only' NOSUPERUSER NOBYPASSRLS")
    async def iteration(args,repeat,server_cpus):
        database=f'b1_repeat_{repeat}'
        with psycopg.connect(root_dsn,autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL('CREATE DATABASE {}').format(psycopg.sql.Identifier(database)))
        url=owner.set(database=database)
        config=Config('alembic.ini');config.attributes['database_url']=url.render_as_string(hide_password=False)
        command.upgrade(config,'head')
        with psycopg.connect(root_dsn,autocommit=True) as conn:
            conn.execute('GRANT agent_ops_app TO b1_runtime')
        os.environ['AGENT_OPS_SUPPORT_BACKEND']='postgres'
        os.environ['AGENT_OPS_DATABASE_URL']=url.set(username='b1_runtime',password='isolated-fixture-only').render_as_string(hide_password=False)
        return await original(args,repeat,server_cpus)
    b0.iteration=iteration
    sys.argv=['benchmark-b0.py','--output',str(a.output),'--repeats','5','--requests','128','--concurrency','8','--warmup','10']
    asyncio.run(b0.main())
    metadata=json.loads((a.output/'environment.json').read_text())
    metadata['original_harness_mode_literal']=metadata['mode']
    metadata['mode']='deterministic / local tools / memory integration disabled / PostgreSQL support / one worker / trace ratio1 exporter none'
    metadata['postgres']={'image':'pgvector/pgvector:pg16','version':pg.exec('postgres --version')[1].decode().strip(),
        'cpus':cpus[1:],'fresh_database_each_repeat':True,'runtime_schema_owner':False,
        'api_db_cpu_overlap':True,'note':'unchanged B0 API placement; database shares non-client cores; DB CPU not included in server_cpu_seconds'}
    metadata['wrapper_sha256']=b0.digest(Path(__file__))
    metadata['baseline_difference']='B0 in-memory tickets evict at256; PostgreSQL has no such capacity cap. 128 measured creates + one warmup key per fresh database stays below256.'
    (a.output/'environment.json').write_text(json.dumps(metadata,indent=2))
