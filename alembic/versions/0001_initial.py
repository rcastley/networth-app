"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id",           sa.Integer, primary_key=True),
        sa.Column("name",         sa.String,  nullable=False, unique=True),
        sa.Column("sort_order",   sa.Integer, nullable=False, server_default="100"),
        sa.Column("in_net_worth", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("in_liquid",    sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_liability", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("color",        sa.String,  nullable=False, server_default="#3B82F6"),
    )
    op.create_table(
        "accounts",
        sa.Column("id",          sa.Integer, primary_key=True),
        sa.Column("name",        sa.String,  nullable=False),
        sa.Column("category_id", sa.Integer, sa.ForeignKey("categories.id"), nullable=False),
        sa.Column("notes",       sa.String,  nullable=False, server_default=""),
        sa.Column("sort_order",  sa.Integer, nullable=False, server_default="100"),
        sa.Column("is_active",   sa.Boolean, nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "snapshots",
        sa.Column("id",            sa.Integer, primary_key=True),
        sa.Column("snapshot_date", sa.Date,    nullable=False, unique=True),
        sa.Column("notes",         sa.String,  nullable=False, server_default=""),
        sa.Column("created_at",    sa.DateTime, nullable=False),
    )
    op.create_table(
        "balances",
        sa.Column("id",          sa.Integer, primary_key=True),
        sa.Column("snapshot_id", sa.Integer, sa.ForeignKey("snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id",  sa.Integer, sa.ForeignKey("accounts.id",  ondelete="CASCADE"), nullable=False),
        sa.Column("amount",      sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.UniqueConstraint("snapshot_id", "account_id", name="uq_snap_acc"),
    )


def downgrade() -> None:
    op.drop_table("balances")
    op.drop_table("snapshots")
    op.drop_table("accounts")
    op.drop_table("categories")
