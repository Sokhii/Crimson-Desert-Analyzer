"""SQLite access layer. The GUI never touches SQL directly - it goes through here."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional, Sequence

from .schema import LATEST_VERSION, MIGRATIONS


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class MigrationError(RuntimeError):
    pass


class Database:
    """A thread-affine connection wrapper.

    Each thread that needs the database should create its own ``Database`` on
    the same file (SQLite in WAL mode handles concurrent readers + one writer).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._lock = threading.RLock()
        self.migrate()

    # ----------------------------------------------------------- migrations
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> List[int]:
        applied = []
        current = self.schema_version()
        if current > LATEST_VERSION:
            raise MigrationError(
                f"database schema {current} is newer than this application supports ({LATEST_VERSION})"
            )
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            with self._lock:
                try:
                    self.conn.executescript("BEGIN;\n" + sql + f"\nPRAGMA user_version = {version};\nCOMMIT;")
                except sqlite3.Error as exc:
                    self.conn.rollback()
                    raise MigrationError(f"migration {version} failed: {exc}") from exc
            applied.append(version)
        return applied

    # -------------------------------------------------------------- helpers
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.conn.execute("BEGIN")
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        return default if row is None else row[0]

    def commit(self) -> None:
        with self._lock:
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------- installation
    def get_or_create_installation(self, root_path: str, label: str = "") -> int:
        row = self.query_one("SELECT id FROM installation WHERE root_path = ?", (root_path,))
        if row:
            return int(row["id"])
        cur = self.execute(
            "INSERT INTO installation(root_path, label, first_seen) VALUES (?, ?, ?)", (root_path, label, now_iso())
        )
        self.commit()
        return int(cur.lastrowid)

    def latest_installation(self) -> Optional[sqlite3.Row]:
        return self.query_one("SELECT * FROM installation ORDER BY last_scanned DESC NULLS LAST, id DESC LIMIT 1")

    def latest_scan(self, installation_id: Optional[int] = None) -> Optional[sqlite3.Row]:
        if installation_id is None:
            return self.query_one("SELECT * FROM scan ORDER BY id DESC LIMIT 1")
        return self.query_one("SELECT * FROM scan WHERE installation_id = ? ORDER BY id DESC LIMIT 1", (installation_id,))
