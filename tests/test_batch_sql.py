"""Batched executemany over libsql: one round trip per chunk, same rows as sqlite3."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import libsql
import pytest

from jobagent.db import Database, UpsertResult
from jobagent.models import Posting
from jobagent.sqlconn import LibsqlConnection

SCHEMA = (
    "CREATE TABLE t (id INTEGER PRIMARY KEY, k TEXT UNIQUE, a, b, c, d, e, f, g, h);"
    "CREATE TABLE n (x INTEGER, y TEXT);"
)


class Trips:
    """A raw libsql connection that counts round trips: libsql sends each executemany
    row on its own, so a row costs one."""

    def __init__(self, conn: Any) -> None:
        self._conn, self.n = conn, 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.n += 1
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Any) -> Any:
        rows = list(rows)
        self.n += len(rows)
        return self._conn.executemany(sql, rows)


def pair(tmp_path: Path) -> tuple[sqlite3.Connection, LibsqlConnection, Trips]:
    lite = sqlite3.connect(str(tmp_path / "a.db"))
    raw = Trips(libsql.connect(str(tmp_path / "b.db")))
    remote = LibsqlConnection(raw)
    for conn in (lite, remote):
        conn.executescript(SCHEMA)
        conn.commit()
    raw.n = 0
    return lite, remote, raw


def same(lite: Any, remote: Any, sql: str, rows: Sequence[Sequence[Any]]) -> int:
    """Runs one executemany on both; the tables and row counts must match."""
    want = lite.executemany(sql, rows).rowcount
    got = remote.executemany(sql, rows).rowcount
    lite.commit()
    remote.commit()
    raw = remote._conn  # the checks below are not part of the batch's round trips
    for table in ("t", "n"):
        q = f"SELECT * FROM {table} ORDER BY rowid"
        assert [tuple(r) for r in raw._conn.execute(q).fetchall()] == lite.execute(q).fetchall()
    assert got == want, sql
    return int(got)


def seed(lite: Any, remote: Any, n: int) -> None:
    rows = [(i, f"k{i}", i, f"b{i}") for i in range(1, n + 1)]
    for conn in (lite, remote):
        conn.executemany("INSERT INTO t(id, k, a, b) VALUES (?,?,?,?)", rows)
        conn.commit()


def test_insert_is_one_trip_per_chunk_with_nulls_and_many_columns(tmp_path: Path) -> None:
    lite, remote, raw = pair(tmp_path)
    rows = [(f"k{i}", i, None, "x" * (i % 3), 1.5, None, b"\x00", i, None) for i in range(450)]
    sql = "INSERT INTO t(k, a, b, c, d, e, f, g, h) VALUES (?, ?,?,?,?,?,?,?,?)"
    assert same(lite, remote, sql, rows) == 450
    assert raw.n == 3  # chunks of 200: 200, 200, 50


@pytest.mark.parametrize("size", [1, 199, 200, 201, 400])
def test_chunk_boundaries(tmp_path: Path, size: int) -> None:
    lite, remote, raw = pair(tmp_path)
    same(lite, remote, "INSERT INTO n(x, y) VALUES (?,?)", [(i, str(i)) for i in range(size)])
    assert raw.n == -(-size // 200)


def test_wide_rows_stay_under_the_bound_parameter_limit(tmp_path: Path) -> None:
    lite, remote, raw = pair(tmp_path)
    cols = ", ".join(f"c{i}" for i in range(400))
    for conn in (lite, remote):
        conn.execute(f"CREATE TABLE w ({cols})")
    raw.n = 0
    rows = [tuple(range(r, r + 400)) for r in range(200)]
    remote.executemany(f"INSERT INTO w VALUES ({','.join('?' * 400)})", rows)
    assert raw.n == 3  # 81 rows of 400 fit under 32766 marks
    assert remote.execute("SELECT COUNT(*) FROM w").fetchone()[0] == 200


def test_insert_keeps_on_conflict_clauses(tmp_path: Path) -> None:
    lite, remote, raw = pair(tmp_path)
    seed(lite, remote, 5)
    raw.n = 0
    skip = "INSERT INTO t(k, a) VALUES (?,?) ON CONFLICT(k) DO NOTHING"
    assert same(lite, remote, skip, [("k1", 9), ("new", 1), ("k2", 9), ("new", 2)]) == 1
    upsert = (
        "INSERT INTO t(k, a, b) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET a=excluded.a,"
        " b=excluded.b"
    )
    same(lite, remote, upsert, [("k3", 30, None), ("k9", 90, "x"), ("k3", 31, "last")])
    assert raw.n == 2
    no_target = "INSERT INTO t(k, a) VALUES (?,?) ON CONFLICT DO NOTHING"
    same(lite, remote, no_target, [("k4", 1), ("z", 2)])


def test_update_by_id_is_one_trip_per_chunk(tmp_path: Path) -> None:
    lite, remote, raw = pair(tmp_path)
    seed(lite, remote, 450)
    raw.n = 0
    rows = [(None if i % 7 == 0 else f"s{i}", i * 2, i) for i in range(1, 451, 1)]
    rows += [("ghost", 0, 9999), ("none", 0, None)]  # no row matches
    assert same(lite, remote, "UPDATE t SET b=?, a=? WHERE id=?", rows) == 450
    assert raw.n == 3


def test_update_with_constants_and_many_columns(tmp_path: Path) -> None:
    lite, remote, raw = pair(tmp_path)
    seed(lite, remote, 30)
    raw.n = 0
    sql = "UPDATE t SET c='closed', a=?, d=0, e=?, f=NULL, g=?, h=? WHERE k=?"
    rows = [(i, None, "g", 2.5, f"k{i}") for i in range(1, 31)]
    assert same(lite, remote, sql, rows) == 30
    assert raw.n == 1


def test_update_without_marks_in_set_uses_in(tmp_path: Path) -> None:
    lite, remote, raw = pair(tmp_path)
    seed(lite, remote, 10)
    raw.n = 0
    assert same(lite, remote, "UPDATE t SET b='~' || rowid WHERE id=?", [(1,), (3,), (5,)]) == 3
    assert same(lite, remote, "UPDATE t SET c='closed' WHERE id=?", [(2,), (None,)]) == 1
    assert raw.n == 2


def test_update_on_two_keys(tmp_path: Path) -> None:
    lite, remote, raw = pair(tmp_path)
    seed(lite, remote, 10)
    raw.n = 0
    rows = [("x", 1, "k1"), ("y", 2, "wrong"), ("z", 3, "k3")]
    assert same(lite, remote, "UPDATE t SET c=? WHERE id=? AND k=?", rows) == 2
    assert raw.n == 1


def test_delete_uses_in(tmp_path: Path) -> None:
    lite, remote, raw = pair(tmp_path)
    seed(lite, remote, 300)
    raw.n = 0
    rows = [(i,) for i in range(1, 260)] + [(1,), (None,)]
    assert same(lite, remote, "DELETE FROM t WHERE id=?", rows) == 259
    assert raw.n == 2


@pytest.mark.parametrize(
    "sql, rows",
    [
        ("UPDATE t SET a=a+? WHERE id=?", [(1, 1), (2, 1)]),  # same row twice, adds up
        ("UPDATE t SET a=? WHERE id=?", [(1, 2), (5, 2)]),  # last write wins
        ("UPDATE t SET id=? WHERE id=?", [(100, 1), (1, 2)]),  # moves the key it matches
        ("UPDATE t SET a=a+1 WHERE id=?", [(3,), (3,)]),
        ("INSERT INTO t(k, a) SELECT ?, ?", [("s1", 1), ("s2", 2)]),
        ("UPDATE t SET b='a,b' WHERE id=?", [(4,), (5,)]),
        ("DELETE FROM t WHERE id=? OR a=?", [(6, 7), (8, 9)]),
    ],
)
def test_other_shapes_fall_back_to_one_row_at_a_time(
    tmp_path: Path, sql: str, rows: list[tuple[Any, ...]]
) -> None:
    lite, remote, raw = pair(tmp_path)
    seed(lite, remote, 10)
    raw.n = 0
    same(lite, remote, sql, rows)
    assert raw.n <= len(rows)


def test_empty_batch_sends_nothing(tmp_path: Path) -> None:
    lite, remote, raw = pair(tmp_path)
    assert same(lite, remote, "INSERT INTO n(x, y) VALUES (?,?)", []) == 0
    assert raw.n == 0


def greenhouse(i: int, **kw: Any) -> Posting:
    return Posting(source="greenhouse", company="Acme", title=f"AI Engineer {i}",
                   location="Bengaluru", experience_text="2-3 yrs",
                   url=f"https://job-boards.greenhouse.io/acme/jobs/{i}", **kw)  # fmt: skip


def counted_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Database, Trips]:
    made: list[Trips] = []

    def connect(path: Path | str) -> Any:
        made.append(Trips(libsql.connect(str(path))))
        return LibsqlConnection(made[-1])

    monkeypatch.setattr("jobagent.db.connect", connect)
    return Database(tmp_path / "t.db"), made[0]


def save_portal(db: Database, postings: list[Posting]) -> list[UpsertResult]:
    """What runs.save_portal asks of the database for one portal."""
    saved = db.upsert_many(postings)
    db.save_validators({"https://boards-api.greenhouse.io/v1/boards/acme/jobs": ("e", None, "h")})
    db.close_vanished(postings, clean=True, after_runs=3)
    return saved


def test_a_600_posting_portal_with_200_new_saves_in_a_few_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, raw = counted_db(tmp_path, monkeypatch)
    save_portal(db, [greenhouse(i) for i in range(400)])
    raw.n = 0
    results = save_portal(db, [greenhouse(i) for i in range(600)])
    assert [r.outcome for r in results] == ["duplicate"] * 400 + ["new"] * 200
    assert raw.n <= 15
    count = lambda t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    assert (count("jobs"), count("job_links"), count("events")) == (600, 600, 600)
    assert len({r.job_id for r in results}) == 600
    for r, p in zip(results[400:], [greenhouse(i) for i in range(400, 600)], strict=True):
        assert db.get_job(r.job_id)["title"] == p.title
        assert db.links(r.job_id)[0]["url"] == p.url
    db.close()
