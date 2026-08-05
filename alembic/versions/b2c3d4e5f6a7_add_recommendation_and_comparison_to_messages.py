"""add recommended_documents and comparison_sections to messages

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-02
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'messages',
        sa.Column('recommended_documents', sa.JSON(), nullable=True),
    )
    op.add_column(
        'messages',
        sa.Column('comparison_sections', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('messages', 'comparison_sections')
    op.drop_column('messages', 'recommended_documents')
