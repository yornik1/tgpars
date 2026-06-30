"""Database engine / session setup and schema initialisation."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def _ensure_sqlite_dir(database_url: str) -> None:
    """Create the parent directory for a file-based SQLite database."""
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        db_path = Path(database_url[len(prefix) :])
        if db_path.parent and str(db_path.parent) not in ("", "."):
            db_path.parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(database_url: str) -> Engine:
    _ensure_sqlite_dir(database_url)
    # A busy timeout lets the collector and backfill share the SQLite file
    # without immediate "database is locked" errors.
    connect_args = {"timeout": 30} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, future=True, connect_args=connect_args)


def init_db(engine: Engine) -> None:
    """Create all tables if they do not exist, then apply lightweight migrations."""
    Base.metadata.create_all(engine)
    _ensure_columns(engine)


def _ensure_columns(engine: Engine) -> None:
    """Idempotently add columns introduced after a table was first created.

    SQLAlchemy's create_all does not ALTER existing tables, so new nullable
    columns are added here for already-existing SQLite databases.
    """
    from sqlalchemy import text

    wanted = {"messages": {"reply_to_msg_id": "BIGINT"}}
    with engine.begin() as conn:
        for table, columns in wanted.items():
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            for name, coltype in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
