"""SQLite access. One file, WAL mode, plain SQL.

WAL matters here: the agent writes while the dashboard reads, and without it the
dashboard blocks mid-run.

Connections are per-thread. FastAPI runs sync route handlers in a threadpool, and
sqlite3 refuses a connection used outside the thread that made it, so `Database`
hands each thread its own connection to the same file. WAL allows the concurrent
readers that creates; `busy_timeout` absorbs the moments two of them write.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with the pragmas this project relies on."""
    path = Path(db_path)
    if str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Apply the schema. Safe to call on every start — every statement is IF NOT EXISTS."""
    conn.executescript(_SCHEMA_FILE.read_text())
    current = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    if current is None:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, utcnow()),
        )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Wrap a unit of work. Rolls back on any exception.

    `connect` uses autocommit (isolation_level=None), so transactions are explicit.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class Database:
    """One database file, one connection per thread."""

    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        init_db(self.connection())

    def connection(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            connections, self._all = self._all, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.ProgrammingError:
                # Closing another thread's connection is refused; the process is
                # going away anyway, so there is nothing useful to do about it.
                pass
        self._local = threading.local()


def open_database(db_path: Path | str) -> Database:
    return Database(db_path)
