"""Opens the database: a local SQLite file, or Turso (hosted SQLite) when configured."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, cast


class Row(Mapping[str, Any]):
    """Matches sqlite3.Row: read by column name or by position."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        self._names = list(names)
        self._values = list(values)

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._names.index(key)]

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


class _Cursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.lastrowid: int | None = cursor.lastrowid
        self.rowcount: int = cursor.rowcount

    def _row(self, values: Sequence[Any] | None) -> Row | None:
        if values is None:
            return None
        return Row([d[0] for d in self._cursor.description], values)

    def fetchone(self) -> Row | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[Row]:
        names = [d[0] for d in self._cursor.description or []]
        return [Row(names, values) for values in self._cursor.fetchall()]


class LibsqlConnection:
    """The slice of sqlite3.Connection this project uses, over a libsql connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Cursor:
        return _Cursor(self._conn.execute(sql, tuple(params)))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self._conn.executemany(sql, [tuple(r) for r in rows])

    def executescript(self, script: str) -> None:
        self._conn.executescript(script)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> LibsqlConnection:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if kind is None:
            self._conn.commit()
        else:
            self._conn.rollback()


def connect(path: Path | str) -> sqlite3.Connection:
    """Turso when TURSO_DATABASE_URL is set (free hosts wipe local disk); else a private file."""
    url = os.environ.get("TURSO_DATABASE_URL", "")
    if url:
        import libsql

        token = os.environ.get("TURSO_AUTH_TOKEN", "")
        # Typed as sqlite3 so callers stay checked; the wrapper covers every call they make.
        return cast(sqlite3.Connection, LibsqlConnection(libsql.connect(url, auth_token=token)))
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    # Web requests may open and use a connection on different worker threads, never at once.
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets `job-agent review` read while a pass writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn
