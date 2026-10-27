"""P3 accounts, refresh-token rotation and ticket ownership. No P4 queue/claim/agent tables."""

from alembic import op

revision = "0004_auth"
down_revision = "0003_proxy_provenance"
branch_labels = None
depends_on = None


def upgrade():
    statements = """
    CREATE SCHEMA auth;
    -- Credentials live apart from the 1M commerce.users placeholders: only a customer account
    -- explicitly linked to one placeholder can log in as that user.
    CREATE TABLE auth.accounts (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      email text NOT NULL CHECK(email = lower(email) AND length(email) BETWEEN 3 AND 254),
      password_hash text NOT NULL CHECK(password_hash LIKE '$argon2id$%'),
      role text NOT NULL CHECK(role IN ('customer','agent','admin')),
      customer_user_id bigint UNIQUE REFERENCES commerce.users(id),
      status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CHECK((role = 'customer') = (customer_user_id IS NOT NULL))
    );
    CREATE UNIQUE INDEX accounts_email ON auth.accounts(email);
    -- Only SHA-256 digests of opaque refresh tokens are stored, family = one login session.
    CREATE TABLE auth.refresh_tokens (
      token_hash char(64) PRIMARY KEY,
      family_id uuid NOT NULL,
      account_id bigint NOT NULL REFERENCES auth.accounts(id),
      issued_at timestamptz NOT NULL,
      expires_at timestamptz NOT NULL,
      family_expires_at timestamptz NOT NULL,
      used_at timestamptz,
      revoked_at timestamptz,
      CHECK(expires_at > issued_at AND family_expires_at >= expires_at)
    );
    CREATE INDEX refresh_tokens_family ON auth.refresh_tokens(family_id);
    CREATE INDEX refresh_tokens_account ON auth.refresh_tokens(account_id);
    GRANT USAGE ON SCHEMA auth TO agent_ops_app;
    GRANT SELECT,INSERT,UPDATE ON auth.accounts,auth.refresh_tokens TO agent_ops_app;

    -- Existing P1/P2 tickets keep NULL owner: only agents/admins can read them.
    ALTER TABLE support.tickets ADD COLUMN customer_user_id bigint REFERENCES commerce.users(id);
    CREATE INDEX tickets_customer_created ON support.tickets(customer_user_id,created_at DESC,ticket_id DESC)
      WHERE customer_user_id IS NOT NULL;
    CREATE INDEX conversations_user ON commerce.conversations(user_id,id);
    """
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    statements = """
    DROP INDEX commerce.conversations_user;
    DROP INDEX support.tickets_customer_created;
    ALTER TABLE support.tickets DROP COLUMN customer_user_id;
    DROP SCHEMA auth CASCADE;
    """
    for statement in statements.split(";"):
        if statement.strip():
            op.execute(statement)
