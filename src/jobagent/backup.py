"""Dump the database (local SQLite or Turso) to gzipped SQL, and restore it into a fresh file."""

from __future__ import annotations

import gzip
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _private(path: Path, *, new: bool = False) -> BinaryIO:
    """Open a file only the owner can read: dumps hold personal data."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if new else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def dump_sql(conn: Any) -> Iterator[str]:
    """Like sqlite3's iterdump, but built from plain SELECTs so a libsql connection works too.
    SQLite's own quote() writes each value, so text, blobs and floats round-trip exactly."""
    yield "BEGIN TRANSACTION;\n"
    objects = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        " AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
    ).fetchall()
    for kind, name, sql in ((r["type"], r["name"], r["sql"]) for r in objects):
        if kind != "table":
            continue
        yield f"{sql};\n"
        info = conn.execute("SELECT name FROM pragma_table_info(?)", (name,)).fetchall()
        values = " || ',' || ".join(f"quote({_ident(r['name'])})" for r in info)
        table = _ident(name)
        select = f"SELECT 'INSERT INTO {table} VALUES(' || {values} || ');' FROM {table}"
        for row in conn.execute(select).fetchall():
            yield f"{row[0]}\n"
    for kind, _, sql in ((r["type"], r["name"], r["sql"]) for r in objects):
        if kind != "table":
            yield f"{sql};\n"
    yield "COMMIT;\n"


def backup(conn: Any, out: Path) -> int:
    """Write the dump to `out`; returns the number of lines written."""
    lines = 0
    with _private(out) as raw, gzip.open(raw, "wt", encoding="utf-8", newline="") as f:
        for line in dump_sql(conn):
            f.write(line)
            lines += 1
    return lines


def restore(dump: Path, target: Path) -> None:
    """Load a dump into a new SQLite file; never overwrites one that exists."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        _private(target, new=True).close()  # exclusive create: an existing file is never touched
    except FileExistsError:
        raise FileExistsError(f"{target} already exists; restore only into a new file") from None
    conn = sqlite3.connect(target, isolation_level=None)  # the dump holds its own BEGIN/COMMIT
    try:
        statement = last = ""
        # newline="" keeps a \r inside a value as it was
        with gzip.open(dump, "rt", encoding="utf-8", newline="") as f:
            for line in f:  # values may span lines, so feed whole statements only
                statement += line
                if sqlite3.complete_statement(statement):
                    conn.execute(statement)
                    statement, last = "", statement
        if statement.strip() or last.strip() != "COMMIT;":
            raise ValueError("dump does not end in COMMIT; the file is cut short")
    except BaseException:
        conn.close()
        target.unlink(missing_ok=True)
        raise
    conn.close()
