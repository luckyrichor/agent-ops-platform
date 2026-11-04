"""Chinese catalog fields and trigram indexes for retrieval."""
from alembic import op

revision = "0011_chinese_catalog"
down_revision = "0010_idempotency_ttl"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    ALTER TABLE commerce.products ADD COLUMN title_zh text;
    ALTER TABLE commerce.products ADD COLUMN description_zh text;
    CREATE INDEX products_title_zh_trgm ON commerce.products USING gin (title_zh gin_trgm_ops);
    CREATE INDEX products_description_zh_trgm ON commerce.products USING gin (description_zh gin_trgm_ops);
    """)


def downgrade():
    op.execute("""
    DROP INDEX commerce.products_title_zh_trgm;
    DROP INDEX commerce.products_description_zh_trgm;
    ALTER TABLE commerce.products DROP COLUMN description_zh;
    ALTER TABLE commerce.products DROP COLUMN title_zh;
    """)
