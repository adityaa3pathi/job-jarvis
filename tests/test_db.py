"""Batch saves: few remote calls per posting, one transaction per portal (R2, R3)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from jobagent import cli
from jobagent.config import Settings, ensure_data_dir
from jobagent.db import Database
from jobagent.models import Posting


class CountingConn:
    """Wraps a sqlite3 connection and counts calls that would each be a Turso round trip."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.calls: Counter[str] = Counter()

    def execute(self, sql: str, params: Any = ()) -> Any:
        self.calls["execute"] += 1
        return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Any) -> Any:
        self.calls["execute"] += 1
        return self._conn.executemany(sql, rows)

    def executescript(self, script: str) -> None:
        self.calls["executescript"] += 1
        self._conn.executescript(script)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CountingConn:
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.calls["commit"] += 1
        self._conn.__exit__(*exc)


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> list[CountingConn]:
    made: list[CountingConn] = []

    def fake_connect(path: Path | str) -> Any:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        made.append(CountingConn(conn))
        return made[-1]

    monkeypatch.setattr("jobagent.db.connect", fake_connect)
    return made


def posting(i: int = 1, **kw: Any) -> Posting:
    base: dict[str, Any] = {
        "source": "hirist", "url": f"https://www.hirist.tech/j/job-{i}", "company": f"Co{i}",
        "title": "AI Engineer", "location": "Bengaluru", "experience_text": "2-3 yrs",
    }  # fmt: skip
    base.update(kw)
    return Posting.model_validate(base)


def calls(db: Database) -> int:
    conn: CountingConn = db.conn  # type: ignore[assignment]
    return conn.calls["execute"]


def test_existing_posting_costs_at_most_two_calls(counting: list[CountingConn]) -> None:
    db = Database(":memory:")
    db.upsert_posting(posting())
    for again in (posting(), posting(posted_date=date(2026, 10, 1))):
        before = calls(db)
        db.upsert_posting(again)
        assert calls(db) - before <= 2, again


def test_closing_known_jobs_costs_two_calls_each_plus_the_shared_read(
    counting: list[CountingConn],
) -> None:
    db = Database(":memory:")
    db.upsert_many([posting(i) for i in range(600)])
    before = calls(db)
    results = db.upsert_many([posting(i, closed=True) for i in range(600)])
    assert {r.status for r in results} == {"closed"}
    assert calls(db) - before <= 2 * 600 + 2  # update + status event each; two chunked reads


def test_batch_reads_known_rows_once_and_commits_once(counting: list[CountingConn]) -> None:
    db = Database(":memory:")
    db.upsert_many([posting(i) for i in range(1000)])
    before, commits = calls(db), counting[0].calls["commit"]
    results = db.upsert_many([posting(i) for i in range(1000)])
    assert {r.outcome for r in results} == {"duplicate"}
    assert calls(db) - before <= 4  # two chunked reads, one bulk seen-date update
    assert counting[0].calls["commit"] - commits == 1


def test_batch_matches_one_by_one_outcomes() -> None:
    db = Database(":memory:")
    first = db.upsert_posting(posting(1))
    other_board = posting(1, source="cutshort", url="https://cutshort.io/job/x-1")
    results = db.upsert_many(
        [posting(1), other_board, posting(2), posting(2), posting(3, experience_text="10-12 yrs"),
         posting(1, posted_date=date(2026, 10, 1)), posting(1, closed=True)]
    )  # fmt: skip
    assert [r.outcome for r in results] == [
        "duplicate", "merged", "new", "duplicate", "new", "updated", "duplicate",
    ]  # fmt: skip
    assert results[1].job_id == first.job_id and results[3].job_id == results[2].job_id
    assert results[4].status == "dropped" and results[6].status == "closed"
    assert [r["new_status"] for r in db.events(first.job_id)] == ["found", "closed"]
    assert len(db.links(first.job_id)) == 2


def test_failed_batch_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(":memory:")

    def rules(p: Posting, *_: Any) -> None:
        if p.company == "Co2":
            raise ValueError("boom")

    monkeypatch.setattr("jobagent.db.check_hard_rules", rules)
    with pytest.raises(ValueError):
        db.upsert_many([posting(1), posting(2)])
    assert db.list_jobs() == []


def test_two_handles_saving_one_url_leave_one_job_and_one_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = Database(tmp_path / "t.db"), Database(tmp_path / "t.db")
    # b read the table before a committed, so it believes the URL is new.
    monkeypatch.setattr(b, "_known", lambda keyed: ({}, {}))
    saved = a.upsert_posting(posting())
    again = b.upsert_posting(posting())
    assert (again.job_id, again.outcome) == (saved.job_id, "duplicate")
    count = lambda t: a.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    assert (count("jobs"), count("job_links"), count("events")) == (1, 1, 1)
    a.close()
    b.close()


def run(db: Database, seen: list[Posting], *, clean: bool = True) -> int:
    """One portal finishing: save what it read, then sweep what it did not."""
    db.upsert_many(seen)
    return db.close_vanished(seen, clean=clean, after_runs=3)


def test_job_missing_from_three_clean_runs_is_closed() -> None:
    db = Database(":memory:")
    stays, goes = db.upsert_many([posting(1), posting(2)])
    for _ in range(2):
        assert run(db, [posting(1)]) == 0
    assert db.get_job(goes.job_id)["status"] == "found"
    assert run(db, [posting(1)]) == 1
    assert db.get_job(goes.job_id)["status"] == "closed"
    assert db.get_job(stays.job_id)["status"] == "found"
    assert db.events(goes.job_id)[-1]["new_status"] == "closed"


def test_errored_runs_and_reappearing_jobs_do_not_close() -> None:
    db = Database(":memory:")
    _, goes = db.upsert_many([posting(1), posting(2)])
    for _ in range(5):
        run(db, [posting(1)], clean=False)
    run(db, [posting(1)])
    run(db, [posting(1)])
    run(db, [posting(1), posting(2)])  # back again: the count starts over
    run(db, [posting(1)])
    run(db, [posting(1)])
    assert db.get_job(goes.job_id)["status"] == "found"


def test_other_portals_jobs_are_left_alone() -> None:
    db = Database(":memory:")
    other = db.upsert_posting(posting(9, source="cutshort", url="https://cutshort.io/job/9"))
    for _ in range(4):
        run(db, [posting(1)])
    assert db.get_job(other.job_id)["status"] == "found"


def test_old_database_gains_the_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, key TEXT NOT NULL, company TEXT NOT NULL,"
        " title TEXT NOT NULL, location TEXT NOT NULL, experience_text TEXT NOT NULL,"
        " stack_words TEXT NOT NULL, description TEXT NOT NULL, posted_date TEXT,"
        " seen_date TEXT NOT NULL, source TEXT NOT NULL, track TEXT, status TEXT NOT NULL,"
        " drop_reason TEXT, score INTEGER, rank_reason TEXT);"
        "INSERT INTO jobs VALUES (1,'k','Co','T','L','','[]','','',''"
        ",'hirist',NULL,'dropped','unpaid',NULL,NULL);"
    )
    old.commit()
    old.close()
    Database(path).close()
    db = Database(path)  # a second open must tolerate the columns already being there
    job = db.get_job(1)
    assert (job["rules_version"], job["missed_runs"]) == (1, 0)
    db.close()


def test_cli_recheck_prints_a_summary(settings: Settings, capsys: Any) -> None:
    ensure_data_dir(settings)
    db = Database(settings.db_path)
    saved = db.upsert_posting(posting(description="Unpaid leave on request."))
    with db.conn:
        db.conn.execute("UPDATE jobs SET status='dropped', drop_reason='unpaid', rules_version=1")
    db.close()
    assert cli.recheck(settings) == 0
    assert capsys.readouterr().out == "rechecked 1, undropped 1, dropped 0, merged 0\n"
    db = Database(settings.db_path)
    assert db.get_job(saved.job_id)["status"] == "found"
    db.close()
