"""add multi-currency settings and frozen snapshot rates

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-29
"""
import sqlalchemy as sa
from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "currency_code",
                sa.String(length=3),
                nullable=False,
                server_default="GBP",
            )
        )

    with op.batch_alter_table("balances") as batch_op:
        batch_op.add_column(
            sa.Column(
                "currency_code",
                sa.String(length=3),
                nullable=False,
                server_default="GBP",
            )
        )

    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "reporting_currency",
            sa.String(length=3),
            nullable=False,
            server_default="GBP",
        ),
        sa.CheckConstraint("id = 1", name="ck_app_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO app_settings (id, reporting_currency) VALUES (1, 'GBP')"
        )
    )

    op.create_table(
        "snapshot_fx_rates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("rate_per_eur", sa.Numeric(20, 10), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="ecb",
        ),
        sa.CheckConstraint("rate_per_eur > 0", name="ck_snapshot_fx_positive"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id", "currency_code", name="uq_snapshot_fx_currency"
        ),
    )
    op.create_index(
        "ix_snapshot_fx_rates_snapshot_id",
        "snapshot_fx_rates",
        ["snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_snapshot_fx_rates_snapshot_id",
        table_name="snapshot_fx_rates",
    )
    op.drop_table("snapshot_fx_rates")
    op.drop_table("app_settings")

    with op.batch_alter_table("balances") as batch_op:
        batch_op.drop_column("currency_code")
    with op.batch_alter_table("accounts") as batch_op:
        batch_op.drop_column("currency_code")
