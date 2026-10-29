"""Lazy customer memory identities and durable write idempotency."""
from alembic import op

revision = '0007_customer_memory'
down_revision = '0006_payments'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE SCHEMA customer_memory;
    CREATE TABLE customer_memory.identities(
      user_id bigint PRIMARY KEY REFERENCES commerce.users(id),
      tenant_id uuid UNIQUE, memory_user_id uuid UNIQUE,
      state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','deleting')),
      generation bigint NOT NULL DEFAULT 1,
      CHECK((tenant_id IS NULL) = (memory_user_id IS NULL)));
    CREATE TABLE customer_memory.operations(
      user_id bigint NOT NULL REFERENCES commerce.users(id),
      run_id uuid NOT NULL, request_hash text NOT NULL,
      memory_content text, result jsonb, created_at timestamptz NOT NULL DEFAULT now(),
      state text NOT NULL CHECK(state IN ('pending','succeeded','purged')),
      PRIMARY KEY(user_id,run_id));
    GRANT USAGE ON SCHEMA customer_memory TO agent_ops_app;
    GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA customer_memory TO agent_ops_app;''')


def downgrade():
    op.execute('''DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM customer_memory.identities) THEN
        RAISE EXCEPTION 'archive customer memory identities before downgrade';
      END IF;
    END $$; DROP SCHEMA customer_memory CASCADE;''')
