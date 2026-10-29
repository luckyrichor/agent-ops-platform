"""P7 real-product lexical search, separate from later S3 cache experiments."""
from alembic import op

revision = '0008_catalog_search'
down_revision = '0007_customer_memory'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE commerce.products ADD COLUMN search_document tsvector
      GENERATED ALWAYS AS (to_tsvector('english'::regconfig,title || ' ' || sku || ' ' || attrs::text)) STORED;
      CREATE INDEX products_search_document ON commerce.products USING gin(search_document);""")


def downgrade():
    op.execute('DROP INDEX commerce.products_search_document; ALTER TABLE commerce.products DROP COLUMN search_document')
