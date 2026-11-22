"""Record durable document and collection deletion requests."""

from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("delete_requested_at", sa.DateTime(timezone=True)))
    op.add_column("collections", sa.Column("delete_requested_at", sa.DateTime(timezone=True)))
    op.add_column("collections", sa.Column("cleanup_completed_at", sa.DateTime(timezone=True)))


def downgrade():
    raise RuntimeError("Deletion intent is forward-only. Removing it could make deleted documents visible.")
