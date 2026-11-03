#!/usr/bin/env python3
"""清理 S2 压测产生的工单与幂等记录（仅 s2-http- 前缀）。"""
import os, psycopg
from pathlib import Path
url=os.environ.get("S1_DATABASE_URL","").replace("postgresql+psycopg://","postgresql://")
if not url:
    raise SystemExit("S1_DATABASE_URL required")
with psycopg.connect(url, autocommit=True) as c, c.cursor() as cur:
    cur.execute("""CREATE TEMP TABLE s2_ids AS SELECT ticket_id FROM support.idempotency_keys
                   WHERE key LIKE 's2-http-%' AND ticket_id IS NOT NULL""")
    cur.execute("DELETE FROM support.messages WHERE ticket_id IN (SELECT ticket_id FROM s2_ids)")
    cur.execute("DELETE FROM support.ticket_events WHERE ticket_id IN (SELECT ticket_id FROM s2_ids)")
    cur.execute("DELETE FROM support.tickets WHERE ticket_id IN (SELECT ticket_id FROM s2_ids)")
    cur.execute("DELETE FROM support.idempotency_keys WHERE key LIKE 's2-http-%'")
    cur.execute("VACUUM (FULL, ANALYZE) support.tickets")
print("reset ok")
