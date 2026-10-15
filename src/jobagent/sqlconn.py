"""Opens the database: a local SQLite file, or Turso (hosted SQLite) when configured."""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, cast

MAX_MARKS = 32766  # SQLite's limit on `?` marks in one statement
CHUNK_ROWS = 200  # one Turso round trip each; 200 rows took 156 ms live (15-10-2026)


class Row(Mapping[str, Any]):
    """Matches sqlite3.Row: read by column name or by position."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        # Turso may qualify ("j.key") or upper-case ("KEY") plain column names.
        self._names = [_name(n).lower() for n in names]
        self._values = list(values)

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._names.index(key.lower())]

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


_QUALIFIED = re.compile(r"\w+\.(\w+)")


def _name(raw: str) -> str:
    """SQLite leaves a column's name unspecified without AS; Turso may send `j.key`, which
    local SQLite calls `key`. Rows are read by the short name."""
    plain = _QUALIFIED.fullmatch(raw)
    return plain[1] if plain else raw


class _Cursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.lastrowid: int | None = cursor.lastrowid
        self.rowcount: int = cursor.rowcount

    def _names(self) -> list[str]:
        return [_name(d[0]) for d in self._cursor.description or []]

    def fetchone(self) -> Row | None:
        values = self._cursor.fetchone()
        return None if values is None else Row(self._names(), values)

    def fetchall(self) -> list[Row]:
        names = self._names()
        return [Row(names, values) for values in self._cursor.fetchall()]


def chunks[T](rows: Sequence[T], width: int) -> Iterator[Sequence[T]]:
    """Slices of at most CHUNK_ROWS rows whose `width` marks each fit in one statement."""
    size = max(1, min(CHUNK_ROWS, MAX_MARKS // max(width, 1)))
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def marks(rows: int, width: int) -> str:
    """`(?,?),(?,?)` for a multi-row VALUES list."""
    return ",".join(["(" + ",".join("?" * width) + ")"] * rows)


_INSERT = re.compile(
    r"(\s*INSERT\b[^?]*?\bVALUES\s*)\(\s*\?(?:\s*,\s*\?)*\s*\)([^?]*)", re.I | re.S
)
_UPDATE = re.compile(r"\s*UPDATE\s+(\w+)\s+SET\s+(.+?)\s+WHERE\s+(.+?)\s*", re.I | re.S)
_DELETE = re.compile(r"(\s*DELETE\s+FROM\s+\w+\s+WHERE\s+)(\w+)\s*=\s*\?\s*", re.I | re.S)
_SET = re.compile(r"\s*(\w+)\s*=\s*(\?|-?\d+(?:\.\d+)?|'[^',]*'|NULL)\s*", re.I)
_KEY = re.compile(r"\s*(\w+)\s*=\s*\?\s*")


class _Batched:
    """What executemany reports: the rows changed, summed over every statement sent."""

    lastrowid = None

    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


Plan = Callable[[int], str]  # the statement for a chunk of n rows; params are rows flattened


def _plan(sql: str, rows: Sequence[tuple[Any, ...]]) -> tuple[Plan, int] | None:
    """One statement per chunk for an INSERT ... VALUES, an UPDATE matched on `col=?` terms
    or a DELETE on one `col=?`; None when per-row runs could differ from one set-wide run."""
    width = len(rows[0])
    if any(len(r) != width for r in rows):
        return None
    if (m := _INSERT.fullmatch(sql)) and "RETURNING" not in m[2].upper():
        head, tail = m[1], m[2]
        return (lambda n: f"{head}{marks(n, width)}{tail}"), width
    if m := _DELETE.fullmatch(sql):
        head, col = m[1], m[2]
        return (lambda n: f"{head}{col} IN ({','.join('?' * n)})"), 1
    if m := _UPDATE.fullmatch(sql):
        return _plan_update(m[1], m[2], m[3], rows)
    return None


def _plan_update(
    table: str, sets: str, where: str, rows: Sequence[tuple[Any, ...]]
) -> tuple[Plan, int] | None:
    terms = re.split(r"\s+AND\s+", where, flags=re.I)
    keys = [k[1].lower() for t in terms if (k := _KEY.fullmatch(t))]
    marked = sets.count("?")
    if len(keys) != len(terms) or marked + len(keys) != len(rows[0]) or table.lower() == "v":
        return None
    # A row matched twice, or a key the SET moves, depends on run order.
    if len({r[marked:] for r in rows}) != len(rows):
        return None
    if any(re.search(rf"\b{k}\s*=", sets, re.I) for k in keys):
        return None
    if not marked:
        if len(keys) != 1 or "SELECT" in sets.upper():
            return None
        head = f"UPDATE {table} SET {sets} WHERE {keys[0]}"
        return (lambda n: f"{head} IN ({','.join('?' * n)})"), 1
    pairs = [_SET.fullmatch(p) for p in sets.split(",")]
    if not all(pairs):
        return None
    out, column = [], 0
    for p in cast(list[re.Match[str]], pairs):
        if p[2] == "?":
            column += 1
            out.append(f"{p[1]}=v.column{column}")
        else:
            out.append(f"{p[1]}={p[2]}")
    on = " AND ".join(f"{table}.{k}=v.column{column + i}" for i, k in enumerate(keys, 1))
    width = len(rows[0])
    head = f"UPDATE {table} SET {', '.join(out)} FROM (VALUES "
    return (lambda n: f"{head}{marks(n, width)}) AS v WHERE {on}"), width


class LibsqlConnection:
    """The slice of sqlite3.Connection this project uses, over a libsql connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _Cursor:
        return _Cursor(self._conn.execute(sql, tuple(params)))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> _Batched:
        """libsql sends each row as its own round trip (15 s for 200 rows on Turso), so known
        shapes go as one multi-row statement per chunk; others run row by row."""
        batch = [tuple(r) for r in rows]
        plan = _plan(sql, batch) if batch else None
        if plan is None:
            return _Batched(sum(max(self._conn.execute(sql, r).rowcount, 0) for r in batch))
        build, width = plan
        done = 0
        for chunk in chunks(batch, width):
            flat = tuple(v for r in chunk for v in r)
            done += max(self._conn.execute(build(len(chunk)), flat).rowcount, 0)
        return _Batched(done)

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
