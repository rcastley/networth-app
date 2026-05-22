"""snapshot_date -> DateTime, drop unique constraint

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-20
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite stores Date as 'YYYY-MM-DD' (10 chars). Pad existing rows so the value
    # is a valid DATETIME literal ('YYYY-MM-DD HH:MM:SS') before we change the column.
    op.execute(
        "UPDATE snapshots "
        "SET snapshot_date = snapshot_date || ' 00:00:00' "
        "WHERE length(snapshot_date) = 10"
    )

    # Rebuild the table without the unique constraint and with DATETIME type.
    op.execute("""
        CREATE TABLE _snapshots_new (
            id            INTEGER  PRIMARY KEY,
            snapshot_date DATETIME NOT NULL,
            notes         VARCHAR  NOT NULL DEFAULT '',
            created_at    DATETIME NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO _snapshots_new (id, snapshot_date, notes, created_at)
        SELECT id, snapshot_date, notes, created_at FROM snapshots
    """)
    op.execute("DROP TABLE snapshots")
    op.execute("ALTER TABLE _snapshots_new RENAME TO snapshots")


def downgrade() -> None:
    # Lossy: collapse datetimes to dates and re-introduce the unique constraint.
    # If two snapshots share a date, the downgrade will error; user must manually resolve.
    op.execute("""
        CREATE TABLE _snapshots_old (
            id            INTEGER PRIMARY KEY,
            snapshot_date DATE    NOT NULL UNIQUE,
            notes         VARCHAR NOT NULL DEFAULT '',
            created_at    DATETIME NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO _snapshots_old (id, snapshot_date, notes, created_at)
        SELECT id, substr(snapshot_date, 1, 10), notes, created_at FROM snapshots
    """)
    op.execute("DROP TABLE snapshots")
    op.execute("ALTER TABLE _snapshots_old RENAME TO snapshots")
