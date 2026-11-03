"""Idempotency TTL for generic request keys (tickets and orders).

Payment/refund/notification dedupe keeps business uniqueness and no TTL.
"""

from alembic import op

revision = "0010_idempotency_ttl"
down_revision = "0009_memory_delete_audit"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE support.idempotency_keys
      ADD COLUMN expires_at timestamptz NOT NULL DEFAULT (now()+interval '24 hours');
    ALTER TABLE commerce.idempotency_keys
      ADD COLUMN expires_at timestamptz NOT NULL DEFAULT (now()+interval '24 hours');
    CREATE INDEX idempotency_keys_expiry ON support.idempotency_keys(expires_at);
    CREATE INDEX commerce_idempotency_keys_expiry ON commerce.idempotency_keys(expires_at);
    """)


def downgrade():
    op.execute("""
    DROP INDEX commerce.commerce_idempotency_keys_expiry;
    DROP INDEX support.idempotency_keys_expiry;
    ALTER TABLE commerce.idempotency_keys DROP COLUMN expires_at;
    ALTER TABLE support.idempotency_keys DROP COLUMN expires_at;
    """)
