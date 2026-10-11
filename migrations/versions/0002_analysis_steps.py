"""Durable, tenant-scoped role analysis journal."""
from alembic import op
from backend.production.database import analysis_steps
revision = '0002_analysis_steps'
down_revision = '0001_product'
branch_labels = None
depends_on = None

def upgrade():
    analysis_steps.create(op.get_bind(), checkfirst=True)

def downgrade():
    raise RuntimeError('Destructive downgrade disabled. Restore a verified backup instead.')
