"""Real PG + loopback API + production frontend + Playwright. Private credentials/artifacts only."""
import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

from agent_ops.demo_accounts import Roster, provision

p=argparse.ArgumentParser()
p.add_argument('--output',type=Path,required=True)
p.add_argument('--browser',type=Path)
a=p.parse_args()
if a.output.exists() or '.local' not in a.output.resolve().parts:
    p.error('new private .local output directory required')
a.output.mkdir(parents=True,mode=0o700)
folder=a.output.resolve()
secrets_to_redact=[]

def safe(value):
    for secret in secrets_to_redact:
        value=value.replace(secret,'[REDACTED]')
    return value


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


with PostgresContainer('pgvector/pgvector:pg16',driver='psycopg') as pg:
    owner=pg.get_connection_url();cfg=Config('alembic.ini');cfg.attributes['database_url']=owner
    command.upgrade(cfg,'head')
    runtime_password=secrets.token_urlsafe(24);signing=secrets.token_urlsafe(48)
    secrets_to_redact.extend([runtime_password,signing])
    engine=create_engine(owner)
    with engine.begin() as c:
        # Values are SQL-quoted by psycopg through the DB driver, not shell interpolation.
        from psycopg import sql
        role=sql.SQL('CREATE ROLE p5_runtime LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}').format(sql.Literal(runtime_password)).as_string()
        c.execute(text(role));c.execute(text('GRANT agent_ops_app TO p5_runtime'))
        c.execute(text('INSERT INTO commerce.users(id) VALUES(101),(202),(303)'))
        c.execute(text("INSERT INTO commerce.categories VALUES(1,'日常用品'),(2,'办公用品')"))
        for i,title,category,price in [(101,'办公保温杯',1,12900),(102,'桌面收纳盒',2,8900),(103,'轻便帆布袋',1,5900),(104,'纸质笔记本',2,3900),(105,'桌面阅读灯',2,18900),(106,'旅行收纳包',1,7900)]:
            c.execute(text("""INSERT INTO commerce.products(id,sku,title,category_id,price_cents,freight_cents,status)
                VALUES(:id,:sku,:title,:category,:price,600,'active')"""),{'id':i,'sku':f'P{i}','title':title,'category':category,'price':price})
            c.execute(text('INSERT INTO commerce.inventory(product_id,stock) VALUES(:id,30)'),{'id':i})
        c.execute(text("""INSERT INTO commerce.orders(id,user_id,status,total_cents,created_at)
            VALUES(501,101,'shipped',13500,'2026-10-20'),(502,202,'delivered',9500,'2026-10-21'),(503,303,'delivered',6500,'2026-10-22')"""))
        c.execute(text("INSERT INTO commerce.order_items VALUES(501,1,101,1,12900,600),(502,1,102,1,8900,600),(503,1,103,1,5900,600)"))
        c.execute(text("INSERT INTO commerce.shipments VALUES(501,'演示物流','DEMO-501','shipped'),(502,'演示物流','DEMO-502','delivered')"))
        c.execute(text("SELECT setval(pg_get_serial_sequence('commerce.orders','id'),503)"))
    engine.dispose()
    url=make_url(owner).set(username='p5_runtime',password=runtime_password).render_as_string(hide_password=False)
    roster={'accounts':[{'email':f'{role}{n}@p5.invalid','password':secrets.token_urlsafe(20),'role':role}
                       for role,count in [('customer',3),('agent',2),('admin',1)] for n in range(1,count+1)]}
    for account in roster['accounts']:secrets_to_redact.extend([account['email'],account['password']])
    roster_path=folder/'accounts.json';fd=os.open(roster_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(roster,stream)
    asyncio.run(provision(url,Roster.model_validate(roster),False))
    api_port=free_port();web_port=free_port()
    env={**os.environ,'AGENT_OPS_SUPPORT_BACKEND':'postgres','AGENT_OPS_DATABASE_URL':url,
         'AGENT_OPS_AUTH_SECRET':signing,'AGENT_OPS_PLANNER':'deterministic','AGENT_OPS_TRACE_EXPORTER':'none',
         'P5_API_TARGET':f'http://127.0.0.1:{api_port}','P5_BASE_URL':f'http://127.0.0.1:{web_port}',
         'P5_ARTIFACTS':str(folder),'P5_ROSTER_PATH':str(roster_path),'CI':'1'}
    if a.browser:
        env['P5_BROWSER_PATH']=str(a.browser.resolve())
    env.pop('ARK_API_KEY',None)
    processes=[]
    with (folder/'servers.log').open('w') as log:
        try:
            processes.append(subprocess.Popen([sys.executable,'-m','uvicorn','agent_ops.api:create_app','--factory',
                '--host','127.0.0.1','--port',str(api_port),'--no-access-log','--log-level','warning'],env=env,stdout=log,stderr=log))
            processes.append(subprocess.Popen(['npx','vite','preview','--host','127.0.0.1','--port',str(web_port)],
                cwd='frontend',env=env,stdout=log,stderr=log,start_new_session=True))
            with httpx.Client(trust_env=False,timeout=2) as client:
                for _ in range(200):
                    try:
                        if client.get(f'http://127.0.0.1:{web_port}').status_code==200 and client.get(f'http://127.0.0.1:{api_port}/auth/me').status_code==401:break
                    except httpx.HTTPError:pass
                    time.sleep(.1)
                else:raise RuntimeError('loopback services did not start; inspect private log')
            result=subprocess.run(['npm','run','test:e2e'],cwd='frontend',env=env,capture_output=True,text=True,check=False)
            (folder/'playwright.log').write_text(safe(result.stdout+result.stderr))
            report=folder/'playwright.json'
            if report.exists():report.write_text(safe(report.read_text()))
            metadata={'frontend_url':env['P5_BASE_URL'],'api_loopback_port':api_port,'playwright_exit_code':result.returncode,
                      'P0_P2_touched':False,'browser':subprocess.check_output([str(a.browser),'--version'],text=True).strip() if a.browser else 'Playwright bundled Chromium','credentials_mode':oct(roster_path.stat().st_mode&0o777),
                      'screenshots':sorted(str(path) for path in (folder/'screenshots').glob('*.png')),
                      'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'dirty':True}
            (folder/'result.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps({'result':str(folder/'result.json'),'exit_code':result.returncode,'screenshots':len(metadata['screenshots'])}))
            if result.returncode:raise SystemExit(result.returncode)
        finally:
            import signal
            for process in processes:
                if process.poll() is None:
                    if process is processes[-1] and len(processes)>1:os.killpg(process.pid,signal.SIGTERM)
                    else:process.terminate()
            for process in processes:
                try:process.wait(10)
                except subprocess.TimeoutExpired:process.kill();process.wait()
    assert not any(secret in (folder/'servers.log').read_text() for secret in secrets_to_redact)
