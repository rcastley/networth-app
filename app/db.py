"""Database setup."""
import os
import sqlite3

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:////data/networth.db")

# `connect_args` needed for SQLite multi-threaded use (FastAPI uses threadpool)
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    future=True,
)


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        """Enable referential-integrity checks on every SQLite connection."""
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA foreign_keys")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")
        finally:
            cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def assert_foreign_key_integrity(db) -> None:
    """Refuse startup if an existing SQLite database contains orphaned rows."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    violations = db.execute(text("PRAGMA foreign_key_check")).all()
    if not violations:
        return

    examples = ", ".join(
        f"{table} row {row_id}" for table, row_id, _parent, _fk_id in violations[:5]
    )
    remainder = len(violations) - 5
    if remainder > 0:
        examples += f", and {remainder} more"
    raise RuntimeError(
        "Database foreign-key integrity check failed; no data was changed. "
        f"Back up and repair the database before restarting: {examples}."
    )
