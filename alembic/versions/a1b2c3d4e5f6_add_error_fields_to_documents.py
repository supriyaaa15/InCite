"""add error_code and error_message to documents

Revision ID: a1b2c3d4e5f6
Revises: 9b4eeb49ea79
Create Date: 2026-07-31

Adds two nullable columns to the documents table so that both the route
handler (synchronous validation failures) and the ingestion service
(background processing failures) can persist a machine-readable error code
and a user-safe error message alongside the failed status.

Backward-compatible: existing rows are left with NULL in both columns,
which the application treats as "no error recorded".
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '9b4eeb49ea79'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'documents',
        sa.Column('error_code', sa.String(), nullable=True),
    )
    op.add_column(
        'documents',
        sa.Column('error_message', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('documents', 'error_message')
    op.drop_column('documents', 'error_code')
