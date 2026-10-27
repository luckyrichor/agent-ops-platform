"""P10-only intentionally unsafe SELECT-then-UPDATE baseline. Never imported by product code.

Not executed in P4. Requires explicit flag AND isolated agent_ops_s1_* database.
No production routing, no environment flag that could switch a product claim to this code.
"""

import argparse
import os

import psycopg
from sqlalchemy.engine import make_url

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--enable-unsafe-s1-baseline", action="store_true", required=True)
p.add_argument("--ticket-id", required=True)
p.add_argument("--agent-id", required=True, type=int)
a = p.parse_args()
u = make_url(os.environ["AGENT_OPS_S1_DATABASE_URL"])
if not u.database or not u.database.startswith("agent_ops_s1_"):
    p.error("only isolated agent_ops_s1_* databases are permitted")
with psycopg.connect(u.set(drivername="postgresql").render_as_string(hide_password=False)) as c:
    # Deliberately no locking/CAS: two seats may both read waiting_human before either writes.
    row = c.execute(
        "SELECT state,revision FROM support.tickets WHERE ticket_id=%s", (a.ticket_id,)
    ).fetchone()
    if row and row[0] == "waiting_human":
        c.execute(
            """UPDATE support.tickets SET state='claimed',assigned_account_id=%s,
            lease_expires_at=now()+interval '10 minutes',revision=revision+1 WHERE ticket_id=%s""",
            (a.agent_id, a.ticket_id),
        )
        print("experimental update completed; this is not a product claim or correctness result")
