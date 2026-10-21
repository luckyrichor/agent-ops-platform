"""P1 support persistence, no P2 commerce or P3 accounts yet."""

from alembic import op

revision = "0001_support"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE SCHEMA support;
    CREATE TABLE support.tickets (
      ticket_id uuid PRIMARY KEY,
      route text NOT NULL CHECK(route IN ('order_status','return_request','human','clarify')),
      state text NOT NULL CHECK(state IN ('resolved','waiting_human','needs_information')),
      revision integer NOT NULL CHECK(revision>0),
      reason_code text NOT NULL, order_status text,
      history jsonb NOT NULL CHECK(jsonb_typeof(history)='array'),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE support.idempotency_keys (
      scope text NOT NULL, key text NOT NULL CHECK(length(key) BETWEEN 1 AND 128),
      request_hash char(64) NOT NULL,
      ticket_id uuid REFERENCES support.tickets(ticket_id) DEFERRABLE INITIALLY DEFERRED,
      response_snapshot jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(scope,key)
    );
    CREATE TABLE support.ticket_events (
      ticket_id uuid NOT NULL REFERENCES support.tickets(ticket_id),
      revision integer NOT NULL CHECK(revision>0),
      type text NOT NULL, actor text NOT NULL,
      payload jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(ticket_id,revision)
    );
    CREATE FUNCTION support.reject_event_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'ticket_events are append only'; END $$;
    CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON support.ticket_events
      FOR EACH ROW EXECUTE FUNCTION support.reject_event_mutation();
    DO $$ BEGIN
      IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='agent_ops_app') THEN
        CREATE ROLE agent_ops_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
      END IF;
    END $$;
    GRANT USAGE ON SCHEMA support TO agent_ops_app;
    GRANT SELECT,INSERT,UPDATE ON support.tickets,support.idempotency_keys TO agent_ops_app;
    GRANT SELECT,INSERT ON support.ticket_events TO agent_ops_app;
    """)


def downgrade():
    op.execute("DROP SCHEMA support CASCADE")
