"""SQLAlchemy engine + session management (sync sessions; FastAPI endpoints
run sync DB work in threadpool via `Depends(get_db)` or direct `with Session`)."""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL, IS_POSTGRES

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,          # survives Neon idle disconnects
    pool_recycle=280,
    future=True,
    # A self-hosted SQLite deployment serves concurrent agent turns; without a
    # busy timeout a writer briefly holding the file turns a read into
    # "database is locked" — an OperationalError, historically surfaced as 500.
    connect_args={"check_same_thread": False, "timeout": 30} if _IS_SQLITE else {},
)

if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        """WAL + a 30s busy timeout: readers never block behind the log writer."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            # In-memory DBs / filesystems without WAL keep their default mode.
            pass
        finally:
            cursor.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency — always closes the session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_sqlite_dir() -> None:
    if DATABASE_URL.startswith("sqlite"):
        from pathlib import Path

        db_path = DATABASE_URL.replace("sqlite:///", "", 1)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


__all__ = ["engine", "SessionLocal", "get_db", "ensure_sqlite_dir", "ping", "IS_POSTGRES"]
