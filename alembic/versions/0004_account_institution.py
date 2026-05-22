"""add institution_domain and logo_url to accounts

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("institution_domain", sa.String, nullable=True))
        batch.add_column(sa.Column("logo_url",           sa.String, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("logo_url")
        batch.drop_column("institution_domain")
