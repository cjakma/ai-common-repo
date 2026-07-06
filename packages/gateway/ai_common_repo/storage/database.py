"""Thin SQLite wrapper: connection lifecycle + schema bootstrap. No business
logic — repositories build on top of this."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import schema as S


class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def _migrate(self) -> None:
        self._conn.executescript(S.DDL)
        self._conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(S.SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
