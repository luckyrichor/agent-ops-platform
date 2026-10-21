"""P2 owner-approved rule-proxy provenance, no routing quality labels."""
from alembic import op
revision='0003_proxy_provenance'
down_revision='0002_commerce'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('ALTER TABLE support.tickets ADD COLUMN synthetic_distribution_note text')

def downgrade():
    op.execute('ALTER TABLE support.tickets DROP COLUMN synthetic_distribution_note')
