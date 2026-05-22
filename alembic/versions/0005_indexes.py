"""hot-path indexes on snapshots.snapshot_date and balances.account_id

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-21
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_snapshots_snapshot_date", "snapshots", ["snapshot_date"]
    )
    op.create_index(
        "ix_balances_account_id", "balances", ["account_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_balances_account_id", table_name="balances")
    op.drop_index("ix_snapshots_snapshot_date", table_name="snapshots")
