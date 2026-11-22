"""Persist retry state for deletion cleanup."""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("documents", "collections"):
        op.add_column(table, sa.Column("cleanup_attempts", sa.Integer(), nullable=False, server_default="0"))
        op.add_column(table, sa.Column("cleanup_retry_at", sa.DateTime(timezone=True)))
        op.add_column(table, sa.Column("cleanup_error", sa.String(length=120)))
    op.create_index("ix_documents_collection_id", "documents", ["collection_id"])


def downgrade():
    op.drop_index("ix_documents_collection_id", table_name="documents")
    for table in ("documents", "collections"):
        op.drop_column(table, "cleanup_error")
        op.drop_column(table, "cleanup_retry_at")
        op.drop_column(table, "cleanup_attempts")
