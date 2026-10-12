"""Shared stock pools, mappings, reservations and audited allocation plans."""
from alembic import op
from backend.production.database import inventory_pools, sku_mappings, inventory_positions, stock_journal, stock_plans

revision = "0003_shared_inventory"
down_revision = "0002_analysis_steps"
branch_labels = None
depends_on = None


def upgrade():
    for table in (inventory_pools, sku_mappings, inventory_positions, stock_journal, stock_plans):
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Destructive downgrade disabled. Restore a verified backup instead.")
