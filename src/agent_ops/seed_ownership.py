"""Explicit P2 maintenance rule; never guesses ownership from order IDs or ticket text."""

import psycopg

SEED_OWNER_SQL = """WITH certain AS (
    SELECT ticket_id, min(user_id) AS user_id FROM commerce.conversations
    WHERE ticket_id IS NOT NULL GROUP BY ticket_id HAVING count(DISTINCT user_id)=1
) UPDATE support.tickets t SET customer_user_id=c.user_id FROM certain c
  WHERE t.ticket_id=c.ticket_id AND t.customer_user_id IS NULL
    AND t.synthetic_distribution_note IS NOT NULL"""


def backfill_seed_owners(connection: psycopg.Connection[tuple[object, ...]]) -> int:
    """Caller selects P2 and owns the transaction. Ambiguous/unlinked/unmarked rows stay NULL."""
    return connection.execute(SEED_OWNER_SQL).rowcount
