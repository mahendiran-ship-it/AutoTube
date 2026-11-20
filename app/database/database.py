"""SQLite wrapper: thread-local connections, WAL, versioned migrations."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.database.models import MIGRATIONS


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextmanager
    def tx(self):
        with self._write_lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn().execute(sql, params)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    # ---- migrations ---------------------------------------------------
    def current_version(self) -> int:
        c = self.conn()
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                  "(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
        row = c.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"] or 0)

    def migrate(self) -> list[int]:
        applied: list[int] = []
        cur = self.current_version()
        for version, name, sql in sorted(MIGRATIONS):
            if version <= cur:
                continue
            with self._write_lock:
                c = self.conn()
                c.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _split_sql(sql):
                        c.execute(stmt)
                    c.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES (?,?,?)",
                              (version, name, utcnow()))
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
            applied.append(version)
        return applied

    def table_names(self) -> list[str]:
        return [r["name"] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

    def columns(self, table: str) -> list[str]:
        return [r["name"] for r in self.query(f"PRAGMA table_info({table})")]


def _split_sql(script: str) -> list[str]:
    """Split a migration script into statements (no triggers/semicolons in strings used)."""
    out, buf = [], []
    for line in script.splitlines():
        buf.append(line)
        if line.strip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return out
