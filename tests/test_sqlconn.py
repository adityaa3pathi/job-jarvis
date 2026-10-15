"""The Turso path: the same storage code must work over a libsql connection."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import libsql
import pytest
from fastapi.testclient import TestClient

from jobagent.config import Settings
from jobagent.db import Database
from jobagent.models import Posting
from jobagent.sqlconn import LibsqlConnection
from tests import test_web


@pytest.fixture(autouse=True)
def use_libsql(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_connect(path: Path | str) -> Any:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return LibsqlConnection(libsql.connect(str(path)))

    monkeypatch.setattr("jobagent.db.connect", fake_connect)
    monkeypatch.setattr("jobagent.web.store.connect", fake_connect)


def test_jobs_store_reads_rows_by_name_and_position(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    assert not isinstance(db.conn, sqlite3.Connection)
    posting = Posting(source="hirist", url="https://www.hirist.tech/j/a-1", company="Acme",
                      title="Full Stack Developer", location="Pune")  # fmt: skip
    saved = db.upsert_posting(posting)
    assert db.upsert_posting(posting).outcome == "duplicate"
    db.set_status(saved.job_id, "applied", "owner")
    (row,) = db.list_jobs(status="applied")
    assert (row["company"], row["url"]) == ("Acme", "https://www.hirist.tech/j/a-1")
    assert dict(row)["title"] == "Full Stack Developer"
    db.close()


def test_batch_save_and_racing_handles_over_libsql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = Database(tmp_path / "t.db"), Database(tmp_path / "t.db")
    postings = [
        Posting(source="hirist", url=f"https://www.hirist.tech/j/a-{i}", company=f"C{i}",
                title="Full Stack Developer", location="Pune")
        for i in range(3)
    ]  # fmt: skip
    assert [r.outcome for r in a.upsert_many(postings)] == ["new"] * 3
    assert [r.outcome for r in a.upsert_many(postings)] == ["duplicate"] * 3  # executemany path
    monkeypatch.setattr(b, "_known", lambda keyed: ({}, {}))  # b read before a committed
    assert b.upsert_posting(postings[0]).outcome == "duplicate"
    count = lambda t: a.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]  # noqa: E731
    assert (count("jobs"), count("job_links")) == (3, 3)
    a.close()
    b.close()


def test_failed_write_is_rolled_back(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    with pytest.raises(ValueError), db.conn:
        db.conn.execute("INSERT INTO events(job_id, at, new_status) VALUES (1, 'x', 'found')")
        raise ValueError("boom")
    assert db.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0
    db.close()


def test_full_coach_flow_over_libsql(settings: Settings) -> None:
    c: TestClient = test_web.client_for(settings)
    c.post("/api/login", json={"password": test_web.PASSWORD})
    test_web.test_chat_saves_history_and_facts_wait_for_confirmation(c)
    test_web.test_resume_build_uses_confirmed_facts_and_returns_tex(c)


def known_batch(db: Database) -> list[Posting]:
    """Every posting already stored, some under a new URL, all under older rules, half
    pruned: the first run after a rules bump re-judges and refreshes these rows."""
    postings = [
        Posting(source="hirist", url=f"https://www.hirist.tech/j/a-{i}", company=f"C{i}",
                title="Full Stack Developer", location="Pune", description="Build APIs. " * 9,
                experience_text="10-12 years" if i % 2 == 0 else "")
        for i in range(6)
    ]  # fmt: skip
    db.upsert_many(postings)
    with db.conn:
        db.conn.execute("UPDATE jobs SET rules_version=0")
        db.conn.execute("UPDATE events SET at='2020-01-01'")
    assert db.prune() == 3
    moved = {0, 3}  # boards repost a job under a new URL: found by its dedupe key instead
    return [
        p.model_copy(update={"url": f"{p.url}-new"}) if i in moved else p
        for i, p in enumerate(postings)
    ]


def test_fully_known_batch_with_stale_rules_saves_over_libsql(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    results = db.upsert_many(known_batch(db))
    assert [r.outcome for r in results] == ["merged", "duplicate", "duplicate", "merged",
                                            "duplicate", "duplicate"]  # fmt: skip
    assert {r.status for r in results[1::2]} == {"found"}
    db.close()


class _QualifiedNames:
    """A libsql cursor whose server names a plain `j.key` column "j.key": SQLite leaves
    the name of a column without AS unspecified, and the hosted server may qualify it."""

    def __init__(self, cursor: Any, sql: str) -> None:
        self._cursor, self._sql = cursor, sql

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    @property
    def description(self) -> Any:
        cols = self._cursor.description or ()
        plain = lambda n: f"j.{n}" in self._sql and f"AS {n}" not in self._sql  # noqa: E731
        return [(f"j.{d[0]}" if plain(d[0]) else d[0], *d[1:]) for d in cols] or None


class _QualifyingConn:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def execute(self, sql: str, params: Any = ()) -> Any:
        return _QualifiedNames(self._conn.execute(sql, params), sql)


def test_known_batch_saves_when_the_server_qualifies_column_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def qualifying(path: Path | str) -> Any:
        return LibsqlConnection(_QualifyingConn(libsql.connect(str(path))))

    monkeypatch.setattr("jobagent.db.connect", qualifying)
    db = Database(tmp_path / "t.db")
    batch = known_batch(db)
    # Before the fix: ValueError: 'key' is not in list, from the batch's by-key preload.
    assert [r.outcome for r in db.upsert_many(batch)].count("merged") == 2
    db.close()
