"""Initial tenant-isolated operational schema."""
from alembic import op
from backend.production.database import meta
revision='0001_product'
down_revision=None
branch_labels=None
depends_on=None
def upgrade():meta.create_all(op.get_bind())
def downgrade():
    raise RuntimeError('Destructive downgrade disabled. Restore a verified backup instead.')
