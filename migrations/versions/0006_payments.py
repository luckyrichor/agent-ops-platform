"""P6 transactional payments, notifications, refunds and restricted audit."""
from alembic import op

revision = '0006_payments'
down_revision = '0005_workbench'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''
    CREATE SCHEMA payments;
    CREATE TABLE payments.payments (
      order_id bigint PRIMARY KEY REFERENCES commerce.orders(id),
      merchant_no text NOT NULL UNIQUE,
      amount_cents bigint NOT NULL CHECK(amount_cents>0),
      state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','paid','closed','refunded')),
      provider_trade text UNIQUE,
      created_at timestamptz NOT NULL DEFAULT now(),paid_at timestamptz,
      next_query_at timestamptz NOT NULL DEFAULT now()+interval '2 minutes');
    CREATE TABLE payments.notifications (
      notification_id text PRIMARY KEY, payload_hash char(64) NOT NULL,
      merchant_no text NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
    CREATE TABLE payments.refunds (
      refund_no text PRIMARY KEY, order_id bigint NOT NULL UNIQUE REFERENCES payments.payments(order_id),
      ticket_id uuid NOT NULL UNIQUE REFERENCES support.tickets(ticket_id),
      amount_cents bigint NOT NULL CHECK(amount_cents>0),
      state text NOT NULL CHECK(state IN ('pending','unknown','succeeded')),
      approved_by bigint NOT NULL REFERENCES auth.accounts(id),
      created_at timestamptz NOT NULL DEFAULT now(),succeeded_at timestamptz,
      next_query_at timestamptz NOT NULL DEFAULT now());
    CREATE TABLE payments.audit (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,order_id bigint,
      code text NOT NULL, actor text NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
    GRANT USAGE ON SCHEMA payments TO agent_ops_app;
    GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA payments TO agent_ops_app;
    GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA payments TO agent_ops_app;
    ''')


def downgrade():
    op.execute('''DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM payments.payments WHERE state IN ('paid','refunded'))
      OR EXISTS(SELECT 1 FROM payments.refunds) THEN
        RAISE EXCEPTION 'payment financial records require explicit archival before downgrade';
      END IF;
    END $$; DROP SCHEMA payments CASCADE;''')
