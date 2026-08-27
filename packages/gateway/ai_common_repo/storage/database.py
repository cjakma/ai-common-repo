"""Thin SQLite wrapper: connection lifecycle + schema bootstrap. No business
logic — repositories build on top of this.

The gateway deliberately owns one SQLite connection.  ``sqlite3.Connection``
is not safe to use concurrently, even with ``check_same_thread=False`` (that
flag only disables the thread-affinity check).  ``locked()`` therefore holds a
single process-wide re-entrant lock for the complete execute/fetch/commit or
rollback lifecycle.  The re-entrant property is required because service
operations can call more than one repository while already inside a database
operation.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

from . import schema as S


class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self.locked() as c:
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA foreign_keys=ON;")
            c.execute("PRAGMA busy_timeout=5000;")
        self._migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        """Serialize one complete connection/cursor lifecycle.

        Repositories keep their existing SQL shape, but enter this context for
        the whole method, not just for ``execute``.  This prevents another
        thread from advancing a cursor or committing/rolling back the shared
        connection between a query and its fetch or commit.
        """
        with self._lock:
            try:
                yield self._conn
            except BaseException:
                # Preserve the original exception if rollback has no active
                # transaction (for example, a read-only operation failed).
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    def _migrate(self) -> None:
        with self.locked() as c:
            c.executescript(S.DDL)
            c.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(S.SCHEMA_VERSION),),
            )
            c.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def database_locked_methods(cls: type[Any]) -> type[Any]:
    """Wrap every SQLite-backed method on a repository/queue class.

    Existing repository methods already delimit writes with ``commit`` and
    reads with ``fetchone``/``fetchall``.  Wrapping the method as a unit keeps
    those lifecycles intact without duplicating lock/rollback boilerplate in
    every SQL method.  Nested repository calls are safe because ``Database``
    uses ``threading.RLock``.
    """
    for name, method in tuple(vars(cls).items()):
        if name == "__init__" or not callable(method):
            continue

        @wraps(method)
        def locked_method(self: Any, *args: Any, _method=method,
                          **kwargs: Any) -> Any:
            with self._db.locked():
                return _method(self, *args, **kwargs)

        setattr(cls, name, locked_method)
    return cls
