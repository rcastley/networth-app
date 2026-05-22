"""add parent_id and is_group to accounts

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("parent_id", sa.Integer, nullable=True))
        batch.add_column(sa.Column("is_group",  sa.Boolean, nullable=False, server_default=sa.false()))
        batch.create_foreign_key("fk_accounts_parent", "accounts", ["parent_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("fk_accounts_parent", type_="foreignkey")
        batch.drop_column("is_group")
        batch.drop_column("parent_id")
