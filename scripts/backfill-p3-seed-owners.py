"""Fill only provenance-marked P2 seed tickets with one unambiguous conversation customer.

No guessing, no P0 connection. Multiple conversations are OK only when every user agrees.
"""
import json
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url

from agent_ops.seed_ownership import SEED_OWNER_SQL, backfill_seed_owners

folder = Path('.local/p3-followup-2026-10-26')
output = folder / 'ownership.json'
if output.exists():
    raise SystemExit('ownership report exists; inspect instead of repeating')
values = dict(l.split('=', 1) for l in Path('.local/stack.env').read_text().splitlines() if '=' in l)
app = dict(l.split('=', 1) for l in Path('.local/p2-application.env').read_text().splitlines() if '=' in l)
url = make_url(app['AGENT_OPS_DATABASE_URL']).set(username='ops_owner', password=values['POSTGRES_PASSWORD'], drivername='postgresql')
assert url.database == 'agent_ops_p2_seed42'
with psycopg.connect(url.render_as_string(hide_password=False)) as conn:
    conn.execute("SET lock_timeout='5s'")
    before = conn.execute('SELECT count(*) FILTER (WHERE customer_user_id IS NULL),count(*) FROM support.tickets').fetchone()
    import time
    start = time.perf_counter()
    updated = backfill_seed_owners(conn)
    conn.commit()
    elapsed = time.perf_counter()-start
    (folder / 'ownership-commit.json').write_text(json.dumps({
        'before_null_and_total': before, 'updated': updated, 'seconds_including_commit': elapsed}))
    after = conn.execute('SELECT count(*) FILTER (WHERE customer_user_id IS NULL),count(*) FROM support.tickets').fetchone()
    mismatches = conn.execute('''WITH owners AS (
        SELECT ticket_id, min(user_id) AS low, max(user_id) AS high
        FROM commerce.conversations WHERE ticket_id IS NOT NULL GROUP BY ticket_id
    ) SELECT count(*) FROM support.tickets t LEFT JOIN owners c USING(ticket_id)
      WHERE t.customer_user_id IS NOT NULL AND (c.ticket_id IS NULL
            OR c.low<>t.customer_user_id OR c.high<>t.customer_user_id)''').fetchone()[0]
    assert before[0]-after[0] == updated and before[1] == after[1] and mismatches == 0
output.write_text(json.dumps({'database': url.database, 'before_null_and_total': before,
    'after_null_and_total': after, 'updated': updated, 'seconds_including_commit': elapsed,
    'ownership_mismatches': mismatches, 'P0_untouched': True, 'rule': SEED_OWNER_SQL}, indent=2)+'\n')
print(json.dumps({'report': str(output), 'updated': updated, 'remaining_null': after[0]}))
