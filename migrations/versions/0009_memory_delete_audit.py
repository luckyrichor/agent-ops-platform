"""Append-only platform audit surviving customer memory tenant deletion."""
from alembic import op

revision = '0009_memory_delete_audit'
down_revision = '0008_catalog_search'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE customer_memory.deletion_audit(
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      request_id uuid NOT NULL, user_id bigint NOT NULL REFERENCES commerce.users(id),
      outcome text NOT NULL CHECK(outcome IN ('started','completed','failed')),
      created_at timestamptz NOT NULL DEFAULT now());
      GRANT INSERT ON customer_memory.deletion_audit TO agent_ops_app;
      GRANT USAGE ON SEQUENCE customer_memory.deletion_audit_id_seq TO agent_ops_app;
    """)


def downgrade():
    op.execute('DROP TABLE customer_memory.deletion_audit')
