"""P4 support workflow, messages and durable ordered push; no S1 queue indexes."""

from alembic import op

revision = "0005_workbench"
down_revision = "0004_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE support.tickets DROP CONSTRAINT tickets_state_check;
    ALTER TABLE support.tickets ADD CHECK(state IN ('needs_information','resolved','waiting_human','claimed'));
    ALTER TABLE support.tickets ADD COLUMN order_id bigint REFERENCES commerce.orders(id),
      ADD COLUMN assigned_account_id bigint REFERENCES auth.accounts(id),
      ADD COLUMN lease_expires_at timestamptz,
      ADD COLUMN priority integer NOT NULL DEFAULT 1 CHECK(priority BETWEEN 0 AND 2),
      ADD COLUMN sla_due_at timestamptz NOT NULL DEFAULT (now()+interval '24 hours'),
      ADD COLUMN sla_warned boolean NOT NULL DEFAULT false,
      ADD COLUMN sla_overdue boolean NOT NULL DEFAULT false;
    ALTER TABLE support.tickets ADD CONSTRAINT claim_lease_check CHECK(
      (state='claimed' AND assigned_account_id IS NOT NULL AND lease_expires_at IS NOT NULL)
      OR (state<>'claimed' AND assigned_account_id IS NULL AND lease_expires_at IS NULL));
    CREATE TABLE support.messages (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      ticket_id uuid NOT NULL REFERENCES support.tickets(ticket_id),
      sender_role text NOT NULL CHECK(sender_role IN ('customer','agent','system')),
      actor text NOT NULL,content text NOT NULL CHECK(length(content) BETWEEN 1 AND 2000),
      created_at timestamptz NOT NULL DEFAULT now());
    CREATE TABLE support.operations (
      scope text NOT NULL,key text NOT NULL,request_hash char(64) NOT NULL,response jsonb,
      PRIMARY KEY(scope,key));
    CREATE TABLE support.push_clock (singleton boolean PRIMARY KEY CHECK(singleton),value bigint NOT NULL);
    INSERT INTO support.push_clock VALUES(true,0);
    CREATE TABLE support.push_events (
      sequence bigint PRIMARY KEY,ticket_id uuid NOT NULL REFERENCES support.tickets(ticket_id),
      kind text NOT NULL,staff boolean NOT NULL,customer_user_id bigint,
      payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
    CREATE TRIGGER messages_append_only BEFORE UPDATE OR DELETE ON support.messages
      FOR EACH ROW EXECUTE FUNCTION support.reject_event_mutation();
    CREATE TRIGGER push_append_only BEFORE UPDATE OR DELETE ON support.push_events
      FOR EACH ROW EXECUTE FUNCTION support.reject_event_mutation();
    GRANT SELECT,INSERT ON support.messages,support.push_events TO agent_ops_app;
    GRANT SELECT,INSERT,UPDATE ON support.operations,support.push_clock TO agent_ops_app;
    GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA support TO agent_ops_app;
    """)


def downgrade():
    # Non-destructive: refuse downgrade with claimed work instead of silently rewriting history.
    op.execute("""
    DO $$ BEGIN IF EXISTS(SELECT 1 FROM support.tickets WHERE state='claimed') THEN
      RAISE EXCEPTION 'release claimed tickets before downgrade'; END IF; END $$;
    DROP TABLE support.push_events,support.push_clock,support.operations,support.messages;
    ALTER TABLE support.tickets DROP CONSTRAINT claim_lease_check,
      DROP COLUMN order_id,DROP COLUMN assigned_account_id,DROP COLUMN lease_expires_at,
      DROP COLUMN priority,DROP COLUMN sla_due_at,DROP COLUMN sla_warned,DROP COLUMN sla_overdue;
    ALTER TABLE support.tickets DROP CONSTRAINT tickets_state_check;
    ALTER TABLE support.tickets ADD CHECK(state IN ('needs_information','resolved','waiting_human'));
    """)
