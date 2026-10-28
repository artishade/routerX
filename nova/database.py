"""SQLAlchemy engine + session management (sync sessions; FastAPI endpoints
run sync DB work in threadpool via `Depends(get_db)` or direct `with Session`)."""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Generator
from pathlib import Path, PurePosixPath, PureWindowsPath

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


_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def sqlite_file_path(url: str) -> str | None:
    """Real filesystem path of a SQLite URL, or None (in-memory / other backends).

    Accepted forms (query strings are stripped first):

      sqlite:///C:/x.db       three slashes + drive  -> `C:/x.db`       (absolute)
      sqlite:///C:\\x.db      same, backslashes       -> `C:/x.db`
      sqlite:////C:/x.db      four slashes + drive    -> `C:/x.db`       (legacy)
      sqlite:///relative.db   three slashes           -> `relative.db`   (relative)
      sqlite:///./relative.db                         -> `./relative.db`
      sqlite:////abs/posix.db four slashes, no drive  -> `/abs/posix.db`
      sqlite://, sqlite:///:memory:                   -> None (any slash count)

    Exactly four slashes after the scheme mean POSIX-absolute; three mean
    relative unless the payload starts with a drive letter. The result is a
    native path for the *running* platform: a path is parsed with
    `PureWindowsPath`/`PurePosixPath` and only converted when its flavour
    matches the host, so `C:/x.db` never turns into the unopenable `\\C:\\x.db`
    and `//abs/x.db` never turns into a UNC share on POSIX.
    """
    if not url.startswith("sqlite:"):
        return None
    payload = url.split(":", 1)[1].partition("?")[0]
    if payload.startswith("//:"):
        payload = payload[1:]            # `sqlite://:memory:` -> `/:memory:`
    if payload.startswith("//"):
        rest = payload[2:]
        if not rest.startswith("/"):
            path = rest                  # `sqlite://rel.db` - no host, relative
        else:
            rest = rest[1:]              # drop the third slash
            if rest.startswith("/"):     # four slashes
                path = rest[1:] if _WINDOWS_DRIVE_PATH.match(rest[1:]) else rest
            else:
                path = rest              # three slashes: relative or drive path
    elif payload.startswith("/"):
        path = payload[1:]               # `sqlite:/C:/x.db`, `sqlite:/abs/x.db`
        if not _WINDOWS_DRIVE_PATH.match(path):
            path = "/" + path
    else:
        path = payload
    if not path or path.strip("/\\") == ":memory:":
        return None

    is_windows = os.name == "nt"
    if _WINDOWS_DRIVE_PATH.match(path):
        # A drive path is absolute on its own; normalise separators only when
        # the host can use them.
        return PureWindowsPath(path).as_posix() if is_windows else path
    if path.startswith(("/", "\\")):
        # `\\server\share` is absolute only on Windows; on POSIX a leading run
        # of slashes collapses to a single root.
        return str(PureWindowsPath(path)) if is_windows else str(PurePosixPath(path))
    return path


def _sqlite_parent_dir(path: str) -> str | None:
    """Parent directory to create for a DB file, or None when there is none.

    A relative bare filename (`custom.db`) has no directory to make, and a
    file directly under a drive root (`C:/x.db`) or `/` must never trigger
    `mkdir` on `C:` / `\` - that used to abort boot with WinError 123.
    """
    parent = os.path.dirname(path)
    if not parent:
        return None
    if os.name == "nt":
        if re.fullmatch(r"[A-Za-z]:[\\/]*", parent):
            return None
    elif parent == "/":
        return None
    return parent


def ensure_sqlite_dir() -> bool:
    """Create the directory holding the SQLite file. Never raises.

    Runs at import/lifespan time, so a read-only filesystem or a malformed URL
    must not take the whole app down; the real error surfaces when the engine
    opens the database.
    """
    try:
        db_path = sqlite_file_path(DATABASE_URL)
        if not db_path:
            return False
        parent = _sqlite_parent_dir(db_path)
        if not parent:
            return False
        Path(parent).mkdir(parents=True, exist_ok=True)
        return True
    except Exception as err:  # noqa: BLE001 - boot must survive a bad DB URL
        logging.getLogger(__name__).warning(
            "could not create the SQLite directory for %r: %s", DATABASE_URL, err)
        return False


def ping() -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


__all__ = ["engine", "SessionLocal", "get_db", "ensure_sqlite_dir", "sqlite_file_path", "ping", "IS_POSTGRES"]
